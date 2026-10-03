"""Rule behaviour tests.

Layout: tests/fixtures/rules/<file-kind>/<name>. Any line that should produce a
finding carries ``EXPECT:RULE-ID`` (comma-separate several). The harness then
requires the *exact* set of (line, rule) findings across ALL rules - so a
"clean" fixture is a false-positive test, and a "bad" fixture also proves that
the lines without a marker stay silent.
"""

import re
import time
from pathlib import Path

import pytest

from pr_guardian.diff import parse_patch
from pr_guardian.engine import (
    GUARD_RULE_ID,
    MAX_LINE_CHARS,
    evaluate,
    meets_threshold,
)
from pr_guardian.filetypes import FileKind
from pr_guardian.rules import load_rules
from pr_guardian.selection import ReviewTarget

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "rules"
RULES = load_rules()
MARKER = re.compile(r"EXPECT:([A-Z][A-Z0-9]+-\d{3}(?:,[A-Z][A-Z0-9]+-\d{3})*)")

# Secret-shaped test data is assembled here, never stored literally: a literal
# fake key in the repo would trip GitHub push protection and our own repo scan.
SUBSTITUTIONS = {
    "@@AWS_KEY@@": "AKIA" + "QWERTYUIOPASDFGH",
    "@@PEM@@": "-----BEGIN RSA " + "PRIVATE KEY-----",
}


