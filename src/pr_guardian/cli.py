"""Entry point: read env -> validate -> report. (Stage 1: no review yet.)"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import TextIO

from pr_guardian import __version__
from pr_guardian.config import Config, ConfigError

EXIT_OK = 0
# 2 = "you configured me wrong", distinct from 1 = "I found problems in the PR"
# (used from Stage 4). Distinct codes make failures debuggable from the log.
EXIT_CONFIG = 2


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
    print("Stage 1 skeleton: configuration validated; no review performed yet.", file=out)
    return EXIT_OK
