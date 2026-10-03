"""Run rules over the changed lines of each file and produce Findings.

Three principles, each of which prevents a specific kind of bad review:

1. Only ADDED lines can be flagged. Context lines are used to understand
   structure but never blamed, so a PR is never accused of old code.
2. Never guess from missing information. Block-level rules only fire when the
   whole block is visible in the patch; if the hunk cuts it off we stay quiet
   (a false negative is shown honestly in the docs, a false positive erodes
   trust in every future comment).
3. Nothing the diff contains is ever copied into a finding. Messages come from
   the policy file, so a secret in the diff cannot be re-published by us.
"""

from __future__ import annotations

from dataclasses import dataclass

from pr_guardian.config import SEVERITIES
from pr_guardian.diff import ParsedPatch, PatchLine
from pr_guardian.rules import Rule, RuleType
from pr_guardian.selection import ReviewTarget

# Lines longer than this are not regex-scanned. We report them instead of
# skipping silently: padding a line is the obvious way to hide content from a
# scanner, and it also bounds worst-case regex time.
MAX_LINE_CHARS = 10_000
GUARD_RULE_ID = "GUARD-001"
GUARD_MESSAGE = (
    f"Line is longer than {MAX_LINE_CHARS:,} characters and was not scanned by the rules. "
    "Long lines can hide content from review; split it or confirm it is generated."
)


@dataclass(frozen=True)
class Finding:
    rule_id: str
    severity: str
    path: str
    line: int  # line in the NEW file; always a line inside the diff
    message: str


def severity_rank(severity: str) -> int:
    return SEVERITIES.index(severity)


def meets_threshold(severity: str, fail_on: str) -> bool:
    """True if a finding of this severity should fail the check."""
    return fail_on != "none" and severity_rank(severity) >= severity_rank(fail_on)


def evaluate(targets: list[ReviewTarget], rules: list[Rule]) -> list[Finding]:
    found: dict[tuple[str, int, str], Finding] = {}
    for target in targets:
        for finding in _evaluate_file(target, rules):
            found.setdefault((finding.path, finding.line, finding.rule_id), finding)
    return sorted(
        found.values(),
        key=lambda f: (f.path, f.line, -severity_rank(f.severity), f.rule_id),
    )


def _evaluate_file(target: ReviewTarget, rules: list[Rule]) -> list[Finding]:
    applicable = [r for r in rules if target.kind in r.file_types]
    out: list[Finding] = []
    for run in _contiguous_runs(target.patch):
        # Over-long lines are blanked for matching and reported separately.
        texts = [pl.text if len(pl.text) <= MAX_LINE_CHARS else "" for pl in run]
        for i, pl in enumerate(run):
            if pl.added and len(pl.text) > MAX_LINE_CHARS:
                out.append(
                    Finding(GUARD_RULE_ID, "medium", target.path, pl.new_line, GUARD_MESSAGE)
                )
            for rule in applicable:
                if rule.type is RuleType.LINE:
                    if pl.added and _line_matches(rule, texts, i):
                        out.append(_finding(rule, target.path, pl.new_line))
                else:
                    hit = _missing_in_item(rule, run, texts, i)
                    if hit is not None:
                        out.append(_finding(rule, target.path, hit))
    return out


def _finding(rule: Rule, path: str, line: int) -> Finding:
    return Finding(rule.id, rule.severity, path, line, rule.message)


def _contiguous_runs(patch: ParsedPatch) -> list[list[PatchLine]]:
    """Group visible lines into runs of consecutive new-file line numbers.

    Indentation-based structure ("what block am I in?") is only trustworthy
    inside one run: across a gap between hunks we cannot see what is missing.
    """
    runs: list[list[PatchLine]] = []
    for number in sorted(patch.lines):
        line = patch.lines[number]
        if runs and runs[-1][-1].new_line == number - 1:
            runs[-1].append(line)
        else:
            runs.append([line])
    return runs


# --- helpers for structure -------------------------------------------------


def _indent(text: str) -> int:
    expanded = text.expandtabs(4)
    return len(expanded) - len(expanded.lstrip(" "))


def _is_blank_or_comment(text: str) -> bool:
    stripped = text.strip()
    return stripped == "" or stripped.startswith(("#", "//"))


# --- line rules ---------------------------------------------------------------


def _line_matches(rule: Rule, texts: list[str], i: int) -> bool:
    text = texts[i]
    if not text:
        return False
    if not rule.scan_comments and _is_blank_or_comment(text):
        return False  # commented-out code is not live configuration
    if not rule.pattern.search(text):
        return False
    if rule.exclude and rule.exclude.search(text):
        return False
    return not (rule.within and not _inside(texts, i, rule.within))


def _inside(texts: list[str], i: int, within) -> bool:
    """Is line i (or a visible ancestor of it, found by indentation) matching `within`?

    Walk upward, each time looking only at lines indented LESS than the
    current threshold: those are the enclosing blocks, nearest first.
    """
    if within.search(texts[i]):
        return True
    threshold = _indent(texts[i])
    for j in range(i - 1, -1, -1):
        text = texts[j]
        if _is_blank_or_comment(text):
            continue
        indent = _indent(text)
        if indent < threshold:
            if within.search(text):
                return True
            threshold = indent
            if threshold == 0:
                break
    return False


# --- "YAML list item is missing X" rules ------------------------------------


def _missing_in_item(rule: Rule, run: list[PatchLine], texts: list[str], i: int) -> int | None:
    """Return the line to comment on if the list item containing line i lacks `required`."""
    if not texts[i] or not rule.pattern.search(texts[i]):
        return None
    if rule.exclude and rule.exclude.search(texts[i]):
        return None
    bounds = _list_item_bounds(texts, i)
    if bounds is None:
        return None  # item start or end not visible: do not guess
    start, end = bounds
    if not any(pl.added for pl in run[start:end]):
        return None  # the PR did not touch this item
    body = [t for t in texts[start:end] if not _is_blank_or_comment(t)]
    if any(rule.required.search(t) for t in body):  # type: ignore[union-attr]
        return None
    return run[start].new_line


def _list_item_bounds(texts: list[str], i: int) -> tuple[int, int] | None:
    """Find [start, end) of the YAML list item ("- name: x ...") containing line i.

    Returns None unless BOTH the item's first line and the line that ends it
    (the next line at the item's indent or less) are visible.
    """
    line = texts[i]
    if line.lstrip().startswith("- "):
        start = i
    else:
        start = -1
        for j in range(i - 1, -1, -1):
            if _is_blank_or_comment(texts[j]):
                continue
            if _indent(texts[j]) < _indent(line):
                start = j
                break
        if start < 0 or not texts[start].lstrip().startswith("- "):
            return None
    item_indent = _indent(texts[start])
    for k in range(start + 1, len(texts)):
        if _is_blank_or_comment(texts[k]):
            continue
        if _indent(texts[k]) <= item_indent:
            return start, k
    return None  # ran off the end of the visible lines: the item may continue
