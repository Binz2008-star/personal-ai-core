"""Domain types for the Phase 1 vertical slice.

Pure data. No I/O, no provider, no storage, no framework. Everything here is
frozen: domain objects are values, and state changes produce new objects rather
than mutating shared ones.

`Event` is deliberately *not* a memory. See `docs/MEMORY_ARCHITECTURE.md` and
ADR-003: conversations produce events; memories are promoted from experience by
a separate subsystem that does not exist yet.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Role(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class SessionStatus(str, Enum):
    ACTIVE = "active"
    CLOSED = "closed"


class EventType(str, Enum):
    """Conversation events recorded in Phase 1, extended in Phase 2.

    This is not the full learning event taxonomy; Phase 3 extends it further.

    `CONTEXT_ASSEMBLED` and `RETRIEVAL_FAILED` exist because grounding an
    answer in retrieved evidence is only trustworthy if what was retrieved,
    what was dropped and why are all recoverable afterwards. An answer that
    cites evidence nobody can reconstruct is not grounded, it is decorated.
    """

    SESSION_STARTED = "session.started"
    MESSAGE_RECEIVED = "message.received"
    CONTEXT_ASSEMBLED = "context.assembled"
    RETRIEVAL_FAILED = "retrieval.failed"
    GENERATION_REQUESTED = "generation.requested"
    GENERATION_COMPLETED = "generation.completed"
    GENERATION_FAILED = "generation.failed"
    SESSION_CLOSED = "session.closed"


# Language is a Phase 1 field by decision, not a Phase 2 feature: messages are
# written now, and retrofitting a language column later would require migrating
# every stored record. "und" is ISO 639-2 for undetermined.
UNDETERMINED_LANGUAGE = "und"


@dataclass(frozen=True, slots=True)
class User:
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class Session:
    user_id: str
    id: str = field(default_factory=new_id)
    status: SessionStatus = SessionStatus.ACTIVE
    created_at: datetime = field(default_factory=utcnow)

    def closed(self) -> "Session":
        return Session(
            user_id=self.user_id,
            id=self.id,
            status=SessionStatus.CLOSED,
            created_at=self.created_at,
        )


@dataclass(frozen=True, slots=True)
class Message:
    session_id: str
    role: Role
    content: str
    language: str = UNDETERMINED_LANGUAGE
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class Event:
    """An immutable, append-only record that something happened.

    An event is evidence. It is never edited and never deleted; a correction is
    a new event. It is not a memory and does not become one by being recorded.
    """

    session_id: str
    type: EventType
    payload: Mapping[str, Any] = field(default_factory=dict)
    message_id: str | None = None
    actor: str = "system"
    id: str = field(default_factory=new_id)
    occurred_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        # Freeze the payload so a caller holding a reference cannot mutate a
        # recorded event after the fact.
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """What a ModelProvider returns. Provider-neutral by construction."""

    text: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)
