"""Event recorder.

Records what happened. Records only -- it draws no conclusions, updates no
state and writes no memory. Phase 3's learning pipeline consumes these events;
it does not share this module (ARCHITECTURE.md §3 lists `learning/events`
separately, and this is the conversation-side recorder).
"""
from __future__ import annotations

from typing import Any, Mapping

from ..core.contracts import EventRepository
from ..core.domain import Event, EventType


class EventRecorder:
    def __init__(self, events: EventRepository) -> None:
        self._events = events

    def record(
        self,
        *,
        session_id: str,
        type: EventType,
        payload: Mapping[str, Any] | None = None,
        message_id: str | None = None,
        actor: str = "system",
    ) -> Event:
        event = Event(
            session_id=session_id,
            type=type,
            payload=payload or {},
            message_id=message_id,
            actor=actor,
        )
        self._events.append(event)
        return event
