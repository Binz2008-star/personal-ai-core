"""Model registry.

Which models exist, which one is active, and what each one's limits are.
Changing the Boss model is a registry change, not a code change (ADR-002).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..core.config import Settings
from ..core.errors import ConfigError, ModelNotFoundError


class ModelRole(str, Enum):
    BOSS = "boss"
    AUXILIARY = "auxiliary"


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """A model the Core may use.

    `context_window` lives here because the context budget must derive from the
    active model rather than a constant. The audited source hard-coded a 24000
    budget for a 32K model while the Boss model runs at 8192 -- a 3x overflow
    that fails as silent truncation (ADR-005).
    """

    id: str
    provider: str
    name: str
    context_window: int
    role: ModelRole = ModelRole.AUXILIARY
    evidence: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


class ModelRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ModelSpec] = {}
        self._active_id: str | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "ModelRegistry":
        registry = cls()
        registry.register(
            ModelSpec(
                id="model-001",
                provider="ollama",
                name=settings.boss_model,
                context_window=settings.boss_context_window,
                role=ModelRole.BOSS,
            )
        )
        registry.set_active("model-001")
        return registry

    def register(self, spec: ModelSpec) -> None:
        if spec.id in self._specs:
            raise ConfigError(f"model id already registered: {spec.id}")
        self._specs[spec.id] = spec

    def get(self, model_id: str) -> ModelSpec:
        try:
            return self._specs[model_id]
        except KeyError:
            raise ModelNotFoundError(f"unknown model id: {model_id}") from None

    def set_active(self, model_id: str) -> None:
        if model_id not in self._specs:
            raise ModelNotFoundError(f"unknown model id: {model_id}")
        self._active_id = model_id

    @property
    def active(self) -> ModelSpec:
        if self._active_id is None:
            raise ConfigError("no active model set")
        return self._specs[self._active_id]

    def boss(self) -> ModelSpec:
        for spec in self._specs.values():
            if spec.role is ModelRole.BOSS:
                return spec
        raise ModelNotFoundError("no model registered with role=boss")

    def list(self) -> list[ModelSpec]:
        return list(self._specs.values())
