"""The grounded slice, end to end.

    Document -> Chunk -> Embedding -> Index
        -> Retrieval -> RRF -> Provenance -> Budget -> Context
        -> Message -> ModelProvider -> Response -> Event

This is the test that makes the slice vertical. Everything below it had unit
coverage while being reachable from nothing; here the conversation path
actually consumes the knowledge and context layers.

No infrastructure: the model provider is driven by a fake transport, and the
whole knowledge stack is in-memory and offline.
"""
import pytest

from personal_ai_core.conversation.factory import (
    build_grounded_in_memory_service,
    build_in_memory_service,
)
from personal_ai_core.context import (
    GreedyContextAssembler,
    ReserveBasedBudgetPolicy,
    ScriptAwareTokenEstimator,
)
from personal_ai_core.conversation.grounding import GROUNDING_PREAMBLE, ContextBuilder
from personal_ai_core.conversation.service import ConversationService
from personal_ai_core.core.config import Settings
from personal_ai_core.persistence.in_memory import (
    InMemoryEventRepository,
    InMemoryMessageRepository,
    InMemorySessionRepository,
    InMemoryUserRepository,
)
from personal_ai_core.runtime.model_registry import ModelRegistry
from personal_ai_core.runtime.ollama.provider import OllamaProvider
from personal_ai_core.core.domain import EventType, Role
from personal_ai_core.core.errors import RetrievalError
from personal_ai_core.core.knowledge import Document

NOTES_EN = """Hybrid retrieval runs a semantic arm and a lexical arm together.

Reciprocal rank fusion combines the two rankings using ranks, not scores.

Provenance records which arm found a chunk and at what rank it appeared."""

NOTES_AR = """البحث الهجين يشغل ذراعا دلالية وذراعا معجمية معا.

دمج الرتب المتبادلة يوحد الترتيبين باستخدام الرتب وليس الدرجات.

المصدر يسجل أي ذراع وجدت المقطع وفي أي رتبة ظهر."""


class Recorder:
    """A fake transport that keeps every payload the provider sent."""

    def __init__(self, reply="understood"):
        self.reply = reply
        self.payloads = []

    def __call__(self, url, payload, timeout):
        self.payloads.append(payload)
        return {
            "model": payload["model"],
            "message": {"role": "assistant", "content": self.reply},
            "prompt_eval_count": 5,
            "eval_count": 2,
            "done_reason": "stop",
        }

    @property
    def last_messages(self):
        return self.payloads[-1]["messages"]


@pytest.fixture
def transport():
    return Recorder()


@pytest.fixture
def slice_(transport):
    return build_grounded_in_memory_service(transport=transport)


def ingest(slice_, content, *, language="en", uri="file:///notes/x.md"):
    document = Document(source_uri=uri, declared_language=language)
    return document, slice_.ingestion.ingest(document, content)


# --- the wiring exists -----------------------------------------------------


def test_the_grounded_service_reports_that_it_grounds(slice_):
    assert slice_.service.grounded is True


def test_the_ungrounded_factory_is_unchanged(transport):
    """Adding retrieval must not alter a path that already worked."""
    service, events = build_in_memory_service(transport=transport)
    assert service.grounded is False

    session = service.start_session(service.create_user().id)
    service.send(session_id=session.id, content="hello")

    recorded = [e.type for e in events.list_for_session(session.id)]
    assert recorded == [
        EventType.SESSION_STARTED,
        EventType.MESSAGE_RECEIVED,
        EventType.GENERATION_REQUESTED,
        EventType.GENERATION_COMPLETED,
    ]
    assert [m["role"] for m in transport.last_messages] == ["user"]


# --- evidence actually reaches the model ----------------------------------


def test_retrieved_evidence_reaches_the_prompt(slice_, transport):
    ingest(slice_, NOTES_EN)
    session = slice_.service.start_session(slice_.service.create_user().id)

    slice_.service.send(session_id=session.id, content="what does fusion combine?")

    system, user = transport.last_messages[0], transport.last_messages[1]
    assert system["role"] == "system"
    assert GROUNDING_PREAMBLE in system["content"]
    assert "rank" in system["content"].lower()
    assert user["role"] == "user"


