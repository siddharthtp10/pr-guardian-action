import copy

import pytest

from pr_guardian.config import SEVERITIES
from pr_guardian.filetypes import FileKind
from pr_guardian.rules import DEFAULT_POLICY, RuleError, RuleType, load_rules, parse_rules

GOOD = {
    "id": "TST-001",
    "severity": "high",
    "file_types": ["terraform"],
    "pattern": r"foo",
    "message": "Do not foo.",
}


def policy(*rules):
    return {"version": 1, "rules": list(rules)}


def test_default_policy_loads():
    rules = load_rules()
    assert 12 <= len(rules) <= 40
    assert len({r.id for r in rules}) == len(rules)
    assert all(r.severity in SEVERITIES for r in rules)


def test_default_policy_covers_every_file_kind():
    covered = set().union(*(r.file_types for r in load_rules()))
    assert covered == set(FileKind)


def test_default_policy_messages_are_static_text():
    # Messages are posted publicly; they must not be templates that could echo
    # matched (possibly secret) text.
    for rule in load_rules():
        assert "{" not in rule.message and "%" not in rule.message, rule.id


def test_any_expands_to_all_kinds():
    rule = parse_rules(policy({**GOOD, "file_types": ["any"]}))[0]
    assert rule.file_types == set(FileKind)


def test_defaults():
    rule = parse_rules(policy(GOOD))[0]
    assert rule.type is RuleType.LINE and not rule.scan_comments and rule.exclude is None


@pytest.mark.parametrize(
    ("mutation", "needle"),
    [
        ({"severty": "high"}, "unknown key 'severty'"),
        ({"severity": "urgent"}, "severity must be"),
        ({"id": "bad id"}, "id must look like"),
        ({"pattern": "("}, "not a valid regex"),
        ({"pattern": ""}, "non-empty"),
        ({"pattern": "a" * 601}, "longer than"),
        ({"message": " "}, "message is required"),
        ({"file_types": []}, "non-empty list"),
        ({"file_types": ["cobol"]}, "unknown file type"),
        ({"type": "magic"}, "type must be"),
        ({"type": "yaml_item_missing"}, "need 'required'"),
        ({"required": "x"}, "only valid for yaml_item_missing"),
        ({"scan_comments": "yes"}, "scan_comments must be"),
        ({"exclude": "["}, "exclude is not a valid regex"),
    ],
)
def test_invalid_rules_are_rejected_with_a_clear_message(mutation, needle):
    with pytest.raises(RuleError) as exc:
        parse_rules(policy({**GOOD, **mutation}))
    assert needle in str(exc.value)


def test_missing_required_fields():
    bad = copy.deepcopy(GOOD)
    del bad["pattern"]
    with pytest.raises(RuleError, match="pattern is required"):
        parse_rules(policy(bad))


def test_duplicate_ids_rejected():
    with pytest.raises(RuleError, match="duplicate id"):
        parse_rules(policy(GOOD, GOOD))


def test_all_problems_reported_together():
    with pytest.raises(RuleError) as exc:
        parse_rules(policy({**GOOD, "severity": "x", "pattern": "("}))
    assert len(exc.value.problems) == 2


@pytest.mark.parametrize("data", [None, [], {"version": 2, "rules": [GOOD]}, {"version": 1}])
def test_bad_top_level(data):
    with pytest.raises(RuleError):
        parse_rules(data)


def test_unreadable_policy(tmp_path):
    with pytest.raises(RuleError, match="cannot read"):
        load_rules(tmp_path / "nope.yaml")
    broken = tmp_path / "broken.yaml"
    broken.write_text("rules: [unclosed")
    with pytest.raises(RuleError):
        load_rules(broken)


def test_policy_file_is_in_the_package_data():
    assert DEFAULT_POLICY.is_file()


def test_block_conditions_are_accepted_when_a_within_pattern_is_present():
    rule = parse_rules(
        policy({**GOOD, "within": r"^\s*ingress", "block_requires": "22", "block_forbids": "443"})
    )[0]
    assert rule.block_requires.search("22") and rule.block_forbids.search("443")


@pytest.mark.parametrize("key", ["block_requires", "block_forbids"])
def test_block_conditions_without_within_are_rejected(key):
    with pytest.raises(RuleError, match="need a 'within' pattern"):
        parse_rules(policy({**GOOD, key: "22"}))


def test_block_conditions_are_rejected_on_non_line_rules():
    bad = {
        **GOOD,
        "type": "yaml_item_missing",
        "required": "x",
        "within": "a",
        "block_requires": "b",
    }
    with pytest.raises(RuleError, match="only valid for line rules"):
        parse_rules(policy(bad))


@pytest.mark.parametrize("key", ["block_requires", "block_forbids"])
def test_block_condition_regexes_are_validated_like_any_other(key):
    with pytest.raises(RuleError, match=f"{key} is not a valid regex"):
        parse_rules(policy({**GOOD, "within": "a", key: "("}))


def test_the_default_policy_only_uses_block_conditions_on_rules_that_have_within():
    for rule in load_rules():
        if rule.block_requires or rule.block_forbids:
            assert rule.within is not None, rule.id
