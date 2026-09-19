"""Wiring.

The composition root: the one place that knows which concrete adapters satisfy
which contracts. Everything else receives protocols.

This is the only module permitted to import `knowledge`, `context`,
`persistence` and `runtime` together — that is what a composition root is for,
and `tests/unit/test_dependency_direction.py` exempts it by name rather than
by accident.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..context import (
    GreedyContextAssembler,
    ReserveBasedBudgetPolicy,
    ScriptAwareTokenEstimator,
)
from ..core.config import Settings
from ..knowledge import (
    FixedSizeChunker,
    HashingEmbeddingProvider,
    HybridRetriever,
    InMemoryChunkCatalog,
    InMemoryLexicalIndex,
    InMemoryVectorIndex,
    IngestionService,
)
from ..persistence.in_memory import (
    InMemoryEventRepository,
    InMemoryMessageRepository,
    InMemorySessionRepository,
    InMemoryUserRepository,
)
from ..runtime.model_registry import ModelRegistry
from ..runtime.ollama.provider import OllamaProvider, Transport
from .grounding import ContextBuilder
from .service import ConversationService


def build_in_memory_service(
    settings: Settings | None = None,
    *,
    transport: Transport | None = None,
) -> tuple[ConversationService, InMemoryEventRepository]:
    """Build the ungrounded slice against in-process storage.

    Unchanged from Phase 1, and deliberately so: adding retrieval must not
    alter a path that already worked. This one does not retrieve at all.

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


@dataclass(frozen=True, slots=True)
class GroundedSlice:
    """Everything a caller needs to use and inspect the grounded slice.

    The indexes and catalog are handed back rather than hidden because a
    retrieval system whose state cannot be inspected cannot be debugged, and
    the alternative is a caller reaching into private attributes.
    """

    service: ConversationService
    events: InMemoryEventRepository
    ingestion: IngestionService
    catalog: InMemoryChunkCatalog
    vector_index: InMemoryVectorIndex
    lexical_index: InMemoryLexicalIndex


def build_grounded_in_memory_service(
    settings: Settings | None = None,
    *,
    transport: Transport | None = None,
    evidence_limit: int = 5,
) -> GroundedSlice:
    """Build the slice with retrieval wired in.

    The budget derives from the **active model in the registry**, not from a
    constant: `ReserveBasedBudgetPolicy` is handed the spec on every turn, so
    changing the Boss model changes the budget with it (ADR-005).

    Everything here is in-memory and offline. The embedding provider is
    `HashingEmbeddingProvider`, which is a development and test implementation
    and not a semantic model — see `docs/PHASE_2_IMPLEMENTATION.md`. Swapping
    in a real one is a change to this function and to nothing above it.
    """
    settings = settings or Settings.from_env()
    registry = ModelRegistry.from_settings(settings)
    provider = OllamaProvider(
        settings.ollama_host,
        timeout_seconds=settings.request_timeout_seconds,
        transport=transport,
    )

    embedder = HashingEmbeddingProvider()
    vector_index = InMemoryVectorIndex(
        model_id=embedder.model_id, dimensions=embedder.dimensions
    )
    lexical_index = InMemoryLexicalIndex()
    catalog = InMemoryChunkCatalog()
    ingestion = IngestionService(
        chunker=FixedSizeChunker(),
        embedder=embedder,
        vector_index=vector_index,
        lexical_index=lexical_index,
        catalog=catalog,
    )
    estimator = ScriptAwareTokenEstimator()
    context_builder = ContextBuilder(
        retriever=HybridRetriever(
            embedder=embedder,
            vector_index=vector_index,
            lexical_index=lexical_index,
            catalog=catalog,
        ),
        assembler=GreedyContextAssembler(estimator),
        budget_policy=ReserveBasedBudgetPolicy(),
        estimator=estimator,
        limit=evidence_limit,
    )

    events = InMemoryEventRepository()
    service = ConversationService(
        users=InMemoryUserRepository(),
        sessions=InMemorySessionRepository(),
        messages=InMemoryMessageRepository(),
        events=events,
        provider=provider,
        registry=registry,
        context_builder=context_builder,
    )
    return GroundedSlice(
        service=service,
        events=events,
        ingestion=ingestion,
        catalog=catalog,
        vector_index=vector_index,
        lexical_index=lexical_index,
    )
