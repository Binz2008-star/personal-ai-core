"""Memory scope: the Phase 5 recall contract (ADR-014, ADR-015).

The scope decides *whose* memories a query may recall, and the contract makes
it explicit rather than inferred:

    MemoryScope.SESSION (default)  one session's memories -- ADR-009, unchanged
    MemoryScope.USER               the owner's memories across their sessions,
                                   owner derived from the session, never named

The reader owns eligibility (as ADR-009 required), the retriever owns ranking,
and both scopes share the same deterministic total order. These tests pin the
contract the Phase 5 acceptance criteria ask for: cross-session recall,
cross-user isolation, active-only eligibility, superseded/rejected exclusion,
deterministic ordering, and InMemory/SQLite conformance.
"""
from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest

from personal_ai_core.core.domain import Session
from personal_ai_core.core.memory import (
    MemoryEvidence,
    MemoryProvenance,
    MemoryQuery,
    MemoryReader,
    MemoryRecord,
    MemoryRetrievalError,
    MemoryScope,
    MemoryStatus,
    MemoryType,
)
from personal_ai_core.memory.retriever import (
    MemoryRetrievalFailure,
    SimpleMemoryRetriever,
)
from personal_ai_core.persistence.in_memory import (
    InMemorySessionRepository,
    SealedMemoryStore,
)
from personal_ai_core.persistence.memory_store import InMemoryMemoryRepository
from personal_ai_core.persistence.sqlite import (
    SqliteMemoryRepository,
    SqliteSessionRepository,
    connect,
)

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "personal_ai_core"


@pytest.fixture
def db(tmp_path):
    connection = connect(tmp_path / "core.db")
    yield connection
    connection.close()


def _record(
    *,
    memory_id: str | None = None,
    session_id: str = "s1",
    content: str = "prefers Arabic replies",
    language: str = "en",
    confidence: float = 0.9,
    status: MemoryStatus = MemoryStatus.ACTIVE,
) -> MemoryRecord:
    record = MemoryRecord(
        session_id=session_id,
        type=MemoryType.PREFERENCES,
        content=content,
        language=language,
        provenance=MemoryProvenance(
            session_id=session_id, event_id="e1", promoted_by="rule:test"
        ),
        status=status,
        confidence=confidence,
    )
    if memory_id is not None:
        record = replace(record, id=memory_id)
    return record


def _query(**overrides) -> MemoryQuery:
    defaults = {
        "session_id": "s1",
        "text": "what do I prefer",
        "language": "en",
        "limit": 5,
        "scope": MemoryScope.SESSION,
    }
    defaults.update(overrides)
    return MemoryQuery(**defaults)  # type: ignore[arg-type]


# ── the scope on the query ---------------------------------------------------


def test_the_default_scope_is_session():
    """Existing behaviour is the default; nothing silently broadens."""
    query = MemoryQuery(session_id="s1", text="hi", language="en")
    assert query.scope is MemoryScope.SESSION


def test_user_scope_is_explicit_opt_in():
    query = _query(scope=MemoryScope.USER)
    assert query.scope is MemoryScope.USER


def test_an_anchor_session_is_still_required_for_user_scope():
    """The user is derived from the session (ADR-014); the query cannot name
    a user, so the session it anchors to remains part of the contract."""
    with pytest.raises(ValueError):
        MemoryQuery(session_id="", text="hi", language="en", scope=MemoryScope.USER)


# ── the reader: eligibility and isolation ------------------------------------


def _resolver_for(
    sessions: InMemorySessionRepository | SqliteSessionRepository,
):
    def resolve(session_id: str) -> str | None:
        session = sessions.get(session_id)
        return session.user_id if session is not None else None

    return resolve


def _user_slice(
    memories: InMemoryMemoryRepository | SqliteMemoryRepository,
    sessions: InMemorySessionRepository | SqliteSessionRepository,
) -> MemoryReader:
    return MemoryReader(source=memories, session_owner=_resolver_for(sessions))


