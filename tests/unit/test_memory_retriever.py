"""SimpleMemoryRetriever: deterministic recall with classified failure.

Two properties carry most of the weight here.

*Every MemoryRetrievalError member has a distinct producer.* A single
`except Exception` around the whole method would have collapsed three
causes into one value and left two members declared but unreachable. The
enumeration test below fails if that ever happens.

*A failure carries only its classification.* The classification is written
into an event payload, so a store's exception text -- which can name a
host, a database or a credential -- must never travel with it.
"""
from __future__ import annotations

import pytest

from personal_ai_core.core.memory import (
    MemoryEvidence,
    MemoryProvenance,
    MemoryQuery,
    MemoryReader,
    MemoryRecord,
    MemoryRetrievalError,
    MemoryStatus,
    MemoryType,
)
from personal_ai_core.memory.retriever import (
    MemoryRetrievalFailure,
    SimpleMemoryRetriever,
)
from personal_ai_core.persistence.memory_store import InMemoryMemoryRepository

SECRET = "postgres://user:hunter2@db.internal:5432/memories"


def _record(
    *,
    session_id: str = "s1",
    content: str = "prefers Arabic replies",
    language: str = "en",
    confidence: float = 0.9,
) -> MemoryRecord:
    return MemoryRecord(
        session_id=session_id,
        type=MemoryType.PREFERENCES,
        content=content,
        language=language,
        provenance=MemoryProvenance(
            session_id=session_id, event_id="e1", promoted_by="rule:test"
        ),
        status=MemoryStatus.ACTIVE,
        confidence=confidence,
    )


def _retriever(*records: MemoryRecord) -> SimpleMemoryRetriever:
    repo = InMemoryMemoryRepository()
    for record in records:
        repo.write(record)
    return SimpleMemoryRetriever(reader=MemoryReader(source=repo))


def _query(**overrides) -> MemoryQuery:
    defaults = {
        "session_id": "s1",
        "text": "what do I prefer",
        "language": "en",
        "limit": 5,
    }
    defaults.update(overrides)
    return MemoryQuery(**defaults)  # type: ignore[arg-type]


class ExplodingReader(MemoryReader):
    """A reader whose failure message carries a credential."""

    def __init__(self) -> None:
        super().__init__(source=InMemoryMemoryRepository())

    def list_active_for_session(self, session_id: str):
        raise RuntimeError(f"connection refused: {SECRET}")


# --- happy path -------------------------------------------------------------


def test_returns_evidence_for_matching_session():
    retriever = _retriever(_record(content="prefers Arabic replies"))
    evidence = retriever.retrieve(_query())
    assert len(evidence) == 1
    assert isinstance(evidence[0], MemoryEvidence)
    assert 0.0 <= evidence[0].relevance <= 1.0


def test_empty_store_returns_empty():
    retriever = _retriever()
    assert retriever.retrieve(_query()) == ()


def test_session_is_a_hard_filter():
    retriever = _retriever(
        _record(session_id="s1", content="mine"),
        _record(session_id="s2", content="theirs"),
    )
    evidence = retriever.retrieve(_query(session_id="s1"))
    assert [e.record.content for e in evidence] == ["mine"]


def test_language_is_a_ranking_signal_not_a_filter():
    """A cross-language memory is ranked lower, never dropped."""
    retriever = _retriever(
        _record(content="same language", language="en", confidence=0.8),
        _record(content="other language", language="ar", confidence=0.8),
    )
    evidence = retriever.retrieve(_query(language="en"))

    contents = [e.record.content for e in evidence]
    assert "other language" in contents, "cross-language memory was dropped"
    assert contents[0] == "same language", "same-language memory did not rank first"


def test_ranking_is_deterministic():
    records = [
        _record(content=f"memory {i}", confidence=0.5 + i * 0.1) for i in range(4)
    ]
    retriever = _retriever(*records)
    first = retriever.retrieve(_query())
    second = retriever.retrieve(_query())
    assert [e.record.id for e in first] == [e.record.id for e in second]
    assert [e.relevance for e in first] == [e.relevance for e in second]