def test_the_prompt_citation_resolves_back_to_the_source(slice_, transport):
    ingest(slice_, NOTES_EN, uri="file:///notes/en.md")
    session = slice_.service.start_session(slice_.service.create_user().id)
    slice_.service.send(session_id=session.id, content="provenance and ranks")

    system = transport.last_messages[0]["content"]
    assert "file:///notes/en.md" in system

    # Every chunk named in the audit event still resolves in the catalog.
    assembled = next(
        e for e in slice_.events.all() if e.type is EventType.CONTEXT_ASSEMBLED
    )
    for chunk_id in assembled.payload["chunk_ids"]:
        chunk = slice_.catalog.get(chunk_id)
        assert chunk is not None
        assert NOTES_EN[chunk.start : chunk.end] == chunk.text


def test_an_arabic_turn_is_grounded_in_arabic_evidence(slice_, transport):
    ingest(slice_, NOTES_AR, language="ar", uri="file:///notes/ar.md")
    session = slice_.service.start_session(slice_.service.create_user().id)

    slice_.service.send(
        session_id=session.id, content="ماذا يوحد دمج الرتب المتبادلة؟", language="ar"
    )

    system = transport.last_messages[0]
    assert system["role"] == "system"
    assert "الرتب" in system["content"]


# --- the grounding message is ephemeral -----------------------------------


def test_the_grounding_message_is_never_persisted(slice_):
    """It is derived from the index at one moment and rebuilt next turn.

    Persisting it would make history un-reproducible and would charge the
    budget for the same evidence again on every later turn.
    """
    ingest(slice_, NOTES_EN)
    session = slice_.service.start_session(slice_.service.create_user().id)
    slice_.service.send(session_id=session.id, content="what does fusion combine?")

    history = slice_.service.history(session.id)
    assert [m.role for m in history] == [Role.USER, Role.ASSISTANT]
    assert all(GROUNDING_PREAMBLE not in m.content for m in history)


def test_evidence_is_not_charged_twice_across_turns(slice_, transport):
    ingest(slice_, NOTES_EN)
    session = slice_.service.start_session(slice_.service.create_user().id)

    slice_.service.send(session_id=session.id, content="what does fusion combine?")
    slice_.service.send(session_id=session.id, content="and what records the rank?")

    # Exactly one system message in the second prompt, not two.
    roles = [m["role"] for m in transport.last_messages]
    assert roles.count("system") == 1
    assert roles[0] == "system"


# --- no evidence -----------------------------------------------------------


def test_an_empty_index_sends_no_system_message(slice_, transport):
    """No evidence means no message: an empty evidence block invites an answer
    that claims to have consulted sources it never received."""
    session = slice_.service.start_session(slice_.service.create_user().id)
    reply = slice_.service.send(session_id=session.id, content="anything at all")

    assert reply.role is Role.ASSISTANT
    assert [m["role"] for m in transport.last_messages] == ["user"]


def test_a_turn_with_no_evidence_still_records_the_attempt(slice_):
    session = slice_.service.start_session(slice_.service.create_user().id)
    slice_.service.send(session_id=session.id, content="anything at all")

    assembled = next(
        e for e in slice_.events.all() if e.type is EventType.CONTEXT_ASSEMBLED
    )
    assert assembled.payload["retrieved"] == 0
    assert assembled.payload["used"] == 0


# --- the audit trail -------------------------------------------------------


def test_the_event_sequence_includes_context_assembly(slice_):
    ingest(slice_, NOTES_EN)
    session = slice_.service.start_session(slice_.service.create_user().id)
    slice_.service.send(session_id=session.id, content="what does fusion combine?")

    assert [e.type for e in slice_.events.list_for_session(session.id)] == [
        EventType.SESSION_STARTED,
        EventType.MESSAGE_RECEIVED,
        EventType.CONTEXT_ASSEMBLED,
        EventType.GENERATION_REQUESTED,
        EventType.GENERATION_COMPLETED,
    ]


