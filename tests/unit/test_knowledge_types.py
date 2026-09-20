"""Phase 2 knowledge domain type contracts."""
import dataclasses

import pytest

from personal_ai_core.core.knowledge import (
    Candidate,
    CandidateList,
    Chunk,
    Document,
    DocumentVersion,
    Embedding,
    FusedCandidate,
    RetrievalMethod,
    RetrievalProvenance,
    RetrievalQuery,
    RetrievalResult,
)


def a_chunk(**kw):
    base = dict(
        document_id="doc", version_id="ver", text="hello", ordinal=0, start=0, end=5
    )
    return Chunk(**{**base, **kw})


# --- immutability ---------------------------------------------------------


def test_knowledge_types_are_frozen():
    for obj in (
        Document(source_uri="file://x"),
        DocumentVersion(document_id="d", content_hash="h"),
        a_chunk(),
        Embedding(vector=(0.1, 0.2), model_id="m"),
        Candidate(chunk_id="c", rank=1, score=0.5, method=RetrievalMethod.VECTOR),
    ):
        # use a field the type actually declares; these are slots dataclasses,
        # so assigning an unknown name raises a different error entirely
        target = dataclasses.fields(obj)[0].name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, target, "mutated")


def test_mappings_cannot_be_mutated_after_construction():
    meta = {"k": "v"}
    doc = Document(source_uri="file://x", metadata=meta)
    with pytest.raises(TypeError):
        doc.metadata["k"] = "other"  # type: ignore[index]  # the point
    meta["k"] = "changed"
    assert doc.metadata["k"] == "v"


# --- Document != Chunk ----------------------------------------------------


def test_document_holds_no_content():
    """Identity must survive the text changing."""
    assert not hasattr(Document(source_uri="file://x"), "text")
    assert not hasattr(Document(source_uri="file://x"), "content")


def test_document_and_chunk_have_separate_identities():
    doc = Document(source_uri="file://x")
    chunk = a_chunk(document_id=doc.id)
    assert chunk.id != doc.id
    assert chunk.document_id == doc.id


def test_chunk_back_reference_is_deterministic():
    """(version_id, start, end) must locate the exact span."""
    content = "the quick brown fox"
    chunk = a_chunk(text="quick", start=4, end=9, version_id="v1")
    assert content[chunk.start : chunk.end] == chunk.text
    assert (chunk.version_id, chunk.start, chunk.end) == ("v1", 4, 9)


def test_chunk_rejects_an_impossible_span():
    with pytest.raises(ValueError, match="precedes start"):
        a_chunk(start=10, end=2)


def test_chunk_rejects_negative_ordinal():
    with pytest.raises(ValueError, match="non-negative"):
        a_chunk(ordinal=-1)


# --- language is per chunk (ADR-006) --------------------------------------


def test_language_lives_on_the_chunk_not_only_the_document():
    doc = Document(source_uri="file://x", declared_language="en")
    arabic = a_chunk(document_id=doc.id, text="مرحبا", language="ar")
    assert doc.declared_language == "en"
    assert arabic.language == "ar"


def test_retrieval_query_requires_a_language_field():
    assert RetrievalQuery(text="x").language == "und"
    assert RetrievalQuery(text="مرحبا", language="ar").language == "ar"


def test_retrieval_query_rejects_a_useless_limit():
    with pytest.raises(ValueError, match="at least 1"):
        RetrievalQuery(text="x", limit=0)


# --- embeddings carry their model -----------------------------------------


def test_embedding_carries_model_identity_and_dimensions():
    e = Embedding(vector=(0.1, 0.2, 0.3), model_id="nomic-embed-text")
    assert e.model_id == "nomic-embed-text"
    assert e.dimensions == 3


def test_embedding_rejects_an_empty_vector():
    with pytest.raises(ValueError, match="must not be empty"):
        Embedding(vector=(), model_id="m")


