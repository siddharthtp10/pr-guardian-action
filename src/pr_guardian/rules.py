"""Load and strictly validate the YAML policy into immutable Rule objects.

WHY strict: a typo in a rule file (a misspelt key, a bad regex, a wrong
severity) must fail loudly at load time. A rule that silently never fires is
the worst kind of bug in a security tool: it looks like a clean PR.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml

from pr_guardian.config import SEVERITIES
from pr_guardian.filetypes import FileKind

DEFAULT_POLICY = Path(__file__).parent / "policies" / "default.yaml"

_RULE_ID = re.compile(r"^[A-Z][A-Z0-9]{1,5}-\d{3}$")
_ALLOWED_KEYS = {
    "id",
    "severity",
    "file_types",
    "message",
    "pattern",
    "type",
    "exclude",
    "within",
    "block_requires",
    "block_forbids",
    "required",
    "scan_comments",
}
MAX_PATTERN_CHARS = 600
MAX_MESSAGE_CHARS = 600


class RuleError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


class RuleType(StrEnum):
    LINE = "line"
    YAML_ITEM_MISSING = "yaml_item_missing"


@dataclass(frozen=True)
class Rule:
    id: str
    severity: str
    file_types: frozenset[FileKind]
    message: str
    pattern: re.Pattern[str]
    type: RuleType = RuleType.LINE
    exclude: re.Pattern[str] | None = None
    within: re.Pattern[str] | None = None
    block_requires: re.Pattern[str] | None = None
    block_forbids: re.Pattern[str] | None = None
    required: re.Pattern[str] | None = None
    scan_comments: bool = False


def load_rules(path: Path | None = None) -> list[Rule]:
    path = path or DEFAULT_POLICY
    try:
        # safe_load: never yaml.load - a policy file must not be able to
        # construct arbitrary Python objects.
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RuleError([f"cannot read policy {path.name}: {exc}"]) from exc
    return parse_rules(data)


def parse_rules(data: object) -> list[Rule]:
    problems: list[str] = []
    if not isinstance(data, dict) or data.get("version") != 1:
        raise RuleError(["policy must be a mapping with 'version: 1'"])
    entries = data.get("rules")
    if not isinstance(entries, list) or not entries:
        raise RuleError(["policy needs a non-empty 'rules' list"])

    rules: list[Rule] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        label = entry.get("id", f"#{index}") if isinstance(entry, dict) else f"#{index}"
        before = len(problems)
        rule = _parse_one(entry, label, problems)
        if rule is not None and len(problems) == before:
            if rule.id in seen:
                problems.append(f"{label}: duplicate id")
            seen.add(rule.id)
            rules.append(rule)
    if problems:
        raise RuleError(problems)
    return rules


def _parse_one(entry: object, label: str, problems: list[str]) -> Rule | None:
    if not isinstance(entry, dict):
        problems.append(f"{label}: rule must be a mapping")
        return None

    for key in sorted(set(entry) - _ALLOWED_KEYS):
        problems.append(f"{label}: unknown key {key!r}")  # catches typos like 'severty'

    rule_id = entry.get("id")
    if not isinstance(rule_id, str) or not _RULE_ID.match(rule_id):
        problems.append(f"{label}: id must look like ABC-001")

    severity = entry.get("severity")
    if severity not in SEVERITIES:
        problems.append(f"{label}: severity must be one of {', '.join(SEVERITIES)}")

    message = entry.get("message")
    if not isinstance(message, str) or not message.strip():
        problems.append(f"{label}: message is required")
    elif len(message) > MAX_MESSAGE_CHARS:
        problems.append(f"{label}: message longer than {MAX_MESSAGE_CHARS} chars")

    try:
        rule_type = RuleType(entry.get("type", "line"))
    except ValueError:
        problems.append(f"{label}: type must be one of {', '.join(t.value for t in RuleType)}")
        rule_type = RuleType.LINE

    file_types = _parse_file_types(entry.get("file_types"), label, problems)
    pattern = _compile(entry, "pattern", label, problems, required=True)
    exclude = _compile(entry, "exclude", label, problems)
    within = _compile(entry, "within", label, problems)
    block_requires = _compile(entry, "block_requires", label, problems)
    block_forbids = _compile(entry, "block_forbids", label, problems)
    required = _compile(entry, "required", label, problems)

    if rule_type is RuleType.YAML_ITEM_MISSING and required is None:
        problems.append(f"{label}: yaml_item_missing rules need 'required'")
    if rule_type is RuleType.LINE and required is not None:
        problems.append(f"{label}: 'required' is only valid for yaml_item_missing")
    if (block_requires or block_forbids) and within is None:
        # The block is the one found by `within`; without it there is nothing to inspect.
        problems.append(f"{label}: block_requires/block_forbids need a 'within' pattern")
    if (block_requires or block_forbids) and rule_type is not RuleType.LINE:
        problems.append(f"{label}: block_requires/block_forbids are only valid for line rules")
    scan_comments = entry.get("scan_comments", False)
    if not isinstance(scan_comments, bool):
        problems.append(f"{label}: scan_comments must be true or false")

    if pattern is None or not isinstance(rule_id, str) or not isinstance(message, str):
        return None
    return Rule(
        id=rule_id,
        severity=str(severity),
        file_types=file_types,
        message=message.strip(),
        pattern=pattern,
        type=rule_type,
        exclude=exclude,
        within=within,
        block_requires=block_requires,
        block_forbids=block_forbids,
        required=required,
        scan_comments=scan_comments is True,
    )


def _parse_file_types(value: object, label: str, problems: list[str]) -> frozenset[FileKind]:
    if not isinstance(value, list) or not value:
        problems.append(f"{label}: file_types must be a non-empty list")
        return frozenset()
    kinds: set[FileKind] = set()
    for item in value:
        if item == "any":
            kinds.update(FileKind)
            continue
        try:
            kinds.add(FileKind(item))
        except ValueError:
            valid = ", ".join([*(k.value for k in FileKind), "any"])
            problems.append(f"{label}: unknown file type {item!r} (valid: {valid})")
    return frozenset(kinds)


def _compile(
    entry: dict, key: str, label: str, problems: list[str], *, required: bool = False
) -> re.Pattern[str] | None:
    raw = entry.get(key)
    if raw is None:
        if required:
            problems.append(f"{label}: {key} is required")
        return None
    if not isinstance(raw, str) or not raw:
        problems.append(f"{label}: {key} must be a non-empty string")
        return None
    if len(raw) > MAX_PATTERN_CHARS:
        problems.append(f"{label}: {key} longer than {MAX_PATTERN_CHARS} chars")
        return None
    try:
        return re.compile(raw)
    except re.error as exc:
        problems.append(f"{label}: {key} is not a valid regex ({exc})")
        return None
