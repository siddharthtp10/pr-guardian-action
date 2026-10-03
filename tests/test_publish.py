from pr_guardian.diff import parse_patch
from pr_guardian.engine import Finding
from pr_guardian.filetypes import FileKind
from pr_guardian.github_api import ReviewComment
from pr_guardian.publish import (
    MAX_INLINE_COMMENTS,
    comment_marker,
    md_code,
    plan_review,
    render_annotations,
    render_summary,
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
    plan = plan_review(findings, [], selection(), "high")
    assert len(plan.comments()) == MAX_INLINE_COMMENTS
    assert len(plan.in_body_only) == 5
    assert "5 more not posted inline" in plan.body


def test_only_matching_rule_path_and_line_are_deduplicated():
    existing = [ReviewComment("a.tf", 1, comment_marker("TF-003"), True)]
    findings = [finding(line=1), finding(line=1, rule="TF-004"), finding(line=2)]
    plan = plan_review(findings, existing, selection(), "high")
    assert [(f.line, f.rule_id) for f in plan.inline] == [(1, "TF-004"), (2, "TF-003")]
    assert plan.duplicates == 1


def test_nothing_new_means_no_review():
    existing = [ReviewComment("a.tf", 1, comment_marker("TF-003"), True)]
    assert plan_review([finding()], existing, selection(), "high") is None
    assert plan_review([], [], selection(), "high") is None
