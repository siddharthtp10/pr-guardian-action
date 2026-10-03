import io
from pathlib import Path

from pr_guardian.cli import EXIT_CONFIG, EXIT_FINDINGS, EXIT_OK, escape_workflow_data, main

FIXTURES = Path(__file__).parent / "fixtures"
GOOD = {"PRG_GITHUB_TOKEN": "ghs_dummy_token_for_tests"}


def run(env, tmp_path=None):
    """Run the CLI with a fake GitHub that reports an empty PR."""
    out = io.StringIO()
    if tmp_path is not None:
        env = {**event_env(tmp_path), **env}
    code = main(env=env, out=out, client_factory=lambda c, x: FakeClient(PullFiles([], False)))
    return code, out.getvalue()


def test_rules_only_run_succeeds_and_says_so(tmp_path):
    code, text = run(GOOD, tmp_path)
    assert code == EXIT_OK
    assert "rules-only" in text
    assert "AI layer disabled" in text


def test_output_never_contains_secret_values(tmp_path):
    code, text = run({**GOOD, "PRG_ANTHROPIC_API_KEY": "super-secret-value"}, tmp_path)
    assert code == EXIT_OK
    assert "rules+ai" in text
    assert "super-secret-value" not in text
    assert "ghs_dummy_token_for_tests" not in text


def test_invalid_config_exits_2_with_annotations():
    code, text = run({**GOOD, "PRG_FAIL_ON": "urgent"})
    assert code == EXIT_CONFIG
    assert text.startswith("::error")


def test_workflow_command_injection_is_neutralised():
    # A hostile input value tries to smuggle a second workflow command via a newline.
    code, text = run({**GOOD, "PRG_FAIL_ON": "x\n::set-output name=pwn::1"})
    assert code == EXIT_CONFIG
    # Two layers defend this: !r in the message and escape_workflow_data().
    # The property that matters: exactly one line, so no forged second command.
    assert len(text.strip().splitlines()) == 1


def test_escape_workflow_data():
    assert escape_workflow_data("a%b\r\nc") == "a%25b%0D%0Ac"


# --- Stage 2: end-to-end through the CLI with a fake GitHub -----------------
import json  # noqa: E402

from pr_guardian.cli import EXIT_API  # noqa: E402
from pr_guardian.github_api import GitHubAPIError, PullFiles, _parse_file  # noqa: E402
from tests.fakes import FakeClient  # noqa: E402


def event_env(tmp_path, head_repo="o/r"):
    payload = {
        "pull_request": {
            "number": 3,
            "head": {"sha": "b" * 40, "repo": {"full_name": head_repo}},
            "base": {"repo": {"full_name": "o/r"}},
        }
    }
    p = tmp_path / "event.json"
    p.write_text(json.dumps(payload))
    return {
        **GOOD,
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_EVENT_PATH": str(p),
        "GITHUB_REPOSITORY": "o/r",
    }


def test_pr_fixture_end_to_end(tmp_path):
    raw = json.loads((FIXTURES / "pr_files.json").read_text())
    result = PullFiles([_parse_file(e) for e in raw], truncated=False)
    out = io.StringIO()
    code = main(env=event_env(tmp_path), out=out, client_factory=lambda c, x: FakeClient(result))
    text = out.getvalue()
    assert code == EXIT_FINDINGS  # the fixture has one high finding (TF-003)
    assert "Reviewing 2 file(s)" in text
    assert "infra/main.tf [terraform]" in text
    assert "NOT reviewed" in text and "big.tf" in text
    assert "1 deleted" in text and "1 unsupported file type" in text


def test_fork_notice_is_printed(tmp_path):
    env = event_env(tmp_path, head_repo="stranger/r")
    out = io.StringIO()
    main(env=env, out=out, client_factory=lambda c, x: FakeClient(PullFiles([], False)))
    assert "fork pull request" in out.getvalue()
    assert "rules-only" in out.getvalue()


def test_api_failure_exits_3(tmp_path):
    out = io.StringIO()
    err = GitHubAPIError("denied\nreally", 403)
    code = main(env=event_env(tmp_path), out=out, client_factory=lambda c, x: FakeClient(err))
    assert code == EXIT_API
    assert "::error title=GitHub API error::denied%0Areally" in out.getvalue()


