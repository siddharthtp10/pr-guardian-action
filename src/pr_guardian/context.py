"""Work out *which* pull request we are reviewing and what we are allowed to do.

Everything here comes from variables GitHub sets on the runner, not from the
PR's content, so it is safe to trust after basic shape validation.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_API_URL = "https://api.github.com"
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA = re.compile(r"^[0-9a-f]{40}$")


class ContextError(ValueError):
    pass


@dataclass(frozen=True)
class PRContext:
    owner: str
    repo: str
    number: int
    head_sha: str
    is_fork: bool
    api_url: str
    # Runs triggered by Dependabot get a read-only token and no Actions secrets,
    # exactly like fork PRs, even though the branch lives in the same repository.
    actor_is_dependabot: bool = False

    @property
    def read_only(self) -> bool:
        return self.is_fork or self.actor_is_dependabot

    @property
    def can_post(self) -> bool:
        """A read-only token cannot write, so we must not even try: results are
        printed (annotations and the job summary need no write permission)."""
        return not self.read_only

    @property
    def read_only_reason(self) -> str:
        if self.is_fork:
            return "fork pull request"
        if self.actor_is_dependabot:
            return "Dependabot run"
        return ""


def load_context(env: Mapping[str, str]) -> PRContext:
    event = env.get("GITHUB_EVENT_NAME", "")
    if event == "pull_request_target":
        # Deliberate refusal. That event runs with the base repo's secrets and a
        # write token while the PR content is attacker-controlled; the standard
        # way it gets exploited is by checking out PR code. We never need it:
        # the plain `pull_request` event is enough because we only read the diff.
        raise ContextError(
            "pull_request_target is not supported (it exposes secrets to untrusted PRs). "
            "Use the 'pull_request' event."
        )
    if event != "pull_request":
        raise ContextError(f"pr-guardian only runs on 'pull_request' events; got {event!r}")

    repository = env.get("GITHUB_REPOSITORY", "")
    if not _REPO.match(repository):
        raise ContextError("GITHUB_REPOSITORY is missing or malformed")
    owner, repo = repository.split("/", 1)

    event_path = env.get("GITHUB_EVENT_PATH", "")
    try:
        with open(event_path, encoding="utf-8") as fh:
            payload = json.load(fh)
        pr = payload["pull_request"]
        number = int(pr["number"])
        head_sha = str(pr["head"]["sha"])
        base_name = pr["base"]["repo"]["full_name"]
        # head.repo is null when the fork was deleted - treat that as a fork.
        head_repo = (pr["head"].get("repo") or {}).get("full_name")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ContextError(f"could not read the pull_request event payload: {exc}") from exc

    if number < 1 or not _SHA.match(head_sha):
        raise ContextError("event payload has an invalid PR number or head SHA")

    api_url = (env.get("GITHUB_API_URL") or DEFAULT_API_URL).rstrip("/")
    if not api_url.startswith("https://"):
        # The token is sent to this URL; never over plain http.
        raise ContextError("GITHUB_API_URL must use https")

    return PRContext(
        owner=owner,
        repo=repo,
        number=number,
        head_sha=head_sha,
        is_fork=head_repo != base_name,
        api_url=api_url,
        # GITHUB_ACTOR (who triggered the run) decides the token's privileges,
        # which is why it is used rather than the PR author.
        actor_is_dependabot=env.get("GITHUB_ACTOR", "") == "dependabot[bot]",
    )
