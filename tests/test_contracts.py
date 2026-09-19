"""Contract tests: adapters satisfy the Core protocols."""
from personal_ai_core.core.contracts import (
    EventRepository,
    MemoryStore,
    MessageRepository,
    ModelProvider,
    SessionRepository,
    UserRepository,
)
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
