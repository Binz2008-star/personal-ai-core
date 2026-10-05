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
from typing import TYPE_CHECKING, Any, Callable, Mapping

from ..agent import (
    Checkpoints,
    FetchUrl,
    RiskPolicy,
    SecretShapeRedactor,
    Shell,
    ToolExecutor,
    WebSearch,
    Workspace,
    default_tools,
)
from ..agent.environment import EnvironmentContext
from ..agent.executor import Confirm
from ..agent.loop import AgentLoop
from ..agent.web import Fetch
from ..context import (
    PatternSecretRedactor,
    HybridContextAssembler,
    ReserveBasedBudgetPolicy,
    ScriptAwareTokenEstimator,
)
from ..core.config import Settings
from ..core.contracts import (
    EventRepository,
    MemoryStore,
    ModelProvider,
    SecretRedactor,
    SessionRepository,
)
from ..core.feedback import FEEDBACK_EVENT_TYPE, feedback_record_from_event
from ..core.memory import MemoryReader
from ..core.observation import Observation, UnobservedFeedback
from ..identity import RESPONSE_POLICY, DefaultIdentityComposer
from ..core.identity import ResponsePolicy
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
from ..learning import FeedbackRecorder, derive_observations
from ..persistence.in_memory import (
    InMemoryEventRepository,
    InMemoryMessageRepository,
    InMemorySessionRepository,
    InMemoryUserRepository,
)
from ..persistence.sqlite import (
    SqliteEventRepository,
    SqliteFeedbackRepository,
    SqliteMemoryRepository,
    SqliteMessageRepository,
    SqliteSessionRepository,
    SqliteUserRepository,
    connect,
)
from ..persistence.sqlite import SchemaVersionMismatch as SqliteSchemaVersionMismatch
from ..persistence.postgres import (
    DatabaseIdentity,
    PostgresEventRepository,
    PostgresMemoryRepository,
    PostgresMessageRepository,
    PostgresSessionRepository,
    PostgresUserRepository,
    SchemaIntent,
    connect as server_connect,
)
from ..runtime.model_registry import ModelRegistry
from ..runtime.llamacpp import GRAMMARS, LlamaCppProvider
from ..runtime.llamacpp.status import loaded_status as llamacpp_loaded_status
from ..runtime.ollama.status import http_probe
from ..runtime.ollama.status import loaded_status as ollama_loaded_status
# `http_transport` is re-exported for the benchmark runner (ADR-022), which
# wraps it to time each model call and may not import `runtime/` itself.
from ..runtime.ollama.provider import OllamaProvider, Transport
from ..runtime.ollama.provider import http_transport as http_transport
from .grounding import ContextBuilder, RenderedEvidenceCost
from .language_guard import GUARD_NOTE
from .service import ConversationService

if TYPE_CHECKING:
    # The relative import keeps the driver out of this module's namespace:
    # the dependency-direction gate only counts absolute imports, and the
    # composition root may name adapters but must not name psycopg itself.
    from ..persistence.postgres import Connection as ServerConnection


def _budget_policy(identity: DefaultIdentityComposer, settings: Settings) -> ReserveBasedBudgetPolicy:
    """Reserves funded from measured text: the identity, and ADR-019's note.

    The note is sent only on a retry, but a retry reuses the turn's prompt, so
    its tokens are reserved on every turn while the guard is on, as their own
    named share: a retry can then never overflow the window the turn was
    assembled against, and the record says what the reserve is for.
    """
    estimator = ScriptAwareTokenEstimator()
    return ReserveBasedBudgetPolicy(
        identity_reserve=identity.tokens(estimator),
        guard_reserve=estimator.estimate(GUARD_NOTE) if settings.language_guard else 0,
    )


def build_llamacpp_provider(
    host: str,
    *,
    grammar: str = "none",
    timeout_seconds: int = 120,
    transport: Transport | None = None,
) -> ModelProvider:
    """Experiment: the llama.cpp adapter, for the evaluation harness only.

    Lives here because naming a concrete adapter is the composition root's
    job; `app/` may not import `runtime/`. `pac` never calls it. `grammar`
    names one of `runtime.llamacpp.GRAMMARS`.
    """
    if grammar not in GRAMMARS:
        raise ValueError(f"unknown grammar: {grammar}")
    return LlamaCppProvider(
        host, timeout_seconds=timeout_seconds, transport=transport, grammar=GRAMMARS[grammar]
    )


