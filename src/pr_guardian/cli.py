"""Entry point: read env -> validate -> fetch the PR diff -> report what will be reviewed."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from typing import TextIO

from pr_guardian import __version__
from pr_guardian.config import Config, ConfigError
from pr_guardian.context import ContextError, PRContext, load_context
from pr_guardian.github_api import GitHubAPIError, GitHubClient
from pr_guardian.report import render_selection
from pr_guardian.selection import select_files

EXIT_OK = 0
# 2 = "you configured me wrong", distinct from 1 = "I found problems in the PR"
# (used from Stage 4). Distinct codes make failures debuggable from the log.
EXIT_CONFIG = 2
EXIT_API = 3  # GitHub unreachable / token rejected: infrastructure, not the PR's fault


def escape_workflow_data(text: str) -> str:
    """Escape text for a GitHub ``::error::`` workflow command.

    WHY: the runner parses stdout lines beginning with ``::``. A value from a
    workflow input containing a newline followed by ``::set-env`` or similar
    could forge commands (log/command injection). GitHub's documented escaping
    for command data is %, CR, LF.
    """
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    out: TextIO | None = None,
    client_factory: Callable[[Config, PRContext], GitHubClient] | None = None,
) -> int:
    del argv  # No CLI flags: the Action's inputs are the only interface.
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out

    try:
        config = Config.from_env(env)
    except ConfigError as exc:
        for problem in exc.problems:
            print(
                f"::error title=Invalid pr-guardian input::{escape_workflow_data(problem)}",
                file=out,
            )
        return EXIT_CONFIG

    # Print only non-secret facts. The key's *presence* is useful for debugging;
    # its value never is.
    print(f"pr-guardian {__version__}", file=out)
    print(f"  mode:      {config.mode}", file=out)
    print(f"  fail-on:   {config.fail_on}", file=out)
    print(f"  max-files: {config.max_files}", file=out)
    print(f"  dry-run:   {str(config.dry_run).lower()}", file=out)
    print(f"  paths:     {', '.join(config.paths) or '(all supported file types)'}", file=out)
    if config.ai_enabled:
        print(f"  ai model:  {config.model}", file=out)
    else:
        print("  AI layer disabled: no anthropic-api-key provided (rules-only mode).", file=out)

    try:
        ctx = load_context(env)
    except ContextError as exc:
        print(f"::error title=Unsupported event::{escape_workflow_data(str(exc))}", file=out)
        return EXIT_CONFIG

    if ctx.is_fork:
        # Say it in the output, not just in the docs: people debugging "why no
        # comments on this PR?" read the log first.
        print(
            "NOTICE: fork pull request. GitHub gives fork PRs a read-only token and no "
            "secrets, so this run is rules-only and results are printed, not posted.",
            file=out,
        )

    factory = client_factory or (lambda c, x: GitHubClient(c.github_token, x.api_url))
    try:
        pulled = factory(config, ctx).list_pull_files(ctx.owner, ctx.repo, ctx.number)
    except GitHubAPIError as exc:
        message = escape_workflow_data(str(exc))
        print(f"::error title=GitHub API error::{message}", file=out)
        return EXIT_API

    selection = select_files(pulled.files, config, api_truncated=pulled.truncated)
    print(render_selection(selection), file=out)
    print("Stage 2: diff handling complete; rules engine arrives in Stage 3.", file=out)
    return EXIT_OK
