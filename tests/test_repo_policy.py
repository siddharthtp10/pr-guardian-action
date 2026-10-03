"""Repo rules enforced as tests.

WHY: security promises made in the README ("every Action is SHA-pinned", "no
pull_request_target") rot silently unless something fails when they break.
These tests are that something.
"""

import re
from pathlib import Path

import pytest
import yaml

from pr_guardian import config

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
SHA = re.compile(r"^[0-9a-f]{40}$")


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def uses_refs(doc: dict) -> list[str]:
    """Every `uses:` value from workflow jobs or composite action steps."""
    steps = []
    for job in doc.get("jobs", {}).values():
        steps += job.get("steps", [])
    steps += doc.get("runs", {}).get("steps", [])
    return [s["uses"] for s in steps if "uses" in s]


@pytest.mark.parametrize("path", [ROOT / "action.yml", *WORKFLOWS], ids=lambda p: p.name)
def test_every_external_action_is_pinned_to_a_full_sha(path):
    for ref in uses_refs(load(path)):
        if ref.startswith("./"):  # local action, same commit as the workflow
            continue
        assert SHA.match(ref.split("@", 1)[1]), f"{path.name}: {ref} is not SHA-pinned"


def test_pre_commit_revs_are_pinned_to_full_sha():
    doc = load(ROOT / ".pre-commit-config.yaml")
    for repo in doc["repos"]:
        assert SHA.match(repo["rev"]), f"{repo['repo']} rev is not a SHA"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflows_never_use_pull_request_target(path):
    doc = load(path)
    triggers = doc.get("on", doc.get(True))  # PyYAML parses bare `on:` as True
    names = triggers if isinstance(triggers, (list, dict)) else [triggers]
    assert "pull_request_target" not in names


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_permissions_are_minimal(path):
    doc = load(path)
    assert doc.get("permissions") is not None, "declare permissions; the default may be write-all"
    # Job-level blocks override the top-level one, so they need checking too.
    blocks = [doc["permissions"]] + [
        job["permissions"] for job in doc["jobs"].values() if "permissions" in job
    ]
    allowed = {("contents", "read"), ("pull-requests", "read"), ("pull-requests", "write")}
    for block in blocks:
        assert set(block.items()) <= allowed


def test_action_inputs_match_expected_set():
    inputs = set(load(ROOT / "action.yml")["inputs"])
    assert inputs == {
        "github-token",
        "anthropic-api-key",
        "model",
        "fail-on",
        "paths",
        "max-files",
        "dry-run",
    }


def test_every_input_is_wired_to_an_env_var_the_code_reads():
    action = load(ROOT / "action.yml")
    run_step = next(s for s in action["runs"]["steps"] if "run" in s)
    wired = {k for k in run_step["env"] if k.startswith("PRG_")}
    read_by_code = {
        config.ENV_GITHUB_TOKEN,
        config.ENV_API_KEY,
        config.ENV_MODEL,
        config.ENV_FAIL_ON,
        config.ENV_PATHS,
        config.ENV_MAX_FILES,
        config.ENV_DRY_RUN,
    }
    assert wired == read_by_code


def test_inputs_are_not_interpolated_into_the_shell_script():
    action = load(ROOT / "action.yml")
    for step in action["runs"]["steps"]:
        assert "${{" not in step.get("run", ""), "use env: instead of ${{ }} inside run:"


def test_action_defaults_match_code_defaults():
    inputs = load(ROOT / "action.yml")["inputs"]
    assert inputs["model"]["default"] == config.DEFAULT_MODEL
    assert inputs["fail-on"]["default"] == config.DEFAULT_FAIL_ON
    assert int(inputs["max-files"]["default"]) == config.DEFAULT_MAX_FILES


def test_anthropic_key_input_has_no_literal_default():
    assert load(ROOT / "action.yml")["inputs"]["anthropic-api-key"]["default"] == ""


def test_no_secret_shaped_strings_in_repo():
    pattern = re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}|gh[pousr]_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}")
    skip = {".git", ".venv", ".pytest_cache", ".ruff_cache", "__pycache__"}
    for path in ROOT.rglob("*"):
        if path.is_file() and not skip & set(path.parts) and path.suffix != ".pyc":
            assert not pattern.search(path.read_text(errors="ignore")), path
