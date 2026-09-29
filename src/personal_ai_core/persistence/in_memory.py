"""In-process repository implementations.

Phase 1 storage. Postgres arrives with the persistence migrations in a later
phase; the contracts do not change when it does -- that is the point of
declaring them in `core.contracts`.

Named `in_memory` for the process-memory sense only. It has nothing to do with
the Core's `memory/` subsystem, which does not exist yet (ADR-003).
"""
from __future__ import annotations

from typing import Sequence

from ..core.domain import Event, Message, Session, User
from ..core.errors import InvariantViolation
from ..core.feedback import (
    FEEDBACK_EVENT_TYPE,
    FEEDBACK_IDEMPOTENCY_KEY,
    FeedbackRecord,
    as_feedback_event,
    feedback_record_from_event,
)
from ..core.memory import MemoryRecord


class InMemoryUserRepository:
    def __init__(self) -> None:
        self._users: dict[str, User] = {}

    def add(self, user: User) -> None:
        self._users[user.id] = user

    def get(self, user_id: str) -> User | None:
        return self._users.get(user_id)


class InMemorySessionRepository:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def add(self, session: Session) -> None:
        self._sessions[session.id] = session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def update(self, session: Session) -> None:
        if session.id not in self._sessions:
            raise KeyError(f"unknown session: {session.id}")
        self._sessions[session.id] = session


class InMemoryMessageRepository:
    def __init__(self) -> None:
        self._messages: list[Message] = []

    def add(self, message: Message) -> None:
        self._messages.append(message)

    def list_for_session(self, session_id: str) -> Sequence[Message]:
        return tuple(m for m in self._messages if m.session_id == session_id)


class InMemoryEventRepository:
    """Append-only. Exposes no update or delete, by design."""

    def __init__(self) -> None:
        self._events: list[Event] = []

    def append(self, event: Event) -> None:
        self._events.append(event)

    def list_for_session(self, session_id: str) -> Sequence[Event]:
        return tuple(e for e in self._events if e.session_id == session_id)

    def all(self) -> Sequence[Event]:
        return tuple(self._events)


class InMemoryFeedbackRepository:
    """Process-memory implementation of `core.contracts.FeedbackRepository`.

    The durable form of a `FeedbackRecord` is a `FEEDBACK_RECORDED` event in
    the wrapped event store, so this implementation and the SQLite one keep
    the same single source of truth (ADR-017 §3.1, review point 6) and reads
    reconstruct records through the shared `feedback_record_from_event`.

    Atomic, idempotent `append` (review point 1): the key check and the write
    happen inside one confined method with no await/yield between them, so
    under this deployment's single writer a duplicate can never double-record.
    Source-event existence is enforced at the same boundary, before anything
    is written.
    """

    def __init__(self, events: InMemoryEventRepository) -> None:
        self._events = events
        self._keys: set[str] = set()

    def append(self, record: FeedbackRecord) -> FeedbackRecord:
        # Source validation FIRST, then the key check, then the write -- the
        # same order `SqliteFeedbackRepository` uses, and deliberately so.
        #
        # This order was previously inverted here (key first), which made the
        # in-memory backend the odd one out: a record that both reused a stored
        # key AND named a source that does not exist was returned as a
        # duplicate in memory but refused as invalid in SQLite. Two
        # implementations of one protocol answering differently on the same
        # input is not a documented nuance, it is a backend-dependent answer
        # to "is this feedback valid?" -- and the answer differs by which one
        # you happened to configure.
        #
        # A record that names nothing is invalid on its own terms whatever
        # happens to be stored, so the invalidity wins. The corner is only
        # reachable from a hand-written key that contradicts its own fields;
        # `feedback_idempotency_key` derives the key FROM `source_event_id`, so
        # a derived key cannot reach it.
        if not self._source_exists(record):
            raise ValueError(
                f"feedback source_event_id {record.source_event_id!r} does not "
                f"exist in session {record.session_id!r}: a judgement of "
                "nothing is not feedback (ADR-017)"
            )
        if record.idempotency_key in self._keys:
            return self._read_by_key(record.idempotency_key)
        self._keys.add(record.idempotency_key)
        self._events.append(as_feedback_event(record))
        return record

    def list_for_source(self, source_event_id: str) -> Sequence[FeedbackRecord]:
        return tuple(
            feedback_record_from_event(e)
            for e in self._events.all()
            if e.type is FEEDBACK_EVENT_TYPE
            and e.payload.get("source_event_id") == source_event_id
        )

    def list_for_session(self, session_id: str) -> Sequence[FeedbackRecord]:
        return tuple(
            feedback_record_from_event(e)
            for e in self._events.all()
            if e.type is FEEDBACK_EVENT_TYPE
            and e.session_id == session_id
        )

    def _source_exists(self, record: FeedbackRecord) -> bool:
        return any(
            e.id == record.source_event_id and e.session_id == record.session_id
            for e in self._events.all()
        )

    def _read_by_key(self, idempotency_key: str) -> FeedbackRecord:
        for e in self._events.all():
            if (
                e.type is FEEDBACK_EVENT_TYPE
                and e.payload.get(FEEDBACK_IDEMPOTENCY_KEY) == idempotency_key
            ):
                return feedback_record_from_event(e)
        raise RuntimeError(
            "feedback idempotency key registered but its event is missing "
            "from the store; the append invariant is broken"
        )


class SealedMemoryStore:
    """A MemoryStore that refuses every operation.

    The conversation path is given this store rather than the real one so
    the `Event != Memory` invariant is structural, not aspirational: any
    memory operation from the conversation flow raises here rather than
    quietly succeeding (ADR-003).

    Every method the `MemoryStore` protocol declares is implemented so the
    sealed store still satisfies `isinstance(sealed, MemoryStore)` after
    Phase 3 upgrades the protocol, and its annotations conform to the
    typed contract. Each method raises before touching its argument, so
    Python's non-enforcement of annotations means the existing test that
    passes a dict to `write` still exercises the "loud refusal" path
    unchanged.

    `attempted_writes` lets a test assert zero attempts were even made,
    which is the stronger claim than "writes raised."
    """

    _SEAL_MESSAGE = (
        "Event != Memory: a conversation turn attempted to reach memory "
        "directly. Memory is written only by the promotion gate. See ADR-003."
    )

    def __init__(self) -> None:
        self.attempted_writes = 0

    def write(self, record: MemoryRecord) -> MemoryRecord:
        # Annotation matches the MemoryStore protocol. Python does not
        # enforce it, so `test_the_sealed_store_refuses_writes_loudly` can
        # still pass a plain dict here and the raise fires before any
        # attribute access -- runtime behavior is unchanged, and the
        # existing behavioral test is byte-for-byte unchanged.
        self.attempted_writes += 1
        raise InvariantViolation(self._SEAL_MESSAGE)

    def read(self, memory_id: str) -> MemoryRecord | None:
        raise InvariantViolation(self._SEAL_MESSAGE)

    def list_active(self) -> Sequence[MemoryRecord]:
        raise InvariantViolation(self._SEAL_MESSAGE)

    def supersede(self, old_id: str, new_record: MemoryRecord) -> MemoryRecord:
        raise InvariantViolation(self._SEAL_MESSAGE)
