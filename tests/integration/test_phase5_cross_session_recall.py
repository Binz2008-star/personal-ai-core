"""Phase 5: cross-session, user-scoped recall on the durable store.

`build_persistent_service(grounded=True)` now composes the durable
`SqliteMemoryRepository` behind a `MemoryReader` that resolves ownership from
the session store (ADR-014), feeding the same deterministic retriever the
in-memory slice uses (ADR-015).

The slice hands the store back (`PersistentSlice.memories`) for the same
reason it hands the indexes back: a recall path whose stored state cannot be
inspected cannot be debugged. These tests drive the composition the way a
caller would — promote real memories through `ExperiencePipeline` in one
session, recall them from another session of the same user, across a process
restart — and pin the isolation that must survive restart.
"""
from __future__ import annotations

from personal_ai_core.conversation.factory import build_persistent_service
from personal_ai_core.core.domain import EventType
from personal_ai_core.core.memory import (
    ExperienceRecord,
    MemoryQuery,
    MemoryReader,
    MemoryScope,
)
from personal_ai_core.memory import SimpleMemoryRetriever
from personal_ai_core.memory.gate import DefaultPromotionGate
from personal_ai_core.memory.pipeline import ExperiencePipeline
from personal_ai_core.memory.rules import ExplicitInstructionRule
from personal_ai_core.persistence.sqlite import (
    SqliteEventRepository,
    SqliteMemoryRepository,
    SqliteSessionRepository,
)

EXPERIENCE = "remember that I prefer concise replies in Arabic"
FOREIGN_EXPERIENCE = "remember that I prefer Haskell to everything"


def fake_transport(url, payload, timeout):
    return {
        "model": payload["model"],
        "message": {"content": "ok"},
        "done_reason": "stop",
    }


def _promote(connection, session_id: str, text: str) -> None:
    """Run the real promotion pipeline over the slice's own connection."""
    pipeline = ExperiencePipeline(
        rules=[ExplicitInstructionRule()],
        gate=DefaultPromotionGate(),
        store=SqliteMemoryRepository(connection),
        events=SqliteEventRepository(connection),
    )
    outcomes = pipeline.ingest(
        ExperienceRecord(session_id=session_id, text=text)
    )
    assert outcomes, f"no promotion outcome for {text!r}"
    assert any(o.decision.value == "promoted" for o in outcomes)


def _resolver(slice_):
    sessions = SqliteSessionRepository(slice_.connection)

    def resolve(session_id: str) -> str | None:
        session = sessions.get(session_id)
        return session.user_id if session is not None else None

    return resolve


def _user_retriever(slice_):
    return SimpleMemoryRetriever(
        reader=MemoryReader(
            source=slice_.memories, session_owner=_resolver(slice_)
        )
    )


def test_recall_is_composed_into_the_persistent_grounded_slice(tmp_path):
    """Wiring is observable, not assumed: a turn through the factory-built
    service reports recall configured, and the durable store is handed back."""
    db = tmp_path / "core.db"
    slice_ = build_persistent_service(
        database=db, transport=fake_transport, grounded=True
    )
    try:
        assert slice_.memories is not None
        user = slice_.service.create_user()
        session = slice_.service.start_session(user.id)
        slice_.service.send(
            session_id=session.id, content="hello there"
        )

        assembled = [
            e
            for e in slice_.events.list_for_session(session.id)
            if e.type is EventType.CONTEXT_ASSEMBLED
        ]
        assert assembled, "no CONTEXT_ASSEMBLED event recorded"
        payload = assembled[-1].payload
        assert payload["memory_enabled"] is True
        assert payload["memories_retrieved"] == 0
    finally:
        slice_.close()


