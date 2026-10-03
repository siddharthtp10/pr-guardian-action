from pr_guardian.ai_review import AIFinding, AIResult
from pr_guardian.diff import parse_patch
from pr_guardian.engine import Finding
from pr_guardian.filetypes import FileKind
from pr_guardian.github_api import Review, ReviewComment
from pr_guardian.publish import (
    AI_MARKER,
    MAX_INLINE_COMMENTS,
    REVIEW_MARKER,
    comment_marker,
    find_our_review,
    md_code,
    plan_review,
    render_annotations,
    render_summary,
    resolved_marker,
)
from pr_guardian.selection import ReviewTarget, Selection, Skipped, SkipReason


def finding(path="a.tf", line=1, rule="TF-003", severity="high"):
    return Finding(rule, severity, path, line, "fixed policy text")


def selection(*skipped):
    target = ReviewTarget("a.tf", FileKind.TERRAFORM, "modified", parse_patch("@@ -0,0 +1 @@\n+x"))
    return Selection(targets=[target], skipped=list(skipped), total_changed=1)


def test_hostile_path_cannot_forge_annotation_properties_or_commands():
    path = "x.tf,line=999,title=pwn:\n::set-output name=a::b"
    [line] = render_annotations([finding(path=path)], "high")
    assert line.count("\n") == 0
    # ',' and ':' are escaped inside properties, so the real line=1 is the only one.
    assert "file=x.tf%2Cline=999%2Ctitle=pwn%3A%0A%3A%3Aset-output name=a%3A%3Ab,line=1," in line


def test_md_code_neutralises_markdown():
    assert md_code("a|b`c\n@org/team [x](http://e)") == "`a\\|b?c?@org/team [x](http://e)`"


def test_summary_table_survives_a_pipe_in_a_file_name():
    sel = selection(Skipped("big|.tf", SkipReason.NO_PATCH))
    md = render_summary([finding(path="evil|name.tf")], sel, "high", "rules-only")
    assert "| high | TF-003 | `evil\\|name.tf:1` |" in md
    assert "- `big\\|.tf`: no patch from GitHub" in md


def test_overflow_findings_go_to_the_body_not_inline():
    findings = [finding(line=n) for n in range(1, MAX_INLINE_COMMENTS + 6)]
    plan = plan_review(findings, [], [], selection(), "high")
    body = plan.render_body([])
    assert len(plan.comments()) == MAX_INLINE_COMMENTS
    assert len(plan.body_only) == 5
    assert "5 more not posted inline" in body


def test_only_matching_rule_path_and_line_are_deduplicated():
    existing = [bot_comment(10, "a.tf", 1, "TF-003")]
    findings = [finding(line=1), finding(line=1, rule="TF-004"), finding(line=2)]
    plan = plan_review(findings, [], existing, selection(), "high")
    assert sorted((f.line, f.rule_id) for f in plan.new) == [(1, "TF-004"), (2, "TF-003")]
    assert plan.kept == 1


def test_nothing_new_and_no_review_means_no_post():
    existing = [bot_comment(10, "a.tf", 1, "TF-003")]
    assert plan_review([finding()], [], existing, selection(), "high").noop
    assert plan_review([], [], [], selection(), "high").noop


# --- re-run reconciliation (the "update, don't duplicate" behaviour) -----------


def bot_comment(cid, path="a.tf", line=1, rule="TF-003", bot=True, reply_to=None):
    return ReviewComment(path, line, comment_marker(rule) + "\ntext", bot, cid, reply_to)


def our_review(rid=1, bot=True):
    return Review(rid, REVIEW_MARKER + "\nold body", bot)


def plan(findings, reviews=(), comments=()):
    return plan_review(findings, list(reviews), list(comments), selection(), "high", "a" * 40)


def test_first_run_with_findings_creates_a_review():
    p = plan([finding(), finding(line=2)])
    assert p.review_id is None and not p.noop and len(p.new) == 2


def test_rerun_with_unchanged_findings_keeps_everything():
    p = plan([finding()], [our_review()], [bot_comment(10)])
    assert p.review_id == 1 and p.new == [] and p.kept == 1
    assert p.delete_ids == [] and p.resolve == [] and not p.noop


def test_new_finding_on_rerun_is_added_next_to_the_kept_one():
    p = plan([finding(), finding(line=5)], [our_review()], [bot_comment(10)])
    assert [f.line for f in p.new] == [5] and p.kept == 1


def test_fixed_finding_comment_is_deleted():
    p = plan([], [our_review()], [bot_comment(10)])
    assert p.delete_ids == [10] and not p.noop  # the review body still needs updating


def test_fixed_finding_with_a_reply_is_resolved_not_deleted():
    reply = ReviewComment("a.tf", 1, "will fix", False, 11, 10)
    p = plan([], [our_review()], [bot_comment(10), reply])
    assert p.delete_ids == [] and p.resolve == [(10, "TF-003")]


def test_outdated_comment_is_replaced():
    outdated = bot_comment(10, line=None)
    p = plan([finding(line=7)], [our_review()], [outdated])
    assert p.delete_ids == [10] and [f.line for f in p.new] == [7]


def test_duplicate_open_comments_collapse_to_one():
    p = plan([finding()], [our_review()], [bot_comment(10), bot_comment(11)])
    assert p.kept == 1 and p.delete_ids == [11]


