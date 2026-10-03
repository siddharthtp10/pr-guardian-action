"""Minimal GitHub REST client (standard library only).

WHY not `requests`/PyGithub: zero runtime dependencies keeps the supply-chain
surface (and the Action's start-up time) at nothing. We need exactly one GET
endpoint in this stage.

Security properties, each covered by a test:
- Redirects are refused. Python's urllib would happily forward the
  Authorization header to wherever a 3xx points.
- We build every URL ourselves and never follow ``Link`` headers from the
  response, so the token can only be sent to the configured API host.
- Error messages never include the token.
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

    def _get_json(self, path: str, params: Mapping[str, object]):
        url = f"{self._base}{path}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(  # noqa: S310 - https enforced in __init__
            url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": f"pr-guardian-action/{__version__}",
            },
        )
        status, headers, body = 0, {}, b""
        for attempt in range(1, ATTEMPTS + 1):
            try:
                status, headers, body = self._opener(request)
            except OSError as exc:  # DNS failure, connection reset, timeout
                if attempt == ATTEMPTS:
                    raise GitHubAPIError(f"network error talking to GitHub: {exc}") from exc
                self._sleep(2 ** (attempt - 1))
                continue
            if status >= 500 and attempt < ATTEMPTS:
                self._sleep(2 ** (attempt - 1))  # 1s, 2s: GitHub 5xx are usually transient
                continue
            break

        if len(body) > MAX_BODY_BYTES:
            raise GitHubAPIError("GitHub response exceeded the size limit")
        if status == 200:
            try:
                return json.loads(body)
            except ValueError as exc:
                raise GitHubAPIError("GitHub returned invalid JSON") from exc
        raise _error_for(status, headers, body)


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


def _error_for(status: int, headers: Mapping[str, str], body: bytes) -> GitHubAPIError:
    if status == 401:
        return GitHubAPIError("GitHub rejected the token (bad or expired)", status)
    if status in (403, 429) and headers.get("x-ratelimit-remaining") == "0":
        reset = headers.get("x-ratelimit-reset", "unknown")
        return GitHubAPIError(f"GitHub rate limit exhausted (resets at epoch {reset})", status)
    if status == 403:
        return GitHubAPIError(
            "GitHub denied access: the token needs at least 'pull-requests: read'", status
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
    try:
        detail = str(json.loads(body).get("message", ""))[:200]
    except (ValueError, AttributeError):
        detail = ""
    return GitHubAPIError(f"unexpected GitHub response {status}: {detail}".rstrip(": "), status)