def test_cross_session_user_recall_survives_a_restart(tmp_path):
    """The full Phase 5 scenario on the durable store.

    User U states a preference in session A. In a later run, in another of
    U's sessions (B), user-scoped recall must surface A's memory. User V's
    memory, promoted in V's session, must never surface for U -- and the
    default session scope must still see nothing cross-session.
    """
    db = tmp_path / "core.db"

    # --- run 1: sessions exist, memories are promoted ---------------------
    slice_ = build_persistent_service(database=db, grounded=True)
    try:
        u = slice_.service.create_user()
        u_a = slice_.service.start_session(u.id)
        u_b = slice_.service.start_session(u.id)
        v = slice_.service.create_user()
        v_c = slice_.service.start_session(v.id)

        _promote(slice_.connection, u_a.id, EXPERIENCE)
        _promote(slice_.connection, v_c.id, FOREIGN_EXPERIENCE)
    finally:
        slice_.close()

    # --- run 2: the same file, a fresh process-shaped composition ---------
    slice2 = build_persistent_service(database=db, grounded=True)
    try:
        retriever = _user_retriever(slice2)

        cross_session = retriever.retrieve(
            MemoryQuery(
                session_id=u_b.id,
                text="what do I prefer",
                language="en",
                scope=MemoryScope.USER,
            )
        )
        assert cross_session, "owner's memory did not survive the restart"
        assert any(EXPERIENCE in e.record.content for e in cross_session)
        assert all(
            FOREIGN_EXPERIENCE not in e.record.content for e in cross_session
        ), "another user's memory leaked into user-scoped recall"

        # The default scope is still session-scoped: B shares its owner with
        # A but inherits none of A's memories unless USER is requested.
        same_session = retriever.retrieve(
            MemoryQuery(
                session_id=u_b.id,
                text="what do I prefer",
                language="en",
                scope=MemoryScope.SESSION,
            )
        )
        assert same_session == ()
    finally:
        slice2.close()


def test_default_turn_recall_remains_session_scoped_across_a_restart(tmp_path):
    """Wiring recall does not broaden turns: a turn from session B of user U
    does not surface a memory promoted in A of the same user, because the
    composition's default scope is SESSION (ADR-015)."""
    db = tmp_path / "core.db"

    slice_ = build_persistent_service(database=db, transport=fake_transport, grounded=True)
    try:
        u = slice_.service.create_user()
        u_a = slice_.service.start_session(u.id)
        u_b = slice_.service.start_session(u.id)
        _promote(slice_.connection, u_a.id, EXPERIENCE)
    finally:
        slice_.close()

    slice2 = build_persistent_service(database=db, transport=fake_transport, grounded=True)
    try:
        slice2.service.send(
            session_id=u_b.id, content="what do I prefer in replies"
        )
        [assembled] = [
            e
            for e in slice2.events.list_for_session(u_b.id)
            if e.type is EventType.CONTEXT_ASSEMBLED
        ]
        payload = assembled.payload
        assert payload["memory_enabled"] is True
        assert payload["memories_retrieved"] == 0
        # The foreign default is quiet, but the capability is there for the
        # explicit query.
        assert _user_retriever(slice2).retrieve(
            MemoryQuery(
                session_id=u_b.id,
                text="what do I prefer",
                language="en",
                scope=MemoryScope.USER,
            )
        )
    finally:
        slice2.close()


def test_memories_promoted_before_a_restart_stay_isolated_by_owner(tmp_path):
    """Ownership is derived from the persisted session rows after restart;
    two users sharing the store can never read each other across sessions."""
    db = tmp_path / "core.db"

    slice_ = build_persistent_service(database=db, grounded=True)
    try:
        u = slice_.service.create_user()
        u_b = slice_.service.start_session(u.id)
        v = slice_.service.create_user()
        v_c = slice_.service.start_session(v.id)
        _promote(slice_.connection, v_c.id, FOREIGN_EXPERIENCE)
    finally:
        slice_.close()

    slice2 = build_persistent_service(database=db, grounded=True)
    try:
        retriever = _user_retriever(slice2)
        recalled = retriever.retrieve(
            MemoryQuery(
                session_id=u_b.id,
                text="anything at all",
                language="en",
                scope=MemoryScope.USER,
            )
        )
        assert recalled == ()
    finally:
        slice2.close()