"""In-memory vector index.

Implements `core.contracts.VectorIndex`.

Named for the mechanism. It ranks by cosine similarity between whatever
vectors it is given; whether those vectors encode meaning is the embedding
model's business, not this index's, and the index enforces only that they all
came from the same model.

This is not a miniature vector database and does not try to be. There is no
approximate nearest-neighbour structure, no graph, no quantization, no
persistence: it scans every entry and computes cosine similarity. That is
O(n) and entirely adequate for the sizes this phase deals with, and being
exact makes it a usable reference to check an approximate index against
later.

What it does carry, which a store would otherwise be trusted to get right:

  - model identity is enforced, so vectors from two embedding models can
    never be compared
  - dimensionality is enforced
  - ordering is fully deterministic, including ties
  - the language filter actually filters (see `language.py`)
"""
from __future__ import annotations

from typing import Sequence

from ..core.errors import IndexError_
from ..core.knowledge import (
    Candidate,
    CandidateList,
    Chunk,
    Embedding,
    RetrievalMethod,
)
from .language import language_matches


class InMemoryVectorIndex:
    """Exact cosine search over stored chunk embeddings.

    `index_version` changes on every mutation. Provenance claims which index
    build produced a result; a version that never changes turns that claim
    into decoration.
    """

    def __init__(
        self,
        *,
        model_id: str,
        dimensions: int,
        name: str = "in-memory-vector",
    ) -> None:
        if dimensions < 1:
            raise ValueError("dimensions must be positive")
        self._model_id = model_id
        self._dimensions = dimensions
        self._name = name
        self._generation = 0
        # chunk_id -> (chunk, unit vector)
        self._entries: dict[str, tuple[Chunk, tuple[float, ...]]] = {}

    @property
    def index_version(self) -> str:
        return f"{self._name}@{self._generation}"

    @property
    def model_id(self) -> str:
        return self._model_id

    def __len__(self) -> int:
        return len(self._entries)

    def add(self, chunks: Sequence[Chunk], embeddings: Sequence[Embedding]) -> None:
        if len(chunks) != len(embeddings):
            raise IndexError_(
                f"got {len(chunks)} chunks and {len(embeddings)} embeddings; "
                "they must correspond one to one"
            )
        for chunk, embedding in zip(chunks, embeddings):
            if embedding.model_id != self._model_id:
                raise IndexError_(
                    f"embedding for chunk {chunk.id} came from "
                    f"{embedding.model_id!r}, index holds {self._model_id!r}; "
                    "vectors from different models are not comparable"
                )
            if embedding.dimensions != self._dimensions:
                raise IndexError_(
                    f"embedding for chunk {chunk.id} has "
                    f"{embedding.dimensions} dimensions, index expects "
                    f"{self._dimensions}"
                )
            self._entries[chunk.id] = (chunk, embedding.vector)
        self._generation += 1

    def remove_document(self, document_id: str) -> None:
        doomed = [
            chunk_id
            for chunk_id, (chunk, _) in self._entries.items()
            if chunk.document_id == document_id
        ]
        for chunk_id in doomed:
            del self._entries[chunk_id]
        self._generation += 1

    def search(
        self, *, embedding: Embedding, limit: int, language: str | None = None
    ) -> CandidateList:
        if limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}")
        if embedding.model_id != self._model_id:
            raise IndexError_(
                f"query embedding came from {embedding.model_id!r}, index "
                f"holds {self._model_id!r}"
            )
        if embedding.dimensions != self._dimensions:
            raise IndexError_(
                f"query embedding has {embedding.dimensions} dimensions, "
                f"index expects {self._dimensions}"
            )

        scored = [
            (self._cosine(embedding.vector, vector), chunk.id)
            for chunk, vector in self._entries.values()
            if language_matches(chunk.language, language)
        ]
        # Highest score first; equal scores by ascending chunk id. The tie rule
        # is arbitrary but fixed -- an unspecified tie order makes a ranking
        # untestable, which is one of the things this phase refused to inherit.
        scored.sort(key=lambda pair: (-pair[0], pair[1]))

        return CandidateList(
            method=RetrievalMethod.VECTOR,
            candidates=tuple(
                Candidate(
                    chunk_id=chunk_id,
                    rank=position,
                    score=score,
                    method=RetrievalMethod.VECTOR,
                )
                for position, (score, chunk_id) in enumerate(scored[:limit], start=1)
            ),
        )

    @staticmethod
    def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
        # Both sides are unit vectors by contract with the embedding provider,
        # so the dot product is the cosine.
        return sum(a * b for a, b in zip(left, right))
