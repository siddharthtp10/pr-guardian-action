"""Minimal GitHub REST client (standard library only).

WHY not `requests`/PyGithub: zero runtime dependencies keeps the supply-chain
surface (and the Action's start-up time) at nothing. We need three endpoints:
list PR files, list existing review comments, and create a review.

Security properties, each covered by a test:
- Redirects are refused. Python's urllib would happily forward the
  Authorization header to wherever a 3xx points.
- We build every URL ourselves and never follow ``Link`` headers from the
  response, so the token can only be sent to the configured API host.
- Error messages never include the token.
- POST is never retried: a timeout after GitHub accepted the review would
  otherwise post it twice. Re-runs are de-duplicated instead (see publish.py).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from pr_guardian import __version__

# Pinned so GitHub changing defaults can't silently change our behaviour.
API_VERSION = "2022-11-28"
PER_PAGE = 100  # documented maximum
MAX_PAGES = 30  # 30 x 100 = 3000, the most files the endpoint ever returns
ATTEMPTS = 3
TIMEOUT_SECONDS = 30
MAX_BODY_BYTES = 25 * 1024 * 1024  # one page of patches; guards runner memory

Response = tuple[int, Mapping[str, str], bytes]
Opener = Callable[[urllib.request.Request], Response]


class GitHubAPIError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ChangedFile:
    filename: str
    status: str  # added | removed | modified | renamed | copied | changed | unchanged
    patch: str | None  # GitHub omits it for binary files and very large diffs
    additions: int
    deletions: int
    previous_filename: str | None = None


@dataclass(frozen=True)
class ReviewComment:
    """An existing inline comment, reduced to what de-duplication needs."""

    path: str
    # GitHub moves `line` to follow the code on newer commits and sets it to
    # null when the commented code changed ("outdated"); we keep that as None.
    line: int | None
    body: str
    author_is_bot: bool
    # Needed to edit/delete a comment and to see whether anyone replied to it.
    id: int = 0
    in_reply_to_id: int | None = None


@dataclass(frozen=True)
class Review:
    """An existing review, reduced to what re-run reconciliation needs."""

    id: int
    body: str
    author_is_bot: bool


@dataclass(frozen=True)
class PullFiles:
    files: list[ChangedFile]
    # True when we hit the 3000-file ceiling: there may be files we cannot see.
    truncated: bool


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None  # makes urllib raise HTTPError for 3xx instead of following


def _default_open(request: urllib.request.Request) -> Response:
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as resp:
            body = resp.read(MAX_BODY_BYTES + 1)
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, body
    except urllib.error.HTTPError as exc:  # non-2xx, including blocked redirects
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read(MAX_BODY_BYTES)


class GitHubClient:
    def __init__(
        self,
        token: str,
        api_url: str,
        opener: Opener = _default_open,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not api_url.startswith("https://"):
            raise ValueError("api_url must use https")
        self._token = token
        self._base = api_url.rstrip("/")
        self._opener = opener
        self._sleep = sleep

    def list_pull_files(self, owner: str, repo: str, number: int) -> PullFiles:
        files: list[ChangedFile] = []
        full_last_page = False
        for page in range(1, MAX_PAGES + 1):
            data = self._get_json(
                f"/repos/{owner}/{repo}/pulls/{number}/files",
                {"per_page": PER_PAGE, "page": page},
            )
            if not isinstance(data, list):
                raise GitHubAPIError("unexpected response shape from the files endpoint")
            files.extend(_parse_file(entry) for entry in data)
            full_last_page = len(data) == PER_PAGE
            if not full_last_page:
                break
        return PullFiles(files=files, truncated=full_last_page)

    def list_review_comments(self, owner: str, repo: str, number: int) -> list[ReviewComment]:
        data = self._paginate(f"/repos/{owner}/{repo}/pulls/{number}/comments")
        return [c for c in map(_parse_comment, data) if c is not None]

    def list_reviews(self, owner: str, repo: str, number: int) -> list[Review]:
        data = self._paginate(f"/repos/{owner}/{repo}/pulls/{number}/reviews")
        return [r for r in map(_parse_review, data) if r is not None]

    def _paginate(self, path: str) -> list:
        items: list = []
        for page in range(1, MAX_PAGES + 1):
            data = self._get_json(path, {"per_page": PER_PAGE, "page": page})
            if not isinstance(data, list):
                raise GitHubAPIError("unexpected response shape from a list endpoint")
            items.extend(data)
            if len(data) < PER_PAGE:
                break
        return items

    def create_review(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        commit_id: str,
        body: str,
        comments: list[dict[str, object]],
    ) -> None:
        # event=COMMENT, never REQUEST_CHANGES: the check status is what gates
        # the merge, and a bot "requesting changes" would need dismissing by hand.
        payload = {"commit_id": commit_id, "body": body, "event": "COMMENT", "comments": comments}
        self._request("POST", f"/repos/{owner}/{repo}/pulls/{number}/reviews", payload=payload)

    def update_review(self, owner: str, repo: str, number: int, review_id: int, body: str) -> None:
        # The API can only change a review's BODY; inline comments are separate
        # objects, edited or deleted one by one below.
        path = f"/repos/{owner}/{repo}/pulls/{number}/reviews/{review_id}"
        self._request("PUT", path, payload={"body": body})

    def create_review_comment(
        self, owner: str, repo: str, number: int, *, commit_id: str, path: str, line: int, body: str
    ) -> None:
        """Add one comment to the PR (used for findings that appear on a re-run)."""
        payload = {
            "commit_id": commit_id,
            "path": path,
            "line": line,
            "side": "RIGHT",
            "body": body,
        }
        self._request("POST", f"/repos/{owner}/{repo}/pulls/{number}/comments", payload=payload)

    def update_review_comment(self, owner: str, repo: str, comment_id: int, body: str) -> None:
        path = f"/repos/{owner}/{repo}/pulls/comments/{comment_id}"
        self._request("PATCH", path, payload={"body": body})

    def delete_review_comment(self, owner: str, repo: str, comment_id: int) -> None:
        self._request("DELETE", f"/repos/{owner}/{repo}/pulls/comments/{comment_id}")

    def _get_json(self, path: str, params: Mapping[str, object]):
        return self._request("GET", path, params=params)

    def _request(
        self,
        method: str,
        path: str,
        params: Mapping[str, object] | None = None,
        payload: object = None,
    ):
        url = f"{self._base}{path}"
        if params:
            url += f"?{urllib.parse.urlencode(params)}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": f"pr-guardian-action/{__version__}",
        }
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(  # noqa: S310 - https enforced in __init__
            url, data=data, headers=headers, method=method
        )
        attempts = ATTEMPTS if method == "GET" else 1
        status, headers_in, body = 0, {}, b""
        for attempt in range(1, attempts + 1):
            try:
                status, headers_in, body = self._opener(request)
            except OSError as exc:  # DNS failure, connection reset, timeout
                if attempt == attempts:
                    raise GitHubAPIError(f"network error talking to GitHub: {exc}") from exc
                self._sleep(2 ** (attempt - 1))
                continue
            if status >= 500 and attempt < attempts:
                self._sleep(2 ** (attempt - 1))  # 1s, 2s: GitHub 5xx are usually transient
                continue
            break

        if len(body) > MAX_BODY_BYTES:
            raise GitHubAPIError("GitHub response exceeded the size limit")
        if 200 <= status < 300:
            try:
                return json.loads(body) if body else None
            except ValueError as exc:
                raise GitHubAPIError("GitHub returned invalid JSON") from exc
        raise _error_for(status, headers_in, body)


def _parse_file(entry: object) -> ChangedFile:
    if not isinstance(entry, dict):
        raise GitHubAPIError("unexpected file entry shape")
    filename, status = entry.get("filename"), entry.get("status")
    if not isinstance(filename, str) or not isinstance(status, str):
        raise GitHubAPIError("file entry is missing filename or status")
    patch = entry.get("patch")
    previous = entry.get("previous_filename")
    return ChangedFile(
        filename=filename,
        status=status,
        patch=patch if isinstance(patch, str) else None,
        additions=int(entry.get("additions") or 0),
        deletions=int(entry.get("deletions") or 0),
        previous_filename=previous if isinstance(previous, str) else None,
    )


def _parse_comment(entry: object) -> ReviewComment | None:
    # Comments are only used to avoid re-posting, so a malformed one is skipped
    # rather than failing the run.
    if not isinstance(entry, dict):
        return None
    path, body, line = entry.get("path"), entry.get("body"), entry.get("line")
    user = entry.get("user") if isinstance(entry.get("user"), dict) else {}
    if not isinstance(path, str) or not isinstance(body, str):
        return None
    reply, comment_id = entry.get("in_reply_to_id"), entry.get("id")
    return ReviewComment(
        path=path,
        line=line if isinstance(line, int) and not isinstance(line, bool) else None,
        body=body,
        author_is_bot=user.get("type") == "Bot",
        id=comment_id if isinstance(comment_id, int) else 0,
        in_reply_to_id=reply if isinstance(reply, int) else None,
    )


def _parse_review(entry: object) -> Review | None:
    if not isinstance(entry, dict) or not isinstance(entry.get("id"), int):
        return None
    user = entry.get("user") if isinstance(entry.get("user"), dict) else {}
    body = entry.get("body")
    return Review(
        id=entry["id"],
        body=body if isinstance(body, str) else "",
        author_is_bot=user.get("type") == "Bot",
    )


def _error_for(status: int, headers: Mapping[str, str], body: bytes) -> GitHubAPIError:
    if status == 401:
        return GitHubAPIError("GitHub rejected the token (bad or expired)", status)
    if status in (403, 429) and headers.get("x-ratelimit-remaining") == "0":
        reset = headers.get("x-ratelimit-reset", "unknown")
        return GitHubAPIError(f"GitHub rate limit exhausted (resets at epoch {reset})", status)
    if status == 403:
        return GitHubAPIError(
            "GitHub denied access: reading needs 'pull-requests: read', posting needs "
            "'pull-requests: write'",
            status,
        )
    if status == 404:
        return GitHubAPIError(
            "pull request not found, or the token cannot see this repository", status
        )
    if 300 <= status < 400:
        return GitHubAPIError(
            "GitHub redirected the request; refusing to forward credentials "
            "(was the repository renamed or transferred?)",
            status,
        )
    if status == 422:
        return GitHubAPIError(
            "GitHub rejected the request as invalid (422), e.g. a comment on a line "
            "outside the diff or a stale commit",
            status,
        )
    try:
        detail = str(json.loads(body).get("message", ""))[:200]
    except (ValueError, AttributeError):
        detail = ""
    return GitHubAPIError(f"unexpected GitHub response {status}: {detail}".rstrip(": "), status)
