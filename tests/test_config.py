"""Configuration tests, including the Boss model guarantee."""
from personal_ai_core.core.config import DEFAULT_BOSS_MODEL, Settings


def test_boss_model_default_is_the_abliterate_model():
    assert Settings.from_env({}).boss_model == "huihui_ai/qwen2.5-abliterate:7b"


def test_boss_model_default_is_not_the_benchmark_model():
    # qwen2.5:7b is a local-llm-rig benchmark result, not the Core model (ADR-002).
    assert DEFAULT_BOSS_MODEL != "qwen2.5:7b"
    assert Settings.from_env({}).boss_model != "qwen2.5:7b"


def test_boss_context_window_matches_the_configured_rig():
    # Not the audited source's 24000 (ADR-005).
    settings = Settings.from_env({})
    assert settings.boss_context_window == 8192
    assert settings.boss_context_window != 24000


def test_settings_are_env_overridable():
    settings = Settings.from_env(
        {"PAC_BOSS_MODEL": "other:7b", "PAC_BOSS_CONTEXT_WINDOW": "4096"}
    )
    assert settings.boss_model == "other:7b"
    assert settings.boss_context_window == 4096
