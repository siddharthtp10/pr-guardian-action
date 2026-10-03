"""Turn findings into what people see: annotations, the job summary and a PR review.

Three outputs, because each one survives a different situation:

- Annotations (``::error file=...``) are plain log lines, so they work even on
  fork PRs, where the token is read-only and nothing can be posted.
- The job summary (``$GITHUB_STEP_SUMMARY``) is a file on the runner, so it
  also needs no write permission.
- The PR review puts each finding next to the code. It needs
  ``pull-requests: write`` and is skipped in dry-run and on forks. On re-runs
  it is RECONCILED, not re-posted (see plan_review).

Everything that reaches Markdown goes through ``md_code``: file names are
attacker-controlled, and an unescaped name could break a table, inject a link
or @-mention a whole team.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from pr_guardian.config import SEVERITIES
from pr_guardian.context import PRContext
from pr_guardian.engine import Finding, meets_threshold, severity_rank
from pr_guardian.github_api import GitHubAPIError, GitHubClient, Review, ReviewComment
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


def resolved_marker(rule_id: str) -> str:
    # Deliberately NOT matched by _COMMENT_MARKER (the ":resolved:" prefix breaks
    # the pattern), so a resolved comment stops counting as an open finding.
    return f"<!-- pr-guardian:resolved:{rule_id} -->"


RESOLVED_NOTE = "**Resolved** - this finding no longer applies to the latest commit."


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


def _key(f: Finding) -> tuple[str, int, str]:
    return (f.path, f.line, f.rule_id)


def find_our_review(reviews: list[Review]) -> Review | None:
    """The newest review we posted. Only bot-authored reviews count: a human
    pasting our marker must not be able to redirect our updates."""
    ours = [r for r in reviews if r.author_is_bot and REVIEW_MARKER in r.body]
    return max(ours, key=lambda r: r.id) if ours else None


@dataclass
class ReviewPlan:
    """What a run must do to make the PR match the current findings.

    Re-run semantics ("update, don't duplicate"):
      * one review per PR: found by its marker, its body is edited in place;
      * an open inline comment on the same (file, line, rule) is KEPT;
      * findings without a comment get one;
      * comments whose finding is gone are deleted - or, when people replied,
        edited to "Resolved" so the conversation survives.
    """

    review_id: int | None
    existing_body: str | None
    new: list[Finding]  # need an inline comment
    body_only: list[Finding]  # over the inline cap: listed in the body instead
    kept: int
    delete_ids: list[int]
    resolve: list[tuple[int, str]]  # (comment id, rule id)
    render_body: Callable[[list[Finding]], str]  # arg: findings that could not be placed inline

    @property
    def noop(self) -> bool:
        # No review yet and nothing to say: skip it. An empty "all clear"
        # review on every PR is noise; the summary and the check already say it.
        return self.review_id is None and not self.new and not self.body_only

    def comments(self) -> list[dict[str, object]]:
        # line + side=RIGHT ("line N of the new file"), not the deprecated
        # `position`. Every Finding.line came from the parsed patch, so it is
        # a line GitHub will accept.
        return [
            {"path": f.path, "line": f.line, "side": "RIGHT", "body": comment_body(f)}
            for f in self.new
        ]


def plan_review(
    findings: list[Finding],
    reviews: list[Review],
    comments: list[ReviewComment],
    sel: Selection,
    fail_on: str,
    head_sha: str = "",
) -> ReviewPlan:
    # Highest severity first, so the cap never hides the worst findings.
    ranked = sorted(findings, key=lambda f: (-severity_rank(f.severity), *_key(f)))
    shown, body_only = ranked[:MAX_INLINE_COMMENTS], ranked[MAX_INLINE_COMMENTS:]
    wanted = {_key(f): f for f in shown}

    replied_to = {c.in_reply_to_id for c in comments if c.in_reply_to_id is not None}
    kept_keys: set[tuple[str, int, str]] = set()
    delete_ids: list[int] = []
    resolve: list[tuple[int, str]] = []
    for c in comments:
        if not c.author_is_bot:
            continue  # never touch (or trust) a human's comment
        for rule_id in _COMMENT_MARKER.findall(c.body):
            key = (c.path, c.line, rule_id)
            if key in wanted and key not in kept_keys:
                kept_keys.add(key)  # c.line is None when outdated -> never matches
            elif c.id in replied_to:
                resolve.append((c.id, rule_id))
            else:
                delete_ids.append(c.id)  # fixed, outdated, or a duplicate of one we kept

    review = find_our_review(reviews)
    return ReviewPlan(
        review_id=review.id if review else None,
        existing_body=review.body if review else None,
        new=[f for key, f in wanted.items() if key not in kept_keys],
        body_only=body_only,
        kept=len(kept_keys),
        delete_ids=delete_ids,
        resolve=resolve,
        render_body=lambda unplaced: _review_body(
            findings, body_only, unplaced, sel, fail_on, head_sha
        ),
    )


def _review_body(
    findings: list[Finding],
    body_only: list[Finding],
    unplaced: list[Finding],
    sel: Selection,
    fail_on: str,
    head_sha: str,
) -> str:
    """The review body describes the CURRENT state, so editing it in place on a
    re-run leaves one accurate review instead of a trail of stale ones."""
    out = [REVIEW_MARKER, "### PR Guardian", "", _verdict(findings, fail_on), ""]
    if findings:
        out.append(f"{len(findings)} open finding(s): {_counts(findings)}.")
    else:
        out.append("No open findings.")
    if unplaced:
        out += ["", "GitHub rejected the inline comments, so these findings are listed here:"]
        out += ["", *_findings_table(unplaced)]
    if body_only:
        out += ["", f"{len(body_only)} more not posted inline, to keep the review readable:"]
        out += ["", *_findings_table(body_only)]
    out += _skipped_section(sel)
    if head_sha:
        out += ["", f"<sub>Updated for commit `{head_sha[:7]}`; edited in place on each run.</sub>"]
    return "\n".join(out)


def post_review(
    client: GitHubClient,
    ctx: PRContext,
    findings: list[Finding],
    sel: Selection,
    fail_on: str,
) -> str:
    """Make the PR's review match the findings. Returns text for the log.

    First run: ONE review carrying every inline comment (one notification, one
    API call). If GitHub rejects it with 422 - one bad comment sinks the whole
    review - retry once with the findings in the body so nobody misses them.
    Re-run: reconcile (see ReviewPlan). POSTs are never retried: a retry after a
    timeout could create a second review.
    """
    reviews = client.list_reviews(ctx.owner, ctx.repo, ctx.number)
    existing = client.list_review_comments(ctx.owner, ctx.repo, ctx.number)
    plan = plan_review(findings, reviews, existing, sel, fail_on, ctx.head_sha)
    if plan.noop:
        if findings:
            return f"Review: all {len(findings)} finding(s) were already commented on; not posting."
        return "Review: no findings; not posting."

    unplaced: list[Finding] = []
    added = len(plan.new)
    if plan.review_id is None:
        try:
            client.create_review(
                ctx.owner,
                ctx.repo,
                ctx.number,
                commit_id=ctx.head_sha,
                body=plan.render_body([]),
                comments=plan.comments(),
            )
        except GitHubAPIError as exc:
            if exc.status != 422:
                raise
            unplaced, added = list(plan.new), 0
            client.create_review(
                ctx.owner,
                ctx.repo,
                ctx.number,
                commit_id=ctx.head_sha,
                body=plan.render_body(unplaced),
                comments=[],
            )
        verb = "created"
    else:
        added = 0
        for f in plan.new:
            try:
                client.create_review_comment(
                    ctx.owner,
                    ctx.repo,
                    ctx.number,
                    commit_id=ctx.head_sha,
                    path=f.path,
                    line=f.line,
                    body=comment_body(f),
                )
                added += 1
            except GitHubAPIError as exc:
                if exc.status != 422:
                    raise
                unplaced.append(f)
        body = plan.render_body(unplaced)
        if body != plan.existing_body:  # an identical re-run makes zero writes
            client.update_review(ctx.owner, ctx.repo, ctx.number, plan.review_id, body)
        verb = "updated"

    # Cleanup is best-effort: failing to tidy must not hide what we just posted.
    notes: list[str] = []
    deleted = resolved = 0
    for comment_id in plan.delete_ids:
        try:
            client.delete_review_comment(ctx.owner, ctx.repo, comment_id)
            deleted += 1
        except GitHubAPIError as exc:
            notes.append(f"could not delete stale comment {comment_id}: {exc}")
    for comment_id, rule_id in plan.resolve:
        try:
            note = f"{resolved_marker(rule_id)}\n{RESOLVED_NOTE}"
            client.update_review_comment(ctx.owner, ctx.repo, comment_id, note)
            resolved += 1
        except GitHubAPIError as exc:
            notes.append(f"could not resolve comment {comment_id}: {exc}")

    msg = (
        f"Review {verb}: {added} comment(s) added, {plan.kept} kept, "
        f"{deleted} deleted, {resolved} resolved"
    )
    if unplaced:
        msg += f"; inline comments rejected (422), {len(unplaced)} finding(s) listed in the body"
    if plan.body_only:
        msg += f"; {len(plan.body_only)} more listed in the body"
    lines = [msg + "."]
    lines += [f"::warning title=Review cleanup::{_escape_data(n)}" for n in notes]
    return "\n".join(lines)