def test_limit_truncates():
    retriever = _retriever(*[_record(content=f"memory {i}") for i in range(10)])
    assert len(retriever.retrieve(_query(limit=3))) == 3


def test_higher_confidence_ranks_higher_all_else_equal():
    retriever = _retriever(
        _record(content="unsure thing", confidence=0.3),
        _record(content="certain thing", confidence=0.95),
    )
    evidence = retriever.retrieve(_query(text="thing"))
    assert evidence[0].record.content == "certain thing"


# --- failure classification: one producer per member ------------------------


def test_invalid_query_from_blank_text():
    retriever = _retriever(_record())
    with pytest.raises(MemoryRetrievalFailure) as caught:
        retriever.retrieve(_query(text="   "))
    assert caught.value.classification is MemoryRetrievalError.INVALID_QUERY


def test_invalid_query_from_limit_above_the_ceiling():
    retriever = _retriever(_record())
    over = SimpleMemoryRetriever.MAX_LIMIT + 1
    with pytest.raises(MemoryRetrievalFailure) as caught:
        retriever.retrieve(_query(limit=over))
    assert caught.value.classification is MemoryRetrievalError.INVALID_QUERY


def test_unavailable_when_the_reader_raises():
    retriever = SimpleMemoryRetriever(reader=ExplodingReader())
    with pytest.raises(MemoryRetrievalFailure) as caught:
        retriever.retrieve(_query())
    assert caught.value.classification is MemoryRetrievalError.UNAVAILABLE


def test_internal_when_the_ranker_raises(monkeypatch):
    retriever = _retriever(_record())

    def boom(records, query):
        raise ValueError("ranker blew up")

    monkeypatch.setattr(retriever, "_rank", boom)
    with pytest.raises(MemoryRetrievalFailure) as caught:
        retriever.retrieve(_query())
    assert caught.value.classification is MemoryRetrievalError.INTERNAL


def test_every_memory_retrieval_error_member_has_a_producer(monkeypatch):
    """No declared member may be unreachable.

    If a future change collapses the retriever's three regions into one
    handler, a member loses its producer and this fails.
    """
    produced: set[MemoryRetrievalError] = set()

    # INVALID_QUERY
    try:
        _retriever(_record()).retrieve(_query(text=" "))
    except MemoryRetrievalFailure as exc:
        produced.add(exc.classification)

    # UNAVAILABLE
    try:
        SimpleMemoryRetriever(reader=ExplodingReader()).retrieve(_query())
    except MemoryRetrievalFailure as exc:
        produced.add(exc.classification)

    # INTERNAL
    retriever = _retriever(_record())
    monkeypatch.setattr(
        retriever, "_rank", lambda records, query: (_ for _ in ()).throw(ValueError())
    )
    try:
        retriever.retrieve(_query())
    except MemoryRetrievalFailure as exc:
        produced.add(exc.classification)

    missing = set(MemoryRetrievalError) - produced
    assert not missing, (
        "MemoryRetrievalError member(s) with no producer: "
        + ", ".join(sorted(m.name for m in missing))
    )


# --- the failure carries nothing but its classification ---------------------


def test_failure_carries_only_the_enum():
    retriever = SimpleMemoryRetriever(reader=ExplodingReader())
    with pytest.raises(MemoryRetrievalFailure) as caught:
        retriever.retrieve(_query())

    exc = caught.value
    assert str(exc) == "unavailable"
    assert exc.args == ("unavailable",)
    assert exc.__cause__ is None, "an original exception was chained"
    assert exc.__context__ is None, "an original exception leaked via __context__"

    for rendered in (str(exc), repr(exc), str(exc.args)):
        assert SECRET not in rendered
        assert "hunter2" not in rendered
        assert "db.internal" not in rendered
