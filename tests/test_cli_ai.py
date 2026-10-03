"""The AI layer end to end, through the real CLI, against fakes (no network)."""

import io
import json
import subprocess
import sys
from pathlib import Path

from pr_guardian.cli import EXIT_FINDINGS, EXIT_OK, main
from pr_guardian.github_api import GitHubAPIError, PullFiles, _parse_file
from pr_guardian.publish import AI_MARKER
from tests.fakes import FakeClient
from tests.fakes_ai import FakeAnthropic, ai_finding, factory_for

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
KEY = "fake-api-key-for-tests-" + "0123456789"

APP = {
    "filename": "k8s/app.yaml",
    "status": "added",
    "additions": 4,
    "deletions": 0,
    "patch": "@@ -0,0 +1,4 @@\n+apiVersion: apps/v1\n+kind: Deployment\n+spec:\n+  replicas: 1",
}


def pr_files(*extra, with_rule_finding=True):
    raw = json.loads((FIXTURES / "pr_files.json").read_text()) if with_rule_finding else []
    return PullFiles([_parse_file(e) for e in [*raw, *extra]], truncated=False)


def make_env(tmp_path, head_repo="o/r", key=KEY, **inputs):
    payload = {
        "pull_request": {
            "number": 3,
            "head": {"sha": "b" * 40, "repo": {"full_name": head_repo}},
            "base": {"repo": {"full_name": "o/r"}},
        }
    }
    event = tmp_path / "event.json"
    event.write_text(json.dumps(payload))
    env = {
        "PRG_GITHUB_TOKEN": "fake-gh-token",
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_REPOSITORY": "o/r",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "GITHUB_OUTPUT": str(tmp_path / "output.txt"),
    }
    if key:
        env["PRG_ANTHROPIC_API_KEY"] = key
    env.update({f"PRG_{k.upper()}": str(v) for k, v in inputs.items()})
    return env


def run(env, gh, fake=None):
    out = io.StringIO()
    factory = factory_for(fake or FakeAnthropic([]))
    code = main(env=env, out=out, client_factory=lambda c, x: gh, ai_client_factory=factory)
    return code, out.getvalue(), factory


def ai_comments(gh):
    return [c for c in gh.stored_comments if AI_MARKER in c.body]


def test_ai_comment_is_posted_labelled_alongside_rule_comments(tmp_path):
    gh = FakeClient(pr_files(APP))
    fake = FakeAnthropic([ai_finding()])
    code, text, factory = run(make_env(tmp_path), gh, fake)

    assert factory.seen == [KEY]
    assert "AI review (advisory" in text and "1 finding(s)" in text
    [review] = gh.reviews
    bodies = [c["body"] for c in review["comments"]]
    ai_body = next(b for b in bodies if AI_MARKER in b)
    assert "AI-generated advisory" in ai_body and "never affects the check result" in ai_body
    assert any("TF-003" in b for b in bodies)  # the rule comment is still there
    assert "AI review (advisory" in review["body"]
    assert code == EXIT_FINDINGS  # from the RULE finding


def test_ai_findings_can_never_fail_the_check(tmp_path):
    """The headline guarantee: a scary-sounding AI finding does not change the verdict."""
    gh = FakeClient(pr_files(APP, with_rule_finding=False))
    scary = ai_finding(
        category="security", confidence="high", title="CRITICAL: remote code execution", line=4
    )
    env = make_env(tmp_path)
    code, text, _ = run(env, gh, FakeAnthropic([scary]))
    assert code == EXIT_OK
    assert len(ai_comments(gh)) == 1
    outputs = Path(env["GITHUB_OUTPUT"]).read_text().splitlines()
    assert "conclusion=success" in outputs and "ai-findings-count=1" in outputs
    # ...even with the strictest threshold:
    code, _, _ = run(
        make_env(tmp_path, fail_on="low"),
        FakeClient(pr_files(APP, with_rule_finding=False)),
        FakeAnthropic([scary]),
    )
    assert code == EXIT_OK


def test_without_a_key_no_ai_runs_and_the_sdk_is_not_even_imported(tmp_path):
    gh = FakeClient(pr_files(APP))
    code, text, factory = run(make_env(tmp_path, key=None), gh)
    assert factory.seen == [] and "AI review" not in text
    assert code == EXIT_FINDINGS and ai_comments(gh) == []
    # Importing the CLI must not import `anthropic`: rules-only installs don't have it.
    probe = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-c",
            "import sys; import pr_guardian.cli; sys.exit(1 if 'anthropic' in sys.modules else 0)",
        ],
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": ""},
        check=False,
    )
    assert probe.returncode == 0


def test_fork_prs_never_call_the_api_even_if_a_key_is_present(tmp_path):
    gh = FakeClient(pr_files(APP))
    fake = FakeAnthropic([ai_finding()])
    code, text, factory = run(make_env(tmp_path, head_repo="stranger/r"), gh, fake)
    assert fake.calls == [] and factory.seen == []
    assert "fork pull request" in text and gh.writes() == []
    assert code == EXIT_FINDINGS