def test_resolved_comments_no_longer_count_as_open():
    resolved = ReviewComment("a.tf", 1, resolved_marker("TF-003") + "\nResolved", True, 10)
    p = plan([finding()], [our_review()], [resolved])
    assert [f.rule_id for f in p.new] == ["TF-003"] and p.delete_ids == []


def test_human_comments_and_reviews_are_neither_trusted_nor_touched():
    human = bot_comment(10, bot=False)
    p = plan([finding()], [our_review(bot=False)], [human])
    assert p.review_id is None  # a human's look-alike review is not "ours"
    assert [f.rule_id for f in p.new] == ["TF-003"]  # and cannot suppress a finding
    assert p.delete_ids == [] and p.resolve == []


def test_newest_of_our_reviews_wins():
    assert find_our_review([our_review(1), our_review(9), our_review(4)]).id == 9


def test_cap_keeps_the_most_severe_findings_inline():
    many = [finding(path=f"f{i:02}.tf", severity="low") for i in range(MAX_INLINE_COMMENTS + 3)]
    many.append(finding(path="zzz.tf", rule="SEC-001", severity="critical"))
    p = plan(many)
    assert any(f.severity == "critical" for f in p.new)
    assert len(p.body_only) == 4 and all(f.severity == "low" for f in p.body_only)


def test_review_body_describes_current_state_and_commit():
    body = plan([finding()], [our_review()], [bot_comment(10)]).render_body([])
    assert body.startswith(REVIEW_MARKER)
    assert "1 open finding(s)" in body and "`aaaaaaa`" in body
    assert "No open findings" in plan([]).render_body([])


# --- advisory (AI) comments in the plan -------------------------------------------


def ai_result(*findings, status="ok", reason=""):
    return AIResult(status, reason, "claude-sonnet-5-5", findings=list(findings))


def ai_item(path="a.tf", line=9):
    return AIFinding(path, line, "reliability", "medium", "A title", "A comment.")


def ai_comment(cid, path="a.tf", line=9, bot=True):
    return ReviewComment(path, line, AI_MARKER + "\nadvisory", bot, cid)


def plan_ai(findings, ai, reviews=(), comments=()):
    return plan_review(findings, list(reviews), list(comments), selection(), "high", "a" * 40, ai)


def test_new_ai_finding_is_planned_and_labelled():
    p = plan_ai([], ai_result(ai_item()))
    assert [a.line for a in p.new_ai] == [9] and not p.noop
    [payload] = p.comments()
    assert "AI-generated advisory" in payload["body"] and AI_MARKER in payload["body"]


def test_ai_comment_is_not_repeated_on_the_same_line():
    p = plan_ai([], ai_result(ai_item()), [our_review()], [ai_comment(10)])
    assert p.new_ai == [] and p.ai_delete_ids == []


def test_ai_comment_is_not_removed_just_because_the_model_stayed_quiet():
    for ai in (ai_result(), None, ai_result(status="failed", reason="down")):
        p = plan_ai([], ai, [our_review()], [ai_comment(10)])
        assert p.ai_delete_ids == [] and p.ai_resolve_ids == [], ai


def test_outdated_ai_comment_is_retired_even_when_the_ai_did_not_run():
    p = plan_ai([], None, [our_review()], [ai_comment(10, line=None)])
    assert p.ai_delete_ids == [10]
    reply = ReviewComment("a.tf", 9, "thanks", False, 11, 10)
    p = plan_ai([], None, [our_review()], [ai_comment(10, line=None), reply])
    assert p.ai_resolve_ids == [10] and p.ai_delete_ids == []


def test_human_look_alike_ai_comments_are_ignored():
    p = plan_ai([], ai_result(ai_item()), [our_review()], [ai_comment(10, bot=False)])
    assert [a.line for a in p.new_ai] == [9]  # not suppressed
    assert p.ai_delete_ids == []  # and not touched


def test_rule_comments_take_priority_for_the_inline_budget():
    rules = [finding(line=n) for n in range(1, MAX_INLINE_COMMENTS + 1)]
    p = plan_ai(rules, ai_result(ai_item(line=99)))
    assert len(p.new) == MAX_INLINE_COMMENTS and p.new_ai == []


def test_ai_on_a_line_with_a_rule_finding_is_not_double_posted():
    p = plan_ai([finding(line=9)], ai_result(ai_item(line=9)))
    assert p.new_ai == []


def test_ai_comments_do_not_confuse_the_rule_reconciler():
    # The AI marker must not match the rule-comment pattern, or a fixed rule
    # finding's cleanup could delete AI comments (or vice versa).
    p = plan_ai([], None, [our_review()], [ai_comment(10)])
    assert p.delete_ids == [] and p.resolve == [] and p.kept == 0


def test_review_body_says_when_ai_ran_and_when_it_did_not():
    ok = plan_ai([], ai_result(ai_item()), [our_review()]).render_body([])
    assert "AI review (advisory" in ok and "never affect the check" in ok
    down = plan_ai([], ai_result(status="failed", reason="the API was down")).render_body([])
    assert "AI review did not run: the API was down" in down
    assert "AI review" not in plan_ai([], None).render_body([])
