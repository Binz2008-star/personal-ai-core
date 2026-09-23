"""Wiring.

The composition root: the one place that knows which concrete adapters satisfy
which contracts. Everything else receives protocols.

This is the only module permitted to import `knowledge`, `context`,
`persistence` and `runtime` together — that is what a composition root is for,
and `tests/unit/test_dependency_direction.py` exempts it by name rather than
by accident.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..context import (
    HybridContextAssembler,
    ReserveBasedBudgetPolicy,
    ScriptAwareTokenEstimator,
)
from ..core.config import Settings
from ..core.memory import MemoryReader
from ..identity import DefaultIdentityComposer
from ..memory import SimpleMemoryRetriever
from ..persistence.memory_store import InMemoryMemoryRepository
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
from ..persistence.sqlite import (
    SqliteEventRepository,
    SqliteMessageRepository,
    SqliteSessionRepository,
    SqliteUserRepository,
    connect,
)
from ..runtime.model_registry import ModelRegistry
from ..runtime.ollama.provider import OllamaProvider, Transport
from .grounding import ContextBuilder, RenderedEvidenceCost
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
    # The identity share is funded from the composed text, measured by the
    # same estimator the rest of the budget uses. ADR-005: a budget is derived
    # and its inputs recorded. Zero was the honest figure while identity/ did
    # not exist; any constant would be an invented one now that it does.
    identity = DefaultIdentityComposer()
    budget_policy = ReserveBasedBudgetPolicy(
        identity_reserve=identity.tokens(ScriptAwareTokenEstimator())
    )
    # ADR-011 prerequisite B: this slice retrieves nothing, so it has no
    # allocation to read the reserve from. It still gets the policy, because
    # an output limit that applies only where retrieval is wired is not a
    # limit -- and this is the slice the live smoke test exercises.
    service = ConversationService(
        users=InMemoryUserRepository(),
        sessions=InMemorySessionRepository(),
        messages=InMemoryMessageRepository(),
        events=events,
        provider=provider,
        registry=registry,
        budget_policy=budget_policy,
        identity=identity,
    )
    return service, events


@dataclass(frozen=True, slots=True)
class PersistentSlice:
    """The ungrounded slice on a durable store, plus the handles to inspect it.

    The connection is handed back because the caller opened a file and is the
    one who must close it. A factory that hides an open file handle makes the
    caller responsible for a resource it cannot see.
    """

    service: ConversationService
    events: SqliteEventRepository
    connection: sqlite3.Connection

    def close(self) -> None:
        self.connection.close()


def build_persistent_service(
    settings: Settings | None = None,
    *,
    database: str | Path,
    transport: Transport | None = None,
) -> PersistentSlice:
    """The same slice as `build_in_memory_service`, on SQLite (ADR-010 D+B).

    Identical in every respect a caller can observe except one: it is still
    there after the process exits. Everything above `core.contracts` is
    unchanged, which is the substitution that boundary was built for and this
    is its first real exercise.

    `database` is REQUIRED and has no default. A default would have to be a
    path -- some directory under the user's home -- and a library that writes
    to a place the caller did not name is a library that loses data somewhere
    the caller does not look. The entry point decides where the file lives;
    this only decides what goes in it.

    Knowledge is not persisted and this slice does not retrieve, which are the
    same decision seen from two sides: chunks and vectors are derived (ADR-010
    R4) and rebuilt by re-ingestion, so a durable grounded slice needs an
    answer to "when is the corpus re-ingested?" that nothing has given yet.
    """
    settings = settings or Settings.from_env()
    registry = ModelRegistry.from_settings(settings)
    provider = OllamaProvider(
        settings.ollama_host,
        timeout_seconds=settings.request_timeout_seconds,
        transport=transport,
    )
    connection = connect(database)
    events = SqliteEventRepository(connection)
    identity = DefaultIdentityComposer()
    budget_policy = ReserveBasedBudgetPolicy(
        identity_reserve=identity.tokens(ScriptAwareTokenEstimator())
    )
    service = ConversationService(
        users=SqliteUserRepository(connection),
        sessions=SqliteSessionRepository(connection),
        messages=SqliteMessageRepository(connection),
        events=events,
        provider=provider,
        registry=registry,
        budget_policy=budget_policy,
        identity=identity,
    )
    return PersistentSlice(service=service, events=events, connection=connection)


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
    # Present only when `enable_memory=True`. Handed back for the same
    # reason the indexes are: a recall path whose stored state cannot be
    # inspected cannot be debugged. The repository is the write side and is
    # reachable only by a caller that also builds an ExperiencePipeline --
    # the conversation path never sees it.
    memories: InMemoryMemoryRepository | None = None


def build_grounded_in_memory_service(
    settings: Settings | None = None,
    *,
    transport: Transport | None = None,
    evidence_limit: int = 5,
    enable_memory: bool = False,
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

    # One composer and one policy instance, each handed to everything that
    # needs it. Two policy instances would be two sources for the same number;
    # two composers would be two sources for the text the first was costed
    # from.
    identity = DefaultIdentityComposer()
    budget_policy = ReserveBasedBudgetPolicy(
        identity_reserve=identity.tokens(ScriptAwareTokenEstimator())
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

    # Recall is opt-in. When it is off, no repository exists and no reader
    # is constructed, so there is nothing for the conversation path to
    # reach even by accident.
    memories: InMemoryMemoryRepository | None = None
    memory_retriever = None
    if enable_memory:
        memories = InMemoryMemoryRepository()
        # The reader is wrapped here, in the composition root, and only
        # around a real repository. A SealedMemoryStore is never wrapped:
        # the seal belongs on the write path, and recall reaches a
        # different object entirely.
        memory_retriever = SimpleMemoryRetriever(
            reader=MemoryReader(source=memories)
        )

    context_builder = ContextBuilder(
        retriever=HybridRetriever(
            embedder=embedder,
            vector_index=vector_index,
            lexical_index=lexical_index,
            catalog=catalog,
        ),
        # F-4: charge evidence as rendered, not as bare text. Without this
        # the grounding message overruns the budget it was assembled against.
        assembler=HybridContextAssembler(
            estimator, rendered_cost=RenderedEvidenceCost(estimator)
        ),
        budget_policy=budget_policy,
        estimator=estimator,
        memory_retriever=memory_retriever,
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
        budget_policy=budget_policy,
        identity=identity,
        context_builder=context_builder,
    )
    return GroundedSlice(
        service=service,
        events=events,
        ingestion=ingestion,
        catalog=catalog,
        vector_index=vector_index,
        lexical_index=lexical_index,
        memories=memories,
    )
