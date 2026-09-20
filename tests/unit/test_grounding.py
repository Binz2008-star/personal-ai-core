"""ContextBuilder — retrieval, budgeting and assembly for one turn.

Driven entirely by fakes standing in for the contracts. The point of this file
is the *orchestration decisions*, not the components: what order things happen
in, what happens when there is nothing to ground with, and what the resulting
message does and does not contain.
"""
import pytest

from personal_ai_core.context import (
    ReserveBasedBudgetPolicy,
    ScriptAwareTokenEstimator,
)
from personal_ai_core.context.assembler import HybridContextAssembler
from personal_ai_core.conversation.grounding import (
    GROUNDING_PREAMBLE,
    ContextBuilder,
    render_evidence,
    summarize,
)
from personal_ai_core.core.context import ContextBudget, ExclusionReason
from personal_ai_core.core.domain import Message, Role
from personal_ai_core.core.errors import RetrievalError
from personal_ai_core.core.knowledge import (
    RetrievalMethod,
    RetrievalProvenance,
    RetrievalResult,
)


class Spec:
    def __init__(self, context_window=8192):
        self.context_window = context_window
        self.name = "boss"
        self.provider = "ollama"


def result(chunk, source_uri="file:///notes/en.md", embedding_model_id="fake-embed-v1"):
    return RetrievalResult(
        chunk=chunk,
        provenance=RetrievalProvenance(
            document_id=chunk.document_id,
            version_id=chunk.version_id,
            chunk_id=chunk.id,
            start=chunk.start,
            end=chunk.end,
            methods=(RetrievalMethod.VECTOR,),
            ranks={RetrievalMethod.VECTOR: 1},
            scores={RetrievalMethod.VECTOR: 0.9},
            fused_score=0.5,
            index_version="fake@1",
            embedding_model_id=embedding_model_id,
            source_uri=source_uri,
        ),
    )


class FakeRetriever:
    def __init__(self, results=(), error=None):
        self.results = list(results)
        self.error = error
        self.queries = []

    def retrieve(self, query):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.results


def builder(retriever, *, limit=5, generation_reserve=1024, overhead=256):
    estimator = ScriptAwareTokenEstimator()
    return ContextBuilder(
        retriever=retriever,
        assembler=HybridContextAssembler(estimator),
        budget_policy=ReserveBasedBudgetPolicy(
            generation_reserve=generation_reserve, overhead=overhead
        ),
        estimator=estimator,
        limit=limit,
    )


def user(text):
    return Message(session_id="s1", role=Role.USER, content=text)


# --- the happy path --------------------------------------------------------


def test_a_grounding_message_carries_the_evidence_and_the_preamble(make_chunk):
    retriever = FakeRetriever([result(make_chunk("Retrieval has two arms.", chunk_id="c1"))])
    grounding = builder(retriever).build(
        session_id="s1", query="arms", language="en", model=Spec(), history=[]
    )

    assert grounding.message is not None
    assert grounding.message.role is Role.SYSTEM
    assert GROUNDING_PREAMBLE in grounding.message.content
    assert "Retrieval has two arms." in grounding.message.content
    assert grounding.used == 1
    assert grounding.retrieved == 1


def test_the_grounding_message_belongs_to_the_turn_it_grounds(make_chunk):
    retriever = FakeRetriever([result(make_chunk("text", chunk_id="c1"))])
    grounding = builder(retriever).build(
        session_id="session-42", query="q", language="en", model=Spec(), history=[]
    )
    assert grounding.message.session_id == "session-42"


def test_evidence_is_rendered_with_a_resolvable_citation(make_chunk):
    """Source and character range travel with each passage.

    A citation the reader cannot resolve back to a span of a named document is
    not a citation.
    """
    chunk = make_chunk("Some evidence.", chunk_id="c1", start=40)
    rendered = render_evidence([result(chunk, source_uri="file:///notes/a.md")])
    assert "file:///notes/a.md" in rendered
    assert f"characters {chunk.start}-{chunk.end}" in rendered
    assert "Some evidence." in rendered


def test_rendering_is_deterministic(make_chunk):
    results = [result(make_chunk(f"passage {i}", chunk_id=f"c{i}")) for i in range(3)]
    assert render_evidence(results) == render_evidence(results)


def test_the_language_of_the_turn_reaches_the_query(make_chunk):
    retriever = FakeRetriever([])
    builder(retriever).build(
        session_id="s1", query="مرحبا", language="ar", model=Spec(), history=[]
    )
    assert retriever.queries[0].language == "ar"
    assert retriever.queries[0].text == "مرحبا"


def test_the_evidence_limit_reaches_the_query(make_chunk):
    retriever = FakeRetriever([])
    builder(retriever, limit=3).build(
        session_id="s1", query="q", language="en", model=Spec(), history=[]
    )
    assert retriever.queries[0].limit == 3


# --- no evidence means no message -----------------------------------------


def test_finding_nothing_produces_no_message():
    """An empty evidence block invites an answer that claims to have consulted
    sources it never received."""
    grounding = builder(FakeRetriever([])).build(
        session_id="s1", query="q", language="en", model=Spec(), history=[]
    )
    assert grounding.message is None
    assert grounding.used == 0
    assert grounding.retrieved == 0


def test_everything_being_dropped_by_the_budget_produces_no_message(make_chunk):
    retriever = FakeRetriever([result(make_chunk("x" * 5000, chunk_id="c1"))])
    grounding = builder(retriever, generation_reserve=8000, overhead=0).build(
        session_id="s1", query="q", language="en", model=Spec(), history=[]
    )
    assert grounding.message is None
    assert grounding.retrieved == 1
    assert grounding.dropped == 1


