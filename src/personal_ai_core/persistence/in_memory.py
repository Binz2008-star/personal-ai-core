"""In-process repository implementations.

Phase 1 storage. Postgres arrives with the persistence migrations in a later
phase; the contracts do not change when it does -- that is the point of
declaring them in `core.contracts`.

Named `in_memory` for the process-memory sense only. It has nothing to do with
the Core's `memory/` subsystem, which does not exist yet (ADR-003).
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..core.domain import Event, Message, Session, User
from ..core.errors import InvariantViolation


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
    """A MemoryStore that refuses every write.

    Phase 1 has no memory subsystem, and no conversation path may create one.
    This makes that structural instead of aspirational: if any code in the
    conversation flow ever reaches for memory, it raises here rather than
    quietly succeeding.

    `attempted_writes` lets a test assert zero attempts were even made, which
    is the stronger claim.
    """

    def __init__(self) -> None:
        self.attempted_writes = 0

    def write(self, record: Mapping[str, Any]) -> None:
        self.attempted_writes += 1
        raise InvariantViolation(
            "Event != Memory: a conversation turn attempted to write memory "
            "directly. Memory is written only by the promotion gate, which is "
            "a later phase. See ADR-003."
        )
