"""Wiring for the Phase 1 slice.

The composition root: the one place that knows which concrete adapters satisfy
which contracts. Everything else receives protocols.
"""
from __future__ import annotations

from ..core.config import Settings
from ..persistence.in_memory import (
    InMemoryEventRepository,
    InMemoryMessageRepository,
    InMemorySessionRepository,
    InMemoryUserRepository,
)
from ..runtime.model_registry import ModelRegistry
from ..runtime.ollama.provider import OllamaProvider, Transport
from .service import ConversationService


def build_in_memory_service(
    settings: Settings | None = None,
    *,
    transport: Transport | None = None,
) -> tuple[ConversationService, InMemoryEventRepository]:
    """Build the slice against in-process storage.

    `transport` is injectable so the slice can be exercised end to end without
    a live Ollama server.
    """
    settings = settings or Settings.from_env()
    registry = ModelRegistry.from_settings(settings)
    provider = OllamaProvider(
        settings.ollama_host,
        timeout_seconds=settings.request_timeout_seconds,
        transport=transport,
    )
    events = InMemoryEventRepository()
    service = ConversationService(
        users=InMemoryUserRepository(),
        sessions=InMemorySessionRepository(),
        messages=InMemoryMessageRepository(),
        events=events,
        provider=provider,
        registry=registry,
    )
    return service, events
