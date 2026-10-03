"""Parse a unified-diff patch into "which lines can I comment on?".

WHY this matters: GitHub rejects an inline review comment (the *whole* review,
in fact) if its line is not part of the diff. Every finding therefore has to
be mapped to a line that actually appears in the patch. This module is the
single source of truth for that mapping.

The input is the per-file ``patch`` string from the "list pull request files"
API: hunks only, no ``---``/``+++`` headers.

Design choice - fail closed: if a hunk's line counts don't match its header we
raise PatchError instead of guessing. A wrong guess would put a comment on the
wrong line of someone's code, which is worse than skipping the file loudly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(ValueError):
    """The patch text is not a well-formed unified diff."""


@dataclass(frozen=True)
class PatchLine:
    new_line: int  # line number in the NEW file, i.e. the RIGHT side of the diff
    position: int  # GitHub "position": lines below the first @@, across all hunks
    text: str
    added: bool  # True for '+' lines, False for unchanged context lines


@dataclass(frozen=True)
class ParsedPatch:
    # Keyed by new-file line number. Removed ('-') lines are absent on purpose:
    # they have no line in the new file, so a RIGHT-side comment can't target them.
    lines: dict[int, PatchLine]
    hunk_count: int

    def is_commentable(self, line: int) -> bool:
        return line in self.lines

    def position_for(self, line: int) -> int | None:
        found = self.lines.get(line)
        return found.position if found else None

    def added_lines(self) -> list[PatchLine]:
        return [pl for pl in self.lines.values() if pl.added]


def parse_patch(patch: str) -> ParsedPatch:
    lines: dict[int, PatchLine] = {}
    position = -1  # first "@@" header is position 0; the next line is position 1
    hunks = 0
    in_hunk = False
    old_left = new_left = 0
    new_no = 0

    # split("\n"), NOT splitlines(): splitlines also breaks on \x0b, \x0c,
    # etc., which can legitimately appear inside YAML/Terraform strings and
    # would silently shift every later line number.
    rows = patch.split("\n")
    for index, raw in enumerate(rows):
        header = _HUNK_HEADER.match(raw)
        if header:
            if in_hunk and (old_left or new_left):
                raise PatchError("hunk ended before its declared line count")
            # An omitted count in a header means 1 (e.g. "@@ -5 +5 @@").
            old_left = int(header[2]) if header[2] is not None else 1
            new_left = int(header[4]) if header[4] is not None else 1
            new_no = int(header[3])
            position += 1
            hunks += 1
            in_hunk = True
            continue

        if not in_hunk:
            if raw.strip() == "":
                continue
            raise PatchError("content found before the first hunk header")

        if raw == "" and index == len(rows) - 1:
            # Only the *final* empty element is the patch's trailing newline.
            # An empty element in the middle is a blank context line. Treating
            # the last one as context too would let a truncated hunk pass.
            continue

        position += 1
        marker = raw[:1] or " "  # some tools strip the lone space of a blank context line
        text = raw[1:].rstrip("\r")

        if marker == "+":
            new_left -= 1
            lines[new_no] = PatchLine(new_no, position, text, added=True)
            new_no += 1
        elif marker == "-":
            old_left -= 1
        elif marker == " ":
            old_left -= 1
            new_left -= 1
            lines[new_no] = PatchLine(new_no, position, text, added=False)
            new_no += 1
        elif marker == "\\":
            pass  # "\ No newline at end of file": occupies a position, not a line
        else:
            raise PatchError(f"unexpected line prefix {marker!r}")

        if old_left < 0 or new_left < 0:
            raise PatchError("hunk has more lines than its header declares")

    if old_left or new_left:
        raise PatchError("patch ended before the last hunk was complete")
    return ParsedPatch(lines=lines, hunk_count=hunks)