def test_a_blank_query_is_not_retrieved_for():
    retriever = FakeRetriever([])
    grounding = builder(retriever).build(
        session_id="s1", query="   ", language="en", model=Spec(), history=[]
    )
    assert retriever.queries == []
    assert grounding.message is None


# --- budgeting -------------------------------------------------------------


def test_the_budget_accounts_for_the_history_that_was_passed(make_chunk):
    retriever = FakeRetriever([result(make_chunk("evidence", chunk_id="c1"))])
    short = builder(retriever).build(
        session_id="s1", query="q", language="en", model=Spec(), history=[]
    )
    long = builder(retriever).build(
        session_id="s1",
        query="q",
        language="en",
        model=Spec(),
        history=[user("a" * 4000)],
    )
    assert long.allocation.history > short.allocation.history
    assert long.context.budget.available_tokens < short.context.budget.available_tokens


def test_the_budget_derives_from_the_model_it_was_given(make_chunk):
    retriever = FakeRetriever([result(make_chunk("evidence", chunk_id="c1"))])
    small = builder(retriever).build(
        session_id="s1", query="q", language="en", model=Spec(4096), history=[]
    )
    large = builder(retriever).build(
        session_id="s1", query="q", language="en", model=Spec(32768), history=[]
    )
    assert large.context.budget.available_tokens > small.context.budget.available_tokens


def test_the_budget_names_its_policy(make_chunk):
    grounding = builder(FakeRetriever([])).build(
        session_id="s1", query="q", language="en", model=Spec(), history=[]
    )
    assert "reserve-based" in grounding.context.budget.source


# --- failure ---------------------------------------------------------------


def test_a_retrieval_failure_propagates_rather_than_degrading_silently():
    """An ungrounded answer the caller believes is grounded is worse than a
    failed turn: the first is invisible."""
    with pytest.raises(RetrievalError):
        builder(FakeRetriever(error=RetrievalError("index down"))).build(
            session_id="s1", query="q", language="en", model=Spec(), history=[]
        )


def test_an_impossible_limit_is_rejected_at_construction():
    with pytest.raises(ValueError):
        builder(FakeRetriever([]), limit=0)


# --- the audit payload -----------------------------------------------------


def test_the_summary_records_what_was_dropped_and_why(make_chunk):
    """The exclusions are the part that explains a bad answer."""
    results = [
        result(make_chunk("short", chunk_id="c1")),
        result(make_chunk("y" * 4000, chunk_id="c2")),
    ]
    grounding = builder(
        FakeRetriever(results), generation_reserve=8100, overhead=0
    ).build(session_id="s1", query="q", language="en", model=Spec(), history=[])

    payload = summarize(grounding)
    assert payload["retrieved"] == 2
    assert payload["used"] == 1
    assert payload["dropped"] == 1
    assert payload["excluded_by_reason"] == {
        ExclusionReason.BUDGET_EXHAUSTED.value: 1
    }
    assert payload["chunk_ids"] == ["c1"]


def test_the_summary_records_the_whole_allocation(make_chunk):
    grounding = builder(FakeRetriever([])).build(
        session_id="s1", query="q", language="en", model=Spec(), history=[user("hi")]
    )
    payload = summarize(grounding)
    for key in (
        "context_window",
        "history_tokens",
        "generation_reserve",
        "overhead",
        "budget_tokens",
        "budget_source",
        "overcommitted",
    ):
        assert key in payload, f"{key} missing from the audit payload"
    assert payload["context_window"] == 8192


def test_the_summary_reports_every_embedder_not_just_the_first(make_chunk):
    """Audit finding 1.

    If two results ever disagree about which embedder ranked them, the audit
    trail must show the disagreement rather than pick one and look consistent.
    A mixed index is how a provenance record starts quietly attributing a real
    model's ranking to a stand-in, or the reverse.
    """
    results = [
        result(make_chunk("a", chunk_id="c1"), embedding_model_id="real-model-v2"),
        result(make_chunk("b", chunk_id="c2"), embedding_model_id="hashing-stand-in"),
    ]
    grounding = builder(FakeRetriever(results)).build(
        session_id="s1", query="q", language="en", model=Spec(), history=[]
    )
    assert summarize(grounding)["embedding_model_ids"] == [
        "hashing-stand-in",
        "real-model-v2",
    ]


def test_the_summary_reports_one_embedder_once(make_chunk):
    results = [
        result(make_chunk("a", chunk_id="c1"), embedding_model_id="one-model"),
        result(make_chunk("b", chunk_id="c2"), embedding_model_id="one-model"),
    ]
    grounding = builder(FakeRetriever(results)).build(
        session_id="s1", query="q", language="en", model=Spec(), history=[]
    )
    assert summarize(grounding)["embedding_model_ids"] == ["one-model"]


def test_a_lexical_only_result_contributes_no_embedder(make_chunk):
    chunk = make_chunk("a", chunk_id="c1")
    lexical_only = RetrievalResult(
        chunk=chunk,
        provenance=RetrievalProvenance(
            document_id=chunk.document_id,
            version_id=chunk.version_id,
            chunk_id=chunk.id,
            start=chunk.start,
            end=chunk.end,
            methods=(RetrievalMethod.LEXICAL,),
            ranks={RetrievalMethod.LEXICAL: 1},
        ),
    )
    grounding = builder(FakeRetriever([lexical_only])).build(
        session_id="s1", query="q", language="en", model=Spec(), history=[]
    )
    assert summarize(grounding)["embedding_model_ids"] == []
