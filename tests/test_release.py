"""Release hygiene: versions agree, the changelog is well formed, and the release runbook
cannot silently contain a typo that would, e.g., force-push a branch."""

import re
import subprocess
import tomllib
from pathlib import Path

import yaml

from pr_guardian import __version__

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = (ROOT / "CHANGELOG.md").read_text()
RELEASING = (ROOT / "docs" / "RELEASING.md").read_text()
README = (ROOT / "README.md").read_text()
SEMVER = r"\d+\.\d+\.\d+"


def released_versions():
    return re.findall(rf"^## \[({SEMVER})\] - (\d{{4}}-\d{{2}}-\d{{2}})$", CHANGELOG, flags=re.M)


# --- versions ------------------------------------------------------------------------


def test_package_version_pyproject_and_changelog_agree():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    newest = released_versions()[0][0]
    assert __version__ == pyproject == newest


def test_changelog_has_unreleased_section_and_newest_first_ordering():
    assert re.search(r"^## \[Unreleased\]$", CHANGELOG, flags=re.M)
    versions = [tuple(map(int, v.split("."))) for v, _ in released_versions()]
    assert versions == sorted(versions, reverse=True) and len(set(versions)) == len(versions)


def test_every_release_has_a_link_and_unreleased_compares_from_the_newest():
    for version, _ in released_versions():
        assert re.search(
            rf"^\[{re.escape(version)}\]: https://github\.com/\S+/tag/v{re.escape(version)}$",
            CHANGELOG,
            flags=re.M,
        ), version
    newest = released_versions()[0][0]
    assert f"/compare/v{newest}...HEAD" in CHANGELOG


def test_readme_and_runbook_use_the_current_major_tag():
    major = __version__.split(".")[0]
    assert f"pr-guardian-action@v{major}" in README
    assert f"v{major}" in RELEASING and f"refs/tags/v{major}" in RELEASING


def test_project_urls_point_at_this_repository():
    urls = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["urls"]
    assert all("github.com/siddharthtp10/pr-guardian-action" in u for u in urls.values())


def test_action_metadata_is_publishable():
    action = yaml.safe_load((ROOT / "action.yml").read_text())
    assert action["name"].strip() and action["description"].strip()
    assert action["branding"]["icon"] and action["branding"]["color"]
    assert all(spec.get("description") for spec in action["inputs"].values())
    assert all(spec.get("description") for spec in action["outputs"].values())


# --- release notes extraction (the command in the runbook) ---------------------------------


def test_the_runbooks_release_notes_command_extracts_the_right_section():
    script = "awk '/^## \\[1\\.0\\.0\\]/{f=1;next} /^## \\[/{f=0} f' CHANGELOG.md"
    out = subprocess.run(["bash", "-c", script], cwd=ROOT, capture_output=True, text=True).stdout
    assert "### Added" in out and "### Security" in out
    assert "## [" not in out  # stops at the next version heading
    assert "[Unreleased]" not in out.split("### Known limitations")[0]
    assert script in RELEASING  # the doc really contains this exact command


# --- the runbook's metadata block --------------------------------------------------------------


def metadata():
    block = re.search(r"```text\n(description: .*?\ntopics: .*?)\n```", RELEASING, flags=re.S)
    assert block, "metadata block missing"
    values = dict(line.split(": ", 1) for line in block[1].splitlines())
    return values["description"], values["topics"].split(",")


def test_description_and_topics_satisfy_github_limits():
    description, topics = metadata()
    assert 0 < len(description) <= 350
    assert len(topics) <= 20 and len(set(topics)) == len(topics)
    assert all(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,49}", t) for t in topics)


def test_the_gh_repo_edit_command_uses_exactly_the_documented_metadata():
    description, topics = metadata()
    assert f'--description "{description}"' in RELEASING
    used = re.findall(r"--add-topic ([a-z0-9-]+)", RELEASING)
    assert used == topics


def test_description_is_honest_about_what_the_project_is():
    description, _ = metadata()
    assert "advisory" in description and "Terraform" in description


# --- safety properties of the runbook's commands -------------------------------------------------


def commands():
    bash_blocks = re.findall(r"```bash\n(.*?)```", RELEASING, flags=re.S)
    return [
        ln.strip()
        for block in bash_blocks
        for ln in block.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def test_the_only_force_push_moves_the_floating_major_tag():
    major = __version__.split(".")[0]
    forced = [c for c in commands() if re.search(r"push\s+(--force|-f)\b|--force", c)]
    assert forced, "the floating tag move should be documented"
    assert all(
        c == f"git push --force origin refs/tags/v{major}" or "refs/tags/v" in c for c in forced
    )
    prose = RELEASING.replace("```", "")
    assert not re.search(r"git push (-f|--force)\s+origin\s+(main|HEAD|\+)", prose)


def test_no_command_pushes_branches_or_all_tags():
    for c in commands():
        if c.startswith("git push"):
            assert "--all" not in c and "--tags" not in c and "--mirror" not in c, c
            assert (
                re.fullmatch(
                    r"git push (--force )?origin (refs/tags/v\d+|v\d+\.\d+\.\d+(-rc\.\d+)?)", c
                )
                or c == "git push"
            ), c


def test_gh_api_endpoints_are_the_ones_that_were_verified():
    verified = {
        "private-vulnerability-reporting",
        "vulnerability-alerts",
        "automated-security-fixes",
        "actions/permissions/workflow",
        "actions/permissions/fork-pr-contributor-approval",
    }
    found = re.findall(r'gh api -X PUT "repos/\$REPO/([a-z/-]+)"', RELEASING)
    assert set(found) == verified


def test_the_runbook_never_puts_a_secret_value_anywhere():
    for c in commands():
        if "gh secret" in c:
            assert c.endswith('--repo "$REPO"') and "=" not in c and "--body" not in c, c


def test_security_policy_matches_the_enabled_reporting_channel():
    policy = (ROOT / "SECURITY.md").read_text()
    assert "private vulnerability reporting" in policy.lower()
    assert (
        "private-vulnerability-reporting" in RELEASING
    )  # the runbook enables what the policy promises
    assert "do not open a public issue" in policy.lower()