def test_wrong_event_exits_2(tmp_path):
    out = io.StringIO()
    code = main(env={**event_env(tmp_path), "GITHUB_EVENT_NAME": "push"}, out=out)
    assert code == EXIT_CONFIG and "::error title=Unsupported event" in out.getvalue()


def test_rules_run_end_to_end_and_report_findings(tmp_path):
    raw = json.loads((FIXTURES / "pr_files.json").read_text())
    result = PullFiles([_parse_file(e) for e in raw], truncated=False)
    out = io.StringIO()
    main(env=event_env(tmp_path), out=out, client_factory=lambda c, x: FakeClient(result))
    text = out.getvalue()
    assert "Rules loaded:" in text
    assert "[HIGH] TF-003 infra/main.tf:3" in text  # acl = "public-read" on line 3
    assert "Findings: 1 (1 high)" in text
    assert "public-read" not in text.split("Findings:")[1]  # never echoes the diff


# --- Stage 4: posting results ------------------------------------------------
from pr_guardian.github_api import ReviewComment  # noqa: E402
from pr_guardian.publish import comment_marker  # noqa: E402


def fixture_files():
    raw = json.loads((FIXTURES / "pr_files.json").read_text())
    return PullFiles([_parse_file(e) for e in raw], truncated=False)


def run_fixture(tmp_path, client=None, **env):
    client = client or FakeClient(fixture_files())
    out = io.StringIO()
    summary = tmp_path / "summary.md"
    full_env = {**event_env(tmp_path), "GITHUB_STEP_SUMMARY": str(summary), **env}
    code = main(env=full_env, out=out, client_factory=lambda c, x: client)
    return code, out.getvalue(), client, summary


def test_posts_one_review_with_an_inline_comment_and_fails_the_check(tmp_path):
    code, text, client, _ = run_fixture(tmp_path)
    assert code == EXIT_FINDINGS
    assert len(client.reviews) == 1
    review = client.reviews[0]
    assert review["commit_id"] == "b" * 40
    [comment] = review["comments"]
    assert comment["path"] == "infra/main.tf"
    assert (comment["line"], comment["side"]) == (3, "RIGHT")
    assert comment_marker("TF-003") in comment["body"]
    assert "public-read" not in comment["body"]  # message only, never the diff
    assert "Review created: 1 comment(s) added" in text


def test_annotation_is_an_error_when_failing_and_a_warning_when_advisory(tmp_path):
    _, text, _, _ = run_fixture(tmp_path)
    assert "::error file=infra/main.tf,line=3,title=TF-003 (high)::" in text
    code, text, _, _ = run_fixture(tmp_path, PRG_FAIL_ON="none")
    assert code == EXIT_OK
    assert "::warning file=infra/main.tf,line=3," in text


def test_dry_run_does_not_post(tmp_path):
    code, text, client, _ = run_fixture(tmp_path, PRG_DRY_RUN="true")
    assert code == EXIT_FINDINGS  # dry-run changes posting, not the verdict
    assert client.reviews == []
    assert "dry-run, not posting" in text


def test_fork_pr_does_not_post_but_still_annotates(tmp_path):
    client = FakeClient(fixture_files())
    out = io.StringIO()
    env = event_env(tmp_path, head_repo="stranger/r")
    code = main(env=env, out=out, client_factory=lambda c, x: client)
    assert code == EXIT_FINDINGS
    assert client.reviews == []
    assert "::error file=infra/main.tf,line=3" in out.getvalue()


def test_rerun_does_not_repeat_a_comment_still_on_the_same_line(tmp_path):
    body = comment_marker("TF-003") + "\nold text"
    client = FakeClient(fixture_files(), existing=[ReviewComment("infra/main.tf", 3, body, True)])
    code, text, client, _ = run_fixture(tmp_path, client)
    assert code == EXIT_FINDINGS  # the check still fails: it never depends on comments
    assert client.reviews == []
    assert "already commented on" in text


def test_outdated_or_human_comments_do_not_suppress_a_finding(tmp_path):
    body = comment_marker("TF-003")
    existing = [
        ReviewComment("infra/main.tf", None, body, True),  # outdated: code changed since
        ReviewComment("infra/main.tf", 3, body, False),  # a human pasting our marker
    ]
    _, _, client, _ = run_fixture(tmp_path, FakeClient(fixture_files(), existing=existing))
    assert len(client.reviews) == 1