@pytest.fixture(params=["in_memory", "sqlite"])
def stores(request, db):
    """(memories, sessions) on both substrates, same shape."""
    if request.param == "in_memory":
        return InMemoryMemoryRepository(), InMemorySessionRepository()
    return SqliteMemoryRepository(db), SqliteSessionRepository(db)


def test_user_scope_recalls_across_the_owners_sessions(stores):
    memories, sessions = stores
    # User u1 owns s1 and s2; user u2 owns s3.
    sessions.add(Session(user_id="u1", id="s1"))
    sessions.add(Session(user_id="u1", id="s2"))
    sessions.add(Session(user_id="u2", id="s3"))
    mine_a = _record(memory_id="m1", session_id="s1", content="prefers Arabic")
    mine_b = _record(memory_id="m2", session_id="s2", content="prefers concise")
    theirs = _record(memory_id="m3", session_id="s3", content="prefers Haskell")
    for record in (mine_a, mine_b, theirs):
        memories.write(record)

    reader = _user_slice(memories, sessions)
    recalled = reader.list_active_for_session_owner("s2")

    assert [r.id for r in recalled] == ["m1", "m2"]


def test_user_scope_never_crosses_users(stores):
    memories, sessions = stores
    sessions.add(Session(user_id="u1", id="s1"))
    sessions.add(Session(user_id="u2", id="s2"))
    memories.write(_record(memory_id="m1", session_id="s1"))
    memories.write(_record(memory_id="m2", session_id="s2"))

    reader = _user_slice(memories, sessions)
    result = reader.list_active_for_session_owner("s1")
    assert [r.id for r in result] == ["m1"]


def test_user_scope_anchor_with_no_owner_recalls_nothing(stores):
    memories, sessions = stores
    sessions.add(Session(user_id="u1", id="s1"))
    memories.write(_record(memory_id="m1", session_id="s1"))

    reader = _user_slice(memories, sessions)
    assert reader.list_active_for_session_owner("ghost-session") == ()


def test_user_scope_never_attributes_a_record_with_no_owner(stores):
    memories, sessions = stores
    sessions.add(Session(user_id="u1", id="s1"))
    memories.write(_record(memory_id="m1", session_id="s1"))
    # s9 exists in *memories* but in no session store: nobody owns it.
    memories.write(_record(memory_id="m9", session_id="s9"))

    reader = _user_slice(memories, sessions)
    assert [r.id for r in reader.list_active_for_session_owner("s1")] == ["m1"]


def test_user_scope_requires_a_resolver():
    reader = MemoryReader(source=InMemoryMemoryRepository())
    with pytest.raises(ValueError):
        reader.list_active_for_session_owner("s1")


def test_rejected_and_superseded_are_not_eligible_in_user_scope(stores):
    memories, sessions = stores
    sessions.add(Session(user_id="u1", id="s1"))
    sessions.add(Session(user_id="u1", id="s2"))
    active = _record(memory_id="m1", session_id="s1")
    rejected = _record(
        memory_id="m2", session_id="s1", content="rejected thing",
        status=MemoryStatus.REJECTED,
    )
    superseded = _record(
        memory_id="m3", session_id="s2", content="superseded thing",
        status=MemoryStatus.SUPERSEDED,
    )
    for record in (active, rejected, superseded):
        memories.write(record)

    reader = _user_slice(memories, sessions)
    assert [r.id for r in reader.list_active_for_session_owner("s1")] == ["m1"]


def test_the_reader_grows_no_write_surface(stores):
    """User scope is a *read* widening; the boundary stays read-only."""
    memories, sessions = stores
    reader = _user_slice(memories, sessions)
    assert not hasattr(reader, "write")
    assert not hasattr(reader, "supersede")


