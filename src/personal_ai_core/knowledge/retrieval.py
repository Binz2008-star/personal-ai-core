"""Hybrid retrieval.

Implements `core.contracts.Retriever`.

Runs the vector and lexical arms, fuses their rankings, and returns results
carrying full provenance. It depends on the four contracts it composes --
`EmbeddingProvider`, `VectorIndex`, `LexicalIndex`, `RankFusion` -- and on no
concrete implementation of any of them, so the in-memory stack here and a
future persistent one are the same call site.
"""
from __future__ import annotations

from typing import Sequence

from ..core.contracts import EmbeddingProvider, LexicalIndex, RankFusion, VectorIndex
from ..core.errors import RetrievalError
from ..core.knowledge import (
    CandidateList,
    RetrievalMethod,
    RetrievalProvenance,
    RetrievalQuery,
    RetrievalResult,
)
from .catalog import InMemoryChunkCatalog
from .fusion import ReciprocalRankFusion

# How deep each arm searches before fusion.
#
# A fixed floor, NOT a multiple of the caller's `limit`. The audited system
# used `max(match_count * 8, 50)`, which means asking for 3 results instead of
# 10 silently searched a shallower pool and could return a different top 3.
# Retrieval depth is a quality decision; the number of rows a caller wants to
# display is not. They are separated here.
DEFAULT_CANDIDATE_DEPTH = 50


class HybridRetriever:
    """Semantic + lexical candidate generation, fused into one ranking."""

    def __init__(
        self,
        *,
        embedder: EmbeddingProvider,
        vector_index: VectorIndex,
        lexical_index: LexicalIndex,
        catalog: InMemoryChunkCatalog,
        fusion: RankFusion | None = None,
        candidate_depth: int = DEFAULT_CANDIDATE_DEPTH,
    ) -> None:
        if candidate_depth < 1:
            raise ValueError("candidate_depth must be positive")
        self._embedder = embedder
        self._vector_index = vector_index
        self._lexical_index = lexical_index
        self._catalog = catalog
        self._fusion = fusion or ReciprocalRankFusion()
        self._candidate_depth = candidate_depth

    def retrieve(self, query: RetrievalQuery) -> list[RetrievalResult]:
        if not query.text.strip():
            raise RetrievalError("retrieval query text is empty")
        if not query.methods:
            raise RetrievalError("retrieval query names no methods")

        # The depth floor never drops below what the caller asked for: a
        # caller requesting more results than the depth would otherwise get a
        # list truncated by a setting they cannot see.
        depth = max(self._candidate_depth, query.limit)

        lists: list[CandidateList] = []
        index_versions: dict[RetrievalMethod, str] = {}
        embedding_model_id: str | None = None

        if RetrievalMethod.VECTOR in query.methods:
            embedding = self._embedder.embed([query.text])[0]
            lists.append(
                self._vector_index.search(
                    embedding=embedding, limit=depth, language=query.language
                )
            )
            index_versions[RetrievalMethod.VECTOR] = self._vector_index.index_version
            # Recorded so a reader of the provenance can tell which embedder
            # produced this ranking. "Found by the vector arm" is equally true
            # of a real model and of a hashing stand-in.
            embedding_model_id = self._embedder.model_id

        if RetrievalMethod.LEXICAL in query.methods:
            lists.append(
                self._lexical_index.search(
                    text=query.text, limit=depth, language=query.language
                )
            )
            index_versions[RetrievalMethod.LEXICAL] = self._lexical_index.index_version

        fused = self._fuse(lists, limit=query.limit)

        results: list[RetrievalResult] = []
        for candidate in fused:
            chunk = self._catalog.get(candidate.chunk_id)
            if chunk is None:
                # An index holds an id the catalog does not. That is a wiring
                # or eviction bug, and dropping the row silently would turn it
                # into "the answer was just a bit worse today".
                raise RetrievalError(
                    f"chunk {candidate.chunk_id} is indexed but not in the catalog"
                )
            results.append(
                RetrievalResult(
                    chunk=chunk,
                    provenance=RetrievalProvenance(
                        document_id=chunk.document_id,
                        version_id=chunk.version_id,
                        chunk_id=chunk.id,
                        start=chunk.start,
                        end=chunk.end,
                        methods=candidate.methods(),
                        ranks={c.method: c.rank for c in candidate.contributions},
                        scores={c.method: c.score for c in candidate.contributions},
                        fused_score=candidate.fused_score,
                        index_version=self._describe_indexes(index_versions),
                        embedding_model_id=embedding_model_id,
                        source_uri=self._catalog.source_uri(chunk.document_id),
                    ),
                )
            )
        return results

    def _fuse(self, lists: Sequence[CandidateList], *, limit: int):
        fuse_detailed = getattr(self._fusion, "fuse_detailed", None)
        if fuse_detailed is None:
            raise RetrievalError(
                f"{type(self._fusion).__name__} cannot report per-arm "
                "contributions, so provenance could not be built"
            )
        return fuse_detailed(lists, limit=limit)

    @staticmethod
    def _describe_indexes(versions: dict[RetrievalMethod, str]) -> str:
        return ";".join(
            f"{method.value}={version}" for method, version in sorted(
                versions.items(), key=lambda item: item[0].value
            )
        )
