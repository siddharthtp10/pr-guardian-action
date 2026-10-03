"""Remove anything that looks like a secret BEFORE text leaves the runner.

Why this exists: the AI layer sends diff hunks to a third-party API. A PR can
contain a leaked credential (that is exactly what SEC-001..003 look for), and
"the model never sees it" is a much stronger guarantee than "the model was told
to ignore it".

Design rules:
- Redact, never reject: a hunk with a secret is still worth reviewing.
- Preserve LINE STRUCTURE exactly (one output line per input line). The model
  reports findings by line number, so redaction must never shift lines.
- Prefer over-redaction. A masked harmless value costs a little review quality;
  a leaked key costs far more.
- Counts only: results report how many of each kind were removed, never values.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----")
_PEM_END = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----")

# (kind, pattern). Whole-match replacement unless noted.
_TOKEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA)[0-9A-Z]{16}\b")),
    (
        "github-token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,255}|github_pat_[A-Za-z0-9_]{50,255})\b"),
    ),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}")),
    ("api-key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ),
]

# user:password@ inside a URL. Lookarounds keep the scheme and host readable.
_URL_CREDENTIALS = re.compile(r"(?<=://)[^\s/@:]+:[^\s/@]+(?=@)")
# "Authorization: Bearer abc..." - keep the scheme word, mask the credential.
_AUTH_HEADER = re.compile(r"(?i)\b(bearer|basic)(\s+)[A-Za-z0-9._~+/=-]{16,}")

# key = value where the KEY looks sensitive. Quoted or unquoted values.
_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?P<key>(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key
        |secret[_-]?key|private[_-]?key|credential)s?["']?\s*[:=]\s*["']?)
    (?P<value>[^\s"',;#]{6,})
    """
)
# A value that is clearly a reference to a secret rather than the secret itself.
_REFERENCE = re.compile(
    r"^(?:\$|\{\{|%\(|var\.|local\.|data\.|module\.|each\.|secrets\.|env\.|"
    r"aws_|\[REDACTED)"
)

_CANDIDATE = re.compile(r"[A-Za-z0-9+/_=-]{32,}")
_HEX_ONLY = re.compile(r"^[0-9a-fA-F]+$")
_ENTROPY_THRESHOLD = 4.2  # bits/char; random base64 is ~5.5, English/identifiers ~3-4
MIN_CONFIGURED_SECRET_LEN = 8  # shorter values would shred ordinary words


@dataclass(frozen=True)
class Redaction:
    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def _entropy(s: str) -> float:
    freq = Counter(s)
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in freq.values())


def _mask(kind: str) -> str:
    return f"[REDACTED:{kind}]"


def redact(text: str, known_secrets: tuple[str, ...] = ()) -> Redaction:
    """Redact `text`. `known_secrets` are exact values to remove everywhere
    (the Action's own API key and token, so they can never be echoed back)."""
    counts: Counter[str] = Counter()
    secrets = sorted(
        {s for s in known_secrets if len(s) >= MIN_CONFIGURED_SECRET_LEN}, key=len, reverse=True
    )

    out: list[str] = []
    in_pem = False
    for line in text.split("\n"):
        for secret in secrets:
            if secret in line:
                counts["configured-secret"] += line.count(secret)
                line = line.replace(secret, _mask("configured-secret"))

        # Private key blocks span lines. Mask every line from BEGIN through END
        # (or to the end of the text if the block is cut off by the hunk).
        if in_pem or _PEM_BEGIN.search(line):
            if not in_pem:
                counts["private-key"] += 1
            in_pem = not _PEM_END.search(line)
            out.append(_mask("private-key"))
            continue

        line = _redact_line(line, counts)
        out.append(line)
    return Redaction("\n".join(out), {k: v for k, v in counts.items() if v})


def _redact_line(line: str, counts: Counter[str]) -> str:
    for kind, pattern in _TOKEN_PATTERNS:
        line, n = pattern.subn(_mask(kind), line)
        counts[kind] += n

    line, n = _URL_CREDENTIALS.subn(_mask("url-credentials"), line)
    counts["url-credentials"] += n

    def auth(m: re.Match[str]) -> str:
        counts["auth-header"] += 1
        return f"{m[1]}{m[2]}{_mask('auth-header')}"

    line = _AUTH_HEADER.sub(auth, line)

    def assignment(m: re.Match[str]) -> str:
        value = m["value"]
        if _REFERENCE.match(value):
            return m[0]  # `password = var.db_password` carries no secret
        counts["credential-assignment"] += 1
        return m["key"] + _mask("credential")

    line = _ASSIGNMENT.sub(assignment, line)

    def candidate(m: re.Match[str]) -> str:
        token = m[0]
        if _HEX_ONLY.match(token):
            # Git SHAs (40) and sha256 digests (64) are public and useful context
            # (they show an action IS pinned). Other long hex runs are masked.
            if len(token) in (40, 64):
                return token
            counts["hex-secret"] += 1
            return _mask("hex-secret")
        if _entropy(token) >= _ENTROPY_THRESHOLD:
            counts["high-entropy"] += 1
            return _mask("high-entropy")
        return token

    return _CANDIDATE.sub(candidate, line)
