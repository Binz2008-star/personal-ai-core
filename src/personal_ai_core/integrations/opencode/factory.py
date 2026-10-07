"""Composition root for the integration; the RC conversation path is unchanged."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from ...context import ScriptAwareTokenEstimator
from ...conversation.factory import _budget_policy, build_reply_redactor, describe_loaded, profile_budget
from ...core.config import Settings
from ...core.redaction import RedactionError
from ...identity import DefaultIdentityComposer
from ...runtime.model_registry import ModelRegistry
from ...runtime.ollama.provider import OllamaProvider
from .budget import OpenCodeContextBudget
from .service import IntegrationError, OpenCodeProviderService, StructuredModelProvider
from .store import OpenCodeSessionStore


def build_service(*, settings: Settings | None = None, profile_path: Path | None = None,
                  store_path: Path | None = None,
                  provider: StructuredModelProvider | None = None,
                  audit: Callable[[Mapping[str, Any]], None] | None = None,
                  audit_required: bool = False,
                  audit_warning: Callable[[str], None] | None = None) -> OpenCodeProviderService:
    settings = settings or Settings.from_env()
    profile = settings.profile
    if profile_path is not None:
        parts = []
        for file in (profile_path, profile_path.parent / "projects.md"):
            if file.is_file():
                try:
                    text = file.read_text(encoding="utf-8").strip()
                except (OSError, UnicodeDecodeError) as error:
                    raise IntegrationError("profile_unreadable", "The configured profile/projects files must be readable UTF-8 text.") from error
                if text:
                    parts.append(text)
        profile = "\n\n".join(parts)
    cost = profile_budget(profile, settings)
    if not cost.fits:
        raise IntegrationError("profile_too_large", f"The profile and projects cost about {cost.tokens} tokens; the limit is {cost.allowed}. Shorten them by about {cost.excess_chars} characters; nothing was truncated.")
    redactor = build_reply_redactor()
    try:
        profile = redactor.redact(profile).text
    except RedactionError as error:
        raise IntegrationError("profile_redaction_failed", "The profile could not be checked for secrets; it was not sent to the model.") from error
    identity = DefaultIdentityComposer(profile=profile)
    live = provider is None
    provider = provider or OllamaProvider(settings.ollama_host, timeout_seconds=settings.request_timeout_seconds)
    registry = ModelRegistry.from_settings(settings, provider=provider.name)
    estimator = ScriptAwareTokenEstimator()
    budget = OpenCodeContextBudget(estimator=estimator, policy=_budget_policy(identity, settings))

    def confirm_context() -> None:
        loaded = describe_loaded("ollama", ollama_host=settings.ollama_host,
                                 llamacpp_host="", model=registry.active.name)
        actual = loaded.get("context_length")
        if not loaded.get("probed") or not isinstance(actual, int):
            raise IntegrationError("context_window_unconfirmed", "PAC could not confirm the model's actual loaded context window.", 502)
        if actual < settings.boss_context_window:
            raise IntegrationError("context_window_not_honored", f"The model loaded {actual} context tokens, below PAC's configured {settings.boss_context_window}; the response was withheld.", 502)

    return OpenCodeProviderService(provider=provider, model=registry.active, identity=identity,
        budget=budget, redactor=redactor, sampling=settings.boss_sampling,
        store=OpenCodeSessionStore(store_path) if store_path is not None else None,
        confirm_context=confirm_context if live else None,
        audit=audit, audit_required=audit_required, audit_warning=audit_warning)
