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
