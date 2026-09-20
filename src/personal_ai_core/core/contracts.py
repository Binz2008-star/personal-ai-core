"""Core protocols.

These are the boundary. Application services depend on these; infrastructure
implements them. Nothing here imports a provider, a driver or a transport.

Dependency direction (ARCHITECTURE.md §4):

    infrastructure  ──implements──▶  core.contracts  ◀──depends on──  application
"""
from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .context import BudgetedContext, ContextAllocation, ContextBudget
from .domain import Event, Message, ModelResponse, Session, User
from .knowledge import (
    CandidateList,
    Chunk,
    Document,
    DocumentVersion,
    Embedding,
    RetrievalQuery,
    RetrievalResult,
)
from .memory import MemoryCandidate, MemoryRecord, PromotionOutcome


@runtime_checkable
class ModelProvider(Protocol):
    """A source of generated text.

    The Core talks to models only through this. Swapping Ollama for another
    backend must not require a change above this line — if it does, the
    abstraction has failed (ADR-002).
    """

    @property
    def name(self) -> str:
        """Provider identifier, e.g. "ollama"."""
        ...

    def generate(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        options: Mapping[str, Any] | None = None,
    ) -> ModelResponse:
        """Produce a response for `messages` using `model`.

        Raises `ProviderError` if the backend fails.
        """
        ...


@runtime_checkable
class ModelSpecLike(Protocol):
    """The shape the application needs from a registered model.

    `context_window` is part of the contract because the context budget must
    derive from the active model rather than a constant (ADR-005).
    """

    @property
    def name(self) -> str: ...

    @property
    def provider(self) -> str: ...

    @property
    def context_window(self) -> int: ...


@runtime_checkable
class ModelRegistry(Protocol):
    """Which model is currently active.

    The application depends on this protocol, not on the concrete registry in
    `runtime.model_registry`. That keeps a later persistent or remote registry
    substitutable without touching the conversation layer.
    """

    @property
    def active(self) -> ModelSpecLike: ...


@runtime_checkable
class UserRepository(Protocol):
    def add(self, user: User) -> None: ...
    def get(self, user_id: str) -> User | None: ...


@runtime_checkable
class SessionRepository(Protocol):
    def add(self, session: Session) -> None: ...
    def get(self, session_id: str) -> Session | None: ...
    def update(self, session: Session) -> None: ...


@runtime_checkable
class MessageRepository(Protocol):
    def add(self, message: Message) -> None: ...
    def list_for_session(self, session_id: str) -> Sequence[Message]: ...


@runtime_checkable
class EventRepository(Protocol):
    """Append-only event storage.

    There is deliberately no `update` and no `delete`. An event is evidence;
    corrections are new events (ADR-003).
    """

    def append(self, event: Event) -> None: ...
    def list_for_session(self, session_id: str) -> Sequence[Event]: ...


@runtime_checkable
class MemoryStore(Protocol):
    """Persistent memory.

    Two implementations coexist by design:

    - `SealedMemoryStore` sits on the conversation path and raises on every
      write. Its structural presence enforces the `Event != Memory`
      invariant: a conversation turn that reaches for memory raises rather
      than quietly succeeds (ADR-003).
    - The real repository, written in Phase 3, sits behind the promotion
      gate and is the only path through which a `MemoryRecord` reaches
      durable storage.

    Supersede is non-destructive: the old record is retained with
    `status=SUPERSEDED` and linked from the new record. A deletion would
    make the audit trail unrecoverable.
    """

    def write(self, record: MemoryRecord) -> MemoryRecord: ...

    def read(self, memory_id: str) -> MemoryRecord | None: ...

    def list_active(self) -> Sequence[MemoryRecord]: ...

    def supersede(self, old_id: str, new_record: MemoryRecord) -> MemoryRecord: ...


@runtime_checkable
class PromotionGate(Protocol):
    """Decides whether a candidate becomes a memory.

    A pure function: `evaluate` never writes, never emits events and never
    calls back into the pipeline. The gate's only job is to say
    promoted / rejected / held; the pipeline acts on that decision.

    A held decision is the conflict outcome: the candidate contradicts an
    active record and the gate refuses to silently overwrite. The pipeline
    records the conflict and hands the resolution to a later phase.
    """

    def evaluate(
        self,
        candidate: MemoryCandidate,
        active: Sequence[MemoryRecord],
    ) -> PromotionOutcome: ...


