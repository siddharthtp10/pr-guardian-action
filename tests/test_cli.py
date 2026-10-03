import io
from pathlib import Path

from pr_guardian.cli import EXIT_CONFIG, EXIT_OK, escape_workflow_data, main

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


class FakeClient:
    def __init__(self, result):
        self.result = result

    def list_pull_files(self, owner, repo, number):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_pr_fixture_end_to_end(tmp_path):
    raw = json.loads((FIXTURES / "pr_files.json").read_text())
    result = PullFiles([_parse_file(e) for e in raw], truncated=False)
    out = io.StringIO()
    code = main(env=event_env(tmp_path), out=out, client_factory=lambda c, x: FakeClient(result))
    text = out.getvalue()
    assert code == EXIT_OK
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
