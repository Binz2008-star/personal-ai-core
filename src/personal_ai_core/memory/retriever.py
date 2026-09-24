"""Memory recall.

Read-only by construction. This module holds no `MemoryStore`, only a
`MemoryReader`, so there is no expression here that could reach a write.

Failure classification
----------------------

`retrieve` has three disjoint regions, each the sole producer of one
`MemoryRetrievalError`. The regions are narrow on purpose: a single
`except Exception` wrapped around the whole method would collapse every
distinct cause into one value and leave the other two members declared but
unreachable -- an enum that lies about what the subsystem can express.

    Region 1  retriever-owned query constraints  -> INVALID_QUERY
    Region 2  the reader call                    -> UNAVAILABLE
    Region 3  the ranking computation            -> INTERNAL

Every raise inside a handler uses `from None`, so no original exception is
chained and `__cause__` exposes nothing. The failure carries the enum and
nothing else.
"""
from __future__ import annotations

import re
from typing import Sequence

from ..core.memory import (
    MemoryEvidence,
    MemoryQuery,
    MemoryReader,
    MemoryRecord,
    MemoryRetrievalError,
    MemoryScope,
)


class MemoryRetrievalFailure(Exception):
    """A classified recall failure.

    The message is the classification's own value and nothing else. A
    store's exception text can name a host, a database, a user or a
    credential, and this failure is recorded into an event payload -- so
    the raw text never travels with it.
    """

    def __init__(self, classification: MemoryRetrievalError) -> None:
        super().__init__(classification.value)
        self.classification = classification


_TOKEN = re.compile(r"\w+", re.UNICODE)


def _tokens(text: str) -> frozenset[str]:
    return frozenset(match.group().lower() for match in _TOKEN.finditer(text))


def _overlap(left: frozenset[str], right: frozenset[str]) -> float:
    """Jaccard overlap in [0, 1]. Empty on either side means no signal."""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


class SimpleMemoryRetriever:
    """Scope-aware, language-weighted, deterministic recall.

    Eligibility is the reader's job and the scope's: `MemoryScope.SESSION`
    asks for one session's active memories, `MemoryScope.USER` for the owner's
    across sessions (ADR-015). This class only dispatches on the scope and
    ranks what the reader admits.

    Ranking is surface-level and says so. It combines how sure the gate was
    that a memory is true, whether it is in the language of this turn, and
    how much vocabulary it shares with the query:

        score = 0.5 * confidence
              + 0.3 * language_match
              + 0.2 * surface_overlap

    This is not semantic retrieval and does not claim to be -- the same
    distinction ADR-006 draws for the lexical arm of document retrieval. A
    semantic ranker is a later phase and a different class.

    Ordering is a total order -- score, then recency, then id -- so the
    same query over the same records always produces the same sequence,
    whatever the scope.
    """

    MAX_LIMIT = 100

    _WEIGHT_CONFIDENCE = 0.5
    _WEIGHT_LANGUAGE = 0.3
    _WEIGHT_OVERLAP = 0.2

    def __init__(self, *, reader: MemoryReader) -> None:
        self._reader = reader

    def retrieve(self, query: MemoryQuery) -> Sequence[MemoryEvidence]:
        # Region 1: constraints this retriever owns. `MemoryQuery` validates
        # what the type can know; these belong to this implementation's cost
        # model and ranking requirements, so they are checked here.
        if not query.text.strip():
            # There is nothing to compute surface overlap against.
            raise MemoryRetrievalFailure(MemoryRetrievalError.INVALID_QUERY)
        if query.limit > self.MAX_LIMIT:
            raise MemoryRetrievalFailure(MemoryRetrievalError.INVALID_QUERY)

        # Region 2: the reader call, and only the reader call.
        #
        # The scope decides which eligibility method the reader answers with;
        # the reader, not this class, holds the filter (ADR-015). A reader
        # that cannot resolve users raises for USER scope, and that raise is
        # classified UNAVAILABLE like any other unreachable read.
        #
        # The raise happens *after* the handler, not inside it. `raise ...
        # from None` clears `__cause__` but Python still populates
        # `__context__` with the original exception, and a store's error
        # text can name a host, a database or a credential -- which any
        # traceback render or `exc_info` logger would then surface.
        # Raising with no exception being handled attaches no context at
        # all.
        failed: MemoryRetrievalError | None = None
        records: Sequence[MemoryRecord] = ()
        try:
            if query.scope is MemoryScope.USER:
                records = self._reader.list_active_for_session_owner(query.session_id)
            else:
                records = self._reader.list_active_for_session(query.session_id)
        except Exception:
            failed = MemoryRetrievalError.UNAVAILABLE
        if failed is not None:
            raise MemoryRetrievalFailure(failed)

        # Region 3: the ranking computation, and only that.
        ranked: list[MemoryEvidence] = []
        try:
            ranked = self._rank(records, query)
        except Exception:
            failed = MemoryRetrievalError.INTERNAL
        if failed is not None:
            raise MemoryRetrievalFailure(failed)

        return tuple(ranked[: query.limit])

    def _rank(
        self, records: Sequence[MemoryRecord], query: MemoryQuery
    ) -> list[MemoryEvidence]:
        query_tokens = _tokens(query.text)
        scored: list[tuple[float, MemoryRecord]] = []

        for record in records:
            language_match = 1.0 if record.language == query.language else 0.0
            score = (
                self._WEIGHT_CONFIDENCE * record.confidence
                + self._WEIGHT_LANGUAGE * language_match
                + self._WEIGHT_OVERLAP * _overlap(query_tokens, _tokens(record.content))
            )
            scored.append((min(max(score, 0.0), 1.0), record))

        # Total order: score desc, then most recently updated, then id, so
        # ties never depend on the order the store happened to return.
        scored.sort(key=lambda pair: (-pair[0], -pair[1].updated_at.timestamp(), pair[1].id))
        return [MemoryEvidence(record=record, relevance=score) for score, record in scored]
