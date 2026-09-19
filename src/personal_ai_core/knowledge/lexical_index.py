"""In-memory lexical index.

Implements `core.contracts.LexicalIndex`.

Ranking is Okapi BM25, in the variant stated exactly below. That choice is
deliberate on two counts.

First, it is a published, parameterized ranking function rather than an
invented score, so its behaviour can be reasoned about and its parameters
argued with. A hand-rolled term-frequency count would have been shorter and
would have made the ranking impossible to defend.

Second -- and this is the point of the whole module -- BM25 is
language-neutral. ADR-006 records the audited lexical arm as
`to_tsvector('english', ...)`: English stemming and an English stop-word list
applied to every document regardless of its language. Arabic text went through
an English stemmer and came out unstemmed and unmatched, with no error. Here
there is no per-language asset to get wrong: tokenization is Unicode-based
(`text.py`) and scoring uses only counts.

The price is real and stated rather than hidden: no stemming means "running"
and "run" are different terms in every language. Raising recall with
per-language morphology is future work that must arrive as a tested,
per-language component -- not as one language's defaults applied to all.

Exactly which BM25
------------------

Per document term, this is textbook Okapi, with no approximation::

                       f(q,D) * (k1 + 1)
    IDF(q) * -----------------------------------------
             f(q,D) + k1 * (1 - b + b * |D| / avgdl)

using Lucene's always-positive IDF, ``ln(1 + (N - df + 0.5) / (df + 0.5))``.

The part worth naming, because "BM25" alone does not pin it down, is the
**query** term frequency. The full Okapi formula carries a third saturating
factor, ``(k3 + 1) * qf / (k3 + qf)``. This implementation iterates the query
terms *with duplicates* and sums, which is that factor with ``k3`` unbounded:
a term repeated three times in the query contributes three times as much.

That is a legitimate variant, not a defect, and it is the behaviour a caller
gets -- but it is a choice, so it is written down and pinned by a test rather
than left for someone to rediscover from a surprising ranking. The two common
alternatives, deduplicating query terms or saturating with a finite ``k3``,
would both change rankings for repeated-term queries.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Sequence

from ..core.knowledge import (
    Candidate,
    CandidateList,
    Chunk,
    RetrievalMethod,
)
from .language import language_matches
from .text import tokenize

# Okapi BM25 parameters. k1 controls how fast term frequency saturates; b
# controls how strongly length normalization applies. These are the standard
# defaults, named here so a change to them is a visible decision.
BM25_K1 = 1.5
BM25_B = 0.75

# Query term frequency is linear: the query terms are iterated with duplicates
# and summed. Equivalent to the full Okapi formula with k3 unbounded. See the
# module docstring, "Exactly which BM25".
BM25_QUERY_TERM_FREQUENCY = "linear"


class InMemoryLexicalIndex:
    """BM25 over Unicode-tokenized chunk text."""

    def __init__(
        self,
        *,
        k1: float = BM25_K1,
        b: float = BM25_B,
        name: str = "in-memory-lexical",
    ) -> None:
        if k1 < 0:
            raise ValueError("k1 must be non-negative")
        if not 0.0 <= b <= 1.0:
            raise ValueError("b must be between 0 and 1")
        self._k1 = k1
        self._b = b
        self._name = name
        self._generation = 0
        self._chunks: dict[str, Chunk] = {}
        self._terms: dict[str, Counter[str]] = {}
        self._lengths: dict[str, int] = {}
        self._document_frequency: Counter[str] = Counter()

    @property
    def index_version(self) -> str:
        return f"{self._name}@{self._generation}"

    def __len__(self) -> int:
        return len(self._chunks)

    def add(self, chunks: Sequence[Chunk]) -> None:
        for chunk in chunks:
            # Re-adding the same chunk must not double-count it in the
            # document frequencies; retract the old posting first.
            if chunk.id in self._chunks:
                self._retract(chunk.id)
            counts = Counter(tokenize(chunk.text))
            self._chunks[chunk.id] = chunk
            self._terms[chunk.id] = counts
            self._lengths[chunk.id] = sum(counts.values())
            for term in counts:
                self._document_frequency[term] += 1
        self._generation += 1

    def remove_document(self, document_id: str) -> None:
        doomed = [
            chunk_id
            for chunk_id, chunk in self._chunks.items()
            if chunk.document_id == document_id
        ]
        for chunk_id in doomed:
            self._retract(chunk_id)
        self._generation += 1

    def _retract(self, chunk_id: str) -> None:
        for term in self._terms.pop(chunk_id, {}):
            self._document_frequency[term] -= 1
            if self._document_frequency[term] <= 0:
                del self._document_frequency[term]
        self._chunks.pop(chunk_id, None)
        self._lengths.pop(chunk_id, None)

    def search(
        self, *, text: str, limit: int, language: str | None = None
    ) -> CandidateList:
        if limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}")

        query_terms = tokenize(text)
        if not query_terms or not self._chunks:
            return CandidateList(method=RetrievalMethod.LEXICAL)

        total = len(self._chunks)
        average_length = sum(self._lengths.values()) / total

        scored: list[tuple[float, str]] = []
        for chunk_id, chunk in self._chunks.items():
            if not language_matches(chunk.language, language):
                continue
            score = self._score(chunk_id, query_terms, total, average_length)
            # A chunk sharing no term with the query is not a weak match, it is
            # not a match. Returning it would give fusion a rank to reward.
            if score > 0.0:
                scored.append((score, chunk_id))

        scored.sort(key=lambda pair: (-pair[0], pair[1]))

        return CandidateList(
            method=RetrievalMethod.LEXICAL,
            candidates=tuple(
                Candidate(
                    chunk_id=chunk_id,
                    rank=position,
                    score=score,
                    method=RetrievalMethod.LEXICAL,
                )
                for position, (score, chunk_id) in enumerate(scored[:limit], start=1)
            ),
        )

    def _score(
        self,
        chunk_id: str,
        query_terms: Sequence[str],
        total: int,
        average_length: float,
    ) -> float:
        counts = self._terms[chunk_id]
        length = self._lengths[chunk_id]
        score = 0.0
        # Duplicates are deliberate, not an oversight: iterating them is what
        # makes query term frequency linear (module docstring).
        for term in query_terms:
            frequency = counts.get(term, 0)
            if frequency == 0:
                continue
            document_frequency = self._document_frequency.get(term, 0)
            # Lucene's IDF variant: always positive, so a term present in every
            # chunk contributes little rather than subtracting.
            idf = math.log(
                1.0
                + (total - document_frequency + 0.5) / (document_frequency + 0.5)
            )
            denominator = frequency + self._k1 * (
                1.0 - self._b + self._b * (length / average_length if average_length else 1.0)
            )
            score += idf * (frequency * (self._k1 + 1.0)) / denominator
        return score
