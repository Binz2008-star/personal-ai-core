"""Phase 3 memory domain types.

Pure values. No I/O, no provider, no persistence, no framework. Everything
here is a frozen dataclass or a str-Enum, matching the convention used by the
rest of `core.domain`.

The types split the memory subsystem into three lifecycle roles that are
otherwise easy to confuse:

    ExperienceRecord    what was said, still un-evaluated
    MemoryCandidate     a rule proposes an experience is memorable
    MemoryRecord        the gate's decision, written into the store

A `MemoryRecord` is only ever constructed by the promotion pipeline. A rule
produces a `MemoryCandidate`; the gate returns a `PromotionOutcome`; the
pipeline materializes the final record. That separation is deliberate: a
proposed status on a candidate would be a status the gate has not yet
assigned, and a record with a status it did not earn is exactly the kind of
quiet lie ADR-003 exists to prevent.

See `docs/MEMORY_ARCHITECTURE.md` for the taxonomy this file expresses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence

from .domain import new_id, utcnow


class MemoryType(str, Enum):
    """What kind of thing a memory is.

    Only the types Phase 3 has a producer for are declared. `MEMORY_ARCHITECTURE.md`
    lists a wider taxonomy; a member appears here when a rule in `memory/rules.py`
    actually assigns it. Declaring a member without a producer would make the
    enum lie about what the subsystem can express.
    """

    PREFERENCES = "preferences"
    LESSONS = "lessons"
    SEMANTIC = "semantic"
    EPISODIC = "episodic"


class MemoryStatus(str, Enum):
    """The lifecycle state of a `MemoryRecord`.

    `ACTIVE` and `REJECTED` are assigned at promotion time; `SUPERSEDED` is
    assigned only by the store when a newer record replaces an existing one.
    A hold decision does not produce a record and so has no status: it lives
    on `PromotionDecision` and `PromotionOutcome`, where it belongs.
    """

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class PromotionDecision(str, Enum):
    """What the gate decided about a candidate.

    `HELD` is the conflict outcome: the candidate contradicts an active
    record, and the gate refuses to silently overwrite. No record is written
    for a hold; only an event.
    """

    PROMOTED = "promoted"
    REJECTED = "rejected"
    HELD = "held"


@dataclass(frozen=True, slots=True)
class MemoryProvenance:
    """Where a memory came from.

    A memory without provenance is a claim without a citation. `event_id` is
    required, not optional, so every record can be traced back to a specific
    recorded event.
    """

    session_id: str
    event_id: str
    promoted_by: str
    promoted_at: datetime = field(default_factory=utcnow)
    message_id: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """A durable memory.

    Only the promotion pipeline constructs these. A rule proposes a
    `MemoryCandidate`; the gate decides; the pipeline materializes the
    record with the status the decision earned.
    """

    session_id: str
    type: MemoryType
    content: str
    language: str
    provenance: MemoryProvenance
    status: MemoryStatus
    confidence: float
    id: str = field(default_factory=new_id)
    version: int = 1
    supersedes: str | None = None
    links: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("MemoryRecord.content must be non-empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"MemoryRecord.confidence out of range: {self.confidence!r}"
            )
        if self.version < 1:
            raise ValueError(f"MemoryRecord.version must be >= 1, got {self.version}")
        # `supersedes` and `status` are deliberately independent:
        # - `supersedes` on record X points from a newer version to the older
        #   record X replaced (a forward-in-time link recorded once, at write).
        # - `SUPERSEDED` status is assigned to the OLD record when a newer one
        #   arrives. A record with SUPERSEDED status may or may not itself
        #   carry `supersedes` (a root record has none; a mid-chain one does).
        # There is no useful invariant coupling the two, and asserting one
        # here would make the chain A -> B -> C impossible to represent.
        if self.status is MemoryStatus.REJECTED and self.supersedes is not None:
            raise ValueError(
                "MemoryStatus.REJECTED must not carry a `supersedes` link: "
                "a rejected candidate replaced nothing"
            )
        # Provenance carries the session a record came from. The record's
        # own session_id must agree, or the record and its citation are
        # about different sessions -- an inconsistency the pipeline never
        # produces but any direct caller could.
        if self.provenance.session_id != self.session_id:
            raise ValueError(
                f"MemoryRecord.session_id ({self.session_id!r}) does not match "
                f"provenance.session_id ({self.provenance.session_id!r})"
            )


@dataclass(frozen=True, slots=True)
class ExperienceRecord:
    """The raw input to the memory subsystem: what happened, un-evaluated.

    An experience is not itself a memory. Whether any of it becomes one is
    the pipeline's decision.
    """

    session_id: str
    text: str
    event_ids: tuple[str, ...] = ()
    signals: Mapping[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=new_id)
    message_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        # Freeze signals so a caller cannot mutate them after handing the
        # experience to the pipeline.
        object.__setattr__(self, "signals", MappingProxyType(dict(self.signals)))


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """A rule's proposal that an experience contains a memorable claim.

    Holds the proposed fields directly. There is deliberately no
    pre-constructed `MemoryRecord` inside a candidate: a record's status is
    something the gate assigns, not something a rule proposes.
    """

    experience_id: str
    type: MemoryType
    content: str
    language: str
    confidence: float
    provenance: MemoryProvenance
    rule: str
    rationale: str
    id: str = field(default_factory=new_id)
    proposed_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("MemoryCandidate.content must be non-empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"MemoryCandidate.confidence out of range: {self.confidence!r}"
            )


@dataclass(frozen=True, slots=True)
class PromotionOutcome:
    """The gate's decision about a single candidate."""

    decision: PromotionDecision
    candidate_id: str
    reason: str
    conflicts_with: str | None = None
    memory_id: str | None = None