# --- Phase 2 knowledge contracts ------------------------------------------
#
# Protocols only. No implementation exists behind these yet.
#
# None of them names Postgres, pgvector, HNSW, tsvector, SQL, a connection, a
# vector operator or a provider. That is the point: the existing Second Brain
# retrieval is a valuable asset to adapt behind these, and an asset must not
# become the architecture.


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Turns text into vectors.

    `model_id` and `dimensions` are part of the contract because an embedding
    is only comparable against others from the same model at the same size.
    A provider that cannot state both cannot be validated.

    Raises `EmbeddingError` on failure. Returning a short, empty or
    wrong-dimension result is a contract violation, not a degraded success.
    """

    @property
    def model_id(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    def embed(self, texts: Sequence[str]) -> Sequence[Embedding]:
        """Embed each text, in order. len(result) == len(texts)."""
        ...


@runtime_checkable
class Chunker(Protocol):
    """Splits a document version's content into retrievable chunks.

    Must be deterministic: the same content and settings produce the same
    chunks, including their offsets. Non-deterministic chunking makes every
    citation unverifiable.
    """

    def chunk(
        self, *, document: Document, version: DocumentVersion, content: str
    ) -> Sequence[Chunk]: ...


@runtime_checkable
class VectorIndex(Protocol):
    """Vector candidate generation.

    Named for the mechanism, not for a capability. This contract is satisfied
    by any provider of comparable vectors -- a real embedding model, or a
    hashing stand-in that captures only surface overlap. It does not promise
    that the ranking means anything; that promise belongs to the embedding
    model, and `EmbeddingProvider.model_id` is what identifies it.

    Expresses what retrieval needs, not how a store provides it. An in-memory
    implementation and a pgvector adapter satisfy this identically; neither
    leaks through it.
    """

    @property
    def index_version(self) -> str:
        """Identifies the index build, for provenance."""
        ...

    def add(self, chunks: Sequence[Chunk], embeddings: Sequence[Embedding]) -> None: ...

    def remove_document(self, document_id: str) -> None: ...

    def search(
        self, *, embedding: Embedding, limit: int, language: str | None = None
    ) -> CandidateList:
        """Return vector candidates, ranked best first, 1-based ranks."""
        ...


@runtime_checkable
class LexicalIndex(Protocol):
    """Lexical candidate generation.

    Separate from `VectorIndex` because the two are genuinely different
    capabilities with different failure modes, and because the audited lexical
    arm was hard-coded to English (ADR-006). `language` is an explicit
    parameter here so that an implementation must decide what to do with it
    rather than defaulting silently.
    """

    @property
    def index_version(self) -> str: ...

    def add(self, chunks: Sequence[Chunk]) -> None: ...

    def remove_document(self, document_id: str) -> None: ...

    def search(
        self, *, text: str, limit: int, language: str | None = None
    ) -> CandidateList:
        """Return lexical candidates, ranked best first, 1-based ranks."""
        ...


@runtime_checkable
class RankFusion(Protocol):
    """Combines ranked candidate lists into one ranking.

    Consumes ranks rather than scores, because scores from different retrieval
    paths share no scale. Must be deterministic, including tie-breaking --
    see `FusedCandidate.sort_key`.

    Reciprocal Rank Fusion is the intended first implementation. The audited
    RRF is classified `implemented, unproven` with zero dedicated tests, so it
    is characterized before it is adapted, not assumed correct.
    """

    def fuse(self, lists: Sequence[CandidateList], *, limit: int) -> Sequence[str]:
        """Return chunk ids, best first."""
        ...


@runtime_checkable
class Retriever(Protocol):
    """Hybrid retrieval: vector + lexical candidates, fused and ranked.

    Results carry full provenance. A result that cannot say which path found
    it and at what rank does not satisfy this contract.
    """

    def retrieve(self, query: RetrievalQuery) -> Sequence[RetrievalResult]: ...


@runtime_checkable
class TokenEstimator(Protocol):
    """Estimates token cost of text.

    A contract rather than a helper because the estimate must be correct for
    Arabic as well as English. ADR-005 records the audited `CHARS_PER_TOKEN=4`
    heuristic as unsafe here; an implementation is expected to be
    tokenizer-backed and is tested in both languages.
    """

    @property
    def model_id(self) -> str: ...

    def estimate(self, text: str) -> int: ...


@runtime_checkable
class ContextBudgetPolicy(Protocol):
    """Decides how much of a model's window retrieved evidence may use.

    A separate contract from `ContextAssembler` because these are two
    decisions, not one: how much may be spent, and what to spend it on. The
    audited source collapsed them into a constant, which is why neither could
    be questioned.

    `history_tokens` is a parameter because the budget shrinks as a
    conversation grows. A budget computed once from the window alone is
    correct on turn one and wrong by turn twenty.
    """

    def allocate(
        self, *, model: ModelSpecLike, history_tokens: int
    ) -> ContextAllocation: ...


@runtime_checkable
class ContextAssembler(Protocol):
    """Fits retrieval results into a budget, recording what was excluded."""

    def assemble(
        self, results: Sequence[RetrievalResult], *, budget: ContextBudget
    ) -> BudgetedContext: ...
