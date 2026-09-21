"""Contract tests: adapters satisfy the Core protocols."""
from personal_ai_core.core.contracts import (
    ContextBudgetPolicy,
    EventRepository,
    MemoryRetriever,
    MemoryStore,
    MessageRepository,
    ModelProvider,
    ModelRegistry,
    ModelSpecLike,
    PromotionGate,
    SessionRepository,
    UserRepository,
)
# Aliased on import because `core.contracts` and `context.assembler` both
# define a `HybridContextAssembler` -- one a Protocol, one a class. Without
# the alias this module would bind one name to two different objects and the
# check below would compare a thing to itself.
from personal_ai_core.core.contracts import (
    HybridContextAssembler as HybridContextAssemblerProtocol,
)
from personal_ai_core.context.assembler import HybridContextAssembler
from personal_ai_core.context.budget import ReserveBasedBudgetPolicy
from personal_ai_core.context.token_estimator import ScriptAwareTokenEstimator
from personal_ai_core.core.memory import MemoryReader
from personal_ai_core.memory.gate import DefaultPromotionGate
from personal_ai_core.memory.retriever import SimpleMemoryRetriever
from personal_ai_core.persistence.memory_store import InMemoryMemoryRepository
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


def test_the_real_memory_store_satisfies_its_contract():
    """The store that actually writes, checked against the contract.

    `SealedMemoryStore` was the only thing ever checked against `MemoryStore`,
    and it is the decoy: it implements all four methods and raises on every
    one. So the conformance evidence covered the implementation whose whole
    job is to refuse, and not the one a durable backend will replace.

    `MemoryStore` is the boundary that is supposed to make that replacement
    safe. Checking only the sealed store proves it holds for an object that
    does nothing.
    """
    assert isinstance(InMemoryMemoryRepository(), MemoryStore)


def test_four_declared_protocols_have_a_conforming_implementation():
    """Four protocols had no conformance evidence at all.

    `PromotionGate` and `MemoryRetriever` arrived in Phases 3 and 4,
    `ContextBudgetPolicy` in Phase 2, the hybrid assembler in Phase 4. Each
    was declared, implemented, and never checked against its own declaration.
    """
    assert isinstance(DefaultPromotionGate(), PromotionGate)
    assert isinstance(
        SimpleMemoryRetriever(reader=MemoryReader(source=InMemoryMemoryRepository())),
        MemoryRetriever,
    )
    assert isinstance(ReserveBasedBudgetPolicy(), ContextBudgetPolicy)
    assert isinstance(
        HybridContextAssembler(ScriptAwareTokenEstimator()),
        HybridContextAssemblerProtocol,
    )


# --- Signature conformance, which the assertions above do NOT establish ----
#
# `isinstance` against a runtime_checkable Protocol is a PRESENCE check. It
# verifies the method names exist and nothing else: a class whose every
# method takes the wrong arguments passes it. Measured, not assumed --
#
#     missing method   -> isinstance False
#     wrong signatures -> isinstance True
#
# These annotated assignments are what pins the signatures. They are checked
# by pyright, which CI runs since PR #11, and rejected there if an
# implementation's signature drifts from the contract it claims to satisfy.
# Nothing here runs at test time beyond construction; the assertion is
# static, and its absence from a pytest report is the point, not an omission.

_memory_store: MemoryStore = InMemoryMemoryRepository()
_promotion_gate: PromotionGate = DefaultPromotionGate()
_memory_retriever: MemoryRetriever = SimpleMemoryRetriever(
    reader=MemoryReader(source=InMemoryMemoryRepository())
)
_budget_policy: ContextBudgetPolicy = ReserveBasedBudgetPolicy()
_hybrid_assembler: HybridContextAssemblerProtocol = HybridContextAssembler(
    ScriptAwareTokenEstimator()
)


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