# --- Phase 4: recall -------------------------------------------------------
#
# Reading memory is not writing memory. Everything below is read-only by
# construction: no type here can reach `MemoryStore.write`, and the reader
# is a nominal class rather than a Protocol precisely so a write-capable or
# write-refusing store cannot be substituted for it structurally.


class MemoryRetrievalError(str, Enum):
    """Stable classification of a memory-retrieval failure.

    These values travel into event payloads, so they are part of the
    contract: an exception message, stack trace, URL, provider name or
    connection string must never be substituted for one. A payload that
    carries whatever the store happened to say is a payload nobody can
    safely log.

    Each member has exactly one producer region in `SimpleMemoryRetriever`,
    and no member is reachable from more than one.
    """

    UNAVAILABLE = "unavailable"
    INVALID_QUERY = "invalid_query"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class MemoryQuery:
    """One turn's recall request.

    `session_id` is a hard filter, applied at the reader boundary so no
    caller can widen it. `language` is a ranking signal only: dropping a
    memory because the current turn is in another language would discard a
    preference the user actually stated.

    Validation here covers only what this type can know. A blank `text` or
    an unbounded `limit` are the retriever's constraints, not the query's,
    and are classified `INVALID_QUERY` there.
    """

    session_id: str
    text: str
    language: str
    limit: int = 5

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("MemoryQuery.session_id must be non-empty")
        if self.limit < 1:
            raise ValueError(f"MemoryQuery.limit must be >= 1, got {self.limit}")


@dataclass(frozen=True, slots=True)
class MemoryEvidence:
    """A memory ranked for one recall turn.

    `relevance` is the ranker's judgement for this query, deliberately
    distinct from `record.confidence`, which is how sure the promotion gate
    was that the claim is true. A certain memory can be irrelevant here.
    """

    record: MemoryRecord
    relevance: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.relevance <= 1.0:
            raise ValueError(
                f"MemoryEvidence.relevance out of range: {self.relevance!r}"
            )


class _ReadableStore(Protocol):
    """The read slice `MemoryReader` delegates to.

    Declared here rather than imported from `core.contracts` because
    `contracts` imports this module; naming the shape locally keeps the
    dependency one-way.
    """

    def read(self, memory_id: str) -> "MemoryRecord | None": ...

    def list_active(self) -> Sequence["MemoryRecord"]: ...


class MemoryReader:
    """A read-only view over a memory repository.

    A class, not a Protocol, and that is the whole point. A Protocol would
    be satisfied by `SealedMemoryStore` -- which has both method names and
    raises on both -- leaving "this object is technically a reader but must
    never be used as one" as a rule enforced by comment. Nominal typing
    makes the boundary structural instead: a reader is something that was
    deliberately constructed as one.

    `list_active_for_session` folds the session filter into the read
    contract so a caller cannot forget it.
    """

    def __init__(self, source: _ReadableStore) -> None:
        self._source = source

    def read(self, memory_id: str) -> MemoryRecord | None:
        return self._source.read(memory_id)

    def list_active_for_session(self, session_id: str) -> Sequence[MemoryRecord]:
        return tuple(
            record
            for record in self._source.list_active()
            if record.session_id == session_id
        )
