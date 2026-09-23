"""Configuration.

The Boss model is configuration, never a literal in business logic
(ARCHITECTURE.md §4, ADR-002). This module is the one place its default name
appears.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# Personal AI Core Boss model (ADR-002).
#
# Deliberately NOT qwen2.5:7b. That model is a `local-llm-rig` benchmark result
# -- it scored equally on that project's refusal set -- and a benchmark result
# is not a Core model decision. The two are kept distinct on purpose.
DEFAULT_BOSS_MODEL = "huihui_ai/qwen2.5-abliterate:7b"

# Context window for the Boss model as configured on the target rig. Consumed
# by the context budget (ADR-005); never assumed from another model's size.
DEFAULT_BOSS_CONTEXT_WINDOW = 8192

DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 120


@dataclass(frozen=True, slots=True)
class Settings:
    boss_model: str = DEFAULT_BOSS_MODEL
    boss_context_window: int = DEFAULT_BOSS_CONTEXT_WINDOW
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
