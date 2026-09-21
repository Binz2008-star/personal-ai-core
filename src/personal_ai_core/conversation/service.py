"""ConversationService — the vertical slice.

    User -> Session -> Message -> [retrieve -> budget -> assemble]
         -> ModelProvider -> OllamaProvider -> Boss model -> Response -> Event

Application layer. It orchestrates domain objects and contracts, and knows
nothing about Ollama, HTTP, an index or a storage engine: every collaborator
arrives as a protocol or as a same-layer object built from protocols.

Grounding is **optional**. With no `context_builder` the turn behaves exactly
as it did before retrieval existed — that is deliberate, so adding the
knowledge layer could not change a path that already worked.

It records events. It never writes memory.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..core.contracts import (
    EventRepository,
    ModelSpecLike,
    MessageRepository,
    ModelProvider,
    ModelRegistry,
    SessionRepository,
    UserRepository,
)
from ..core.domain import (
    UNDETERMINED_LANGUAGE,
    EventType,
    Message,
    Role,
    Session,
    User,
)
from ..core.errors import ProviderError
from .events import EventRecorder
from .grounding import ContextBuilder, summarize


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
        context_builder: ContextBuilder | None = None,
    ) -> None:
        self._users = users
        self._sessions = sessions
        self._messages = messages
        self._provider = provider
        self._registry = registry
        self._context_builder = context_builder
        self._recorder = EventRecorder(events)

    @property
    def grounded(self) -> bool:
        """Whether this service retrieves evidence for a turn.

        Exposed because "was that answer grounded?" must be answerable without
        reading the wiring.
        """
        return self._context_builder is not None

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

        grounding = self._ground(
            session_id=session_id,
            query=content,
            language=language,
            spec=spec,
            history=history,
            message_id=user_message.id,
        )

        # The grounding message is prepended for this call only. It is never
        # given to the message repository: it is derived from the index at this
        # moment, it is rebuilt next turn, and persisting it would charge the
        # budget for the same evidence again on every later turn.
        prompt: Sequence[Message] = history
        if grounding is not None and grounding.message is not None:
            prompt = [grounding.message, *history]

        self._recorder.record(
            session_id=session_id,
            type=EventType.GENERATION_REQUESTED,
            payload={
                "model": spec.name,
                "provider": spec.provider,
                "message_count": len(prompt),
                "grounded": grounding is not None and grounding.message is not None,
                "evidence_chunks": grounding.used if grounding is not None else 0,
            },
            message_id=user_message.id,
        )

        try:
            response = self._provider.generate(
                model=spec.name, messages=prompt, options=options
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

    def _ground(
        self,
        *,
        session_id: str,
        query: str,
        language: str,
        spec: ModelSpecLike,
        history: Sequence[Message],
        message_id: str,
    ):
        """Retrieve and budget evidence for this turn, or return None.

        A retrieval failure is recorded and then re-raised rather than
        swallowed. Answering anyway would produce an ungrounded reply that the
        caller believes is grounded, which is worse than a failed turn: the
        first is visible, the second is not.
        """
        if self._context_builder is None:
            return None

        try:
            grounding = self._context_builder.build(
                session_id=session_id,
                query=query,
                language=language,
                model=spec,
                history=history,
            )
        except Exception as exc:
            self._recorder.record(
                session_id=session_id,
                type=EventType.RETRIEVAL_FAILED,
                payload={"error": str(exc), "error_type": type(exc).__name__},
                message_id=message_id,
            )
            raise

        self._recorder.record(
            session_id=session_id,
            type=EventType.CONTEXT_ASSEMBLED,
            payload=summarize(grounding),
            message_id=message_id,
        )
        return grounding

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