def test_a_sealed_store_still_degrades_visibly_under_user_scope():
    """The sealed conversation-path store keeps refusing reads in both
    scopes; user scope must not accidentally make it answerable. The reader
    reaches the store (the anchor resolves to an owner), the store raises,
    and the retriever classifies the failure UNAVAILABLE."""
    reader = MemoryReader(
        source=SealedMemoryStore(),
        session_owner=lambda sid: "u1" if sid == "s1" else None,
    )
    with pytest.raises(Exception):
        reader.list_active_for_session_owner("s1")

    retriever = SimpleMemoryRetriever(reader=reader)
    with pytest.raises(MemoryRetrievalFailure) as caught:
        retriever.retrieve(_query(scope=MemoryScope.USER))
    assert caught.value.classification is MemoryRetrievalError.UNAVAILABLE


# ── the retriever: dispatch, isolation, classification -----------------------


def test_user_scope_returns_the_owner_across_sessions(stores):
    memories, sessions = stores
    sessions.add(Session(user_id="u1", id="s1"))
    sessions.add(Session(user_id="u1", id="s2"))
    sessions.add(Session(user_id="u2", id="s3"))
    memories.write(_record(memory_id="m1", session_id="s1", content="prefers Arabic"))
    memories.write(_record(memory_id="m2", session_id="s2", content="prefers concise"))
    memories.write(_record(memory_id="m3", session_id="s3", content="prefers Haskell"))
    retriever = SimpleMemoryRetriever(
        reader=MemoryReader(
            source=memories, session_owner=_resolver_for(sessions)
        )
    )

    evidence = retriever.retrieve(_query(session_id="s2", scope=MemoryScope.USER))

    assert {e.record.id for e in evidence} == {"m1", "m2"}
    assert all(isinstance(e, MemoryEvidence) for e in evidence)


def test_session_scope_is_the_default_and_still_hard(stores):
    """A query that does not ask for USER gets ADR-009 behaviour: only this
    session, even though the same owner also has other sessions."""
    memories, sessions = stores
    sessions.add(Session(user_id="u1", id="s1"))
    sessions.add(Session(user_id="u1", id="s2"))
    memories.write(_record(memory_id="m1", session_id="s1"))
    memories.write(_record(memory_id="m2", session_id="s2"))
    retriever = SimpleMemoryRetriever(
        reader=MemoryReader(
            source=memories, session_owner=_resolver_for(sessions)
        )
    )

    assert [e.record.id for e in retriever.retrieve(_query(session_id="s1"))] == ["m1"]


def test_user_scope_without_a_resolver_is_unavailable():
    """A missing resolver is a configuration gap, not a query error: the
    retriever classifies it UNAVAILABLE rather than leaking the ValueError."""
    retriever = SimpleMemoryRetriever(
        reader=MemoryReader(source=InMemoryMemoryRepository())
    )
    with pytest.raises(MemoryRetrievalFailure) as caught:
        retriever.retrieve(_query(scope=MemoryScope.USER))
    assert caught.value.classification is MemoryRetrievalError.UNAVAILABLE


def test_user_scope_ranking_is_deterministic(stores):
    memories, sessions = stores
    sessions.add(Session(user_id="u1", id="s1"))
    sessions.add(Session(user_id="u1", id="s2"))
    for i in range(4):
        memories.write(
            _record(
                memory_id=f"m{i}",
                session_id="s1" if i % 2 == 0 else "s2",
                content=f"prefers thing {i}",
                confidence=0.5 + i * 0.1,
            )
        )
    retriever = SimpleMemoryRetriever(
        reader=MemoryReader(
            source=memories, session_owner=_resolver_for(sessions)
        )
    )
    query = _query(session_id="s2", scope=MemoryScope.USER)

    first = retriever.retrieve(query)
    second = retriever.retrieve(query)

    assert [e.record.id for e in first] == [e.record.id for e in second]
    assert [e.relevance for e in first] == [e.relevance for e in second]


