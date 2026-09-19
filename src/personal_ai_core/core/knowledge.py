"""Knowledge domain types for the Phase 2 contract.

Types and value objects only. No retrieval, no indexing, no embedding, no
storage — those arrive behind the protocols in `core.contracts` once this
contract is settled.

Nothing here knows that Postgres, pgvector, HNSW, tsvector, Ollama or the
Second Brain lineage exist. Those are adapters behind the contract, and the
contract is deliberately written first so that none of them can own it.

Every field earns its place by serving the contract. Fields present in the
audited legacy schema but not required here are deliberately absent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .domain import UNDETERMINED_LANGUAGE, new_id, utcnow


class RetrievalMethod(str, Enum):
    """Which retrieval path produced a candidate.

    Part of provenance, not a detail: "which path found this" is one of the
    questions a retrieval result must be able to answer.
    """

    SEMANTIC = "semantic"
    LEXICAL = "lexical"
    FUSED = "fused"


# --- source ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Document:
    """A source of knowledge, identified independently of its content.

    Holds no content. Content belongs to a `DocumentVersion`, because a
    document's identity must survive its text changing — otherwise a citation
    made last week cannot be resolved today.

    `declared_language` is a document-level hint only. Retrieval language is
    decided per chunk (ADR-006): a document is frequently mixed, and treating
    its declared language as authoritative is how Arabic content inside an
    English document becomes unreachable.
    """

    source_uri: str
    title: str = ""
    declared_language: str = UNDETERMINED_LANGUAGE
    metadata: Mapping[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class DocumentVersion:
    """A specific, immutable revision of a document's content.

    Required by the provenance contract: a retrieval result must name the
    exact version that produced it. Without this, "where did this come from"
    is answerable only as long as nothing has been re-ingested.

    `content_hash` is the identity of the text itself, which also makes
    re-ingestion idempotent.
    """

    document_id: str
    content_hash: str
    revision: int = 1
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utcnow)


# --- retrievable unit -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class Chunk:
    """A retrievable span of one document version.

    `Document != Chunk`. A document is a source; a chunk is a unit of
    retrieval. They have separate identities and separate lifetimes, and a
    chunk is only meaningful relative to the version it was cut from.

    The back-reference is deterministic: `(version_id, start, end)` locates the
    exact character span, so a citation can be re-read and checked rather than
    trusted. `ordinal` preserves reading order within the version.

    `language` is per chunk and is a first-class retrieval input (ADR-006).
    """

    document_id: str
    version_id: str
    text: str
    ordinal: int
    start: int
    end: int
    language: str = UNDETERMINED_LANGUAGE
    metadata: Mapping[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=new_id)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        if self.end < self.start:
            raise ValueError(f"chunk end {self.end} precedes start {self.start}")
        if self.ordinal < 0:
            raise ValueError(f"chunk ordinal must be non-negative, got {self.ordinal}")


# --- embeddings -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Embedding:
    """A vector together with the model that produced it.

    The model identity travels with the vector on purpose. Comparing vectors
    from two different embedding models is meaningless but numerically silent,
    so the contract makes the mismatch detectable instead of plausible.
    """

    vector: tuple[float, ...]
    model_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "vector", tuple(self.vector))
        if not self.vector:
            raise ValueError("embedding vector must not be empty")

    @property
    def dimensions(self) -> int:
        return len(self.vector)


# --- candidates and fusion ------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candidate:
    """One chunk as proposed by one retrieval path.

    `rank` is that path's own 1-based ordering. `score` is that path's own
    score and is **not** comparable across paths — cosine similarity and a
    lexical score share no scale. Fusion therefore consumes ranks, not scores.
    """

    chunk_id: str
    rank: int
    score: float
    method: RetrievalMethod

    def __post_init__(self) -> None:
        if self.rank < 1:
            raise ValueError(f"rank is 1-based, got {self.rank}")


@dataclass(frozen=True, slots=True)
class CandidateList:
    """The ordered output of a single retrieval path."""

    method: RetrievalMethod
    candidates: tuple[Candidate, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))
        for candidate in self.candidates:
            if candidate.method is not self.method:
                raise ValueError(
                    f"candidate method {candidate.method} does not match list "
                    f"method {self.method}"
                )


@dataclass(frozen=True, slots=True)
class FusedCandidate:
    """A chunk after fusion, carrying what each path contributed.

    Contributions are kept rather than collapsed into one number so a ranking
    can be explained and tested. A fused score with no visible inputs is a
    ranking nobody can audit.

    **Tie-breaking is part of the contract.** Equal fused scores are ordered by
    ascending `chunk_id`. This is arbitrary but it is *stable*: the same inputs
    must always produce the same order, or the ranking is untestable. Use
    `sort_key` for descending-score ordering.
    """

    chunk_id: str
    fused_score: float
    contributions: tuple[Candidate, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "contributions", tuple(self.contributions))

    @property
    def sort_key(self) -> tuple[float, str]:
        """Deterministic ordering key: highest score first, then chunk_id."""
        return (-self.fused_score, self.chunk_id)

    def methods(self) -> tuple[RetrievalMethod, ...]:
        return tuple(dict.fromkeys(c.method for c in self.contributions))


# --- provenance -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetrievalProvenance:
    """Why this chunk is here, and where it came from.

    A first-class contract, not a metadata bag. It must answer, without
    consulting anything else:

      - Where did this originate?          `document_id`, `source_uri`
      - Which version produced it?         `version_id`
      - Which exact text?                  `chunk_id`, `start`, `end`
      - Which retrieval path found it?     `methods`
      - What ranking led to selection?     `ranks`, `scores`, `fused_score`
      - Against which index?               `index_version`

    The audited source carried provenance only to file level, which is why
    this is specified as a type rather than left to convention.
    """

    document_id: str
    version_id: str
    chunk_id: str
    start: int
    end: int
    methods: tuple[RetrievalMethod, ...] = ()
    ranks: Mapping[RetrievalMethod, int] = field(default_factory=dict)
    scores: Mapping[RetrievalMethod, float] = field(default_factory=dict)
    fused_score: float | None = None
    index_version: str | None = None
    source_uri: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "methods", tuple(self.methods))
        object.__setattr__(self, "ranks", MappingProxyType(dict(self.ranks)))
        object.__setattr__(self, "scores", MappingProxyType(dict(self.scores)))


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """A retrieved chunk with its full provenance."""

    chunk: Chunk
    provenance: RetrievalProvenance

    def __post_init__(self) -> None:
        if self.provenance.chunk_id != self.chunk.id:
            raise ValueError(
                "provenance chunk_id does not match the chunk it accompanies"
            )


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    """What retrieval was asked for.

    `language` is explicit rather than inferred. ADR-006 records that the
    audited lexical arm was hard-coded to English and degraded silently for
    Arabic; making language a required part of the query means a retriever
    cannot quietly ignore it.
    """

    text: str
    limit: int = 10
    language: str = UNDETERMINED_LANGUAGE
    methods: tuple[RetrievalMethod, ...] = (
        RetrievalMethod.SEMANTIC,
        RetrievalMethod.LEXICAL,
    )
    filters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "methods", tuple(self.methods))
        object.__setattr__(self, "filters", MappingProxyType(dict(self.filters)))
        if self.limit < 1:
            raise ValueError(f"limit must be at least 1, got {self.limit}")
