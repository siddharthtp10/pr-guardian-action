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
    run_step = next(s for s in action["runs"]["steps"] if s.get("name") == "Run PR Guardian")
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
    pattern = re.compile(
        r"sk-ant-[A-Za-z0-9_-]{10,}|gh[pousr]_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}"
        r"|-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"
    )
    skip = {".git", ".venv", ".pytest_cache", ".ruff_cache", "__pycache__"}
    for path in ROOT.rglob("*"):
        if path.is_file() and not skip & set(path.parts) and path.suffix != ".pyc":
            for match in pattern.finditer(path.read_text(errors="ignore")):
                # AWS's documented example key is allowed (SEC-001 ignores it too).
                assert "EXAMPLE" in match[0], f"{path}: secret-shaped string"


def _pins(path):
    """name==version lines of a requirements file (ignores comments and --hash lines)."""
    lines = [ln.strip().rstrip("\\").strip() for ln in path.read_text().splitlines()]
    return sorted(ln for ln in lines if re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][0-9A-Za-z.]*", ln))


def test_declared_dependencies_match_the_inputs_the_locks_are_built_from():
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert sorted(project["dependencies"]) == _pins(ROOT / "requirements.in")
    assert sorted(project["optional-dependencies"]["ai"]) == _pins(ROOT / "requirements-ai.in")


def test_lock_files_pin_every_package_with_hashes():
    for name in ("requirements.txt", "requirements-ai.txt"):
        text = (ROOT / name).read_text()
        packages = _pins(ROOT / name)
        assert packages, name
        # One --hash per pinned package at least; pip --require-hashes needs it for ALL.
        assert text.count("--hash=sha256:") >= len(packages), name
        assert "git+" not in text and "http://" not in text and "https://" not in text, name


def test_locks_contain_the_declared_top_level_packages():
    assert "pyyaml==6.0.3" in _pins(ROOT / "requirements.txt")
    assert "anthropic==1.11.0" in _pins(ROOT / "requirements-ai.txt")


def test_dev_requirements_repeat_the_runtime_pins_exactly():
    dev = _pins(ROOT / "requirements-dev.txt")
    for pin in _pins(ROOT / "requirements.in") + _pins(ROOT / "requirements-ai.in"):
        assert pin in dev, f"{pin} missing from requirements-dev.txt"


def test_install_step_is_hash_checked_and_never_sees_the_api_key():
    action = load(ROOT / "action.yml")
    step = next(
        s for s in action["runs"]["steps"] if s.get("name") == "Install runtime dependencies"
    )
    assert "--require-hashes" in step["run"]
    assert "${{" not in step["run"]
    # The step may learn WHETHER a key was given, never the key itself.
    assert set(step["env"]) == {"ACTION_PATH", "WITH_AI"}
    assert "inputs.anthropic-api-key" in step["env"]["WITH_AI"]
    assert "!= ''" in step["env"]["WITH_AI"]
    assert "requirements-ai.txt" in step["run"]


def test_only_the_run_step_receives_the_api_key():
    action = load(ROOT / "action.yml")
    holders = [
        s.get("name")
        for s in action["runs"]["steps"]
        if "${{ inputs.anthropic-api-key }}" in str(s.get("env", {})) + str(s.get("with", {}))
    ]
    assert holders == ["Run PR Guardian"]


def test_repo_passes_its_own_review():
    """Dogfood: real files from this repo must not trigger any rule.

    (tests/fixtures and examples/ are excluded - they are intentionally bad.) Doubles as a
    false-positive check on real-world YAML.
    """
    from pr_guardian.engine import evaluate
    from pr_guardian.filetypes import classify
    from pr_guardian.rules import load_rules
    from pr_guardian.selection import ReviewTarget
    from tests.test_engine import added_patch

    targets = []
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT).as_posix()
        if not path.is_file() or rel.startswith(
            ("tests/fixtures/", "examples/", ".git/", ".venv/")
        ):
            continue
        kind = classify(rel)
        if kind:
            targets.append(ReviewTarget(rel, kind, "added", added_patch(path.read_text())))
    assert targets, "expected at least the workflow, action.yml and the policy file"
    assert evaluate(targets, load_rules()) == []


def test_action_declares_outputs_wired_to_the_run_step():
    action = load(ROOT / "action.yml")
    step = next(s for s in action["runs"]["steps"] if s.get("name") == "Run PR Guardian")
    assert step["id"] == "guardian"
    for name, spec in action["outputs"].items():
        assert spec["value"] == "${{ steps.guardian.outputs." + name + " }}"


def _guardian_paths(workflow: str, job: str) -> set[str]:
    steps = load(ROOT / ".github" / "workflows" / workflow)["jobs"][job]["steps"]
    step = next(s for s in steps if s.get("uses") == "./")
    return {p.strip() for p in step["with"]["paths"].splitlines() if p.strip()}


def test_self_review_excludes_every_intentionally_bad_directory_in_both_workflows():
    """Regression: Stage 6 added examples/ but only one of the two workflows excluded it,
    so the PR that introduced it went red on its own demo inputs."""
    bad_dirs = {"!tests/fixtures/**", "!examples/**"}
    assert _guardian_paths("ci.yml", "action-smoke") == bad_dirs
    assert _guardian_paths("pr-guardian.yml", "review") == bad_dirs
