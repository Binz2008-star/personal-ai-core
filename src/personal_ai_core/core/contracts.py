"""Core protocols.

These are the boundary. Application services depend on these; infrastructure
implements them. Nothing here imports a provider, a driver or a transport.

Dependency direction (ARCHITECTURE.md §4):

    infrastructure  ──implements──▶  core.contracts  ◀──depends on──  application
"""
from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .domain import Event, Message, ModelResponse, Session, User


@runtime_checkable
class ModelProvider(Protocol):
    """A source of generated text.

    The Core talks to models only through this. Swapping Ollama for another
    backend must not require a change above this line — if it does, the
    abstraction has failed (ADR-002).
    """

    @property
    def name(self) -> str:
        """Provider identifier, e.g. "ollama"."""
        ...

    def generate(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        options: Mapping[str, Any] | None = None,
    ) -> ModelResponse:
        """Produce a response for `messages` using `model`.

        Raises `ProviderError` if the backend fails.
        """
        ...


@runtime_checkable
class UserRepository(Protocol):
    def add(self, user: User) -> None: ...
    def get(self, user_id: str) -> User | None: ...


@runtime_checkable
class SessionRepository(Protocol):
    def add(self, session: Session) -> None: ...
    def get(self, session_id: str) -> Session | None: ...
    def update(self, session: Session) -> None: ...


@runtime_checkable
class MessageRepository(Protocol):
    def add(self, message: Message) -> None: ...
    def list_for_session(self, session_id: str) -> Sequence[Message]: ...


@runtime_checkable
class EventRepository(Protocol):
    """Append-only event storage.

    There is deliberately no `update` and no `delete`. An event is evidence;
    corrections are new events (ADR-003).
    """

    def append(self, event: Event) -> None: ...
    def list_for_session(self, session_id: str) -> Sequence[Event]: ...


@runtime_checkable
class MemoryStore(Protocol):
    """Persistent memory.

    Declared in Phase 1 but **not implemented**. It exists here so the
    `Event != Memory` invariant can be enforced structurally rather than by
    convention: the conversation path is given a store that refuses writes, and
    a test asserts the path never attempts one.

    Phase 3 supplies a real implementation behind the promotion gate.
    """

    def write(self, record: Mapping[str, Any]) -> None: ...