def test_the_generation_event_says_whether_the_turn_was_grounded(slice_):
    ingest(slice_, NOTES_EN)
    session = slice_.service.start_session(slice_.service.create_user().id)
    slice_.service.send(session_id=session.id, content="what does fusion combine?")

    requested = next(
        e for e in slice_.events.all() if e.type is EventType.GENERATION_REQUESTED
    )
    assert requested.payload["grounded"] is True
    assert requested.payload["evidence_chunks"] >= 1


def test_the_budget_in_the_audit_trail_comes_from_the_boss_model(slice_):
    ingest(slice_, NOTES_EN)
    session = slice_.service.start_session(slice_.service.create_user().id)
    slice_.service.send(session_id=session.id, content="what does fusion combine?")

    payload = next(
        e for e in slice_.events.all() if e.type is EventType.CONTEXT_ASSEMBLED
    ).payload
    assert payload["context_window"] == 8192
    assert "reserve-based" in payload["budget_source"]
    # A budget is not a context window (ADR-005).
    assert 0 < payload["budget_tokens"] < payload["context_window"]


def test_a_retrieval_failure_is_recorded_and_fails_the_turn(transport):
    """Answering anyway would produce an ungrounded reply the caller believes
    is grounded.

    Composed by hand rather than by reaching into the factory's internals --
    a test that pokes a private attribute stops testing the wiring it claims
    to test the moment the wiring changes shape.
    """

    class BrokenRetriever:
        def retrieve(self, query):
            raise RetrievalError("index unavailable")

    estimator = ScriptAwareTokenEstimator()
    events = InMemoryEventRepository()
    service = ConversationService(
        users=InMemoryUserRepository(),
        sessions=InMemorySessionRepository(),
        messages=InMemoryMessageRepository(),
        events=events,
        provider=OllamaProvider("http://unused", transport=transport),
        registry=ModelRegistry.from_settings(Settings()),
        context_builder=ContextBuilder(
            retriever=BrokenRetriever(),
            assembler=GreedyContextAssembler(estimator),
            budget_policy=ReserveBasedBudgetPolicy(),
            estimator=estimator,
        ),
    )

    session = service.start_session(service.create_user().id)
    with pytest.raises(RetrievalError):
        service.send(session_id=session.id, content="what does fusion combine?")

    types = [e.type for e in events.list_for_session(session.id)]
    assert EventType.RETRIEVAL_FAILED in types
    assert EventType.GENERATION_REQUESTED not in types
    assert EventType.GENERATION_COMPLETED not in types
    assert transport.payloads == [], "the model was called despite a failed retrieval"


# --- Event != Memory still holds ------------------------------------------


def test_grounding_writes_no_memory(slice_):
    """ADR-003. Retrieval reads an index; it must not promote anything."""
    ingest(slice_, NOTES_EN)
    session = slice_.service.start_session(slice_.service.create_user().id)
    slice_.service.send(session_id=session.id, content="what does fusion combine?")

    assert not hasattr(slice_.service, "_memory")
    for event in slice_.events.all():
        assert "memory" not in event.type.value


def test_re_ingesting_unchanged_notes_keeps_earlier_citations_valid(slice_):
    """Finding 5 and Finding 4 meeting: a citation issued through the
    conversation path survives an unchanged re-ingestion."""
    document, _ = ingest(slice_, NOTES_EN)
    session = slice_.service.start_session(slice_.service.create_user().id)
    slice_.service.send(session_id=session.id, content="what does fusion combine?")

    cited = next(
        e for e in slice_.events.all() if e.type is EventType.CONTEXT_ASSEMBLED
    ).payload["chunk_ids"]
    assert cited

    report = slice_.ingestion.ingest(document, NOTES_EN)
    assert report.unchanged is True
    assert all(slice_.catalog.get(chunk_id) is not None for chunk_id in cited)
