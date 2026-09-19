"""Retrieval methods name the mechanism, never a claimed capability.

Audit finding 1. `RetrievalMethod.SEMANTIC` was reported in provenance for
results produced by a character-n-gram hashing provider, which captures
surface overlap and encodes no meaning at all. The field was false, and a
provenance record that overstates its own method is worse than none because
it is believed.

The correction is not a caveat in prose. It is that the vocabulary describes
what actually happened -- vectors were compared -- and that the identity of
the model which produced those vectors travels with the result, so a reader
can judge what the comparison was worth.
"""
import pytest

from personal_ai_core.core.knowledge import RetrievalMethod, RetrievalProvenance
from personal_ai_core.knowledge import (
    FixedSizeChunker,
    HashingEmbeddingProvider,
    HybridRetriever,
    InMemoryChunkCatalog,
    InMemoryLexicalIndex,
    InMemoryVectorIndex,
    IngestionService,
)
from personal_ai_core.core.knowledge import Document, RetrievalQuery

NOTES = "Retrieval compares vectors.\n\nFusion combines two rankings by rank."


@pytest.fixture
def stack():
    embedder = HashingEmbeddingProvider()
    vector_index = InMemoryVectorIndex(
        model_id=embedder.model_id, dimensions=embedder.dimensions
    )
    lexical_index = InMemoryLexicalIndex()
    catalog = InMemoryChunkCatalog()
    document = Document(source_uri="file:///notes.md", declared_language="en")
    IngestionService(
        chunker=FixedSizeChunker(max_chars=60, overlap_chars=5),
        embedder=embedder,
        vector_index=vector_index,
        lexical_index=lexical_index,
        catalog=catalog,
    ).ingest(document, NOTES)
    retriever = HybridRetriever(
        embedder=embedder,
        vector_index=vector_index,
        lexical_index=lexical_index,
        catalog=catalog,
    )
    return retriever, embedder


# --- the vocabulary --------------------------------------------------------


def test_there_is_no_method_claiming_semantics():
    assert not hasattr(RetrievalMethod, "SEMANTIC")
    assert "semantic" not in {m.value for m in RetrievalMethod}


def test_the_vector_arm_is_named_for_its_mechanism():
    assert RetrievalMethod.VECTOR.value == "vector"


def test_every_method_name_describes_a_mechanism():
    """A capability word here is a claim the Core cannot currently back."""
    forbidden = {"semantic", "meaning", "smart", "intelligent", "neural", "understand"}
    for method in RetrievalMethod:
        assert not any(word in method.value.lower() for word in forbidden), method


# --- provenance tells the truth -------------------------------------------


def test_provenance_reports_the_vector_arm_not_a_semantic_one(stack):
    retriever, _ = stack
    result = retriever.retrieve(RetrievalQuery(text="compares vectors", limit=1))[0]
    assert RetrievalMethod.VECTOR in result.provenance.methods


def test_provenance_names_the_embedder_that_ranked_the_result(stack):
    """"Found by the vector arm" is equally true of a real embedding model and
    of a hashing stand-in. The reader needs to know which."""
    retriever, embedder = stack
    result = retriever.retrieve(RetrievalQuery(text="compares vectors", limit=1))[0]

    assert result.provenance.embedding_model_id == embedder.model_id
    assert "hashing" in result.provenance.embedding_model_id


def test_the_embedder_identity_changes_when_the_embedder_does():
    """So provenance cannot keep claiming an old model after a swap."""
    a = HashingEmbeddingProvider().model_id
    b = HashingEmbeddingProvider(dimensions=512).model_id
    assert a != b


def test_no_embedder_is_named_when_no_vector_arm_ran(stack):
    """A lexical-only retrieval must not imply a vector model was involved."""
    retriever, _ = stack
    result = retriever.retrieve(
        RetrievalQuery(
            text="fusion combines", limit=1, methods=(RetrievalMethod.LEXICAL,)
        )
    )[0]
    assert result.provenance.methods == (RetrievalMethod.LEXICAL,)
    assert result.provenance.embedding_model_id is None


def test_embedding_model_id_defaults_to_unknown_rather_than_to_a_guess():
    provenance = RetrievalProvenance(
        document_id="d", version_id="v", chunk_id="c", start=0, end=1
    )
    assert provenance.embedding_model_id is None
