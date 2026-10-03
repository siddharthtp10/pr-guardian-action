"""Human-readable "what did we review and what did we skip" text.

File names in a PR are attacker-controlled (git allows newlines, backticks and
control characters in names). They are sanitised before they go anywhere a
human or a parser will read them: the log now, the Markdown job summary and PR
comments in later stages.
"""

from __future__ import annotations

from pr_guardian.selection import Selection

_MAX_NAME = 200


def safe_name(name: str) -> str:
    cleaned = "".join(c if c.isprintable() and c != "`" else "?" for c in name)
    return cleaned if len(cleaned) <= _MAX_NAME else cleaned[: _MAX_NAME - 1] + "…"


def render_selection(sel: Selection) -> str:
    # Every line starts with spaces or a letter, never "::", so a hostile file
    # name can't be parsed by the runner as a workflow command.
    out = [f"Changed files in PR: {sel.total_changed}"]
    out.append(f"Reviewing {len(sel.targets)} file(s):")
    for t in sel.targets:
        commentable = len(t.patch.lines)
        out.append(f"  - {safe_name(t.path)} [{t.kind}] ({commentable} commentable line(s))")
    if sel.skipped:
        out.append(f"Skipped {len(sel.skipped)} supported file(s) - NOT reviewed:")
        for s in sel.skipped:
            out.append(f"  - {safe_name(s.path)}: {s.reason.value}")
    quiet = [
        (sel.removed, "deleted"),
        (sel.unsupported_type, "unsupported file type"),
        (sel.outside_paths, "outside the 'paths' filter"),
        (sel.content_unchanged, "content unchanged (e.g. pure rename)"),
    ]
    parts = [f"{n} {label}" for n, label in quiet if n]
    if parts:
        out.append("Not applicable: " + ", ".join(parts) + ".")
    if sel.api_truncated:
        out.append(
            "WARNING: this PR has 3000+ files, GitHub's API limit. Some files could not be listed."
        )
    return "\n".join(out)