def test_two_models_produce_distinguishable_embeddings():
    """Comparing across models is meaningless; the type makes it detectable."""
    a = Embedding(vector=(1.0, 0.0), model_id="model-a")
    b = Embedding(vector=(1.0, 0.0), model_id="model-b")
    assert a != b
    assert a.model_id != b.model_id


# --- candidates and fusion ------------------------------------------------


def test_candidate_rank_is_one_based():
    with pytest.raises(ValueError, match="1-based"):
        Candidate(chunk_id="c", rank=0, score=1.0, method=RetrievalMethod.VECTOR)


def test_candidate_list_rejects_a_mismatched_method():
    lexical = Candidate(chunk_id="c", rank=1, score=1.0, method=RetrievalMethod.LEXICAL)
    with pytest.raises(ValueError, match="does not match list method"):
        CandidateList(method=RetrievalMethod.VECTOR, candidates=(lexical,))


def test_fusion_tie_breaking_is_deterministic():
    """Equal scores must order identically every time, or ranking is untestable."""
    a = FusedCandidate(chunk_id="zzz", fused_score=0.5)
    b = FusedCandidate(chunk_id="aaa", fused_score=0.5)
    c = FusedCandidate(chunk_id="mmm", fused_score=0.9)
    ordered = [x.chunk_id for x in sorted([a, b, c], key=lambda x: x.sort_key)]
    assert ordered == ["mmm", "aaa", "zzz"]
    # stable across repetition
    for _ in range(5):
        assert [x.chunk_id for x in sorted([a, b, c], key=lambda x: x.sort_key)] == ordered


def test_fused_candidate_reports_contributing_methods():
    fused = FusedCandidate(
        chunk_id="c",
        fused_score=0.8,
        contributions=(
            Candidate(chunk_id="c", rank=1, score=0.9, method=RetrievalMethod.VECTOR),
            Candidate(chunk_id="c", rank=3, score=0.4, method=RetrievalMethod.LEXICAL),
        ),
    )
    assert fused.methods() == (RetrievalMethod.VECTOR, RetrievalMethod.LEXICAL)


# --- provenance answers the required questions ----------------------------


def test_provenance_answers_every_required_question():
    chunk = a_chunk()
    prov = RetrievalProvenance(
        document_id="doc",
        version_id="ver",
        chunk_id=chunk.id,
        start=0,
        end=5,
        methods=(RetrievalMethod.VECTOR, RetrievalMethod.LEXICAL),
        ranks={RetrievalMethod.VECTOR: 1, RetrievalMethod.LEXICAL: 4},
        scores={RetrievalMethod.VECTOR: 0.91},
        fused_score=0.77,
        index_version="idx-1",
        source_uri="file://x",
    )
    assert prov.source_uri == "file://x"          # where did it originate
    assert prov.version_id == "ver"               # which version
    assert (prov.chunk_id, prov.start, prov.end) == (chunk.id, 0, 5)  # which text
    assert RetrievalMethod.LEXICAL in prov.methods                    # which path
    assert prov.ranks[RetrievalMethod.VECTOR] == 1                  # what ranking
    assert prov.fused_score == 0.77
    assert prov.index_version == "idx-1"          # against which index


def test_provenance_is_a_type_not_a_metadata_dict():
    prov = RetrievalProvenance(
        document_id="d", version_id="v", chunk_id="c", start=0, end=1
    )
    assert not isinstance(prov, dict)
    with pytest.raises(TypeError):
        prov.ranks[RetrievalMethod.VECTOR] = 1  # type: ignore[index]  # the point


def test_result_rejects_provenance_for_a_different_chunk():
    chunk = a_chunk()
    wrong = RetrievalProvenance(
        document_id="doc", version_id="ver", chunk_id="someone-else", start=0, end=5
    )
    with pytest.raises(ValueError, match="does not match"):
        RetrievalResult(chunk=chunk, provenance=wrong)
