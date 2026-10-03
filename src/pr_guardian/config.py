"""Typed, validated configuration.

WHY this module exists: every input arrives as a string from the workflow
file, which is user-controlled text. We parse and validate it in exactly one
place, fail fast with *all* problems listed, and hand the rest of the program
a frozen object it can trust. Nothing else in the codebase reads os.environ.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath

# Ordered low -> high. Position in this tuple is the severity rank used later
# to compare a finding against the fail-on threshold.
SEVERITIES = ("low", "medium", "high", "critical")
# "none" = never fail the check (report only). It is a threshold, not a severity.
FAIL_ON_CHOICES = (*SEVERITIES, "none")

# Verified against the Anthropic models page on 2026-10-03. Sonnet 5.5 is the
# default rather than the cheaper Haiku 4.5 because Haiku 4.5's published
# retirement floor is 2026-10-15; a default that may vanish is a bad default.
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_FAIL_ON = "high"
DEFAULT_MAX_FILES = 50
# Hard ceiling so a typo like 100000 cannot turn into an unbounded API loop.
MAX_FILES_CEILING = 500

# The Action maps each input to one of these env vars (see action.yml).
# Composite actions, unlike JavaScript/Docker actions, do NOT auto-create
# INPUT_* variables, so the mapping is explicit - and tested.
ENV_GITHUB_TOKEN = "PRG_GITHUB_TOKEN"  # noqa: S105 - env var *name*, not a secret
ENV_API_KEY = "PRG_ANTHROPIC_API_KEY"
ENV_MODEL = "PRG_MODEL"
ENV_FAIL_ON = "PRG_FAIL_ON"
ENV_PATHS = "PRG_PATHS"
ENV_MAX_FILES = "PRG_MAX_FILES"
ENV_DRY_RUN = "PRG_DRY_RUN"

_TRUE = {"true"}
_FALSE = {"false"}
_SPLIT = re.compile(r"[\n,]")


class ConfigError(ValueError):
    """Raised with every validation problem at once, not just the first."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass(frozen=True)
class Config:
    # repr=False on both secrets: an accidental print(config) or a traceback
    # that includes the object must never leak a credential into public logs.
    github_token: str = field(repr=False)
    anthropic_api_key: str | None = field(repr=False)
    model: str
    fail_on: str
    paths: tuple[str, ...]
    max_files: int
    dry_run: bool

    @property
    def ai_enabled(self) -> bool:
        """AI is opt-in by the mere presence of a key; no key = rules-only."""
        return self.anthropic_api_key is not None

    @property
    def mode(self) -> str:
        return "rules+ai" if self.ai_enabled else "rules-only"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Config:
        problems: list[str] = []

        token = env.get(ENV_GITHUB_TOKEN, "").strip()
        # Dry-run still lists nothing from GitHub in Stage 1, but later stages
        # fetch the diff in dry-run too, so a token is always required.
        if not token:
            problems.append("github-token is required")

        # Empty string means "not provided" (composite inputs default to '').
        api_key = env.get(ENV_API_KEY, "").strip() or None
        model = env.get(ENV_MODEL, "").strip() or DEFAULT_MODEL

        fail_on = (env.get(ENV_FAIL_ON, "").strip() or DEFAULT_FAIL_ON).lower()
        if fail_on not in FAIL_ON_CHOICES:
            problems.append(f"fail-on must be one of {', '.join(FAIL_ON_CHOICES)}; got {fail_on!r}")

        paths, path_problems = _parse_paths(env.get(ENV_PATHS, ""))
        problems.extend(path_problems)

        max_files = DEFAULT_MAX_FILES
        raw_max = env.get(ENV_MAX_FILES, "").strip()
        if raw_max:
            try:
                max_files = int(raw_max)
            except ValueError:
                problems.append(f"max-files must be an integer; got {raw_max!r}")
            else:
                if not 1 <= max_files <= MAX_FILES_CEILING:
                    problems.append(f"max-files must be between 1 and {MAX_FILES_CEILING}")

        dry_run = False
        raw_dry = env.get(ENV_DRY_RUN, "").strip().lower()
        if raw_dry in _TRUE:
            dry_run = True
        elif raw_dry and raw_dry not in _FALSE:
            # Strict on purpose: "yes"/"1" silently meaning false would make a
            # user think they were in dry-run while real comments got posted.
            problems.append(f"dry-run must be 'true' or 'false'; got {raw_dry!r}")

        if problems:
            raise ConfigError(problems)

        return cls(
            github_token=token,
            anthropic_api_key=api_key,
            model=model,
            fail_on=fail_on,
            paths=paths,
            max_files=max_files,
            dry_run=dry_run,
        )


def _parse_paths(raw: str) -> tuple[tuple[str, ...], list[str]]:
    """Split newline/comma separated globs and reject unsafe ones.

    Paths are only ever used to *filter* PR file names (never to open files on
    disk), but rejecting absolute and ``..`` patterns keeps that true even if
    a later stage changes, and gives users an early, clear error.
    """
    paths: list[str] = []
    problems: list[str] = []
    for item in _SPLIT.split(raw):
        item = item.strip()
        if not item:
            continue
        # A leading "!" makes an exclusion ("!tests/fixtures/**"); validate the rest.
        body = item[1:].strip() if item.startswith("!") else item
        parts = PurePosixPath(body).parts
        if not body or body.startswith("/") or ".." in parts:
            problems.append(f"paths entry must be repo-relative without '..'; got {item!r}")
        else:
            paths.append(item)
    return tuple(paths), problems
