"""Turn findings into what people see: annotations, the job summary and a PR review.

Three outputs, because each one survives a different situation:

- Annotations (``::error file=...``) are plain log lines, so they work even on
  fork PRs, where the token is read-only and nothing can be posted.
- The job summary (``$GITHUB_STEP_SUMMARY``) is a file on the runner, so it
  also needs no write permission.
- The PR review puts each finding next to the code. It needs
  ``pull-requests: write`` and is skipped in dry-run and on forks.

Everything that reaches Markdown goes through ``md_code``: file names are
attacker-controlled, and an unescaped name could break a table, inject a link
or @-mention a whole team.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pr_guardian.config import SEVERITIES
from pr_guardian.context import PRContext
from pr_guardian.engine import Finding, meets_threshold
from pr_guardian.github_api import GitHubAPIError, GitHubClient, ReviewComment
from pr_guardian.report import safe_name
from pr_guardian.selection import Selection

# A review with hundreds of comments is unreadable, and every comment is one
# more chance of a 422 that rejects the whole review. The rest go in the body.
MAX_INLINE_COMMENTS = 30
MAX_BODY_FINDINGS = 100

REVIEW_MARKER = "<!-- pr-guardian:review -->"
_COMMENT_MARKER = re.compile(r"<!-- pr-guardian:([A-Z][A-Z0-9]{1,5}-\d{3}) -->")


def comment_marker(rule_id: str) -> str:
    return f"<!-- pr-guardian:{rule_id} -->"


def md_code(text: str) -> str:
    """Render untrusted text as an inline code span that is safe inside a table.

    safe_name() already removes backticks (so the span cannot be closed early)
    and control characters. Inside a code span Markdown links and @-mentions
    are inert; only '|' still matters, because GFM splits table cells on it.
    """
    return "`" + safe_name(text).replace("|", "\\|") + "`"


# --- annotations --------------------------------------------------------------


def _escape_property(text: str) -> str:
    # Workflow-command property values additionally need ':' and ',' escaped,
    # or a file name like "a,line=1" could rewrite the annotation's location.
    return (
        text.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
        .replace(":", "%3A")
        .replace(",", "%2C")
    )


def _escape_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def render_annotations(findings: list[Finding], fail_on: str) -> list[str]:
    lines = []
    for f in findings:
        level = "error" if meets_threshold(f.severity, fail_on) else "warning"
        props = (
            f"file={_escape_property(f.path)},line={f.line},"
            f"title={_escape_property(f'{f.rule_id} ({f.severity})')}"
        )
        lines.append(f"::{level} {props}::{_escape_data(f.message)}")
    return lines


# --- job summary ----------------------------------------------------------------


def _verdict(findings: list[Finding], fail_on: str) -> str:
    failing = [f for f in findings if meets_threshold(f.severity, fail_on)]
    if failing:
        return f"**Check failed:** {len(failing)} finding(s) at or above `{fail_on}` severity."
    if fail_on == "none":
        return "**Check passed:** `fail-on: none`, findings are advisory."
    return f"**Check passed:** no findings at or above `{fail_on}` severity."


def _counts(findings: list[Finding]) -> str:
    counts = {sev: sum(1 for f in findings if f.severity == sev) for sev in SEVERITIES}
    return ", ".join(f"{counts[s]} {s}" for s in reversed(SEVERITIES) if counts[s]) or "none"


def _findings_table(findings: list[Finding]) -> list[str]:
    rows = ["| Severity | Rule | Location |", "|---|---|---|"]
    for f in findings[:MAX_BODY_FINDINGS]:
        rows.append(f"| {f.severity} | {f.rule_id} | {md_code(f'{f.path}:{f.line}')} |")
    if len(findings) > MAX_BODY_FINDINGS:
        rows.append(f"\n...and {len(findings) - MAX_BODY_FINDINGS} more (see the job log).")
    return rows


def _skipped_section(sel: Selection) -> list[str]:
    if not sel.skipped and not sel.api_truncated:
        return []
    out = ["", f"**Not reviewed ({len(sel.skipped)} supported file(s)):**", ""]
    out += [f"- {md_code(s.path)}: {s.reason.value}" for s in sel.skipped]
    if sel.api_truncated:
        out.append("- This PR has 3000+ files, GitHub's API limit; some could not be listed.")
    return out


def render_summary(findings: list[Finding], sel: Selection, fail_on: str, mode: str) -> str:
    out = ["## PR Guardian", "", _verdict(findings, fail_on), ""]
    out.append(
        f"Reviewed {len(sel.targets)} file(s) in {mode} mode. Findings: {_counts(findings)}."
    )
    if findings:
        out += ["", *_findings_table(findings)]
    out += _skipped_section(sel)
    return "\n".join(out) + "\n"


# --- PR review -------------------------------------------------------------------


def comment_body(f: Finding) -> str:
    # The message is fixed policy text; nothing from the diff is quoted.
    return f"{comment_marker(f.rule_id)}\n**{f.severity.upper()} · {f.rule_id}**\n\n{f.message}"


def already_posted(existing: list[ReviewComment]) -> set[tuple[str, int, str]]:
    """Findings we commented on before that still point at the same code.

    GitHub keeps an inline comment's `line` in step with later commits and
    sets it to null once that code changes, so (path, line, rule) is a stable
    key across re-runs and an outdated comment does not suppress a new one.
    Only bot comments count: a human pasting our marker cannot hide a finding
    (and could not change the check result anyway, which ignores comments).
    """
    keys = set()
    for c in existing:
        if not c.author_is_bot or c.line is None:
            continue
        for rule_id in _COMMENT_MARKER.findall(c.body):
            keys.add((c.path, c.line, rule_id))
    return keys


@dataclass
class ReviewPlan:
    body: str
    inline: list[Finding]
    in_body_only: list[Finding] = field(default_factory=list)
    duplicates: int = 0

    def comments(self) -> list[dict[str, object]]:
        # line + side=RIGHT ("line N of the new file"), not the deprecated
        # `position`. Every Finding.line came from the parsed patch, so it is
        # a line GitHub will accept.
        return [
            {"path": f.path, "line": f.line, "side": "RIGHT", "body": comment_body(f)}
            for f in self.inline
        ]


def plan_review(
    findings: list[Finding], existing: list[ReviewComment], sel: Selection, fail_on: str
) -> ReviewPlan | None:
    """Decide what to post. None means there is nothing new to say."""
    seen = already_posted(existing)
    new = [f for f in findings if (f.path, f.line, f.rule_id) not in seen]
    if not new:
        return None
    inline, rest = new[:MAX_INLINE_COMMENTS], new[MAX_INLINE_COMMENTS:]
    return ReviewPlan(
        body=_review_body(findings, new, rest, sel, fail_on),
        inline=inline,
        in_body_only=rest,
        duplicates=len(findings) - len(new),
    )


def _review_body(
    findings: list[Finding],
    new: list[Finding],
    body_only: list[Finding],
    sel: Selection,
    fail_on: str,
    *,
    inline_failed: bool = False,
) -> str:
    out = [REVIEW_MARKER, "### PR Guardian", "", _verdict(findings, fail_on), ""]
    out.append(f"{len(new)} new finding(s) on this commit ({_counts(new)}).")
    if len(new) < len(findings):
        out.append(f"{len(findings) - len(new)} already commented on earlier are not repeated.")
    if inline_failed:
        out += ["", "GitHub rejected the inline comments, so the findings are listed here:"]
        out += ["", *_findings_table(new)]
    elif body_only:
        out += ["", f"{len(body_only)} more not posted inline, to keep the review readable:"]
        out += ["", *_findings_table(body_only)]
    out += _skipped_section(sel)
    return "\n".join(out)


def post_review(
    client: GitHubClient,
    ctx: PRContext,
    findings: list[Finding],
    sel: Selection,
    fail_on: str,
) -> str:
    """Post one review with inline comments. Returns a line for the log.

    One review (not N separate comments) means one notification for the PR
    author and one API call. If GitHub rejects it with 422 - one bad comment
    sinks the whole review - retry once with every finding in the body, so the
    author still hears about them.
    """
    existing = client.list_review_comments(ctx.owner, ctx.repo, ctx.number)
    plan = plan_review(findings, existing, sel, fail_on)
    if plan is None:
        if findings:
            return f"Review: all {len(findings)} finding(s) were already commented on; not posting."
        return "Review: no findings; not posting."
    try:
        client.create_review(
            ctx.owner,
            ctx.repo,
            ctx.number,
            commit_id=ctx.head_sha,
            body=plan.body,
            comments=plan.comments(),
        )
    except GitHubAPIError as exc:
        if exc.status != 422:
            raise
        new = plan.inline + plan.in_body_only
        body = _review_body(findings, new, [], sel, fail_on, inline_failed=True)
        client.create_review(
            ctx.owner, ctx.repo, ctx.number, commit_id=ctx.head_sha, body=body, comments=[]
        )
        return f"Review: inline comments rejected (422); posted {len(new)} finding(s) in the body."
    msg = f"Review: posted {len(plan.inline)} inline comment(s)"
    if plan.in_body_only:
        msg += f", {len(plan.in_body_only)} more in the review body"
    if plan.duplicates:
        msg += f"; skipped {plan.duplicates} already commented"
    return msg + "."
