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
    # The `ingress {` opener and the port are unchanged context lines; only the CIDR is added.
    # Port 443 is public web: low (TF-006), never the high admin-port rule.
    patch = parse_patch(
        "@@ -1,4 +1,5 @@\n"
        "   ingress {\n"
        "     from_port = 443\n"
        '+    cidr_blocks = ["0.0.0.0/0"]\n'
        "   }\n"
        " }"
    )
    out = evaluate([ReviewTarget("a.tf", FileKind.TERRAFORM, "modified", patch)], RULES)
    assert [f.rule_id for f in out] == ["TF-006"]
    ssh = parse_patch(str(patch_text_for_port(22)))
    out = evaluate([ReviewTarget("a.tf", FileKind.TERRAFORM, "modified", ssh)], RULES)
    assert [f.rule_id for f in out] == ["TF-001"]


def patch_text_for_port(port):
    return (
        "@@ -1,4 +1,5 @@\n"
        "   ingress {\n"
        f"     from_port = {port}\n"
        '+    cidr_blocks = ["0.0.0.0/0"]\n'
        "   }\n"
        " }"
    )


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
    "pw-eq-spaces": "password =" + " " * (MAX_LINE_CHARS - 12) + "x",
    "pw-unquoted": "password: " + "a1" * (MAX_LINE_CHARS // 2 - 5),
    "pw-many": "password: a1b2c3d4 " * (MAX_LINE_CHARS // 19),
    "cidr-list": "public_access_cidrs = [" + '"1.1.1.1/32", ' * (MAX_LINE_CHARS // 14 - 3),
    "curl-many": "RUN " + "curl " * (MAX_LINE_CHARS // 5 - 1),
    "curl-pipe": "RUN curl " + "x" * (MAX_LINE_CHARS - 12) + " |",
    "port-digits": "from_port = " + "2" * (MAX_LINE_CHARS - 12),
    "indent-then-key": " " * (MAX_LINE_CHARS - 20) + "publicly_accessible = true",
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


# --- block_requires / block_forbids (sibling-line checks) ----------------------------


def tf_ids(text, kind=FileKind.TERRAFORM):
    return sorted({f.rule_id for f in evaluate([target(text, kind)], RULES)})


def security_group(port, protocol="tcp", cidr="0.0.0.0/0"):
    return (
        'resource "aws_security_group" "x" {\n'
        "  ingress {\n"
        f"    from_port   = {port}\n"
        f"    to_port     = {port}\n"
        f'    protocol    = "{protocol}"\n'
        f'    cidr_blocks = ["{cidr}"]\n'
        "  }\n"
        "}\n"
    )


@pytest.mark.parametrize(
    "port", [22, 23, 3389, 5900, 3306, 5432, 1433, 1521, 6379, 9200, 11211, 27017]
)
def test_administrative_and_database_ports_open_to_the_world_are_high(port):
    assert tf_ids(security_group(port)) == ["TF-001"]


@pytest.mark.parametrize("port", [80, 443, 8080, 8443])
def test_ordinary_service_ports_open_to_the_world_are_only_low(port):
    assert tf_ids(security_group(port)) == ["TF-006"]


def test_all_protocols_and_all_ports_are_high_even_though_the_port_number_is_innocent():
    assert tf_ids(security_group(0, protocol="-1")) == ["TF-001"]
    wide = security_group(0).replace("to_port     = 0", "to_port     = 65535")
    assert tf_ids(wide) == ["TF-001"]


def test_a_restricted_cidr_is_never_a_finding_whatever_the_port():
    assert tf_ids(security_group(22, cidr="10.0.0.0/8")) == []
    assert tf_ids(security_group(443, cidr="10.0.0.0/8")) == []


def test_ipv6_anywhere_is_treated_like_ipv4_anywhere():
    text = security_group(22).replace('"0.0.0.0/0"', '"::/0"')
    assert tf_ids(text) == ["TF-001"]


def test_the_two_ingress_blocks_in_one_file_are_judged_independently():
    text = (
        'resource "aws_security_group" "x" {\n'
        "  ingress {\n    from_port = 443\n    to_port = 443\n"
        '    cidr_blocks = ["0.0.0.0/0"]\n  }\n'
        "  ingress {\n    from_port = 22\n    to_port = 22\n"
        '    cidr_blocks = ["0.0.0.0/0"]\n  }\n'
        "}\n"
    )
    found = [(f.line, f.rule_id) for f in evaluate([target(text, FileKind.TERRAFORM)], RULES)]
    assert found == [(5, "TF-006"), (10, "TF-001")]


def test_the_rule_resource_form_gets_the_same_port_logic():
    def rule(port):
        return (
            'resource "aws_vpc_security_group_ingress_rule" "r" {\n'
            '  cidr_ipv4   = "0.0.0.0/0"\n'
            f"  from_port   = {port}\n"
            f"  to_port     = {port}\n"
            '  ip_protocol = "tcp"\n'
            "}\n"
        )

    assert tf_ids(rule(3389)) == ["TF-002"]
    assert tf_ids(rule(443)) == ["TF-006"]
    all_proto = rule(0).replace('"tcp"', '"-1"')
    assert tf_ids(all_proto) == ["TF-002"]


def one_file(patch):
    return evaluate(
        [ReviewTarget("a.tf", FileKind.TERRAFORM, "modified", parse_patch(patch))], RULES
    )


def test_a_block_cut_off_by_the_diff_is_never_guessed_about():
    # Opener and port visible, CIDR added, but the closing brace is NOT in the diff, so we
    # cannot know what else the block contains: both the "requires" and "forbids" rules stay silent.
    cut = '@@ -1,2 +1,3 @@\n   ingress {\n     from_port = 22\n+    cidr_blocks = ["0.0.0.0/0"]'
    assert one_file(cut) == []
    cut_443 = cut.replace("22", "443")
    assert one_file(cut_443) == []


def test_only_the_cidr_line_visible_means_silence_not_a_guess():
    lone = '@@ -9,1 +9,2 @@\n     protocol = "tcp"\n+    cidr_blocks = ["0.0.0.0/0"]'
    assert one_file(lone) == []


def test_the_port_line_may_be_an_unchanged_context_line():
    whole = (
        "@@ -1,4 +1,5 @@\n   ingress {\n     from_port = 22\n"
        '+    cidr_blocks = ["0.0.0.0/0"]\n   }\n }'
    )
    assert [f.rule_id for f in one_file(whole)] == ["TF-001"]


def test_a_commented_out_port_does_not_count_as_a_sibling():
    text = security_group(443).replace(
        "    from_port   = 443", "    # from_port = 22\n    from_port   = 443"
    )
    assert tf_ids(text) == ["TF-006"]


# --- unquoted secrets (SEC-003) ---------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "kind"),
    [
        ("db_password: hunter2hunter2", FileKind.KUBERNETES),
        ("  access_token: tok9f8e7d6c5b4a", FileKind.KUBERNETES),
        ("ENV DB_PASSWORD=hunter2hunter2", FileKind.DOCKERFILE),
        ("ARG API_TOKEN=abcd1234efgh5678", FileKind.DOCKERFILE),
        ('  api_key: "abcd1234efgh5678"', FileKind.KUBERNETES),
    ],
)
def test_unquoted_and_quoted_secret_values_are_found(line, kind):
    assert "SEC-003" in tf_ids(line + "\n", kind)


@pytest.mark.parametrize(
    "line",
    [
        "# token: use-the-vault-instead",  # prose in a comment: no digit
        "password: ${DB_PASSWORD}",
        "password: $(cat /run/secrets/pw1)",
        "token: short1",  # under 8 characters
        "password: changeme-please1",
        "secretName: my-tls-secret2",  # key does not END in a secret-ish word
        "password_file: /run/secrets/pw1",
        "password = var.db_password2",
        "password = random_password.db1.result",
        "token = module.auth.token2",
        "secret = data.aws_ssm_parameter.pw2.value",
    ],
)
def test_references_prose_and_placeholders_are_not_findings(line):
    assert "SEC-003" not in tf_ids(line + "\n", FileKind.TERRAFORM)
    assert "SEC-003" not in tf_ids(line + "\n", FileKind.KUBERNETES)


# --- the other new rules, in one table --------------------------------------------------------


@pytest.mark.parametrize(
    ("rule_id", "kind", "bad", "good"),
    [
        ("TF-007", FileKind.TERRAFORM, "publicly_accessible = true", "publicly_accessible = false"),
        ("TF-008", FileKind.TERRAFORM, "storage_encrypted = false", "storage_encrypted = true"),
        ("TF-008", FileKind.TERRAFORM, "encrypted = false", "encrypted = true"),
        (
            "TF-009",
            FileKind.TERRAFORM,
            'public_access_cidrs = ["10.0.0.0/8", "0.0.0.0/0"]',
            'public_access_cidrs = ["10.0.0.0/8"]',
        ),
        ("TF-010", FileKind.TERRAFORM, 'http_tokens = "optional"', 'http_tokens = "required"'),
        (
            "TF-011",
            FileKind.TERRAFORM,
            'image_tag_mutability = "MUTABLE"',
            'image_tag_mutability = "IMMUTABLE"',
        ),
        ("K8S-005", FileKind.KUBERNETES, "hostNetwork: true", "hostNetwork: false"),
        ("K8S-005", FileKind.KUBERNETES, "hostPID: true", "hostPID: false"),
        ("K8S-005", FileKind.KUBERNETES, "hostIPC: true", "hostIPC: false"),
        (
            "K8S-006",
            FileKind.KUBERNETES,
            "allowPrivilegeEscalation: true",
            "allowPrivilegeEscalation: false",
        ),
        ("GHA-004", FileKind.GITHUB_ACTIONS, "permissions: write-all", "permissions: read-all"),
        (
            "DOCKER-003",
            FileKind.DOCKERFILE,
            "RUN curl -fsSL https://example.com/i.sh | sh",
            "RUN curl -fsSL https://example.com/i.sh | sha256sum -c -",
        ),
        (
            "DOCKER-003",
            FileKind.DOCKERFILE,
            "RUN wget -qO- https://example.com/i | sudo bash -",
            "RUN curl -fsSL https://example.com/t.tgz | tar -xz",
        ),
    ],
)
def test_each_new_rule_fires_on_the_bad_form_and_not_on_the_good_one(rule_id, kind, bad, good):
    assert rule_id in tf_ids(bad + "\n", kind)
    assert rule_id not in tf_ids(good + "\n", kind)


def test_new_high_rules_are_exactly_the_ones_that_should_gate_a_merge():
    high = {r.id for r in RULES if r.severity == "high"}
    assert {"TF-001", "TF-002", "TF-007", "K8S-005", "GHA-004"} <= high
    # Heuristic or context-dependent findings must NOT block by default (fail-on: high).
    low_or_medium = {
        r.id: r.severity
        for r in RULES
        if r.id in {"TF-006", "TF-008", "TF-009", "TF-010", "TF-011"}
    }
    assert set(low_or_medium.values()) <= {"low", "medium"}
