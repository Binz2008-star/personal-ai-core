"""ConversationService — the Phase 1 vertical slice.

    User -> Session -> Message -> ModelProvider -> OllamaProvider
         -> Boss model -> Response -> Event

Application layer. It orchestrates domain objects and contracts, and knows
nothing about Ollama, HTTP or storage engines: every collaborator arrives as a
protocol.

It records events. It never writes memory.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..core.contracts import (
    EventRepository,
    MessageRepository,
    ModelProvider,
    ModelRegistry,
    SessionRepository,
    UserRepository,
)
from ..core.domain import (
    UNDETERMINED_LANGUAGE,
    Event,
    EventType,
    Message,
    Role,
    Session,
    User,
)
from ..core.errors import ProviderError
from .events import EventRecorder


class ConversationService:
    def __init__(
        self,
        *,
        users: UserRepository,
        sessions: SessionRepository,
        messages: MessageRepository,
        events: EventRepository,
        provider: ModelProvider,
        registry: ModelRegistry,
    ) -> None:
        self._users = users
        self._sessions = sessions
        self._messages = messages
        self._provider = provider
        self._registry = registry
        self._recorder = EventRecorder(events)

    def create_user(self) -> User:
        user = User()
        self._users.add(user)
        return user

    def start_session(self, user_id: str) -> Session:
        session = Session(user_id=user_id)
        self._sessions.add(session)
        self._recorder.record(
            session_id=session.id,
            type=EventType.SESSION_STARTED,
            payload={"user_id": user_id},
            actor=user_id,
        )
        return session

    def send(
        self,
        *,
        session_id: str,
        content: str,
        language: str = UNDETERMINED_LANGUAGE,
        options: Mapping[str, Any] | None = None,
    ) -> Message:
        """One conversation turn.

        Persists the user message, generates a reply through the active model,
        persists the reply, and records an event at each step -- including on
        failure, because a failed turn is evidence too.
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"unknown session: {session_id}")

        user_message = Message(
            session_id=session_id,
            role=Role.USER,
            content=content,
            language=language,
        )
        self._messages.add(user_message)
        self._recorder.record(
            session_id=session_id,
            type=EventType.MESSAGE_RECEIVED,
            payload={"role": Role.USER.value, "language": language},
            message_id=user_message.id,
            actor=session.user_id,
        )

        spec = self._registry.active
        history = self._messages.list_for_session(session_id)

        self._recorder.record(
            session_id=session_id,
            type=EventType.GENERATION_REQUESTED,
            payload={
                "model": spec.name,
                "provider": spec.provider,
                "message_count": len(history),
            },
            message_id=user_message.id,
        )

        try:
            response = self._provider.generate(
                model=spec.name, messages=history, options=options
            )
        except ProviderError as exc:
            self._recorder.record(
                session_id=session_id,
                type=EventType.GENERATION_FAILED,
                payload={"model": spec.name, "error": str(exc)},
                message_id=user_message.id,
            )
            raise

        reply = Message(
            session_id=session_id,
            role=Role.ASSISTANT,
            content=response.text,
            language=language,
        )
        self._messages.add(reply)
        self._recorder.record(
            session_id=session_id,
            type=EventType.GENERATION_COMPLETED,
            payload={
                "model": response.model,
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
                "finish_reason": response.finish_reason,
            },
            message_id=reply.id,
        )
        return reply

    def close_session(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"unknown session: {session_id}")
        closed = session.closed()
        self._sessions.update(closed)
        self._recorder.record(session_id=session_id, type=EventType.SESSION_CLOSED)
        return closed

    def history(self, session_id: str) -> Sequence[Message]:
        return self._messages.list_for_session(session_id)
