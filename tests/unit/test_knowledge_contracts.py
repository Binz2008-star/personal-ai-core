"""Phase 2 protocol contracts.

Each protocol is satisfied here by a minimal fake. That is the point of this
step: the contract must be expressible and testable before any implementation
exists, so that no adapter -- pgvector, Neon, or the Second Brain lineage --
gets to define it by being first.

These fakes are test doubles, not the in-memory implementation. They assert
shape, not behaviour.
"""
import pytest

from personal_ai_core.core import contracts as c
from personal_ai_core.core.context import BudgetedContext, ContextBudget
from personal_ai_core.core.knowledge import (
    Candidate,
    CandidateList,
    Chunk,
    Document,
    DocumentVersion,
    Embedding,
    RetrievalMethod,
    RetrievalQuery,
)


class FakeEmbeddingProvider:
    model_id = "fake-embed"
    dimensions = 3

    def embed(self, texts):
        return [Embedding(vector=(0.0, 0.0, 0.0), model_id=self.model_id) for _ in texts]


class FakeChunker:
    def chunk(self, *, document, version, content):
        return [
            Chunk(
                document_id=document.id,
                version_id=version.id,
                text=content,
                ordinal=0,
                start=0,
                end=len(content),
            )
        ]


class FakeVectorIndex:
    index_version = "fake-v1"

    def add(self, chunks, embeddings): ...
    def remove_document(self, document_id): ...

    def search(self, *, embedding, limit, language=None):
        return CandidateList(method=RetrievalMethod.VECTOR)


class FakeLexicalIndex:
    index_version = "fake-v1"

    def add(self, chunks): ...
    def remove_document(self, document_id): ...

    def search(self, *, text, limit, language=None):
        return CandidateList(method=RetrievalMethod.LEXICAL)


class FakeRankFusion:
    def fuse(self, lists, *, limit):
        return []


class FakeRetriever:
    def retrieve(self, query):
        return []


class FakeTokenEstimator:
    model_id = "fake-tokenizer"

    def estimate(self, text):
        return len(text)


class FakeContextAssembler:
    def assemble(self, results, *, budget):
        return BudgetedContext(budget=budget)


PROTOCOL_FAKES = [
    (FakeEmbeddingProvider, c.EmbeddingProvider),
    (FakeChunker, c.Chunker),
    (FakeVectorIndex, c.VectorIndex),
    (FakeLexicalIndex, c.LexicalIndex),
    (FakeRankFusion, c.RankFusion),
    (FakeRetriever, c.Retriever),
    (FakeTokenEstimator, c.TokenEstimator),
    (FakeContextAssembler, c.ContextAssembler),
]


@pytest.mark.parametrize(
    "fake,protocol", PROTOCOL_FAKES, ids=lambda x: getattr(x, "__name__", str(x))
)
def test_every_phase2_protocol_is_satisfiable(fake, protocol):
    assert isinstance(fake(), protocol)


def test_embedding_provider_returns_one_embedding_per_text():
    provider = FakeEmbeddingProvider()
    out = provider.embed(["a", "b", "c"])
    assert len(out) == 3
    assert all(e.dimensions == provider.dimensions for e in out)
    assert all(e.model_id == provider.model_id for e in out)


def test_indexes_report_a_version_for_provenance():
    assert FakeVectorIndex().index_version
    assert FakeLexicalIndex().index_version


def test_index_search_returns_a_list_tagged_with_its_own_method():
    vec = FakeVectorIndex().search(
        embedding=Embedding(vector=(0.0,), model_id="m"), limit=5
    )
    lex = FakeLexicalIndex().search(text="q", limit=5)
    assert vec.method is RetrievalMethod.VECTOR
    assert lex.method is RetrievalMethod.LEXICAL


def test_lexical_index_accepts_language_explicitly():
    """ADR-006: an implementation must decide, not default silently."""
    result = FakeLexicalIndex().search(text="مرحبا", limit=5, language="ar")
    assert result.method is RetrievalMethod.LEXICAL


def test_chunker_produces_chunks_that_locate_their_source():
    doc = Document(source_uri="file://x")
    ver = DocumentVersion(document_id=doc.id, content_hash="h")
    content = "some text"
    chunks = FakeChunker().chunk(document=doc, version=ver, content=content)
    for chunk in chunks:
        assert chunk.document_id == doc.id
        assert chunk.version_id == ver.id
        assert content[chunk.start : chunk.end] == chunk.text


def test_fusion_consumes_ranks_not_scores():
    """Scores from different paths share no scale; the signature reflects that."""
    lists = [
        CandidateList(
            method=RetrievalMethod.VECTOR,
            candidates=(
                Candidate(
                    chunk_id="a", rank=1, score=0.9, method=RetrievalMethod.VECTOR
                ),
            ),
        ),
        CandidateList(
            method=RetrievalMethod.LEXICAL,
            candidates=(
                Candidate(
                    chunk_id="a", rank=2, score=17.3, method=RetrievalMethod.LEXICAL
                ),
            ),
        ),
    ]
    assert FakeRankFusion().fuse(lists, limit=5) == []


def test_retriever_consumes_a_query_object():
    assert FakeRetriever().retrieve(RetrievalQuery(text="q", language="ar")) == []


def test_context_assembler_returns_a_budgeted_context():
    budget = ContextBudget(available_tokens=100, source="test")
    out = FakeContextAssembler().assemble([], budget=budget)
    assert isinstance(out, BudgetedContext)
    assert out.budget.available_tokens == 100
