"""Entry point: read env -> validate -> fetch the PR diff -> run rules -> report and post."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from typing import TextIO

from pr_guardian import __version__
from pr_guardian.config import Config, ConfigError
from pr_guardian.context import ContextError, PRContext, load_context
from pr_guardian.engine import evaluate, meets_threshold
from pr_guardian.github_api import GitHubAPIError, GitHubClient
from pr_guardian.publish import post_review, render_annotations, render_summary
from pr_guardian.report import render_findings, render_selection
from pr_guardian.rules import RuleError, load_rules
from pr_guardian.selection import select_files

EXIT_OK = 0
EXIT_FINDINGS = 1  # the PR has findings at or above fail-on: the check goes red
# 2 = "you configured me wrong", distinct from 1 = "I found problems in the PR".
# Distinct codes make failures debuggable from the log.
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
    client = factory(config, ctx)
    try:
        pulled = client.list_pull_files(ctx.owner, ctx.repo, ctx.number)
    except GitHubAPIError as exc:
        message = escape_workflow_data(str(exc))
        print(f"::error title=GitHub API error::{message}", file=out)
        return EXIT_API

    selection = select_files(pulled.files, config, api_truncated=pulled.truncated)
    print(render_selection(selection), file=out)

    try:
        rules = load_rules()
    except RuleError as exc:
        for problem in exc.problems:
            print(f"::error title=Invalid policy::{escape_workflow_data(problem)}", file=out)
        return EXIT_CONFIG
    findings = evaluate(selection.targets, rules)
    print(f"Rules loaded: {len(rules)}", file=out)
    print(render_findings(findings), file=out)
    for line in render_annotations(findings, config.fail_on):
        print(line, file=out)
    _write_summary(env, render_summary(findings, selection, config.fail_on, config.mode), out)

    failed = any(meets_threshold(f.severity, config.fail_on) for f in findings)
    exit_code = EXIT_FINDINGS if failed else EXIT_OK
    # Step outputs let a later workflow step react (e.g. label the PR) without
    # parsing logs. Set before posting so they exist even if posting fails.
    _write_outputs(
        env, {"conclusion": "failure" if failed else "success", "findings-count": len(findings)}
    )

    if config.dry_run:
        print("Review: dry-run, not posting.", file=out)
    elif not ctx.can_post:
        print("Review: fork PR, not posting (read-only token). See the annotations.", file=out)
    else:
        try:
            print(post_review(client, ctx, findings, selection, config.fail_on), file=out)
        except GitHubAPIError as exc:
            # Annotations and the summary are already out, so the findings are
            # not lost. Still fail: a review that silently never appears looks
            # exactly like a clean PR.
            message = escape_workflow_data(str(exc))
            print(f"::error title=Could not post review::{message}", file=out)
            return exit_code or EXIT_API
    return exit_code


def _write_outputs(env: Mapping[str, str], outputs: Mapping[str, str | int]) -> None:
    """Append `name=value` lines to $GITHUB_OUTPUT. Values are only ever numbers
    and fixed words; the newline check is defence in depth against output injection."""
    path = env.get("GITHUB_OUTPUT", "")
    if not path:
        return
    lines = []
    for name, value in outputs.items():
        text = str(value)
        if "\n" in text or "\r" in text:
            raise ValueError("output values must be single-line")
        lines.append(f"{name}={text}\n")
    with open(path, "a", encoding="utf-8") as fh:
        fh.writelines(lines)


def _write_summary(env: Mapping[str, str], markdown: str, out: TextIO) -> None:
    """Append to the job summary. Missing outside Actions; never fatal."""
    path = env.get("GITHUB_STEP_SUMMARY", "")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(markdown)
    except OSError as exc:
        print(
            f"::warning title=Job summary not written::{escape_workflow_data(str(exc))}", file=out
        )
