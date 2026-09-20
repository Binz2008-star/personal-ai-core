"""Grounding with recall wired in.

Three things are pinned here.

*`memory_enabled` is configuration, not outcome.* Inferring it from
`memories_retrieved` would make "recall is off" and "recall found nothing"
indistinguishable, and those need different answers when someone asks why
an answer lacked context.

*Recall failure degrades the turn; it does not fail it.* That is
deliberately asymmetric with document retrieval, which raises.

*The payload carries a classification, never an exception message.* The
payload is logged, and a store's error text can name a credential.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from personal_ai_core.context.assembler import HybridContextAssembler
from personal_ai_core.context.budget import ReserveBasedBudgetPolicy
from personal_ai_core.context.token_estimator import ScriptAwareTokenEstimator
from personal_ai_core.conversation.grounding import (
    GROUNDING_PREAMBLE,
    MEMORY_PREAMBLE,
    ContextBuilder,
    summarize,
)
from personal_ai_core.core.knowledge import (
    Chunk,
    RetrievalMethod,
    RetrievalProvenance,
    RetrievalResult,
)
from personal_ai_core.core.memory import (
    MemoryProvenance,
    MemoryReader,
    MemoryRecord,
    MemoryRetrievalError,
    MemoryStatus,
    MemoryType,
)
from personal_ai_core.memory.retriever import (
    MemoryRetrievalFailure,
    SimpleMemoryRetriever,
)
from personal_ai_core.persistence.memory_store import InMemoryMemoryRepository

SECRET = "postgres://user:hunter2@db.internal:5432/memories"
SRC = Path(__file__).resolve().parents[2] / "src" / "personal_ai_core"


class Spec:
    context_window = 8192
    name = "boss"
    provider = "ollama"


def chunk(text: str, chunk_id: str = "c1") -> Chunk:
    return Chunk(
        document_id="doc-1",
        version_id="ver-1",
        text=text,
        ordinal=0,
        start=0,
        end=len(text),
        id=chunk_id,
    )


def result(c: Chunk) -> RetrievalResult:
    return RetrievalResult(
        chunk=c,
        provenance=RetrievalProvenance(
            document_id=c.document_id,
            version_id=c.version_id,
            chunk_id=c.id,
            start=c.start,
            end=c.end,
            methods=(RetrievalMethod.LEXICAL,),
            source_uri="file:///notes.md",
        ),
    )


def record(content: str = "prefers Arabic replies") -> MemoryRecord:
    return MemoryRecord(
        session_id="s1",
        type=MemoryType.PREFERENCES,
        content=content,
        language="en",
        provenance=MemoryProvenance(
            session_id="s1", event_id="e1", promoted_by="rule:explicit_instruction"
        ),
        status=MemoryStatus.ACTIVE,
        confidence=0.9,
    )


class FakeRetriever:
    def __init__(self, results=()):
        self.results = list(results)

    def retrieve(self, query):
        return tuple(self.results)


class ExplodingMemoryRetriever:
    """Raises a classified failure whose underlying cause held a secret."""

    def retrieve(self, query):
        raise MemoryRetrievalFailure(MemoryRetrievalError.UNAVAILABLE)


class ForeignMemoryRetriever:
    """A third-party retriever that raises outside this contract."""

    def retrieve(self, query):
        raise RuntimeError(f"connection refused: {SECRET}")


def builder(*, results=(), memory_retriever=None) -> ContextBuilder:
    estimator = ScriptAwareTokenEstimator()
    return ContextBuilder(
        retriever=FakeRetriever(results),
        assembler=HybridContextAssembler(estimator),
        budget_policy=ReserveBasedBudgetPolicy(),
        estimator=estimator,
        memory_retriever=memory_retriever,
    )


def real_memory_retriever(*records: MemoryRecord) -> SimpleMemoryRetriever:
    repo = InMemoryMemoryRepository()
    for r in records:
        repo.write(r)
    return SimpleMemoryRetriever(reader=MemoryReader(source=repo))


def build(b: ContextBuilder, query: str = "what do I prefer"):
    return b.build(
        session_id="s1", query=query, language="en", model=Spec(), history=[]
    )


# --- memory_enabled is configuration, not outcome ---------------------------


def test_memory_enabled_is_false_when_no_retriever_is_wired():
    grounding = build(builder(results=[result(chunk("doc text"))]))
    assert grounding.memory_enabled is False
    assert grounding.memories_retrieved == 0
    assert grounding.memory_error is None


def test_memory_enabled_is_true_when_wired_and_memories_are_found():
    grounding = build(
        builder(
            results=[result(chunk("doc text"))],
            memory_retriever=real_memory_retriever(record()),
        )
    )
    assert grounding.memory_enabled is True
    assert grounding.memories_retrieved == 1
    assert grounding.memory_error is None


def test_memory_enabled_is_true_when_wired_and_nothing_is_found():
    """The distinguishing case: wired but empty is not the same as off."""
    grounding = build(
        builder(
            results=[result(chunk("doc text"))],
            memory_retriever=real_memory_retriever(),  # empty store
        )
    )
    assert grounding.memory_enabled is True
    assert grounding.memories_retrieved == 0
    assert grounding.memory_error is None


def test_memory_enabled_is_true_when_wired_and_retrieval_fails():
    grounding = build(
        builder(
            results=[result(chunk("doc text"))],
            memory_retriever=ExplodingMemoryRetriever(),
        )
    )
    assert grounding.memory_enabled is True
    assert grounding.memories_retrieved == 0
    assert grounding.memory_error is MemoryRetrievalError.UNAVAILABLE


# --- Option A: recall failure degrades, never fails -------------------------


def test_a_memory_failure_does_not_fail_the_turn():
    grounding = build(
        builder(
            results=[result(chunk("doc text stays"))],
            memory_retriever=ExplodingMemoryRetriever(),
        )
    )
    assert grounding.message is not None
    assert "doc text stays" in grounding.message.content
    assert grounding.used == 1


def test_a_foreign_retriever_error_is_classified_internal():
    grounding = build(
        builder(
            results=[result(chunk("doc text"))],
            memory_retriever=ForeignMemoryRetriever(),
        )
    )
    assert grounding.memory_error is MemoryRetrievalError.INTERNAL
    assert grounding.message is not None


def test_document_retrieval_failure_still_raises():
    """The asymmetry is deliberate and must stay."""

    class FailingRetriever:
        def retrieve(self, query):
            raise RuntimeError("index down")

    estimator = ScriptAwareTokenEstimator()
    b = ContextBuilder(
        retriever=FailingRetriever(),
        assembler=HybridContextAssembler(estimator),
        budget_policy=ReserveBasedBudgetPolicy(),
        estimator=estimator,
        memory_retriever=real_memory_retriever(record()),
    )
    with pytest.raises(RuntimeError, match="index down"):
        build(b)


# --- the grounding message ---------------------------------------------------


def test_memory_and_documents_produce_two_labelled_sections():
    grounding = build(
        builder(
            results=[result(chunk("document passage"))],
            memory_retriever=real_memory_retriever(record("prefers Arabic replies")),
        )
    )
    assert grounding.message is not None
    content = grounding.message.content
    assert MEMORY_PREAMBLE in content
    assert GROUNDING_PREAMBLE in content
    assert "prefers Arabic replies" in content
    assert "document passage" in content
    # Memory is labelled as recollection before the retrieved-source block.
    assert content.index(MEMORY_PREAMBLE) < content.index(GROUNDING_PREAMBLE)


def test_documents_only_produce_one_section():
    grounding = build(
        builder(
            results=[result(chunk("document passage"))],
            memory_retriever=real_memory_retriever(),
        )
    )
    assert grounding.message is not None
    assert MEMORY_PREAMBLE not in grounding.message.content
    assert GROUNDING_PREAMBLE in grounding.message.content


def test_memory_only_produces_one_section():
    grounding = build(
        builder(results=[], memory_retriever=real_memory_retriever(record()))
    )
    assert grounding.message is not None
    assert MEMORY_PREAMBLE in grounding.message.content
    assert GROUNDING_PREAMBLE not in grounding.message.content


def test_neither_produces_no_message():
    grounding = build(builder(results=[], memory_retriever=real_memory_retriever()))
    assert grounding.message is None


def test_a_recalled_memory_carries_its_promoting_rule_and_time():
    grounding = build(
        builder(results=[], memory_retriever=real_memory_retriever(record()))
    )
    assert grounding.message is not None
    assert "rule:explicit_instruction" in grounding.message.content


def test_an_empty_query_skips_both_retrievals_but_keeps_memory_enabled():
    grounding = build(
        builder(memory_retriever=real_memory_retriever(record())), query="   "
    )
    assert grounding.message is None
    assert grounding.memory_enabled is True
    assert grounding.memories_retrieved == 0
    assert grounding.memory_error is None


# --- the event payload -------------------------------------------------------


def test_payload_carries_stable_memory_accounting():
    grounding = build(
        builder(
            results=[result(chunk("document passage"))],
            memory_retriever=real_memory_retriever(record()),
        )
    )
    payload = summarize(grounding)

    assert payload["memory_enabled"] is True
    assert payload["memories_retrieved"] == 1
    assert payload["memories_used"] == 1
    assert payload["memories_dropped"] == 0
    assert payload["memory_error"] is None
    assert len(payload["memory_ids"]) == 1
    # Documents and memory are accounted separately under one budget.
    assert payload["evidence_tokens"] > 0
    assert payload["memory_tokens"] > 0
    assert (
        payload["evidence_tokens"] + payload["memory_tokens"]
        <= payload["budget_tokens"]
    )


def test_payload_memory_error_is_a_stable_classification():
    grounding = build(
        builder(results=[], memory_retriever=ExplodingMemoryRetriever())
    )
    payload = summarize(grounding)
    assert payload["memory_error"] == "unavailable"
    assert payload["memory_enabled"] is True


def test_payload_never_carries_raw_exception_data():
    """End-to-end: a credential in a store's error must not reach the log."""
    grounding = build(
        builder(
            results=[result(chunk("doc text"))],
            memory_retriever=ForeignMemoryRetriever(),
        )
    )
    payload = summarize(grounding)
    rendered = repr(payload)

    assert payload["memory_error"] == "internal"
    assert SECRET not in rendered
    assert "hunter2" not in rendered
    assert "db.internal" not in rendered
    assert "connection refused" not in rendered


# --- structural guards --------------------------------------------------------


def test_context_builder_does_not_accept_a_memory_store():
    """Recall reaches a retriever, never a store."""
    params = inspect.signature(ContextBuilder.__init__).parameters
    assert "memory_retriever" in params
    assert "memory_store" not in params
    annotations = {
        name: str(p.annotation) for name, p in params.items() if p.annotation
    }
    assert not any("MemoryStore" in a for a in annotations.values())


def test_conversation_service_never_receives_a_memory_store():
    """No factory hands ConversationService anything memory-shaped."""
    offenders = []
    for path in SRC.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name != "ConversationService":
                continue
            for keyword in node.keywords:
                if keyword.arg and "memory" in keyword.arg.lower():
                    offenders.append(f"{path.relative_to(SRC)}: {keyword.arg}")
    assert not offenders, (
        "ConversationService was handed a memory collaborator: " + str(offenders)
    )