def added_patch(text: str):
    """Treat the whole text as a brand-new file: every line is an added line."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    body = "\n".join("+" + ln for ln in lines)
    return parse_patch(f"@@ -0,0 +1,{len(lines)} @@\n{body}")


def target(text: str, kind: FileKind, path: str = "f") -> ReviewTarget:
    return ReviewTarget(path, kind, "added", added_patch(text))


def read_fixture(path: Path) -> str:
    text = path.read_text()
    for placeholder, value in SUBSTITUTIONS.items():
        text = text.replace(placeholder, value)
    return text


FIXTURES = sorted(p for p in FIXTURE_ROOT.rglob("*") if p.is_file())


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_fixture_findings_match_expectations_exactly(path):
    text = read_fixture(path)
    kind = FileKind(path.parent.name)
    expected = {
        (number, rule_id)
        for number, line in enumerate(text.split("\n"), start=1)
        for match in [MARKER.search(line)]
        if match
        for rule_id in match[1].split(",")
    }
    actual = {(f.line, f.rule_id) for f in evaluate([target(text, kind)], RULES)}
    assert actual == expected, (
        f"unexpected: {sorted(actual - expected)}  missing: {sorted(expected - actual)}"
    )


def test_every_rule_has_a_positive_fixture_and_a_clean_fixture_per_kind():
    marked = set()
    for path in FIXTURES:
        marked.update(rid for m in MARKER.finditer(read_fixture(path)) for rid in m[1].split(","))
    assert {r.id for r in RULES} <= marked, "rules without a positive fixture"
    for kind in FileKind:
        assert any((FIXTURE_ROOT / kind.value).glob("clean.*")), f"no clean fixture: {kind}"


# --- behaviour that fixtures cannot express -----------------------------------


def test_only_added_lines_are_flagged():
    patch = parse_patch(
        "@@ -1,3 +1,3 @@\n"
        ' resource "aws_s3_bucket_acl" "a" {\n'
        '-  acl = "private"\n'
        '+  acl = "public-read"\n'
        " }"
    )
    out = evaluate([ReviewTarget("a.tf", FileKind.TERRAFORM, "modified", patch)], RULES)
    assert [(f.line, f.rule_id) for f in out] == [(2, "TF-003")]


def test_pre_existing_violation_on_a_context_line_is_not_blamed_on_the_pr():
    patch = parse_patch(
        '@@ -1,3 +1,4 @@\n   acl = "public-read"\n   other = 1\n+  new = 2\n   x = 3'
    )
    assert evaluate([ReviewTarget("a.tf", FileKind.TERRAFORM, "modified", patch)], RULES) == []


def test_removed_violation_is_not_reported():
    patch = parse_patch('@@ -1,2 +1,1 @@\n-  acl = "public-read"\n   keep = 1')
    assert evaluate([ReviewTarget("a.tf", FileKind.TERRAFORM, "modified", patch)], RULES) == []


def test_ingress_context_is_found_across_a_visible_context_line():
    # The `ingress {` opener is an unchanged context line; the bad CIDR is added.
    patch = parse_patch(
        "@@ -1,4 +1,5 @@\n"
        "   ingress {\n"
        "     from_port = 443\n"
        '+    cidr_blocks = ["0.0.0.0/0"]\n'
        "   }\n"
        " }"
    )
    out = evaluate([ReviewTarget("a.tf", FileKind.TERRAFORM, "modified", patch)], RULES)
    assert [f.rule_id for f in out] == ["TF-001"]


def test_ingress_opener_not_visible_means_no_guess():
    # Only the added line is visible: we cannot know whether it is ingress or egress.
    patch = parse_patch('@@ -10,1 +10,2 @@\n     from_port = 0\n+    cidr_blocks = ["0.0.0.0/0"]')
    assert evaluate([ReviewTarget("a.tf", FileKind.TERRAFORM, "modified", patch)], RULES) == []


CONTAINER_TAIL = (
    "     - name: web\n"
    "       image: nginx:1.27\n"
    "+      imagePullPolicy: Always\n"
    "     - name: next\n"
)


def test_missing_limits_not_reported_when_the_container_block_is_cut_off():
    # The hunk ends inside the last container: its `resources:` may be below.
    patch = parse_patch(
        "@@ -1,2 +1,3 @@\n"
        "     - name: web\n"
        "       image: nginx:1.27\n"
        "+      imagePullPolicy: Always"
    )
    assert evaluate([ReviewTarget("d.yaml", FileKind.KUBERNETES, "modified", patch)], RULES) == []


def test_missing_limits_reported_when_the_whole_block_is_visible():
    patch = parse_patch("@@ -1,3 +1,4 @@\n" + CONTAINER_TAIL.rstrip("\n"))
    out = evaluate([ReviewTarget("d.yaml", FileKind.KUBERNETES, "modified", patch)], RULES)
    assert [(f.line, f.rule_id) for f in out] == [(1, "K8S-001")]


def test_untouched_container_is_not_reported():
    patch = parse_patch(
        "@@ -1,7 +1,7 @@\n"
        "     - name: web\n"
        "       image: nginx:1.27\n"
        "     - name: next\n"
        "-      image: a:1\n"
        "+      image: a:2\n"
        "       resources:\n"
        "         limits: {memory: 1Gi}\n"
        "   other: x"
    )
    out = evaluate([ReviewTarget("d.yaml", FileKind.KUBERNETES, "modified", patch)], RULES)
    assert out == []  # `web` has no limits but this PR did not touch it


def test_findings_never_echo_matched_text():
    secret = "AKIA" + "QWERTYUIOPASDFGH"
    password = "hunter2hunter2"  # noqa: S105 - fake test value
    text = f'key = "{secret}"\ndb_password = "{password}"\n'
    out = evaluate([target(text, FileKind.TERRAFORM)], RULES)
    assert {f.rule_id for f in out} == {"SEC-001", "SEC-003"}
    for f in out:
        assert secret not in f.message and password not in f.message


def test_overlong_line_is_reported_not_silently_skipped():
    long_line = 'acl = "public-read" ' + "x" * (MAX_LINE_CHARS + 1)
    out = evaluate([target(long_line, FileKind.TERRAFORM)], RULES)
    assert [f.rule_id for f in out] == [GUARD_RULE_ID]


def test_findings_are_deduplicated_and_sorted():
    text = 'acl = "public-read"\nblock_public_acls = false\n'
    twice = [target(text, FileKind.TERRAFORM, "b.tf"), target(text, FileKind.TERRAFORM, "a.tf")]
    out = evaluate(twice, RULES)
    assert [(f.path, f.line) for f in out] == [("a.tf", 1), ("a.tf", 2), ("b.tf", 1), ("b.tf", 2)]


def test_rules_only_apply_to_their_file_kind():
    # A Dockerfile-style line inside a Terraform file must not trigger DOCKER rules.
    assert evaluate([target("FROM node:latest\n", FileKind.TERRAFORM)], RULES) == []


@pytest.mark.parametrize(
    ("severity", "fail_on", "expected"),
    [
        ("high", "high", True),
        ("critical", "high", True),
        ("medium", "high", False),
        ("low", "low", True),
        ("critical", "none", False),
    ],
)
def test_meets_threshold(severity, fail_on, expected):
    assert meets_threshold(severity, fail_on) is expected


# --- hostile input: regex denial of service ----------------------------------

HOSTILE = {
    "a-run": "a" * MAX_LINE_CHARS,
    "spaces": " " * MAX_LINE_CHARS,
    "quotes": '"' * MAX_LINE_CHARS,
    "open-brackets": "[" * MAX_LINE_CHARS,
    "uses": "uses: " + "a" * (MAX_LINE_CHARS - 6),
    "uses-at": "uses: x@" + "a" * (MAX_LINE_CHARS - 8),
    "expr": "${{ " * (MAX_LINE_CHARS // 4),
    "expr-event": "run: ${{ github.event.pull_request" + ".x" * (MAX_LINE_CHARS // 2 - 20),
    "secret-key": "password" * (MAX_LINE_CHARS // 8),
    "secret-val": 'password = "' + "a" * (MAX_LINE_CHARS - 13),
    "image": "image: " + "a:" * (MAX_LINE_CHARS // 2 - 4),
    "actions": "actions = [" + '"a", ' * (MAX_LINE_CHARS // 5 - 3),
    "dash": "- " * (MAX_LINE_CHARS // 2),
    "from": "FROM " + "a" * (MAX_LINE_CHARS - 5),
    "begin": "-----BEGIN " + "A " * (MAX_LINE_CHARS // 2 - 6),
}


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_no_rule_is_slow_on_hostile_lines(name):
    for kind in FileKind:
        start = time.perf_counter()
        evaluate([target(HOSTILE[name], kind)], RULES)
        elapsed = time.perf_counter() - start
        # Linear patterns finish in milliseconds; 1s leaves headroom for slow CI
        # while still catching quadratic/exponential backtracking.
        assert elapsed < 1.0, f"{name}/{kind}: {elapsed:.2f}s"


def test_docs_list_exactly_the_rules_in_the_policy():
    docs = (Path(__file__).parent.parent / "docs" / "rules.md").read_text()
    documented = set(re.findall(r"^\| ([A-Z][A-Z0-9]+-\d{3}) \|", docs, flags=re.MULTILINE))
    assert documented == {r.id for r in RULES} | {GUARD_RULE_ID}


@pytest.mark.parametrize("path", [p for p in FIXTURES if p.suffix in (".yml", ".yaml")], ids=str)
def test_yaml_fixtures_are_valid_yaml(path):
    # A fixture a real YAML parser rejects is not testing realistic input.
    import yaml

    yaml.safe_load(read_fixture(path))