def test_rejected_inline_comments_fall_back_to_the_review_body(tmp_path):
    client = FakeClient(fixture_files(), post_errors=[GitHubAPIError("bad line", 422)])
    code, text, client, _ = run_fixture(tmp_path, client)
    assert code == EXIT_FINDINGS
    [review] = client.reviews
    assert review["comments"] == []
    assert "`infra/main.tf:3`" in review["body"]
    assert "inline comments rejected (422)" in text


def test_post_failure_is_loud(tmp_path):
    err = GitHubAPIError("GitHub denied access", 403)
    client = FakeClient(fixture_files(), post_errors=[err])
    code, text, _, _ = run_fixture(tmp_path, client, PRG_FAIL_ON="none")
    assert code == EXIT_API  # nothing failed the check, but the review never appeared
    assert "::error title=Could not post review::GitHub denied access" in text
    client = FakeClient(fixture_files(), post_errors=[err])
    code, _, _, _ = run_fixture(tmp_path, client)
    assert code == EXIT_FINDINGS  # findings are the more useful signal when both apply


def test_no_findings_means_no_review_and_a_green_check(tmp_path):
    code, text, client, summary = run_fixture(tmp_path, FakeClient(PullFiles([], False)))
    assert code == EXIT_OK
    assert client.reviews == []
    assert "Check passed" in summary.read_text()


def test_job_summary_lists_findings_and_skipped_files(tmp_path):
    _, _, _, summary = run_fixture(tmp_path)
    md = summary.read_text()
    assert "**Check failed:** 1 finding(s) at or above `high`" in md
    assert "| high | TF-003 | `infra/main.tf:3` |" in md
    assert "`infra/big.tf`" in md  # skipped files are visible here too


# --- Stage 4b: re-run reconciliation, outputs, exclusions ---------------------


def test_rerun_on_the_same_pr_makes_no_new_writes(tmp_path):
    """The headline requirement: same PR, same findings, run twice."""
    client = FakeClient(fixture_files())
    run_fixture(tmp_path, client)
    assert client.writes() == ["create_review"]
    reviews_after_first = len(client.stored_reviews)

    client.calls.clear()
    code, text, _, _ = run_fixture(tmp_path, client)

    assert code == EXIT_FINDINGS  # the verdict is recomputed every run
    assert len(client.stored_reviews) == reviews_after_first == 1
    assert len(client.open_comments()) == 1
    assert client.writes() == []  # identical body, nothing new: zero writes
    assert "Review updated: 0 comment(s) added, 1 kept" in text


def test_new_commit_updates_the_review_body_in_place(tmp_path):
    client = FakeClient(fixture_files())
    run_fixture(tmp_path, client)
    client.calls.clear()
    env = event_env(tmp_path)
    payload = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text())
    payload["pull_request"]["head"]["sha"] = "c" * 40
    Path(env["GITHUB_EVENT_PATH"]).write_text(json.dumps(payload))

    out = io.StringIO()
    main(env=env, out=out, client_factory=lambda c, x: client)

    assert client.writes() == ["update_review"]  # edited, not a second review
    assert "ccccccc" in client.stored_reviews[0].body
    assert len(client.stored_reviews) == 1


def test_fixed_finding_removes_its_comment_and_says_so(tmp_path):
    client = FakeClient(fixture_files())
    run_fixture(tmp_path, client)
    assert len(client.open_comments()) == 1

    client.result = PullFiles([], False)  # the developer fixed it
    code, text, _, _ = run_fixture(tmp_path, client)

    assert code == EXIT_OK
    assert client.open_comments() == []
    assert "1 deleted" in text
    assert "No open findings" in client.stored_reviews[0].body


def test_step_outputs_are_written(tmp_path):
    output = tmp_path / "out.txt"
    run_fixture(tmp_path, GITHUB_OUTPUT=str(output))
    assert output.read_text().splitlines() == [
        "conclusion=failure",
        "findings-count=1",
        "ai-findings-count=0",
    ]
    output.unlink()
    run_fixture(tmp_path, FakeClient(PullFiles([], False)), GITHUB_OUTPUT=str(output))
    assert output.read_text().splitlines() == [
        "conclusion=success",
        "findings-count=0",
        "ai-findings-count=0",
    ]


def test_paths_exclusion_removes_files_from_review(tmp_path):
    code, text, client, _ = run_fixture(tmp_path, PRG_PATHS="!infra/**")
    assert code == EXIT_OK and client.reviews == []
