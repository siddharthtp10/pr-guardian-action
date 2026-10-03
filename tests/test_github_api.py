import json

import pytest

from pr_guardian.github_api import (
    MAX_PAGES,
    PER_PAGE,
    GitHubAPIError,
    GitHubClient,
)

API = "https://api.github.com"


def entry(i):
    return {
        "filename": f"f{i}.tf",
        "status": "added",
        "patch": "@@ -0,0 +1 @@\n+x",
        "additions": 1,
        "deletions": 0,
    }


class FakeOpener:
    """Returns queued (status, headers, body) tuples and records requests."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ok(entries):
    return 200, {}, json.dumps(entries).encode()


def client(opener, sleeps=None):
    return GitHubClient(
        "tok_secret",
        API,
        opener=opener,
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
    )


def test_single_page():
    op = FakeOpener(ok([entry(1), entry(2)]))
    result = client(op).list_pull_files("o", "r", 7)
    assert [f.filename for f in result.files] == ["f1.tf", "f2.tf"]
    assert not result.truncated
    req = op.requests[0]
    assert req.full_url == f"{API}/repos/o/r/pulls/7/files?per_page=100&page=1"
    assert req.get_header("Authorization") == "Bearer tok_secret"
    assert req.get_header("X-github-api-version") == "2022-11-28"


def test_paginates_until_short_page():
    op = FakeOpener(ok([entry(i) for i in range(PER_PAGE)]), ok([entry(999)]))
    result = client(op).list_pull_files("o", "r", 1)
    assert len(result.files) == PER_PAGE + 1
    assert len(op.requests) == 2
    assert "page=2" in op.requests[1].full_url


def test_stops_at_page_ceiling_and_flags_truncation():
    full = ok([entry(i) for i in range(PER_PAGE)])
    op = FakeOpener(*[full] * MAX_PAGES)
    result = client(op).list_pull_files("o", "r", 1)
    assert result.truncated and len(result.files) == MAX_PAGES * PER_PAGE


def test_missing_patch_becomes_none():
    e = entry(1)
    del e["patch"]
    assert client(FakeOpener(ok([e]))).list_pull_files("o", "r", 1).files[0].patch is None


def test_retries_5xx_then_succeeds_with_backoff():
    sleeps = []
    op = FakeOpener((502, {}, b""), (503, {}, b""), ok([entry(1)]))
    assert len(client(op, sleeps).list_pull_files("o", "r", 1).files) == 1
    assert sleeps == [1, 2]


def test_network_error_retried_then_reported():
    op = FakeOpener(OSError("boom"), OSError("boom"), OSError("boom"))
    with pytest.raises(GitHubAPIError, match="network error"):
        client(op).list_pull_files("o", "r", 1)


@pytest.mark.parametrize(
    ("status", "headers", "needle"),
    [
        (401, {}, "rejected the token"),
        (403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "123"}, "rate limit"),
        (403, {}, "pull-requests: read"),
        (404, {}, "not found"),
        (301, {"location": "https://evil.example"}, "refusing to forward credentials"),
    ],
)
def test_error_mapping(status, headers, needle):
    with pytest.raises(GitHubAPIError, match=needle) as exc:
        client(FakeOpener((status, headers, b"{}"))).list_pull_files("o", "r", 1)
    assert exc.value.status == status


def test_4xx_is_not_retried():
    op = FakeOpener((404, {}, b"{}"))
    with pytest.raises(GitHubAPIError):
        client(op).list_pull_files("o", "r", 1)
    assert len(op.requests) == 1


def test_token_never_in_error_text():
    with pytest.raises(GitHubAPIError) as exc:
        client(FakeOpener(*[(500, {}, b'{"message":"x"}')] * 3)).list_pull_files("o", "r", 1)
    assert "tok_secret" not in str(exc.value)


def test_rejects_non_https_api_url():
    with pytest.raises(ValueError):
        GitHubClient("t", "http://api.example.com")


def test_garbage_response_shapes_raise():
    with pytest.raises(GitHubAPIError):
        client(FakeOpener((200, {}, b'{"not":"a list"}'))).list_pull_files("o", "r", 1)
    with pytest.raises(GitHubAPIError):
        client(FakeOpener((200, {}, b"<html>"))).list_pull_files("o", "r", 1)


def test_real_opener_does_not_follow_redirects(monkeypatch):
    """The default opener must surface a 3xx instead of re-sending credentials."""
    import urllib.request

    from pr_guardian.github_api import _NoRedirect

    handler = _NoRedirect()
    req = urllib.request.Request(API, headers={"Authorization": "Bearer x"})
    assert handler.redirect_request(req, None, 301, "Moved", {}, "https://evil.example") is None