LLAMACPP_GRAMMARS: tuple[str, ...] = tuple(GRAMMARS)


def describe_loaded(
    runtime: str,
    *,
    ollama_host: str,
    llamacpp_host: str,
    model: str,
    probe: Callable[..., Mapping[str, Any]] | None = None,
    live: bool = True,
) -> dict[str, Any]:
    """What the model server reports it has loaded, for the evaluation header.

    The endpoints are each adapter's detail; this names which adapter to ask.
    `probe` replaces the HTTP GET in tests. Without one, a live run asks the
    real server and a run on a test transport asks nothing.
    """
    chosen = probe if probe is not None else (http_probe if live else None)
    if runtime == "llamacpp":
        return llamacpp_loaded_status(chosen, llamacpp_host)
    return ollama_loaded_status(chosen, ollama_host, model)


def default_response_policy() -> ResponsePolicy:
    """The policy every production builder composes (ADR-012).

    Exposed so the evaluation harness can vary ONE field of it in an
    experiment run without importing `identity/`, which `app/` may not.
    `response_policy=` on the in-memory builders exists for that harness
    only: `pac` never passes it, so production composes this default.
    """
    return RESPONSE_POLICY


def build_in_memory_service(
    settings: Settings | None = None,
    *,
    transport: Transport | None = None,
    response_policy: ResponsePolicy | None = None,
    provider: ModelProvider | None = None,
) -> tuple[ConversationService, InMemoryEventRepository]:
    """Build the ungrounded slice against in-process storage.

    Unchanged from Phase 1, and deliberately so: adding retrieval must not
    alter a path that already worked. This one does not retrieve at all.

    `transport` is injectable so the slice can be exercised end to end without
    a live Ollama server.
    """
    settings = settings or Settings.from_env()
    provider = provider or OllamaProvider(
        settings.ollama_host,
        timeout_seconds=settings.request_timeout_seconds,
        transport=transport,
    )
    registry = ModelRegistry.from_settings(settings, provider=provider.name)
    events = InMemoryEventRepository()
    # The identity share is funded from the composed text, measured by the
    # same estimator the rest of the budget uses. ADR-005: a budget is derived
    # and its inputs recorded. Zero was the honest figure while identity/ did
    # not exist; any constant would be an invented one now that it does.
    identity = DefaultIdentityComposer(
        profile=settings.profile, policy=response_policy or RESPONSE_POLICY
    )
    budget_policy = _budget_policy(identity, settings)
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
        estimator=ScriptAwareTokenEstimator(),
        identity=identity,
        sampling=settings.boss_sampling,
        language_guard=settings.language_guard,
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
    # The way a judgement of an earlier reply enters the store (ADR-017 §3.1).
    # Beside the service, not inside it: the recorder writes FEEDBACK_RECORDED
    # events through `FeedbackRepository.append` and reaches no memory store,
    # so the conversation path is exactly what it was without it.
    feedback: FeedbackRecorder
    # Read-only (ADR-017 Unit 2, first consumer): what the feedback recorded in
    # one session amounts to -- `derive_observations` over that session's
    # events. It writes nothing and changes no turn; `pac --observations`
    # prints it so the owner can see what a judgement was taken to mean.
    observations: Callable[[str], ObservationReport]
    # Present only when built with `grounded=True`: the way in for documents.
    # The indexes behind it live in process memory and are gone at exit --
    # see `build_persistent_service`.
    ingestion: IngestionService | None = None
    # Present only when built with `grounded=True`: the durable memory store
    # the recall reader is composed over. The write side is reachable only by
    # a caller that also builds an ExperiencePipeline -- the conversation
    # path never sees it.
    memories: SqliteMemoryRepository | None = None

    def close(self) -> None:
        self.connection.close()


ObservationReport = tuple[tuple[Observation, ...], tuple[UnobservedFeedback, ...]]


