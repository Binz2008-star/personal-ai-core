"""Contract tests: adapters satisfy the Core protocols."""
from personal_ai_core.core.contracts import (
    EventRepository,
    MemoryStore,
    MessageRepository,
    ModelProvider,
    ModelRegistry,
    ModelSpecLike,
    SessionRepository,
    UserRepository,
)
from personal_ai_core.core.config import Settings
from personal_ai_core.core.domain import EventType, Role
from personal_ai_core.runtime.model_registry import ModelRegistry as ConcreteRegistry
from personal_ai_core.persistence.in_memory import (
    InMemoryEventRepository,
    InMemoryMessageRepository,
    InMemorySessionRepository,
    InMemoryUserRepository,
    SealedMemoryStore,
)
from personal_ai_core.runtime.ollama.provider import OllamaProvider


def test_adapters_satisfy_their_protocols():
    assert isinstance(InMemoryUserRepository(), UserRepository)
    assert isinstance(InMemorySessionRepository(), SessionRepository)
    assert isinstance(InMemoryMessageRepository(), MessageRepository)
    assert isinstance(InMemoryEventRepository(), EventRepository)
    assert isinstance(SealedMemoryStore(), MemoryStore)
    assert isinstance(OllamaProvider("http://x"), ModelProvider)


def test_event_repository_exposes_no_mutation():
    repo = InMemoryEventRepository()
    assert not hasattr(repo, "update")
    assert not hasattr(repo, "delete")


def test_concrete_registry_satisfies_the_protocol():
    registry = ConcreteRegistry.from_settings(Settings.from_env({}))
    assert isinstance(registry, ModelRegistry)
    assert isinstance(registry.active, ModelSpecLike)


class StubSpec:
    """A model spec that is not the concrete ModelSpec."""

    name = "stub-model:1b"
    provider = "stub"
    context_window = 2048


class StubRegistry:
    """A registry the Core has never seen, satisfying only the protocol."""

    @property
    def active(self) -> StubSpec:
        return StubSpec()


def test_a_foreign_protocol_compatible_registry_satisfies_the_contract():
    assert isinstance(StubRegistry(), ModelRegistry)
    assert isinstance(StubRegistry().active, ModelSpecLike)


def test_the_service_accepts_any_protocol_compatible_registry():
    """The service must not require the concrete registry.

    If this passes with StubRegistry, the application layer genuinely depends
    on the abstraction rather than on runtime.model_registry.
    """
    from personal_ai_core.conversation.service import ConversationService
    from personal_ai_core.persistence.in_memory import (
        InMemoryEventRepository,
        InMemoryMessageRepository,
        InMemorySessionRepository,
        InMemoryUserRepository,
    )
    from personal_ai_core.runtime.ollama.provider import OllamaProvider

    events = InMemoryEventRepository()
    service = ConversationService(
        users=InMemoryUserRepository(),
        sessions=InMemorySessionRepository(),
        messages=InMemoryMessageRepository(),
        events=events,
        provider=OllamaProvider(
            "http://x",
            transport=lambda u, p, t: {"model": p["model"], "message": {"content": "ok"}},
        ),
        registry=StubRegistry(),
    )

    session = service.start_session(service.create_user().id)
    reply = service.send(session_id=session.id, content="hi")

    assert reply.role is Role.ASSISTANT
    requested = next(e for e in events.all() if e.type is EventType.GENERATION_REQUESTED)
    assert requested.payload["model"] == "stub-model:1b"
    assert requested.payload["provider"] == "stub"
