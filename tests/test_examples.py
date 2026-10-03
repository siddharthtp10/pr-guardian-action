"""The demo inputs must keep doing what the docs say, and must stay harmless."""

import re
from pathlib import Path

import pytest
import yaml

from pr_guardian.engine import evaluate
from pr_guardian.filetypes import classify
from pr_guardian.rules import load_rules
from pr_guardian.selection import ReviewTarget
from tests.test_engine import added_patch

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
RULES = load_rules()


def manifest():
    rows = []
    for line in (EXAMPLES / "manifest.tsv").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            source, dest, expected = line.split("\t")
            rows.append((source, dest, sorted(expected.split(","))))
    return rows


ROWS = manifest()


def findings_for(source, dest):
    text = (EXAMPLES / source).read_text()
    return evaluate([ReviewTarget(dest, classify(dest), "added", added_patch(text))], RULES)


@pytest.mark.parametrize(("source", "dest", "expected"), ROWS, ids=[r[0] for r in ROWS])
def test_each_example_trips_exactly_the_documented_rules(source, dest, expected):
    assert classify(dest) is not None, f"{dest} would not be reviewed at all"
    assert sorted({f.rule_id for f in findings_for(source, dest)}) == expected


def test_examples_cover_every_rule_that_can_be_shown_without_a_secret_shaped_literal():
    shown = {rule for _, _, expected in ROWS for rule in expected}
    # SEC-001/002 need AWS-key / private-key shaped text. Committing it would trip
    # GitHub push protection; those rules are tested with strings assembled at runtime.
    assert {r.id for r in RULES} - shown == {"SEC-001", "SEC-002"}


def test_terraform_egress_is_not_flagged_as_an_ingress_problem():
    hits = [
        f
        for f in findings_for("terraform/main.tf", "demo-pr/terraform/main.tf")
        if f.rule_id == "TF-001"
    ]
    assert len(hits) == 1  # the file has an ingress AND an egress block open to 0.0.0.0/0
    lines = (EXAMPLES / "terraform/main.tf").read_text().splitlines()
    opener = next(ln for ln in reversed(lines[: hits[0].line]) if ln.rstrip().endswith("{"))
    assert opener.strip() == "ingress {"


def test_every_example_announces_that_it_is_intentionally_insecure():
    for source, _, _ in ROWS:
        assert "INTENTIONALLY INSECURE" in (EXAMPLES / source).read_text().splitlines()[0], source


def test_the_example_workflow_is_inert():
    doc = yaml.safe_load((EXAMPLES / "workflows/pr-guardian-demo-bad-workflow.yml").read_text())
    triggers = doc.get("on", doc.get(True))  # PyYAML reads a bare `on:` as True
    assert triggers == "workflow_dispatch"  # only startable by hand, never by a PR or push
    for job in doc["jobs"].values():
        assert str(job["if"]).replace(" ", "") == "${{false}}"
    assert doc["permissions"] == {"contents": "read"}


def test_manifest_paths_are_sane():
    for source, dest, _ in ROWS:
        assert (EXAMPLES / source).is_file()
        assert not dest.startswith("/") and ".." not in dest.split("/")
        assert dest.startswith(("demo-pr/", ".github/workflows/"))


def test_examples_contain_no_secret_shaped_strings():
    pattern = re.compile(
        r"AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|sk-ant-|-----BEGIN [A-Z ]*PRIVATE KEY"
    )
    for path in EXAMPLES.rglob("*"):
        if path.is_file():
            assert not pattern.search(path.read_text()), path