def test_api_failure_degrades_to_rules_only_with_a_warning(tmp_path):
    gh = FakeClient(pr_files(APP))
    code, text, _ = run(make_env(tmp_path), gh, FakeAnthropic(exc=RuntimeError("boom sk-ant-LEAK")))
    assert "::warning title=AI review failed" in text and "sk-ant-LEAK" not in text
    assert code == EXIT_FINDINGS  # unchanged by the AI failure
    assert gh.reviews and ai_comments(gh) == []  # rule review still posted
    assert "AI review did not run" in gh.reviews[0]["body"]


def test_invalid_model_output_is_discarded_not_posted(tmp_path):
    gh = FakeClient(pr_files(APP))
    bad = [ai_finding(line=999), ai_finding(path="not/reviewed.tf")]
    _, text, _ = run(make_env(tmp_path), gh, FakeAnthropic(bad))
    assert ai_comments(gh) == []
    assert "discarded 2: not on a changed line of a reviewed file" in text


def test_rerun_posts_an_ai_comment_once_per_line_even_if_the_wording_changes(tmp_path):
    gh = FakeClient(pr_files(APP))
    env = make_env(tmp_path)
    run(env, gh, FakeAnthropic([ai_finding(title="Single replica")]))
    assert len(ai_comments(gh)) == 1
    gh.calls.clear()
    run(env, gh, FakeAnthropic([ai_finding(title="Only one replica, no HA")]))  # different words
    assert len(ai_comments(gh)) == 1 and "create_review_comment" not in gh.names()


def test_a_rules_only_rerun_does_not_delete_earlier_ai_comments(tmp_path):
    gh = FakeClient(pr_files(APP))
    run(make_env(tmp_path), gh, FakeAnthropic([ai_finding()]))
    assert len(ai_comments(gh)) == 1
    # Later run on the same PR with no key (or a fork, or an outage): the AI simply did not run.
    run(make_env(tmp_path, key=None), gh)
    assert len(ai_comments(gh)) == 1
    run(make_env(tmp_path), gh, FakeAnthropic(exc=RuntimeError("down")))
    assert len(ai_comments(gh)) == 1


def test_outdated_ai_comments_are_retired(tmp_path):
    gh = FakeClient(pr_files(APP))
    env = make_env(tmp_path)
    run(env, gh, FakeAnthropic([ai_finding()]))
    c = ai_comments(gh)[0]
    gh.stored_comments = [type(c)(c.path, None, c.body, True, c.id)]  # GitHub: code changed
    run(env, gh, FakeAnthropic([]))
    assert ai_comments(gh) == []


def test_dry_run_calls_the_model_but_posts_nothing(tmp_path):
    gh = FakeClient(pr_files(APP))
    fake = FakeAnthropic([ai_finding()])
    code, text, _ = run(make_env(tmp_path, dry_run="true"), gh, fake)
    assert len(fake.calls) == 1 and gh.writes() == []
    assert "Review: dry-run, not posting." in text
    assert "[AI reliability/medium] k8s/app.yaml:4" in text


def test_job_summary_has_a_labelled_ai_section_with_cost(tmp_path):
    gh = FakeClient(pr_files(APP))
    env = make_env(tmp_path)
    run(env, gh, FakeAnthropic([ai_finding()], usage=(1500, 400)))
    md = Path(env["GITHUB_STEP_SUMMARY"]).read_text()
    assert "### AI review (advisory)" in md
    assert "never affects the check result" in md
    assert "`k8s/app.yaml:4` **Single replica**" in md
    assert "1500 input / 400 output tokens" in md and "$" in md


def test_ai_findings_on_rule_flagged_lines_are_dropped(tmp_path):
    gh = FakeClient(pr_files(APP))
    dup = ai_finding(path="infra/main.tf", line=3)  # TF-003 already flags this line
    _, text, _ = run(make_env(tmp_path), gh, FakeAnthropic([dup]))
    assert ai_comments(gh) == [] and "duplicates a rule finding" in text


def test_github_post_failure_still_surfaces_in_the_exit_code(tmp_path):
    gh = FakeClient(pr_files(APP), post_errors=[GitHubAPIError("denied", 403)])
    code, text, _ = run(make_env(tmp_path, fail_on="none"), gh, FakeAnthropic([ai_finding()]))
    assert "Could not post review" in text and code != EXIT_OK


def test_dependabot_runs_skip_the_ai_even_if_a_key_is_present(tmp_path):
    gh = FakeClient(pr_files(APP))
    fake = FakeAnthropic([ai_finding()])
    env = {**make_env(tmp_path), "GITHUB_ACTOR": "dependabot[bot]"}
    code, text, factory = run(env, gh, fake)
    assert fake.calls == [] and factory.seen == [] and gh.writes() == []
    assert "AI review (skipped): Dependabot run" in text
