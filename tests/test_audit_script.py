"""scripts/audit-public.sh, run against throwaway repositories.

Secret-shaped test data is assembled at run time so this file itself is clean
(GitHub push protection, and the repo-wide scan, would flag literals).
"""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "audit-public.sh"

AWS_KEY = "AKIA" + "QWERTYUIOPASDFGH"
GH_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
PEM = "-----BEGIN RSA " + "PRIVATE KEY-----"
PERSONAL = "someone" + "@" + "personal-mail.io"  # a "real-looking" address, built at run time
EMPLOYER = "Globex" + "Corp"  # a made-up name standing in for "an employer's name"


def git(repo, *args, **env):
    full = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Dev",
        "GIT_AUTHOR_EMAIL": "dev@users.noreply.github.com",
        "GIT_COMMITTER_NAME": "Dev",
        "GIT_COMMITTER_EMAIL": "dev@users.noreply.github.com",
        **env,
    }
    return subprocess.run(
        ["git", *args], cwd=repo, env=full, check=True, capture_output=True, text=True
    ).stdout


def commit(repo, files, message="change", **env):
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message, **env)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    commit(tmp_path, {"README.md": "# demo\n", "src/app.py": "print('hi')\n"}, "init")
    return tmp_path


def audit(repo, *args, terms=None):
    env = {**os.environ}
    env.pop("AUDIT_EXTRA_TERMS", None)
    if terms is not None:
        env["AUDIT_EXTRA_TERMS"] = terms
    # The timeout turns a regex hang (see the minified-file test) into a fast failure.
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_clean_repository_passes(repo):
    result = audit(repo)
    assert result.returncode == 0, result.stdout
    assert "0 blocking" in result.stdout and "Nothing blocking" in result.stdout


@pytest.mark.parametrize(
    ("secret", "name"),
    [
        (AWS_KEY, "aws-access-key"),
        (GH_TOKEN, "github-token"),
        (PEM, "private-key"),
        ("sk-" + "ant-api03-AbCdEfGhIjKlMn", "anthropic-key"),
    ],
)
def test_secret_in_the_working_tree_blocks_and_is_never_printed(repo, secret, name):
    commit(repo, {"config.txt": f"value = {secret}\n"})
    result = audit(repo)
    assert result.returncode == 1
    assert f"{name} in tracked file at config.txt:1" in result.stdout
    assert secret not in result.stdout + result.stderr


def test_a_secret_deleted_in_a_later_commit_is_still_found_in_history(repo):
    commit(repo, {"leak.txt": f"token = {GH_TOKEN}\n"}, "oops")
    commit(repo, {"leak.txt": "token = removed\n"}, "remove it")
    result = audit(repo)
    assert result.returncode == 1
    assert "github-token appears in the history, commit" in result.stdout
    assert "in tracked file at" not in result.stdout  # gone from HEAD, but not from history
    assert GH_TOKEN not in result.stdout


def test_aws_documentation_examples_are_not_findings(repo):
    commit(
        repo,
        {
            "docs.md": "key AKIAIOSFODNN7EXAMPLE and arn:aws:iam::123456789012:role/x\n",
        },
    )
    assert audit(repo).returncode == 0


def test_an_arn_with_a_real_looking_account_id_blocks(repo):
    # Assembled at run time: a literal ARN here would (rightly) fail the audit of this repo.
    arn = "arn:aws:iam::" + "210987654321" + ":role/deploy"
    commit(repo, {"policy.tf": f'arn = "{arn}"\n'})
    result = audit(repo)
    assert result.returncode == 1 and "aws-arn-with-account-id" in result.stdout


def test_sensitive_files_block_even_after_deletion_but_examples_do_not(repo):
    commit(repo, {".env": "A=1\n", ".env.example": "A=\n", "deploy.pem": "x\n"})
    git(repo, "rm", "-q", ".env", "deploy.pem")
    git(repo, "commit", "-q", "-m", "remove")
    result = audit(repo)
    assert result.returncode == 1
    assert "sensitive and is in the history: .env" in result.stdout
    assert "deploy.pem" in result.stdout
    assert ".env.example" not in result.stdout


def test_extra_terms_are_unset_by_default_and_flagged_when_not_checked(repo):
    result = audit(repo)
    assert "AUDIT_EXTRA_TERMS is not set" in result.stdout and "REVIEW" in result.stdout


@pytest.mark.parametrize("where", ["file", "message", "author", "filename"])
def test_extra_terms_are_found_everywhere_and_never_echoed(repo, where):
    if where == "file":
        commit(repo, {"notes.md": f"built at {EMPLOYER.lower()} last year\n"})
    elif where == "message":
        commit(repo, {"a.txt": "x\n"}, f"port from the {EMPLOYER} tool")
    elif where == "author":
        commit(repo, {"a.txt": "x\n"}, GIT_AUTHOR_NAME=f"{EMPLOYER} Bot")
    else:
        commit(repo, {f"{EMPLOYER}-config.yaml": "a: 1\n"})
    result = audit(repo, terms=f"somethingelse, {EMPLOYER}")
    assert result.returncode == 1, result.stdout
    assert "configured term appears" in result.stdout
    assert (
        EMPLOYER.lower() not in (result.stdout + result.stderr).lower()
    )  # the term is never printed