def test_user_scope_never_crosses_users_at_the_retriever(stores):
    memories, sessions = stores
    sessions.add(Session(user_id="u1", id="s1"))
    sessions.add(Session(user_id="u1", id="s2"))
    sessions.add(Session(user_id="u2", id="s3"))
    memories.write(_record(memory_id="m1", session_id="s1"))
    memories.write(_record(memory_id="m2", session_id="s2"))
    memories.write(_record(memory_id="m3", session_id="s3"))
    retriever = SimpleMemoryRetriever(
        reader=MemoryReader(
            source=memories, session_owner=_resolver_for(sessions)
        )
    )

    evidence = retriever.retrieve(_query(session_id="s2", scope=MemoryScope.USER))
    assert all(e.record.id != "m3" for e in evidence)


def test_limit_truncates_in_user_scope(stores):
    memories, sessions = stores
    sessions.add(Session(user_id="u1", id="s1"))
    sessions.add(Session(user_id="u1", id="s2"))
    for i in range(6):
        memories.write(
            _record(memory_id=f"m{i}", session_id="s1", content=f"prefers {i}")
        )
    retriever = SimpleMemoryRetriever(
        reader=MemoryReader(
            source=memories, session_owner=_resolver_for(sessions)
        )
    )
    assert len(
        retriever.retrieve(_query(session_id="s1", scope=MemoryScope.USER, limit=3))
    ) == 3


# ── InMemory and SQLite agree on user scope ----------------------------------


def test_the_two_substrates_agree_on_user_scope(db):
    """The observable retrieval result is identical on both substrates:
    same ids, same order, same relevance."""
    in_mem_memories = InMemoryMemoryRepository()
    in_mem_sessions = InMemorySessionRepository()
    sqlite_memories = SqliteMemoryRepository(db)
    sqlite_sessions = SqliteSessionRepository(db)

    # One set of logical records, written to both substrates. Constructing a
    # fresh _record per store would stamp each with its own utcnow(), and the
    # retriever's recency tie-break would then legitimately order the two
    # stores differently whenever construction straddles a clock tick -- the
    # nondeterminism this conformance test exists to catch (found by
    # suite-windows, not by the local run).
    mine_a = _record(memory_id="m1", session_id="s1", content="prefers Arabic")
    mine_b = _record(memory_id="m2", session_id="s2", content="prefers concise")
    theirs = _record(memory_id="m3", session_id="s3", content="prefers Haskell")
    for memories, sessions in (
        (in_mem_memories, in_mem_sessions),
        (sqlite_memories, sqlite_sessions),
    ):
        sessions.add(Session(user_id="u1", id="s1"))
        sessions.add(Session(user_id="u1", id="s2"))
        sessions.add(Session(user_id="u2", id="s3"))
        for record in (mine_a, mine_b, theirs):
            memories.write(record)

    def recall(memories, sessions):
        retriever = SimpleMemoryRetriever(
            reader=MemoryReader(
                source=memories, session_owner=_resolver_for(sessions)
            )
        )
        return retriever.retrieve(
            _query(session_id="s2", scope=MemoryScope.USER)
        )

    in_memory = recall(in_mem_memories, in_mem_sessions)
    sqlite = recall(sqlite_memories, sqlite_sessions)

    assert [e.record.id for e in in_memory] == [e.record.id for e in sqlite]
    assert [e.relevance for e in in_memory] == [e.relevance for e in sqlite]
    assert {e.record.id for e in sqlite} == {"m1", "m2"}


# ── no dead scope members ----------------------------------------------------


def test_every_memory_scope_member_has_a_producer_in_src():
    """The same rule the repo enforces for RetrievalMethod and ExclusionReason
    applies here: a declared scope member must be reachable from src/.

    Producers: SESSION is the default at every production query site
    (conversation/grounding.py); USER is the retriever's scope dispatch
    (memory/retriever.py). The defining module does not count.
    """
    produced: set[str] = set()
    for path in SRC_ROOT.rglob("*.py"):
        if path.name == "memory.py" and path.parent.name == "core":
            continue  # the declaration itself is not a producer
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id == "MemoryScope" and isinstance(node.attr, str):
                    produced.add(node.attr)

    missing = {member.name for member in MemoryScope} - produced
    assert not missing, (
        "MemoryScope member(s) with no producer in src/: "
        + ", ".join(sorted(missing))
    )