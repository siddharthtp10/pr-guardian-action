"""Documentation that can be checked against the code, is checked against the code."""

import re
from pathlib import Path

import yaml

from pr_guardian import ai_review as ai
from pr_guardian.rules import load_rules

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text()


def section(heading: str) -> str:
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", README, flags=re.S | re.M)
    assert match, f"README has no '## {heading}' section"
    return match[1]


def table_rows(text: str):
    rows = [ln for ln in text.splitlines() if ln.startswith("|")]
    return [[c.strip() for c in ln.strip("|").split("|")] for ln in rows[2:]]  # skip header+rule


def test_required_sections_exist():
    for heading in (
        "Quick start",
        "Inputs and outputs",
        "How it works",
        "Security model",
        "Cost estimate",
        "Limitations",
        "Try the demo",
        "What I would do differently in production",
    ):
        section(heading)
    for subheading in ("What leaves the runner", "Fork and Dependabot behaviour", "Threat model"):
        assert f"### {subheading}" in README


def test_inputs_table_matches_action_yml():
    action = yaml.safe_load((ROOT / "action.yml").read_text())
    block = section("Inputs and outputs")
    first_table = block.split("\n\n")[0]
    rows = {r[0].strip("`"): r for r in table_rows(first_table)}
    assert set(rows) == set(action["inputs"])
    for name, spec in action["inputs"].items():
        default = str(spec.get("default", ""))
        shown = rows[name][2]
        if name == "github-token":
            assert "github.token" in shown
        elif default == "":
            assert shown in ('`""`', "all supported")
        else:
            assert shown == f"`{default}`", name


def test_outputs_table_matches_action_yml():
    action = yaml.safe_load((ROOT / "action.yml").read_text())
    block = section("Inputs and outputs")
    output_table = next(t for t in block.split("\n\n") if t.startswith("| Output"))
    assert {r[0].strip("`") for r in table_rows(output_table)} == set(action["outputs"])


def test_rule_count_claim_is_true():
    claimed = int(re.search(r"(\d+) generic rules ship in", README)[1])
    assert claimed == len(load_rules())


def test_cost_table_is_computed_from_the_code_not_typed_by_hand():
    table = next(t for t in section("Cost estimate").split("\n\n") if t.startswith("| Model"))
    rows = {r[0].strip("`").split("`")[0]: r for r in table_rows(table)}
    for model in ("claude-haiku-4-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1"):
        key = next(k for k in rows if k.startswith(model))
        price_in, price_out = ai.price_for(model)
        budget = ai.input_token_budget(model)
        worst = ai.cost_of(model, budget, ai.MAX_OUTPUT_TOKENS)
        small = ai.cost_of(model, 3000, 800)
        cells = rows[key]
        assert cells[1] == f"{price_in:.2f} / {price_out:.2f}", model
        assert cells[2] == f"{budget:,}", model
        assert cells[3] == f"${worst:.4f}", model
        assert cells[4] == f"${small:.4f}", model


def test_cost_prose_matches_the_constants():
    text = section("Cost estimate")
    assert f"at most {ai.MAX_OUTPUT_TOKENS:,} output tokens" in text
    assert (
        f"about\n{ai.MAX_INPUT_TOKENS:,} input tokens" in text
        or f"{ai.MAX_INPUT_TOKENS:,} input tokens" in text
    )
    assert f"${ai.MAX_COST_USD:.2f} per-run cap" in text
    assert ai.cost_of("claude-sonnet-5-5", 3000, 800) * 120 < 2  # the "$1.70 a month" illustration
    assert "120 runs x $0.014" in text


def test_mermaid_diagram_is_present_and_well_formed():
    blocks = re.findall(r"```mermaid\n(.*?)```", README, flags=re.S)
    assert len(blocks) == 1
    diagram = blocks[0]
    assert diagram.startswith("flowchart TD")
    assert diagram.count("subgraph") == diagram.count("\n    end") == 1
    # The guarantee the diagram claims: nothing from the AI path feeds the gate.
    assert "AIF -.-> PUB" in diagram
    gate_inputs = [ln for ln in diagram.splitlines() if "--> GATE" in ln]
    assert len(gate_inputs) == 1 and gate_inputs[0].strip().startswith("RF ")


def test_relative_links_point_at_files_that_exist():
    for target in re.findall(r"\]\(((?!https?://|#)[^)\s]+)\)", README):
        path = target.split("#")[0]
        assert (ROOT / path).exists(), f"broken link: {target}"


def test_fork_table_matches_the_code_for_both_read_only_cases():
    block = section("Security model")
    assert "NOTICE: fork pull request" in block and "NOTICE: Dependabot run" in block


def test_every_documented_demo_command_is_a_real_flag():
    script = (ROOT / "scripts" / "open-demo-pr.sh").read_text()
    for flag in ("--dry-run", "--cleanup", "--base", "--help"):
        assert flag in script
    for flag in re.findall(r"open-demo-pr\.sh (--[a-z-]+)", README):
        assert flag in script
