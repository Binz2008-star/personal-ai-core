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

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from ..core.contracts import (
    ContextBudgetPolicy,
    EventRepository,
    IdentityComposer,
    ModelSpecLike,
    MessageRepository,
    ModelProvider,
    ModelRegistry,
    SessionRepository,
    TokenEstimator,
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
from ..core.context import ContextAllocation
from ..core.errors import ContextOverflowError, ProviderError
from ..core.redaction import RedactionError
from .events import EventRecorder
from .grounding import ContextBuilder, summarize
from .language_guard import GUARD_NOTE, check_reply


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
        estimator: TokenEstimator,
        identity: IdentityComposer,
        context_builder: ContextBuilder | None = None,
        sampling: Mapping[str, Any] | None = None,
        language_guard: bool = False,
    ) -> None:
        self._users = users
        self._sessions = sessions
        self._messages = messages
        self._provider = provider
        self._registry = registry
        self._budget_policy = budget_policy
        # Required for the same reason: every turn is measured (ADR-005), not
        # only the grounded ones -- the default path used to send its whole
        # history unmeasured.
        self._estimator = estimator
        # Required, not defaulted, for the reason the budget policy is: the
        # contract must be present in EVERY model call (ADR-011). A service
        # that can be constructed without one can make a call without one.
        self._identity = identity
        self._context_builder = context_builder
        # Sent on every generation, beneath any option the caller names.
        self._sampling = dict(sampling or {})
        # ADR-019. The factory turns it on from Settings; the budget reserves
        # GUARD_NOTE's tokens whenever it is on, so a retry cannot overflow.
        self._language_guard = language_guard
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

        spec = self._registry.active
        # N1: the window this turn is budgeted against is the window the model
        # server is told to use (`num_ctx`). A caller naming another one would
        # make the budget describe a window the server does not have, so it is
        # refused here, before anything is stored -- not overridden in silence.
        named_window = (options or {}).get("num_ctx")
        if named_window is not None and named_window != spec.context_window:
            raise ValueError(
                f"num_ctx {named_window!r} differs from the active model's context "
                f"window {spec.context_window}, which this turn is budgeted against"
            )

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

        # The whole history stays in the store; the prompt gets the newest
        # messages that fit (P1-3). Both paths are measured on what is sent.
        window = window_history(
            self._messages.list_for_session(session_id),
            estimate=self._estimator.estimate,
            fits=lambda tokens: not self._budget_policy.allocate(
                model=spec, history_tokens=tokens).overcommitted,
        )
        history = window.sent

        grounding = self._ground(
            session_id=session_id,
            query=content,
            language=language,
            spec=spec,
            window=window,
            message_id=user_message.id,
        )
        # Every turn has an allocation and a CONTEXT_ASSEMBLED event: the
        # grounded path's, or one measured here from the history alone.
        allocation = (
            grounding.allocation
            if grounding is not None
            else self._measure(
                session_id=session_id, spec=spec, window=window,
                message_id=user_message.id,
            )
        )
        # ADR-005: no silent overflow. Only this turn's own message is left
        # in the window when it does not fit (window_history), so a turn that
        # still needs more than the window is refused here, recorded, and
        # never sent: fail-closed, not cut by the server.
        if allocation.overcommitted:
            self._recorder.record(
                session_id=session_id,
                type=EventType.GENERATION_FAILED,
                payload={
                    "model": spec.name,
                    "error": "the context window is overcommitted; the turn was not sent",
                    "reason": "context_overcommitted",
                    "spoken_for": allocation.spoken_for,
                    "context_window": allocation.context_window,
                    "history_tokens": allocation.history,
                },
                message_id=user_message.id,
            )
            raise ContextOverflowError(
                spoken_for=allocation.spoken_for,
                context_window=allocation.context_window,
                history_tokens=allocation.history,
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

        requested = {
            "model": spec.name,
            # The adapter that serves this turn, not a label (ADR-020 section 3.2).
            "provider": self._provider.name,
            "message_count": len(prompt),
            "grounded": grounding is not None and grounding.message is not None,
            "evidence_chunks": grounding.used if grounding is not None else 0,
            "generation_limit": allocation.generation_reserve,
            # N1: the window the server is told to use -- the one this turn
            # was budgeted against.
            "num_ctx": allocation.context_window,
            "sampling": dict(self._sampling),
        }
        self._recorder.record(
            session_id=session_id,
            type=EventType.GENERATION_REQUESTED,
            payload=requested,
            message_id=user_message.id,
        )

        # ADR-011 prerequisite B. The reserve was accounting-only: computed,
        # recorded on the allocation, and never sent. A number the provider
        # never sees does not reserve anything. `_generation_options` turns it
        # into the provider's output limit.
        generation_options = self._generation_options(allocation=allocation, options=options)

        try:
            response = self._provider.generate(
                model=spec.name, messages=prompt, options=generation_options
            )
        except ProviderError as exc:
            self._recorder.record(
                session_id=session_id,
                type=EventType.GENERATION_FAILED,
                payload={"model": spec.name, **_provider_failure(exc)},
                message_id=user_message.id,
            )
            raise

        if self._language_guard:
            response = self._guard_language(
                session_id=session_id,
                user_message=user_message,
                prompt=prompt,
                evidence=(grounding.message.content,)
                if grounding is not None and grounding.message is not None
                else (),
                spec=spec,
                options=generation_options,
                response=response,
                requested=requested,
            )

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

    def _guard_language(
        self,
        *,
        session_id: str,
        user_message: Message,
        prompt: Sequence[Message],
        evidence: Sequence[str],
        spec: ModelSpecLike,
        options: Mapping[str, Any],
        response: Any,
        requested: Mapping[str, Any],
    ) -> Any:
        """ADR-019 §3.2: one retry on a language violation, then deliver.

        The rejected draft is not shown to the model and is not persisted;
        only its letter counts are recorded. The second reply is delivered
        whatever it is, and the event says whether it passed.
        """
        first = check_reply(user_message.content, response.text, evidence)
        if not first.violation:
            return response
        note = Message(
            session_id=session_id,
            role=Role.SYSTEM,
            content=GUARD_NOTE,
            language=UNDETERMINED_LANGUAGE,
        )
        # ADR-019 §3.3: the second generation is requested on the record like
        # the first, so a turn's cost can be read from its events.
        self._recorder.record(
            session_id=session_id,
            type=EventType.GENERATION_REQUESTED,
            payload={**requested, "message_count": len(prompt) + 1, "attempt": 2},
            message_id=user_message.id,
        )
        def guard_event(**delivered: Any) -> None:
            self._recorder.record(
                session_id=session_id,
                type=EventType.REPLY_LANGUAGE_GUARD,
                payload={
                    "expected": first.expected,
                    "reason": first.reason,
                    "rejected_counts": dict(first.reply_counts),
                    # What the verdict was computed on: quoted Latin removed.
                    # With the reason, this reproduces the decision.
                    "rejected_assessed_counts": dict(first.assessed_counts),
                    # The rejected draft's cost; GENERATION_COMPLETED carries
                    # the delivered reply's, so the two together are the turn's.
                    "rejected_prompt_tokens": response.prompt_tokens,
                    "rejected_completion_tokens": response.completion_tokens,
                    **delivered,
                },
                message_id=user_message.id,
            )

        try:
            retried = self._provider.generate(
                model=spec.name, messages=[*prompt, note], options=options
            )
        except ProviderError as exc:
            self._recorder.record(
                session_id=session_id,
                type=EventType.GENERATION_FAILED,
                payload={"model": spec.name, **_provider_failure(exc), "attempt": 2},
                message_id=user_message.id,
            )
            # The trigger is recorded on this path too: a retry that never
            # returned is still a guard firing, and its counts are evidence.
            guard_event(delivered_counts=None, delivered_passed=None, retry_failed=True)
            raise
        second = check_reply(user_message.content, retried.text, evidence)
        guard_event(
            delivered_counts=dict(second.reply_counts),
            delivered_passed=not second.violation,
            retry_failed=False,
        )
        return retried

    def _generation_options(
        self,
        *,
        allocation: ContextAllocation,
        options: Mapping[str, Any] | None,
    ) -> Mapping[str, Any]:
        """The configured sampling, then caller options, plus the output limit.

        The limit is the turn's own allocation's generation reserve, on every
        path: grounded or not, every turn now has an allocation, so the limit
        cannot depend on whether retrieval happens to be wired.

        An explicit caller value wins: a caller that names `num_predict` or
        `temperature` has said something more specific than the default
        policy, and silently overriding it would make the parameter a lie. The
        limit and the sampling are recorded on `GENERATION_REQUESTED` either
        way, so what was sent is recoverable.

        `num_ctx` is the exception (N1): it is always the allocation's window,
        because a server running another window truncates the prompt without
        a word. `send` has already refused a caller that named a different one.
        """
        merged = {**self._sampling, **dict(options or {})}
        merged.setdefault("num_predict", allocation.generation_reserve)
        merged["num_ctx"] = allocation.context_window
        return merged

    def _measure(
        self,
        *,
        session_id: str,
        spec: ModelSpecLike,
        window: HistoryWindow,
        message_id: str,
    ) -> ContextAllocation:
        """The allocation of a turn with no retrieval, measured and recorded.

        The same policy and the same estimate of the history the grounded path
        uses, so the two paths budget a conversation alike; recorded as
        CONTEXT_ASSEMBLED so the size of every turn against the window can be
        read back, not only the grounded ones."""
        history_tokens = sum(self._estimator.estimate(m.content) for m in window.sent)
        allocation = self._budget_policy.allocate(model=spec, history_tokens=history_tokens)
        self._recorder.record(
            session_id=session_id,
            type=EventType.CONTEXT_ASSEMBLED,
            payload={
                "grounded": False,
                **window.recorded(),
                "context_window": allocation.context_window,
                "history_tokens": allocation.history,
                "generation_reserve": allocation.generation_reserve,
                "overhead": allocation.overhead,
                "identity_reserve": allocation.identity,
                "guard_reserve": allocation.guard,
                "overcommitted": allocation.overcommitted,
                "estimator": self._estimator.model_id,
            },
            message_id=message_id,
        )
        return allocation

    def _ground(
        self,
        *,
        session_id: str,
        query: str,
        language: str,
        spec: ModelSpecLike,
        window: HistoryWindow,
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
                history=window.sent,
            )
        except Exception as exc:
            # The type, not the message: a retriever's message can quote a
            # document path, a database host or the text it failed on (P1-8).
            # A redactor's failure says which of its declared classifications
            # it is, and nothing else can be in it (ADR-018 section 3.8).
            failure: dict[str, object] = {"error_type": type(exc).__name__}
            if isinstance(exc, RedactionError):
                failure["classification"] = exc.classification
            self._recorder.record(
                session_id=session_id,
                type=EventType.RETRIEVAL_FAILED,
                payload=failure,
                message_id=message_id,
            )
            raise

        self._recorder.record(
            session_id=session_id,
            type=EventType.CONTEXT_ASSEMBLED,
            payload={**summarize(grounding), **window.recorded()},
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


@dataclass(frozen=True, slots=True)
class HistoryWindow:
    """The part of a session's history one turn sends, and what it left out."""

    sent: tuple[Message, ...]
    left_out: int
    left_out_tokens: int

    def recorded(self) -> dict[str, object]:
        """For CONTEXT_ASSEMBLED. Every message before `history_first_sent` is
        in the store and was not sent; counts and an id, never the text."""
        return {
            "history_messages": len(self.sent),
            "history_left_out": self.left_out,
            "history_left_out_tokens": self.left_out_tokens,
            "history_first_sent": self.sent[0].id if self.sent else None,
        }


def window_history(
    history: Sequence[Message],
    *,
    estimate: Callable[[str], int],
    fits: Callable[[int], bool],
) -> HistoryWindow:
    """The newest messages whose estimate `fits`, the oldest left out first.

    Gap analysis P1-3. Every turn sent the whole session, so a long enough
    one stopped fitting the window, and from then on every turn was refused
    (ADR-005: refused rather than cut by the server). Now the oldest messages
    are left out of the prompt -- only of the prompt: the store keeps them.

    Deterministic: the same history and the same estimate give the same
    window. The newest message, this turn's own, is always kept; when it does
    not fit alone the window is just that message, and the caller's overflow
    check refuses the turn. Once anything is left out the window starts at a
    user message, so the model never reads a reply without its question.
    """
    tokens = [estimate(m.content) for m in history]
    total = sum(tokens)
    start = 0
    while start < len(history) - 1 and not fits(total):
        total -= tokens[start]
        start += 1
    if start:
        while start < len(history) - 1 and history[start].role is not Role.USER:
            total -= tokens[start]
            start += 1
    return HistoryWindow(
        sent=tuple(history[start:]),
        left_out=start,
        left_out_tokens=sum(tokens[:start]),
    )


def _provider_failure(exc: ProviderError) -> dict[str, object]:
    """What a durable GENERATION_FAILED keeps of a provider's failure.

    Gap analysis P1-8: the message went into the event as it was, and a
    transport's message can name the host, the port and whatever the server
    answered. A row in the event store outlives the terminal it was printed
    on, so it keeps a classification -- the error's type, its kind and an
    HTTP status -- and the person at the terminal still gets the message.
    """
    return {"error_type": type(exc).__name__, "error_kind": exc.kind, "status": exc.status}
