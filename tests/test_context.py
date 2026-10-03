import json

import pytest

from pr_guardian.context import ContextError, load_context

SHA = "a" * 40


def make_env(tmp_path, *, event="pull_request", head_repo="o/r", base_repo="o/r", **extra):
    payload = {
        "pull_request": {
            "number": 12,
            "head": {"sha": SHA, "repo": {"full_name": head_repo} if head_repo else None},
            "base": {"repo": {"full_name": base_repo}},
        }
    }
    path = tmp_path / "event.json"
    path.write_text(json.dumps(payload))
    return {
        "GITHUB_EVENT_NAME": event,
        "GITHUB_EVENT_PATH": str(path),
        "GITHUB_REPOSITORY": base_repo,
        **extra,
    }


def test_same_repo_pr(tmp_path):
    ctx = load_context(make_env(tmp_path))
    assert (ctx.owner, ctx.repo, ctx.number, ctx.head_sha) == ("o", "r", 12, SHA)
    assert not ctx.is_fork and ctx.can_post
    assert ctx.api_url == "https://api.github.com"


def test_fork_pr_cannot_post(tmp_path):
    ctx = load_context(make_env(tmp_path, head_repo="stranger/r"))
    assert ctx.is_fork and not ctx.can_post


def test_deleted_fork_counts_as_fork(tmp_path):
    assert load_context(make_env(tmp_path, head_repo=None)).is_fork


def test_pull_request_target_is_refused(tmp_path):
    with pytest.raises(ContextError, match="pull_request_target"):
        load_context(make_env(tmp_path, event="pull_request_target"))


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", ""])
def test_other_events_refused(tmp_path, event):
    with pytest.raises(ContextError, match="only runs"):
        load_context(make_env(tmp_path, event=event))


def test_bad_repository_and_payload(tmp_path):
    with pytest.raises(ContextError):
        load_context({**make_env(tmp_path), "GITHUB_REPOSITORY": "no-slash"})
    with pytest.raises(ContextError, match="payload"):
        load_context({**make_env(tmp_path), "GITHUB_EVENT_PATH": str(tmp_path / "missing.json")})


def test_http_api_url_refused(tmp_path):
    with pytest.raises(ContextError, match="https"):
        load_context(make_env(tmp_path, GITHUB_API_URL="http://ghe.example/api/v3"))


def test_ghes_api_url_is_honoured(tmp_path):
    ctx = load_context(make_env(tmp_path, GITHUB_API_URL="https://ghe.example/api/v3/"))
    assert ctx.api_url == "https://ghe.example/api/v3"
