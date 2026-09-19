"""The knowledge stack, composed.

Document in, chunked, embedded, indexed on both arms, retrieved, fused,
provenance attached, fitted to a budget. Each part has unit tests; this is
where the seams between them are exercised.

No infrastructure: the whole file runs offline with no service and no model.
"""
import pytest

from personal_ai_core.context import GreedyContextAssembler, ScriptAwareTokenEstimator
from personal_ai_core.core.context import ContextBudget, ExclusionReason
from personal_ai_core.core.errors import RetrievalError
from personal_ai_core.core.knowledge import Document, RetrievalMethod, RetrievalQuery
from personal_ai_core.knowledge import (
    FixedSizeChunker,
    HashingEmbeddingProvider,
    HybridRetriever,
    InMemoryChunkCatalog,
    InMemoryLexicalIndex,
    InMemoryVectorIndex,
    IngestionService,
)

ENGLISH_NOTES = """Hybrid retrieval runs a semantic arm and a lexical arm.

Reciprocal rank fusion combines the two rankings using ranks, not scores.

Provenance records which arm found a chunk and at what rank."""

ARABIC_NOTES = """البحث الهجين يجمع بين الذراع الدلالية والذراع المعجمية.

دمج الرتب المتبادلة يوحد الترتيبين باستخدام الرتب وليس الدرجات.

المصدر يسجل أي ذراع وجدت المقطع وفي أي رتبة."""


@pytest.fixture
def stack():
    embedder = HashingEmbeddingProvider()
    vector_index = InMemoryVectorIndex(
        model_id=embedder.model_id, dimensions=embedder.dimensions
    )
    lexical_index = InMemoryLexicalIndex()
    catalog = InMemoryChunkCatalog()
    ingestion = IngestionService(
        chunker=FixedSizeChunker(max_chars=80, overlap_chars=10),
        embedder=embedder,
        vector_index=vector_index,
        lexical_index=lexical_index,
        catalog=catalog,
    )
    retriever = HybridRetriever(
        embedder=embedder,
        vector_index=vector_index,
        lexical_index=lexical_index,
        catalog=catalog,
    )
    return ingestion, retriever, vector_index, lexical_index, catalog


@pytest.fixture
def english_doc():
    return Document(source_uri="file:///notes/en.md", declared_language="en")


@pytest.fixture
def arabic_doc():
    return Document(source_uri="file:///notes/ar.md", declared_language="ar")


def test_a_document_becomes_retrievable(stack, english_doc):
    ingestion, retriever, _, _, _ = stack
    report = ingestion.ingest(english_doc, ENGLISH_NOTES)
    assert report.chunk_count == 3
    assert report.replaced_previous is False

    results = retriever.retrieve(RetrievalQuery(text="reciprocal rank fusion", limit=3))
    assert results
    assert "fusion" in results[0].chunk.text.lower()


@pytest.mark.parametrize(
    "content, query, language",
    [
        (ENGLISH_NOTES, "provenance records which arm", "en"),
        (ARABIC_NOTES, "المصدر يسجل أي ذراع", "ar"),
    ],
    ids=["en", "ar"],
)
def test_retrieval_works_in_both_languages(stack, content, query, language):
    ingestion, retriever, _, _, _ = stack
    document = Document(source_uri="file:///notes/x.md", declared_language=language)
    ingestion.ingest(document, content)

    results = retriever.retrieve(
        RetrievalQuery(text=query, limit=3, language=language)
    )
    assert results, "no results in " + language
    assert "ذراع" in results[0].chunk.text or "arm" in results[0].chunk.text.lower()


def test_provenance_answers_every_question_it_promises(stack, english_doc):
    ingestion, retriever, vector_index, lexical_index, _ = stack
    report = ingestion.ingest(english_doc, ENGLISH_NOTES)

    result = retriever.retrieve(RetrievalQuery(text="semantic arm", limit=1))[0]
    provenance = result.provenance

    assert provenance.document_id == english_doc.id
    assert provenance.version_id == report.version.id
    assert provenance.chunk_id == result.chunk.id
    assert provenance.source_uri == english_doc.source_uri
    assert provenance.fused_score is not None
    assert set(provenance.methods) <= {
        RetrievalMethod.SEMANTIC,
        RetrievalMethod.LEXICAL,
    }
    assert provenance.methods, "no retrieval arm was recorded"
    for method in provenance.methods:
        assert provenance.ranks[method] >= 1
        assert method in provenance.scores
    assert vector_index.index_version in provenance.index_version
    assert lexical_index.index_version in provenance.index_version


def test_the_offsets_in_provenance_re_read_the_source(stack, english_doc):
    """The point of storing offsets: a citation can be checked, not trusted."""
    ingestion, retriever, _, _, _ = stack
    ingestion.ingest(english_doc, ENGLISH_NOTES)

    for result in retriever.retrieve(RetrievalQuery(text="fusion ranks", limit=5)):
        start, end = result.provenance.start, result.provenance.end
        assert ENGLISH_NOTES[start:end] == result.chunk.text