def _observations_over(events: EventRepository) -> Callable[[str], ObservationReport]:
    """`derive_observations` for one session, fed in the store's own order.

    Feedback is stored as FEEDBACK_RECORDED events in the same table, so one
    `list_for_session` read gives both inputs in `seq` order -- the order
    ADR-017 D7 makes a precondition -- and no new repository method is needed.
    """

    def observe(session_id: str) -> ObservationReport:
        session_events = list(events.list_for_session(session_id))
        feedback = [
            feedback_record_from_event(event)
            for event in session_events
            if event.type is FEEDBACK_EVENT_TYPE
        ]
        return derive_observations(session_events, feedback)

    return observe


@dataclass(frozen=True, slots=True)
class _KnowledgeStack:
    ingestion: IngestionService
    catalog: InMemoryChunkCatalog
    vector_index: InMemoryVectorIndex
    lexical_index: InMemoryLexicalIndex
    context_builder: ContextBuilder


def _knowledge_stack(
    budget_policy: ReserveBasedBudgetPolicy,
    *,
    evidence_limit: int,
    memory_retriever: SimpleMemoryRetriever | None = None,
) -> _KnowledgeStack:
    """The retrieval half of a grounded slice, built once for every store.

    One function rather than two copies, because the grounded slice now exists
    on two stores and the retrieval half must be the same on both: a copy is
    where the F-4 wiring would have been fixed on one path and missed on the
    other.

    Everything here is in-memory and offline. The embedding provider is
    `HashingEmbeddingProvider`, which is a development and test implementation
    and not a semantic model -- see `docs/PHASE_2_IMPLEMENTATION.md`. Swapping
    in a real one is a change to this function and to nothing above it.
    """
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
    # ADR-018: ONE redactor instance, handed to both the builder that renders
    # the evidence and the cost that prices it, so the budget is charged on
    # exactly the text the model receives (§3.2).
    redactor = PatternSecretRedactor()
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
            estimator, rendered_cost=RenderedEvidenceCost(estimator, redactor)
        ),
        budget_policy=budget_policy,
        estimator=estimator,
        redactor=redactor,
        memory_retriever=memory_retriever,
        limit=evidence_limit,
    )
    return _KnowledgeStack(
        ingestion=ingestion,
        catalog=catalog,
        vector_index=vector_index,
        lexical_index=lexical_index,
        context_builder=context_builder,
    )


def _session_owner_for(sessions: SessionRepository) -> Callable[[str], str | None]:
    """`session_id -> user_id`, or None when the session is unknown.

    Ownership is derived, never stored (ADR-014): user-scoped recall resolves
    each session to its owner through the session store the composition root
    already holds. The same resolver serves both the anchor session and the
    record's own session, so no caller ever names a user directly.
    """

    def resolve(session_id: str) -> str | None:
        session = sessions.get(session_id)
        return session.user_id if session is not None else None

    return resolve


def _grounding_for_durable(
    budget_policy: ReserveBasedBudgetPolicy,
    *,
    evidence_limit: int,
    memories: MemoryStore | None,
    sessions: SessionRepository,
) -> _KnowledgeStack | None:
    """The retrieval half of a grounded DURABLE slice, one build for every
    durable store.

    Both durable builders -- SQLite and the server backend -- compose recall
    over the store the same way: the reader resolves ownership from the
    session store (ADR-014) and the deterministic retriever ranks what it
    admits (ADR-015). One function rather than two copies, because that is
    exactly where the F-4 lesson would otherwise be re-learned: wired once
    here, it cannot be fixed on one store's path and missed on the other.

    `memories=None` is the ungrounded slice: no retriever, no stack.
    """
    if memories is None:
        return None
    return _knowledge_stack(
        budget_policy,
        evidence_limit=evidence_limit,
        memory_retriever=SimpleMemoryRetriever(
            reader=MemoryReader(
                source=memories, session_owner=_session_owner_for(sessions)
            )
        ),
    )


