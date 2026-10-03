"""scripts/open-demo-pr.sh, exercised against a local bare "origin" and a fake `gh`.

Nothing here touches GitHub: `gh` is a stub that records its arguments, and the
remote is a bare repository in a temp directory.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "open-demo-pr.sh"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}
FAKE_GH = """#!/usr/bin/env bash
echo "$@" >> "$GH_LOG"
case "$1 $2" in
  "pr create") echo "https://github.com/o/r/pull/99" ;;
esac
exit "${GH_EXIT:-0}"
"""


def git(cwd, *args):
    out = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **GIT_ENV},
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


@pytest.fixture
def sandbox(tmp_path):
    origin, work, bin_dir = tmp_path / "origin.git", tmp_path / "work", tmp_path / "bin"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(work))
    git(work, "switch", "-q", "-c", "main")
    for rel in ("examples", "scripts", ".github/workflows/pr-guardian.yml"):
        src, dst = ROOT / rel, work / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        (shutil.copytree if src.is_dir() else shutil.copy)(src, dst)
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "base")
    git(work, "push", "-q", "-u", "origin", "main")
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    log = tmp_path / "gh.log"
    log.write_text("")
    return {"work": work, "origin": origin, "log": log, "bin": bin_dir}


def run(sandbox, *args, gh_exit="0"):
    env = {
        **os.environ,
        **GIT_ENV,
        "PATH": f"{sandbox['bin']}{os.pathsep}{os.environ['PATH']}",
        "GH_LOG": str(sandbox["log"]),
        "GH_EXIT": gh_exit,
    }
    return subprocess.run(
        ["bash", str(sandbox["work"] / "scripts" / "open-demo-pr.sh"), *args],
        cwd=sandbox["work"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def remote_branches(sandbox):
    refs = git(sandbox["origin"], "for-each-ref", "--format=%(refname:short)", "refs/heads")
    return set(refs.split())


# --- behaviour ----------------------------------------------------------------------


def test_opens_a_draft_pr_from_a_pushed_scratch_branch_and_restores_your_branch(sandbox):
    work = sandbox["work"]
    before = git(work, "rev-parse", "HEAD")
    result = run(sandbox)
    assert result.returncode == 0, result.stderr
    assert "https://github.com/o/r/pull/99" in result.stdout

    (branch,) = [b for b in remote_branches(sandbox) if b.startswith("demo/pr-guardian-")]
    assert re.fullmatch(r"demo/pr-guardian-\d{8}-\d{6}", branch)
    assert f"--cleanup {branch}" in result.stdout

    # The scratch branch carries the demo files, including the one that must live under .github/.
    tree = git(sandbox["origin"], "ls-tree", "-r", "--name-only", branch).split()
    for path in (
        "demo-pr/terraform/main.tf",
        "demo-pr/kubernetes/deployment.yaml",
        "demo-pr/docker/Dockerfile",
        ".github/workflows/pr-guardian-demo-bad-workflow.yml",
    ):
        assert path in tree
    # ...and main on the remote is untouched, as is the user's local branch.
    assert "demo-pr/terraform/main.tf" not in git(
        sandbox["origin"], "ls-tree", "-r", "--name-only", "main"
    )
    assert git(work, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git(work, "rev-parse", "HEAD") == before
    assert git(work, "status", "--porcelain") == ""

    (call,) = [ln for ln in sandbox["log"].read_text().splitlines() if ln.startswith("pr create")]
    for flag in ("--draft", "--base main", f"--head {branch}", "do not merge"):
        assert flag in call


def test_dry_run_changes_nothing_and_needs_neither_network_nor_gh(sandbox):
    (sandbox["bin"] / "gh").unlink()
    result = run(sandbox, "--dry-run")
    assert result.returncode == 0 and "DRY RUN" in result.stdout
    assert "examples/terraform/main.tf -> demo-pr/terraform/main.tf" in result.stdout
    assert remote_branches(sandbox) == {"main"}
    assert git(sandbox["work"], "branch", "--list", "demo/*") == ""


def test_refuses_a_dirty_working_tree(sandbox):
    (sandbox["work"] / "scratch.txt").write_text("uncommitted")
    result = run(sandbox)
    assert result.returncode != 0 and "uncommitted changes" in result.stderr
    assert remote_branches(sandbox) == {"main"}


def test_refuses_when_the_base_branch_has_no_review_workflow(sandbox):
    work = sandbox["work"]
    git(work, "rm", "-q", ".github/workflows/pr-guardian.yml")
    git(work, "commit", "-q", "-m", "remove workflow")
    git(work, "push", "-q", "origin", "main")
    result = run(sandbox)
    assert result.returncode != 0 and "would not be reviewed" in result.stderr
    assert remote_branches(sandbox) == {"main"}


def test_failure_after_branching_still_returns_you_to_your_branch(sandbox):
    result = run(sandbox, gh_exit="1")  # gh fails when opening the PR
    assert result.returncode != 0
    assert git(sandbox["work"], "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_cleanup_closes_the_pr_and_deletes_the_branch(sandbox):
    run(sandbox)
    (branch,) = [b for b in remote_branches(sandbox) if b.startswith("demo/")]
    result = run(sandbox, "--cleanup", branch)
    assert result.returncode == 0
    assert f"pr close {branch} --delete-branch" in sandbox["log"].read_text()


def test_cleanup_falls_back_to_deleting_the_scratch_branch_if_no_pr_exists(sandbox):
    run(sandbox)
    (branch,) = [b for b in remote_branches(sandbox) if b.startswith("demo/")]
    result = run(sandbox, "--cleanup", branch, gh_exit="1")
    assert result.returncode == 0
    assert remote_branches(sandbox) == {"main"}


@pytest.mark.parametrize(
    "bad", ["main", "demo/other", "demo/pr-guardian-1", "feature/x", "--force", ""]
)
def test_cleanup_refuses_anything_that_is_not_a_demo_branch(sandbox, bad):
    result = run(sandbox, "--cleanup", bad)
    assert result.returncode != 0
    assert remote_branches(sandbox) == {"main"}
    assert "pr close" not in sandbox["log"].read_text()


def test_help_and_unknown_arguments(sandbox):
    assert "Usage" in run(sandbox, "--help").stdout
    bad = run(sandbox, "--nope")
    assert bad.returncode != 0 and "unknown argument" in bad.stderr


# --- static safety properties of the script itself ----------------------------------


def script_code():
    """The script without comments (whole-line and trailing), so that a comment that
    *warns* about `git add -A` is not mistaken for code that runs it."""
    lines = [ln for ln in SCRIPT.read_text().splitlines() if not ln.lstrip().startswith("#")]
    return "\n".join(re.sub(r"\s+#\s.*$", "", ln) for ln in lines)


def test_script_is_valid_bash():
    assert subprocess.run(["bash", "-n", str(SCRIPT)], check=False).returncode == 0


def test_script_cannot_force_push_touch_settings_or_secrets():
    code = script_code()
    forbidden = [
        r"--force",
        r"push\s+(-\w*f|\S*\s+\+)",
        r"\bgh\s+secret\b",
        r"\bgh\s+repo\s+(edit|delete)\b",
        r"\bgh\s+api\b",
        r"\bgh\s+workflow\b",
        r"\bgh\s+pr\s+merge\b",
        r"\bgit\s+add\s+(-A|\.)",
        r"\breset\s+--hard\b",
        r"\brm\s+-rf\b",
        r"ANTHROPIC_API_KEY=",
    ]
    for pattern in forbidden:
        assert not re.search(pattern, code), pattern


def test_script_always_uses_strict_mode_and_creates_a_draft():
    code = script_code()
    assert "set -euo pipefail" in code and "--draft" in code