def test_extra_terms_that_do_not_appear_pass(repo):
    result = audit(repo, terms="Initech, Hooli")
    assert result.returncode == 0 and "Nothing blocking" in result.stdout


def test_personal_home_paths_block_but_ci_runner_paths_do_not(repo):
    commit(repo, {"a.md": "see /Users/jane/work/x\n", "b.md": "/home/runner/work/x\n"})
    result = audit(repo)
    assert result.returncode == 1
    assert "absolute home path at a.md:1" in result.stdout
    assert "b.md" not in result.stdout


def test_real_commit_emails_are_a_review_item_not_a_failure(repo):
    commit(
        repo,
        {"a.txt": "x\n"},
        GIT_AUTHOR_EMAIL=PERSONAL,
        GIT_COMMITTER_EMAIL=PERSONAL,
    )
    result = audit(repo)
    assert result.returncode == 0
    assert "real e-mail address in history" in result.stdout
    assert audit(repo, "--strict").returncode == 1  # --strict turns REVIEW into failure


def test_noreply_identities_are_not_flagged(repo):
    commit(repo, {"a.txt": "x\n"}, GIT_AUTHOR_EMAIL="noreply@anthropic.com")
    assert "real e-mail address" not in audit(repo).stdout


def test_attribution_trailers_are_counted_for_review(repo):
    commit(
        repo,
        {"a.txt": "x\n"},
        "feat\n\nCo-Authored-By: Some Tool <noreply@example.com>\nClaude-Session: https://x.invalid/s",
    )
    result = audit(repo)
    assert result.returncode == 0 and "2 trailer line(s)" in result.stdout


def test_emails_in_files_are_reviewed_but_url_credentials_and_pins_are_not(repo):
    commit(
        repo,
        {
            "a.md": "contact " + "jane.doe" + "@" + "corp-mail.io" + "\n",
            "b.py": 'dsn = "postgres://admin:pw@db.host/app"\nuses = "org/x@v1"\n',
        },
    )
    out = audit(repo).stdout
    assert "e-mail-like text at a.md:1" in out
    assert "b.py" not in out


def test_internal_hostnames_outside_tests_are_reviewed(repo):
    commit(repo, {"src/cfg.py": 'HOST = "db.corp"\n', "tests/t.py": 'HOST = "db.corp"\n'})
    out = audit(repo).stdout
    assert "internal-looking host or IP at src/cfg.py:1" in out
    assert "tests/t.py" not in out


def test_large_files_and_todo_markers_are_reviewed(repo):
    commit(repo, {"big.bin": "x" * 1_000_001, "src/m.py": "# TODO: finish\n"})
    out = audit(repo).stdout
    assert "file over 1 MB: big.bin" in out and "unfinished-work marker at src/m.py:1" in out


def test_usage_errors(repo, tmp_path_factory):
    assert audit(repo, "--bogus").returncode == 2
    outside = tmp_path_factory.mktemp("not-a-repo")
    result = audit(outside)
    assert result.returncode == 2 and "not inside a git repository" in result.stderr
    assert (
        "Usage"
        in subprocess.run(
            ["bash", str(SCRIPT), "--help"], capture_output=True, text=True, check=False
        ).stdout
    )


def test_the_script_is_read_only_and_offline():
    code = "\n".join(
        ln for ln in SCRIPT.read_text().splitlines() if not ln.lstrip().startswith("#")
    )
    import re

    verbs = "push|commit|add|rm|reset|rebase|filter|config|tag|checkout|switch|merge|stash|gc|prune"
    assert not re.search(rf"\bgit\s+({verbs})\b", code)
    assert not re.search(r"\b(curl|wget|nc|ssh|scp)\b|\bgh\s", code)
    assert "set -euo pipefail" in code


def test_this_repository_has_nothing_blocking():
    """The real audit, on the real history, as part of the test-suite."""
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    result = audit(ROOT)
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize("shape", ["x", "a.", "1", "-"])
def test_a_huge_single_line_file_cannot_hang_the_audit(repo, shape):
    """Regression: an unbounded `[a-z]+@` e-mail pattern was quadratic on long lines, so a
    minified 1 MB bundle made the audit run for minutes. Each shape stresses a different
    character class used by the script's patterns."""
    commit(repo, {"dist/bundle.min.js": shape * (1_100_000 // len(shape))})
    result = audit(repo)  # audit() has a 60 s hard timeout; this normally takes well under 2 s
    assert result.returncode == 0, result.stdout


def test_reserved_test_domains_are_not_flagged_as_real_addresses(repo):
    commit(repo, {"a.md": "mail dev@host.example, qa@lab.test, ops@x.invalid, me@example.com\n"})
    assert "e-mail-like text" not in audit(repo).stdout
