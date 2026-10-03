import pytest

from pr_guardian.config import (
    DEFAULT_FAIL_ON,
    DEFAULT_MAX_FILES,
    DEFAULT_MODEL,
    Config,
    ConfigError,
)


def env(**overrides: str) -> dict[str, str]:
    base = {"PRG_GITHUB_TOKEN": "ghs_dummy_token_for_tests"}
    base.update({f"PRG_{k.upper()}": v for k, v in overrides.items()})
    return base


def test_defaults_are_rules_only():
    cfg = Config.from_env(env())
    assert cfg.mode == "rules-only"
    assert not cfg.ai_enabled
    assert cfg.model == DEFAULT_MODEL
    assert cfg.fail_on == DEFAULT_FAIL_ON
    assert cfg.max_files == DEFAULT_MAX_FILES
    assert cfg.dry_run is False
    assert cfg.paths == ()


def test_empty_api_key_means_rules_only():
    # Composite actions pass '' for an unset optional input; that must not
    # be mistaken for "AI enabled".
    assert Config.from_env(env(anthropic_api_key="   ")).mode == "rules-only"


def test_api_key_enables_ai():
    cfg = Config.from_env(env(anthropic_api_key="not-a-real-key"))
    assert cfg.mode == "rules+ai"


def test_fail_on_is_case_insensitive():
    assert Config.from_env(env(fail_on="CRITICAL")).fail_on == "critical"
    assert Config.from_env(env(fail_on="none")).fail_on == "none"


def test_paths_split_on_commas_and_newlines():
    cfg = Config.from_env(env(paths="infra/**/*.tf, k8s/*.yaml\n.github/workflows/*.yml\n"))
    assert cfg.paths == ("infra/**/*.tf", "k8s/*.yaml", ".github/workflows/*.yml")


@pytest.mark.parametrize("bad", ["/etc/passwd", "../outside/*.tf", "a/../../b"])
def test_unsafe_paths_rejected(bad):
    with pytest.raises(ConfigError):
        Config.from_env(env(paths=bad))


def test_all_problems_reported_together():
    with pytest.raises(ConfigError) as exc:
        Config.from_env({"PRG_FAIL_ON": "urgent", "PRG_MAX_FILES": "abc", "PRG_DRY_RUN": "yes"})
    # token missing + fail-on + max-files + dry-run
    assert len(exc.value.problems) == 4


@pytest.mark.parametrize("value", ["0", "501", "-3"])
def test_max_files_bounds(value):
    with pytest.raises(ConfigError):
        Config.from_env(env(max_files=value))


def test_dry_run_is_strict():
    assert Config.from_env(env(dry_run="TRUE")).dry_run is True
    assert Config.from_env(env(dry_run="false")).dry_run is False
    with pytest.raises(ConfigError):
        Config.from_env(env(dry_run="1"))


def test_secrets_never_appear_in_repr():
    cfg = Config.from_env(env(anthropic_api_key="super-secret-value"))
    text = repr(cfg)
    assert "super-secret-value" not in text
    assert "ghs_dummy_token_for_tests" not in text
