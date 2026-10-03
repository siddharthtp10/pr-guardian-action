import io

from pr_guardian.cli import EXIT_CONFIG, EXIT_OK, escape_workflow_data, main

GOOD = {"PRG_GITHUB_TOKEN": "ghs_dummy_token_for_tests"}


def run(env):
    out = io.StringIO()
    return main(env=env, out=out), out.getvalue()


def test_rules_only_run_succeeds_and_says_so():
    code, text = run(GOOD)
    assert code == EXIT_OK
    assert "rules-only" in text
    assert "AI layer disabled" in text


def test_output_never_contains_secret_values():
    code, text = run({**GOOD, "PRG_ANTHROPIC_API_KEY": "super-secret-value"})
    assert code == EXIT_OK
    assert "rules+ai" in text
    assert "super-secret-value" not in text
    assert "ghs_dummy_token_for_tests" not in text


def test_invalid_config_exits_2_with_annotations():
    code, text = run({**GOOD, "PRG_FAIL_ON": "urgent"})
    assert code == EXIT_CONFIG
    assert text.startswith("::error")


def test_workflow_command_injection_is_neutralised():
    # A hostile input value tries to smuggle a second workflow command via a newline.
    code, text = run({**GOOD, "PRG_FAIL_ON": "x\n::set-output name=pwn::1"})
    assert code == EXIT_CONFIG
    # Two layers defend this: !r in the message and escape_workflow_data().
    # The property that matters: exactly one line, so no forged second command.
    assert len(text.strip().splitlines()) == 1


def test_escape_workflow_data():
    assert escape_workflow_data("a%b\r\nc") == "a%25b%0D%0Ac"
