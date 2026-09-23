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


# ADR-010's prerequisite for any durable backend, settled here rather than in
# an adapter, because it is a property of the event and not of where it is
# stored.
#
# `Event.payload` was `Mapping[str, Any]` with nothing checking the values:
#
#     Event(payload={"obj": object(), "fn": len})   -> constructed happily
#     json.dumps(dict(event.payload))               -> TypeError
#
# A caller could record an event that cannot be written down, and nothing said
# so until the write failed -- at which point the event is the thing being
# lost. An event that cannot be stored is not evidence.
#
# The check is STRUCTURAL, not `json.dumps`. json.dumps accepts a tuple and
# returns a list, so a round trip gives back something other than what was
# written; it also accepts NaN and Infinity, which are not JSON at all. A
# store that quietly changes your data is worse than one that refuses it, so
# both are rejected here, where the caller that built them is still on the
# stack.
def _check_json_value(value: Any, path: str) -> None:
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return
    if isinstance(value, float):
        # NaN and the infinities have no JSON representation. `json.dumps`
        # emits them anyway, producing output no other parser accepts.
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(
                f"event payload at {path} is {value!r}, which has no JSON "
                "representation. Record a string or omit the key."
            )
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(
                    f"event payload at {path} has a non-string key {key!r}. "
                    "JSON object keys are strings, and converting silently "
                    "would change what is read back."
                )
            _check_json_value(item, f"{path}[{key!r}]")
        return
    if isinstance(value, tuple):
        raise ValueError(
            f"event payload at {path} is a tuple. It would be read back as a "
            "list, so the event would not survive a round trip unchanged. "
            "Use a list."
        )
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check_json_value(item, f"{path}[{index}]")
        return
    raise ValueError(
        f"event payload at {path} is {type(value).__name__}, which cannot be "
        "written to a durable store. Payload values must be strings, numbers, "
        "booleans, None, lists or string-keyed mappings of those."
    )


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
    """Conversation events recorded in Phase 1, extended in Phase 2 and Phase 3.

    `CONTEXT_ASSEMBLED` and `RETRIEVAL_FAILED` exist because grounding an
    answer in retrieved evidence is only trustworthy if what was retrieved,
    what was dropped and why are all recoverable afterwards. An answer that
    cites evidence nobody can reconstruct is not grounded, it is decorated.

    The `MEMORY_*` members are emitted by the Phase 3 promotion pipeline and
    are never produced on the conversation path (ADR-003). Their string
    values deliberately begin with "memory." so that a leak into the
    conversation event stream would fail `test_event_not_memory.py`.
    """

    SESSION_STARTED = "session.started"
    MESSAGE_RECEIVED = "message.received"
    CONTEXT_ASSEMBLED = "context.assembled"
    RETRIEVAL_FAILED = "retrieval.failed"
    GENERATION_REQUESTED = "generation.requested"
    GENERATION_COMPLETED = "generation.completed"
    GENERATION_FAILED = "generation.failed"
    SESSION_CLOSED = "session.closed"
    MEMORY_PROMOTED = "memory.promoted"
    MEMORY_REJECTED = "memory.rejected"
    MEMORY_CONFLICT_DETECTED = "memory.conflict.detected"
    # AGENT_ARCHITECTURE.md section 1, RECORD EVENT: "always, success or
    # failure". One per tool request, whatever the policy decided, and one
    # when a run ends. Emitted only by agent/loop.py.
    AGENT_STEP = "agent.step"
    AGENT_FINISHED = "agent.finished"


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
        # Checked before freezing, and at construction rather than at write
        # time: the caller that built an unstorable payload is on the stack
        # here, and is not when a store later fails to serialise it.
        for key, value in self.payload.items():
            if not isinstance(key, str):
                raise ValueError(
                    f"event payload has a non-string key {key!r}. JSON object "
                    "keys are strings, and converting silently would change "
                    "what is read back."
                )
            _check_json_value(value, f"payload[{key!r}]")
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