def test_a_query_matching_only_one_arm_still_returns_results(stack, english_doc):
    ingestion, retriever, _, _, _ = stack
    ingestion.ingest(english_doc, ENGLISH_NOTES)

    semantic_only = retriever.retrieve(
        RetrievalQuery(text="provenance", limit=3, methods=(RetrievalMethod.SEMANTIC,))
    )
    lexical_only = retriever.retrieve(
        RetrievalQuery(text="provenance", limit=3, methods=(RetrievalMethod.LEXICAL,))
    )
    assert semantic_only and lexical_only
    assert semantic_only[0].provenance.methods == (RetrievalMethod.SEMANTIC,)
    assert lexical_only[0].provenance.methods == (RetrievalMethod.LEXICAL,)


def test_re_ingestion_replaces_rather_than_accumulates(stack, english_doc):
    """Stale chunks from deleted text must stop being retrievable.

    Otherwise a citation resolves to a passage that is no longer in the
    document -- which is worse than no citation, because it looks correct.
    """
    ingestion, retriever, vector_index, lexical_index, catalog = stack
    ingestion.ingest(english_doc, ENGLISH_NOTES)
    first_count = len(catalog)

    report = ingestion.ingest(english_doc, "Only one paragraph survives the edit.")
    assert report.replaced_previous is True
    assert report.version.revision == 2
    assert len(catalog) == 1 < first_count
    assert len(vector_index) == 1
    assert len(lexical_index) == 1

    assert retriever.retrieve(
        RetrievalQuery(text="reciprocal rank fusion", limit=5)
    )[0].chunk.text.startswith("Only one paragraph")


def test_retrieval_is_deterministic_across_repeated_calls(stack, english_doc):
    ingestion, retriever, _, _, _ = stack
    ingestion.ingest(english_doc, ENGLISH_NOTES)
    query = RetrievalQuery(text="ranks not scores", limit=3)

    first = [r.chunk.id for r in retriever.retrieve(query)]
    second = [r.chunk.id for r in retriever.retrieve(query)]
    assert first == second


def test_candidate_depth_does_not_scale_with_the_requested_limit(stack, english_doc):
    """Not inherited: the legacy `max(match_count * 8, 50)` formula.

    Asking for fewer results must not change which results are best. A top-1
    from a limit of 1 must be the same chunk as the top-1 from a limit of 3.
    """
    ingestion, retriever, _, _, _ = stack
    ingestion.ingest(english_doc, ENGLISH_NOTES)

    narrow = retriever.retrieve(RetrievalQuery(text="semantic arm", limit=1))
    wide = retriever.retrieve(RetrievalQuery(text="semantic arm", limit=3))
    assert narrow[0].chunk.id == wide[0].chunk.id


def test_a_limit_larger_than_the_candidate_depth_is_not_silently_truncated(english_doc):
    """The depth floor exists so a caller is never cut off by a hidden setting."""
    embedder = HashingEmbeddingProvider()
    vector_index = InMemoryVectorIndex(
        model_id=embedder.model_id, dimensions=embedder.dimensions
    )
    lexical_index = InMemoryLexicalIndex()
    catalog = InMemoryChunkCatalog()
    IngestionService(
        chunker=FixedSizeChunker(max_chars=100, overlap_chars=10),
        embedder=embedder,
        vector_index=vector_index,
        lexical_index=lexical_index,
        catalog=catalog,
    ).ingest(english_doc, ENGLISH_NOTES)

    retriever = HybridRetriever(
        embedder=embedder,
        vector_index=vector_index,
        lexical_index=lexical_index,
        catalog=catalog,
        candidate_depth=1,
    )
    results = retriever.retrieve(RetrievalQuery(text="arm ranks provenance", limit=3))
    assert len(results) == 3


def test_an_empty_query_is_refused_rather_than_answered(stack, english_doc):
    ingestion, retriever, _, _, _ = stack
    ingestion.ingest(english_doc, ENGLISH_NOTES)
    with pytest.raises(RetrievalError):
        retriever.retrieve(RetrievalQuery(text="   "))


def test_retrieval_feeds_the_context_budget(stack, english_doc):
    ingestion, retriever, _, _, _ = stack
    ingestion.ingest(english_doc, ENGLISH_NOTES)
    results = retriever.retrieve(RetrievalQuery(text="arm ranks provenance", limit=3))

    assembler = GreedyContextAssembler(ScriptAwareTokenEstimator())
    generous = assembler.assemble(results, budget=ContextBudget(1000, source="test"))
    assert len(generous.selected) == len(results)
    assert generous.excluded == ()

    tight = assembler.assemble(results, budget=ContextBudget(5, source="test"))
    assert tight.within_budget
    assert tight.dropped_count > 0
    assert all(
        e.reason is ExclusionReason.BUDGET_EXHAUSTED for e in tight.excluded
    )


def test_an_empty_document_ingests_without_producing_chunks(stack, english_doc):
    ingestion, _, vector_index, lexical_index, catalog = stack
    report = ingestion.ingest(english_doc, "   \n\n  ")
    assert report.chunk_count == 0
    assert len(catalog) == 0
    assert len(vector_index) == 0
    assert len(lexical_index) == 0


def test_two_documents_stay_separable(stack, english_doc, arabic_doc):
    ingestion, retriever, _, _, _ = stack
    ingestion.ingest(english_doc, ENGLISH_NOTES)
    ingestion.ingest(arabic_doc, ARABIC_NOTES)

    arabic_results = retriever.retrieve(
        RetrievalQuery(text="الذراع المعجمية", limit=3, language="ar")
    )
    assert arabic_results
    assert all(r.chunk.document_id == arabic_doc.id for r in arabic_results)
