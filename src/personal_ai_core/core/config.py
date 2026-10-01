"""Configuration.

The Boss model is configuration, never a literal in business logic
(ARCHITECTURE.md §4, ADR-002). This module is the one place its default name
appears.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

# Personal AI Core Boss model (ADR-002).
#
# Deliberately NOT qwen2.5:7b. That model is a `local-llm-rig` benchmark result
# -- it scored equally on that project's refusal set -- and a benchmark result
# is not a Core model decision. The two are kept distinct on purpose.
DEFAULT_BOSS_MODEL = "huihui_ai/qwen2.5-abliterate:7b"

# Context window for the Boss model as configured on the target rig. Consumed
# by the context budget (ADR-005); never assumed from another model's size.
DEFAULT_BOSS_CONTEXT_WINDOW = 8192

# Sampling for the Boss model: the generation config its model card publishes
# (temperature 0.7, top_p 0.8, top_k 20, repeat_penalty 1.05). Before
# 2026-10-01 Core sent none and the model ran on Ollama's defaults, whose wider
# top_k and top_p let a low-probability script through: on the rig, every
# contract failure was Chinese text inside an Arabic reply. Measured on
# contract_v1, two runs each: 21 and 19 PASS with these against 20 and 18
# without, and four Chinese-script failures against six. Owner decision
# 2026-10-01. A modest gain, recorded as one; it does not fix ground-decline-ar.
DEFAULT_BOSS_SAMPLING: Mapping[str, float | int] = MappingProxyType(
    {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "repeat_penalty": 1.05}
)

DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 120


@dataclass(frozen=True, slots=True)
class Settings:
    boss_model: str = DEFAULT_BOSS_MODEL
    boss_context_window: int = DEFAULT_BOSS_CONTEXT_WINDOW
    boss_sampling: Mapping[str, float | int] = field(
        default_factory=lambda: DEFAULT_BOSS_SAMPLING
    )
    ollama_host: str = DEFAULT_OLLAMA_HOST
    request_timeout_seconds: int = DEFAULT_REQUEST_TIMEOUT_SECONDS
    # The owner's profile TEXT, composed into every turn's identity message.
    # Not read from the environment here: where the file lives is the entry
    # point's decision (app/cli.py), as the database path is.
    profile: str = ""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        source = os.environ if env is None else env
        return cls(
            boss_model=source.get("PAC_BOSS_MODEL", DEFAULT_BOSS_MODEL),
            boss_context_window=int(
                source.get("PAC_BOSS_CONTEXT_WINDOW", DEFAULT_BOSS_CONTEXT_WINDOW)
            ),
            ollama_host=source.get("PAC_OLLAMA_HOST", DEFAULT_OLLAMA_HOST),
            request_timeout_seconds=int(
                source.get("PAC_REQUEST_TIMEOUT_SECONDS", DEFAULT_REQUEST_TIMEOUT_SECONDS)
            ),
        )
