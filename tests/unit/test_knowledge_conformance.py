"""The real implementations satisfy the real contracts.

`test_knowledge_contracts.py` proves the protocols are *satisfiable* using
minimal fakes. This file proves the shipped implementations actually satisfy
them -- which is a different claim, and the one that breaks first when a
signature drifts.

`isinstance` against a `runtime_checkable` Protocol only checks that the
members exist, so each check is paired with a call through the protocol's own
signature. A conformance test that never calls anything passes a class with
the right method names and the wrong arguments.
"""
import pytest

from personal_ai_core.context import GreedyContextAssembler, ScriptAwareTokenEstimator
from personal_ai_core.core import contracts as c
from personal_ai_core.core.context import ContextBudget
from personal_ai_core.core.knowledge import RetrievalMethod, RetrievalQuery
from personal_ai_core.knowledge import (
    FixedSizeChunker,
    HashingEmbeddingProvider,
    HybridRetriever,
    InMemoryChunkCatalog,
    InMemoryLexicalIndex,
    InMemoryVectorIndex,
    IngestionService,
    ReciprocalRankFusion,
)

TEXT = "Retrieval joins a vector arm and a lexical arm."


def build_stack():
    embedder = HashingEmbeddingProvider()
    vector_index = InMemoryVectorIndex(
        model_id=embedder.model_id, dimensions=embedder.dimensions
    )
    lexical_index = InMemoryLexicalIndex()
    catalog = InMemoryChunkCatalog()
    return embedder, vector_index, lexical_index, catalog


@pytest.mark.parametrize(
    "implementation, protocol",
    [
        (HashingEmbeddingProvider(), c.EmbeddingProvider),
        (FixedSizeChunker(), c.Chunker),
        (
            InMemoryVectorIndex(model_id="m", dimensions=4),
            c.VectorIndex,
        ),
        (InMemoryLexicalIndex(), c.LexicalIndex),
        (ReciprocalRankFusion(), c.RankFusion),
        (ScriptAwareTokenEstimator(), c.TokenEstimator),
        (GreedyContextAssembler(ScriptAwareTokenEstimator()), c.ContextAssembler),
    ],
    ids=lambda value: getattr(value, "__name__", type(value).__name__),
)
def test_implementation_satisfies_its_protocol(implementation, protocol):
    assert isinstance(implementation, protocol)


def test_the_retriever_satisfies_its_protocol():
    embedder, vector_index, lexical_index, catalog = build_stack()
    retriever = HybridRetriever(
        embedder=embedder,
        vector_index=vector_index,
        lexical_index=lexical_index,
        catalog=catalog,
    )
    assert isinstance(retriever, c.Retriever)


def test_every_protocol_member_is_callable_through_its_signature(document):
    """Calls each contract the way the Core would, not the way the class allows."""
    embedder, vector_index, lexical_index, catalog = build_stack()
    chunker = FixedSizeChunker()

    service = IngestionService(
        chunker=chunker,
        embedder=embedder,
        vector_index=vector_index,
        lexical_index=lexical_index,
        catalog=catalog,
    )
    report = service.ingest(document, TEXT)
    assert report.chunk_count == 1

    embedding = embedder.embed([TEXT])[0]
    vector = vector_index.search(embedding=embedding, limit=5, language=None)
    lexical = lexical_index.search(text=TEXT, limit=5, language=None)
    assert vector.method is RetrievalMethod.VECTOR
    assert lexical.method is RetrievalMethod.LEXICAL

    fused = ReciprocalRankFusion().fuse([vector, lexical], limit=5)
    assert fused

    retriever = HybridRetriever(
        embedder=embedder,
        vector_index=vector_index,
        lexical_index=lexical_index,
        catalog=catalog,
    )
    results = retriever.retrieve(RetrievalQuery(text="vector arm", limit=3))
    assert results

    assembled = GreedyContextAssembler(ScriptAwareTokenEstimator()).assemble(
        results, budget=ContextBudget(500)
    )
    assert assembled.selected
