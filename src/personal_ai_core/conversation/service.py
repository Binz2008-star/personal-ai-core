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
    ContextBudgetPolicy,
    EventRepository,
    IdentityComposer,
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
from .grounding import ContextBuilder, Grounding, summarize


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
        budget_policy: ContextBudgetPolicy,
        identity: IdentityComposer,
        context_builder: ContextBuilder | None = None,
    ) -> None:
        self._users = users
        self._sessions = sessions
        self._messages = messages
        self._provider = provider
        self._registry = registry
        self._budget_policy = budget_policy
        # Required, not defaulted, for the reason the budget policy is: the
        # contract must be present in EVERY model call (ADR-011). A service
        # that can be constructed without one can make a call without one.
        self._identity = identity
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

        # Identity first, then evidence, then the conversation -- ADR-011's
        # implementation boundary, rule 3. The order is the point: the rules
        # that govern the reply are read before the material they govern, and
        # rule 5 is what says which of the two wins when a retrieved document
        # argues with them.
        #
        # Neither message is given to the message repository. The grounding
        # message is derived from the index at this moment and is rebuilt next
        # turn; identity is composed per turn and is not conversation memory
        # (boundary rule 2). Persisting either would charge the budget for the
        # same text again on every later turn.
        identity_message = self._identity.compose(session_id=session_id)
        prompt: Sequence[Message] = [identity_message, *history]
        if grounding is not None and grounding.message is not None:
            prompt = [identity_message, grounding.message, *history]

        self._recorder.record(
            session_id=session_id,
            type=EventType.GENERATION_REQUESTED,
            payload={
                "model": spec.name,
                "provider": spec.provider,
                "message_count": len(prompt),
                "grounded": grounding is not None and grounding.message is not None,
                "evidence_chunks": grounding.used if grounding is not None else 0,
                "generation_limit": self._generation_limit(spec=spec, grounding=grounding),
            },
            message_id=user_message.id,
        )

        # ADR-011 prerequisite B. The reserve was accounting-only: computed,
        # recorded on the allocation, and never sent. A number the provider
        # never sees does not reserve anything. `_generation_options` turns it
        # into the provider's output limit.
        generation_options = self._generation_options(
            spec=spec, grounding=grounding, options=options
        )

        try:
            response = self._provider.generate(
                model=spec.name, messages=prompt, options=generation_options
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

    def _generation_limit(
        self, *, spec: ModelSpecLike, grounding: Grounding | None
    ) -> int:
        """Tokens the model may generate this turn.

        Taken from the turn's own allocation when there is one. Without
        grounding there is no allocation, so the policy is asked directly:
        `history_tokens=0` because only `evidence` depends on history -- the
        reserve does not, and it is the reserve this reads. Both paths run the
        same policy, so both produce the same limit for the same model; that
        equivalence is asserted rather than assumed.

        Enforcement must not depend on whether retrieval happens to be wired.
        A limit that applies only to grounded turns is not a limit.
        """
        if grounding is not None:
            return grounding.allocation.generation_reserve
        return self._budget_policy.allocate(
            model=spec, history_tokens=0
        ).generation_reserve

    def _generation_options(
        self,
        *,
        spec: ModelSpecLike,
        grounding: Grounding | None,
        options: Mapping[str, Any] | None,
    ) -> Mapping[str, Any]:
        """Caller options plus the budget's output limit.

        An explicit caller value wins: a caller that names `num_predict` has
        said something more specific than the default policy, and silently
        overriding it would make the parameter a lie. The limit is recorded on
        `GENERATION_REQUESTED` either way, so what was sent is recoverable.
        """
        merged = dict(options or {})
        merged.setdefault(
            "num_predict", self._generation_limit(spec=spec, grounding=grounding)
        )
        return merged

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

    def has_session(self, session_id: str) -> bool:
        """Whether `send` would accept this session id.

        The same lookup `send` and `close_session` make, exposed read-only so
        a caller that records against a session -- the agent -- refuses the
        sessions this service refuses, rather than keeping its own list.
        """
        return self._sessions.get(session_id) is not None

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