def build_persistent_service(
    settings: Settings | None = None,
    *,
    database: str | Path,
    transport: Transport | None = None,
    grounded: bool = False,
    evidence_limit: int = 5,
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

    Knowledge is not persisted: chunks and vectors are derived (ADR-010 R4)
    and rebuilt by re-ingestion. That left one question open -- "when is the
    corpus re-ingested?" -- and F-2 answers it: **on every run, by the caller,
    through `ingestion`**. With `grounded=True` the slice retrieves; the
    conversation is durable and the corpus is not, and the entry point
    re-reads the documents it was given each time it starts.

    What that costs, stated rather than hidden: a CONTEXT_ASSEMBLED event
    records `chunk_ids`, and a chunk id names a chunk in the run that produced
    it. The durable citation is the one in the prompt -- source URI and
    character range -- which re-ingesting the same file reproduces.

    With `grounded=True`, memory recall is composed over the durable store:
    the reader resolves ownership from the session store (ADR-014) and the
    same deterministic retriever the in-memory slice uses ranks what it
    admits (ADR-015). Memories become durable when a caller runs an
    `ExperiencePipeline` over the same database; recall on this path reads
    whatever earlier runs promoted. Turns recall session-scoped by default;
    user scope is explicit on the query.
    """
    settings = settings or Settings.from_env()
    provider = OllamaProvider(
        settings.ollama_host,
        timeout_seconds=settings.request_timeout_seconds,
        transport=transport,
    )
    registry = ModelRegistry.from_settings(settings, provider=provider.name)
    connection = connect(database)
    events = SqliteEventRepository(connection)
    sessions = SqliteSessionRepository(connection)
    # The durable memory store behind the composition. Recall reads through
    # a reader; the write side stays with ExperiencePipeline and the caller
    # that builds one over the same database.
    memories = SqliteMemoryRepository(connection)
    identity = DefaultIdentityComposer(profile=settings.profile)
    budget_policy = _budget_policy(identity, settings)
    stack = _grounding_for_durable(
        budget_policy,
        evidence_limit=evidence_limit,
        memories=memories if grounded else None,
        sessions=sessions,
    )
    service = ConversationService(
        users=SqliteUserRepository(connection),
        sessions=sessions,
        messages=SqliteMessageRepository(connection),
        events=events,
        provider=provider,
        registry=registry,
        budget_policy=budget_policy,
        estimator=ScriptAwareTokenEstimator(),
        identity=identity,
        context_builder=stack.context_builder if stack is not None else None,
        sampling=settings.boss_sampling,
        language_guard=settings.language_guard,
    )
    return PersistentSlice(
        service=service,
        events=events,
        connection=connection,
        feedback=FeedbackRecorder(SqliteFeedbackRepository(connection)),
        observations=_observations_over(events),
        ingestion=stack.ingestion if stack is not None else None,
        memories=memories if grounded else None,
    )


@dataclass(frozen=True, slots=True)
class ServerSlice:
    """The ungrounded slice on the server database (ADR-016), plus the handles.

    Same resource rule as `PersistentSlice`, one step up: the caller named a
    URL, so the caller opened the connection -- and is the one who must close
    it. A factory that hid an open server connection would make the caller
    responsible for a resource it cannot see.
    """

    service: ConversationService
    events: PostgresEventRepository
    connection: ServerConnection
    # Present only when built with `grounded=True`, exactly as on
    # `PersistentSlice`: the way in for documents (the indexes behind it live
    # in process memory and are gone at exit), and the durable memory store
    # the recall reader is composed over.
    ingestion: IngestionService | None = None
    memories: PostgresMemoryRepository | None = None

    def close(self) -> None:
        self.connection.close()


def build_server_service(
    settings: Settings | None = None,
    *,
    database_url: str,
    intent: SchemaIntent,
    identity: DatabaseIdentity,
    transport: Transport | None = None,
    grounded: bool = False,
    evidence_limit: int = 5,
) -> ServerSlice:
    """The same slice as `build_persistent_service`, on the database ADR-016
    chose (Neon/PostgreSQL) instead of a local file.

    Identical in every respect a caller can observe except where the rows
    live: over the wire, in the database the caller names. Everything above
    `core.contracts` is unchanged, which is the substitution that boundary
    was built for -- this builder is its second real exercise.

    `database_url`, `intent` and `identity` are all REQUIRED and none has a
    default. The first names the database; the second says whether this call is
    creating the schema or using one that exists; the third is the caller's
    independent statement of which host, port and database it believes it is
    reaching. `connect` checks all three against what the server reports before
    it runs a single DDL statement, and refuses on any mismatch.

    `identity` is not derived from `database_url` here, and must not be by a
    caller either. A confirmation read off the URL the code is about to dial
    restates that URL: it would agree with itself by construction, and the
    check it feeds would be incapable of failing. The entry point's job is to
    decide where the URL comes from (e.g. `DATABASE_URL`); the value that
    identifies the database is a separate piece of knowledge, and it is the
    owner's to supply.

    The connection is opened lazily with respect to the driver: a machine without
    the `server` extra gets a RuntimeError that says how to install one, and a
    SQLite-only install neither imports nor pays for psycopg.

    Knowledge is not persisted, exactly as on SQLite (ADR-010 R4): chunks and
    vectors are derived and rebuilt by re-ingestion. With `grounded=True` the
    slice retrieves through the durable server store with ownership resolved
    from the session store (ADR-014/015), so a conversation is durable and a
    corpus is not, just as on the file store.
    """
    settings = settings or Settings.from_env()
    provider = OllamaProvider(
        settings.ollama_host,
        timeout_seconds=settings.request_timeout_seconds,
        transport=transport,
    )
    registry = ModelRegistry.from_settings(settings, provider=provider.name)
    connection = server_connect(database_url, intent=intent, identity=identity)
    events = PostgresEventRepository(connection)
    sessions = PostgresSessionRepository(connection)
    # The durable server store behind the composition; recall reads through a
    # reader, and the write side stays with ExperiencePipeline and the caller
    # that builds one over the same database.
    memories = PostgresMemoryRepository(connection)
    # Named `identity_composer`, not `identity`: this function's `identity`
    # parameter is the DATABASE identity, and binding the identity composer to
    # the same name would shadow it for the rest of the body. It happened to be
    # harmless while `connect` was the only reader, because that call comes
    # first -- which is exactly the kind of accident that stops being harmless
    # the moment someone moves a line.
    identity_composer = DefaultIdentityComposer(profile=settings.profile)
    budget_policy = _budget_policy(identity_composer, settings)
    stack = _grounding_for_durable(
        budget_policy,
        evidence_limit=evidence_limit,
        memories=memories if grounded else None,
        sessions=sessions,
    )
    service = ConversationService(
        users=PostgresUserRepository(connection),
        sessions=sessions,
        messages=PostgresMessageRepository(connection),
        events=events,
        provider=provider,
        registry=registry,
        budget_policy=budget_policy,
        estimator=ScriptAwareTokenEstimator(),
        identity=identity_composer,
        context_builder=stack.context_builder if stack is not None else None,
        sampling=settings.boss_sampling,
        language_guard=settings.language_guard,
    )
    return ServerSlice(
        service=service,
        events=events,
        connection=connection,
        ingestion=stack.ingestion if stack is not None else None,
        memories=memories if grounded else None,
    )


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
    response_policy: ResponsePolicy | None = None,
    provider: ModelProvider | None = None,
) -> GroundedSlice:
    """Build the slice with retrieval wired in.

    The budget derives from the **active model in the registry**, not from a
    constant: `ReserveBasedBudgetPolicy` is handed the spec on every turn, so
    changing the Boss model changes the budget with it (ADR-005).

    Everything here is in-memory and offline; see `_knowledge_stack` for what
    the retrieval half is and is not.
    """
    settings = settings or Settings.from_env()
    provider = provider or OllamaProvider(
        settings.ollama_host,
        timeout_seconds=settings.request_timeout_seconds,
        transport=transport,
    )
    registry = ModelRegistry.from_settings(settings, provider=provider.name)

    # One composer and one policy instance, each handed to everything that
    # needs it. Two policy instances would be two sources for the same number;
    # two composers would be two sources for the text the first was costed
    # from.
    identity = DefaultIdentityComposer(
        profile=settings.profile, policy=response_policy or RESPONSE_POLICY
    )
    budget_policy = _budget_policy(identity, settings)

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

    stack = _knowledge_stack(
        budget_policy,
        evidence_limit=evidence_limit,
        memory_retriever=memory_retriever,
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
        estimator=ScriptAwareTokenEstimator(),
        identity=identity,
        context_builder=stack.context_builder,
        sampling=settings.boss_sampling,
        language_guard=settings.language_guard,
    )
    return GroundedSlice(
        service=service,
        events=events,
        ingestion=stack.ingestion,
        catalog=stack.catalog,
        vector_index=stack.vector_index,
        lexical_index=stack.lexical_index,
        memories=memories,
    )


@dataclass(frozen=True, slots=True)
class AgentSlice:
    """The agent, and the handles a caller needs around it.

    `checkpoints` is handed back so the caller can offer to roll back what a
    run changed; `executor.audit` holds every request, allowed or not.
    """

    loop: AgentLoop
    executor: ToolExecutor
    checkpoints: Checkpoints
    workspace: Workspace


def build_agent(
    settings: Settings | None = None,
    *,
    workspace: str | Path,
    transport: Transport | None = None,
    confirm: Confirm | None = None,
    events: EventRepository | None = None,
    database: str | Path | None = None,
    session_exists: Callable[[str], bool] | None = None,
    web_fetch: Fetch | None = None,
    environment_context: bool = False,
    lenient_protocol: bool = False,
    native_tools: bool = False,
    verify_completion: bool = False,
) -> AgentSlice:
    """The agent of AGENT_ARCHITECTURE.md, wired to the Boss model.

    `workspace` is REQUIRED, for the reason `database` is required on the
    persistent slice: an agent that edits files wherever it was started edits
    files the user did not choose. `confirm` answers ASK; without it, every
    HIGH and CRITICAL request is refused -- the safe default, not a degraded
    one. The same identity contract every conversation turn carries is the
    agent's first message: rule 2 (no action with external effect without
    confirmation) and rule 5 (retrieved text is data) apply to it too.

    `database` is the file the Core's own records live in. It is reserved
    from the workspace -- with its SQLite companions -- so no tool can read,
    overwrite or delete it even when the workspace contains it (F-1).

    `session_exists` is REQUIRED whenever `events` is given -- pass the
    conversation service's `has_session`. Events recorded against a session
    nobody started are evidence about nothing (F-2); with it, the agent
    refuses an unknown session exactly as `send` does.

    `environment_context` (ADR-023 §2.1, unit 2) is OFF unless asked for: when on,
    each run starts with facts the program read from the machine and the workspace
    (system, shell, runtime, git, the supported test command, what `run_command`
    accepts), counted against a fixed budget. Off, the agent is what it was.

    `lenient_protocol` (ADR-024 unit A) is OFF unless asked for: when on, three
    reply shapes the strict protocol refuses are read -- a Python literal, a
    numeric answer, a string `arguments` for a tool with one required field --
    and each is recorded on the outcome. Nothing the model sees changes.

    `native_tools` (ADR-025) is OFF unless asked for: when on, the tools are
    declared through the Boss model's native tool interface and its calls are
    read from the response; each call still becomes an ordinary `ToolRequest`
    through the policy and the executor. The system text loses its JSON-format
    lines. It cannot be combined with `lenient_protocol`.

    `verify_completion` (ADR-023 unit 3, §2.3 `tests_passed`) is OFF unless asked
    for: when on, under a contract that requires action and names a test command,
    the model is told the command, and an answer is accepted only once that exact
    command has passed after the last change; a refused answer costs one failure.
    """
    if events is not None and session_exists is None:
        raise ValueError(
            "an agent that records events must be able to check the session: "
            "pass session_exists"
        )
    settings = settings or Settings.from_env()
    provider = OllamaProvider(
        settings.ollama_host,
        timeout_seconds=settings.request_timeout_seconds,
        transport=transport,
    )
    registry = ModelRegistry.from_settings(settings, provider=provider.name)
    sandbox = Workspace(
        Path(workspace), reserved=() if database is None else (Path(database),)
    )
    checkpoints = Checkpoints(sandbox)
    # The workspace tools, then the reach beyond it: the owner's shell and the
    # web. `shell` and `fetch_url` are HIGH -- asked for every time -- and
    # `web_search` sends only its query, only to the search engine.
    tools = [
        *default_tools(sandbox, checkpoints),
        Shell(sandbox),
        WebSearch(web_fetch),
        FetchUrl(web_fetch),
    ]
    executor = ToolExecutor(tools, RiskPolicy(), confirm=confirm)
    # N2: one composer and one estimator, so the identity reserve is funded
    # from the very text the agent sends. No guard reserve: the agent has no
    # language guard, and reserving for one would shrink every run for nothing.
    estimator = ScriptAwareTokenEstimator()
    identity = DefaultIdentityComposer(profile=settings.profile)
    loop = AgentLoop(
        provider=provider,
        model=registry.active.name,
        executor=executor,
        context_window=registry.active.context_window,
        budget_policy=ReserveBasedBudgetPolicy(identity_reserve=identity.tokens(estimator)),
        estimator=estimator,
        checkpoints=checkpoints,
        identity=identity,
        events=events,
        session_exists=session_exists,
        environment=(
            EnvironmentContext(sandbox.root, estimate=estimator.estimate)
            if environment_context
            else None
        ),
        lenient_protocol=lenient_protocol,
        native_tools=native_tools,
        verify_completion=verify_completion,
    )
    return AgentSlice(loop=loop, executor=executor, checkpoints=checkpoints, workspace=sandbox)


# What opening or writing the store can raise, for an entry point to catch.
STORE_ERRORS: tuple[type[BaseException], ...] = (sqlite3.Error, SqliteSchemaVersionMismatch)


def describe_store_failure(exc: BaseException, database: Path | None) -> str:
    """One paragraph for the person at the terminal, from a STORE_ERRORS error.

    Gap analysis P1-5: a second `pac` writing the same file, a damaged file
    and a file from another schema version each ended in a traceback, which
    says neither what happened nor what to do. Each now gets a sentence and
    the safe next step. A damaged file is never repaired or replaced here:
    the first step is a copy, with the `-wal` and `-shm` files that hold its
    latest writes (ADR-010).
    """
    where = f"the database {database}" if database is not None else "the database"
    text = str(exc)
    if isinstance(exc, sqlite3.OperationalError) and ("locked" in text or "busy" in text):
        return (f"{where} is busy: another pac, or another program, is writing to it. "
                "Close the other one and try again.")
    if isinstance(exc, SqliteSchemaVersionMismatch):
        return f"{where} was written by a different version of pac: {text}"
    if isinstance(exc, sqlite3.IntegrityError):
        return f"{where} cannot be opened: {text}"
    # sqlite's own words for a damaged file. Other DatabaseErrors (a disk I/O
    # error, a read-only file) are not a sign of damage and are not called one.
    if isinstance(exc, sqlite3.DatabaseError) and any(
            words in text for words in ("not a database", "malformed", "corrupt")):
        name = database.name if database is not None else "core.db"
        return (f"{where} cannot be read ({text}); it may be damaged. Before anything "
                f"else, copy it together with {name}-wal and {name}-shm if they exist: "
                "they hold its latest writes. To keep working meanwhile, start a new one "
                "with --database and another path.")
    return f"{where} failed: {text}"


def build_reply_redactor() -> SecretRedactor:
    """What withholds a secret from a chat reply before `pac` prints it.

    Gap analysis P0-6: the agent's answer was checked for secrets and the
    chat reply was printed raw, so a secret the model repeated from the
    history, the profile or a document reached the terminal and its
    scrollback. This is the agent's own check (`agent/verifier.py`), so both
    paths withhold the same shapes; `app` may not import `agent`, so it is
    handed over here, behind `core.contracts.SecretRedactor`.

    It is not ADR-018's evidence redactor and is not given to the service:
    what is stored is unchanged. It withholds at the terminal only.
    """
    return SecretShapeRedactor()
