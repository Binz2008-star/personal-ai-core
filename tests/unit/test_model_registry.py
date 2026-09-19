"""Registry tests: the model is replaceable through configuration alone."""
import pytest

from personal_ai_core.core.config import Settings
from personal_ai_core.core.errors import ConfigError, ModelNotFoundError
from personal_ai_core.runtime.model_registry import ModelRegistry, ModelRole, ModelSpec


def test_registry_from_settings_registers_the_boss_model():
    registry = ModelRegistry.from_settings(Settings.from_env({}))
    boss = registry.boss()
    assert boss.name == "huihui_ai/qwen2.5-abliterate:7b"
    assert boss.role is ModelRole.BOSS
    assert boss.provider == "ollama"
    assert boss.context_window == 8192


def test_active_model_is_the_boss_by_default():
    registry = ModelRegistry.from_settings(Settings.from_env({}))
    assert registry.active.id == registry.boss().id


def test_model_is_replaceable_without_code_change():
    # The whole point of ADR-002: swapping the model is registry configuration.
    registry = ModelRegistry.from_settings(
        Settings.from_env({"PAC_BOSS_MODEL": "some-other-model:8b"})
    )
    assert registry.active.name == "some-other-model:8b"


def test_registering_a_second_model_and_switching():
    registry = ModelRegistry.from_settings(Settings.from_env({}))
    registry.register(
        ModelSpec(id="model-002", provider="ollama", name="aux:3b", context_window=4096)
    )
    registry.set_active("model-002")
    assert registry.active.name == "aux:3b"
    # the boss role is unchanged by switching the active model
    assert registry.boss().name == "huihui_ai/qwen2.5-abliterate:7b"


def test_duplicate_registration_is_rejected():
    registry = ModelRegistry.from_settings(Settings.from_env({}))
    with pytest.raises(ConfigError):
        registry.register(
            ModelSpec(id="model-001", provider="ollama", name="dup", context_window=1)
        )


def test_unknown_model_raises():
    registry = ModelRegistry()
    with pytest.raises(ModelNotFoundError):
        registry.get("nope")
    with pytest.raises(ModelNotFoundError):
        registry.set_active("nope")
    with pytest.raises(ConfigError):
        registry.active
