"""InMemoryVectorIndex and InMemoryLexicalIndex.

Both arms are tested against the same three properties, because the audited
system's failures were in exactly these: a filter that silently returned
nothing, an ordering that was never specified, and an English-only analyzer
applied to Arabic.
"""
import pytest

from personal_ai_core.core.errors import IndexError_
from personal_ai_core.core.knowledge import Embedding, RetrievalMethod
from personal_ai_core.knowledge import (
    HashingEmbeddingProvider,
    InMemoryLexicalIndex,
    InMemoryVectorIndex,
)

ARABIC_A = "الذكاء الاصطناعي يغير طريقة العمل في الشركات"
ARABIC_B = "القهوة العربية تقدم مع التمر في الضيافة"
ENGLISH_A = "artificial intelligence is changing how companies work"
ENGLISH_B = "arabic coffee is served with dates for hospitality"


@pytest.fixture
def embedder():
    return HashingEmbeddingProvider()


@pytest.fixture
def vector_index(embedder):
    return InMemoryVectorIndex(
        model_id=embedder.model_id, dimensions=embedder.dimensions
    )


# --- shared guarantees -----------------------------------------------------


def test_vector_language_filter_returns_matching_content_not_nothing(
    embedder, vector_index, make_chunk
):
    """The verified legacy defect, pinned.

    In the audited `search_brain()`, passing a `language` filter always
    returned an empty list -- indistinguishable, to the caller, from "there is
    no Arabic content".
    """
    chunks = [
        make_chunk(ARABIC_A, chunk_id="ar-1", language="ar"),
        make_chunk(ENGLISH_A, chunk_id="en-1", language="en"),
    ]
    vector_index.add(chunks, embedder.embed([c.text for c in chunks]))

    found = vector_index.search(
        embedding=embedder.embed([ARABIC_A])[0], limit=10, language="ar"
    )
    assert [c.chunk_id for c in found.candidates] == ["ar-1"]


def test_lexical_language_filter_returns_matching_content_not_nothing(make_chunk):
    index = InMemoryLexicalIndex()
    index.add(
        [
            make_chunk(ARABIC_A, chunk_id="ar-1", language="ar"),
            make_chunk(ENGLISH_A, chunk_id="en-1", language="en"),
        ]
    )
    found = index.search(text="الذكاء الاصطناعي", limit=10, language="ar")
    assert [c.chunk_id for c in found.candidates] == ["ar-1"]


@pytest.mark.parametrize("language", [None, "und"], ids=["none", "undetermined"])
def test_no_language_request_filters_nothing(embedder, vector_index, make_chunk, language):
    chunks = [
        make_chunk(ARABIC_A, chunk_id="ar-1", language="ar"),
        make_chunk(ENGLISH_A, chunk_id="en-1", language="en"),
    ]
    vector_index.add(chunks, embedder.embed([c.text for c in chunks]))
    found = vector_index.search(
        embedding=embedder.embed([ARABIC_A])[0], limit=10, language=language
    )
    assert len(found.candidates) == 2


def test_unlabelled_chunks_stay_reachable_when_a_language_is_requested(
    embedder, vector_index, make_chunk
):
    """Undetermined means unknown, not "not this one".

    A strict equality filter would hide every unlabelled chunk the moment a
    caller names a language -- the same user-visible symptom as the legacy
    bug, from a different cause.
    """
    chunks = [
        make_chunk(ARABIC_A, chunk_id="unlabelled", language="und"),
        make_chunk(ENGLISH_A, chunk_id="en-1", language="en"),
    ]
    vector_index.add(chunks, embedder.embed([c.text for c in chunks]))
    found = vector_index.search(
        embedding=embedder.embed([ARABIC_A])[0], limit=10, language="ar"
    )
    assert [c.chunk_id for c in found.candidates] == ["unlabelled"]


# --- vector index ----------------------------------------------------------


def test_vector_ranks_are_one_based_and_ordered_by_similarity(
    embedder, vector_index, make_chunk
):
    chunks = [
        make_chunk(ENGLISH_A, chunk_id="a"),
        make_chunk(ENGLISH_B, chunk_id="b"),
    ]
    vector_index.add(chunks, embedder.embed([c.text for c in chunks]))
    found = vector_index.search(embedding=embedder.embed([ENGLISH_A])[0], limit=10)

    assert [c.rank for c in found.candidates] == [1, 2]
    assert found.candidates[0].chunk_id == "a"
    assert found.candidates[0].score >= found.candidates[1].score
    assert found.method is RetrievalMethod.VECTOR


def test_vector_ties_break_deterministically_on_chunk_id(
    embedder, vector_index, make_chunk
):
    """Identical text means identical vectors; the order must still be fixed."""
    chunks = [make_chunk(ENGLISH_A, chunk_id=cid) for cid in ("c", "a", "b")]
    vector_index.add(chunks, embedder.embed([c.text for c in chunks]))
    found = vector_index.search(embedding=embedder.embed([ENGLISH_A])[0], limit=10)
    assert [c.chunk_id for c in found.candidates] == ["a", "b", "c"]


def test_vector_index_rejects_embeddings_from_another_model(vector_index, make_chunk):
    other = HashingEmbeddingProvider(model_id="some-other-model")
    with pytest.raises(IndexError_, match="not comparable"):
        vector_index.add([make_chunk(ENGLISH_A)], other.embed([ENGLISH_A]))


def test_vector_index_rejects_the_wrong_dimensionality(vector_index, make_chunk, embedder):
    wrong = Embedding(vector=(1.0, 0.0, 0.0), model_id=embedder.model_id)
    with pytest.raises(IndexError_, match="dimensions"):
        vector_index.add([make_chunk(ENGLISH_A)], [wrong])


def test_vector_index_rejects_a_query_from_another_model(embedder, vector_index):
    other = HashingEmbeddingProvider(model_id="some-other-model")
    with pytest.raises(IndexError_):
        vector_index.search(embedding=other.embed([ENGLISH_A])[0], limit=5)


def test_vector_index_rejects_mismatched_input_lengths(vector_index, make_chunk, embedder):
    with pytest.raises(IndexError_, match="one to one"):
        vector_index.add(
            [make_chunk(ENGLISH_A), make_chunk(ENGLISH_B)], embedder.embed([ENGLISH_A])
        )


def test_removing_a_document_removes_its_chunks(embedder, vector_index, make_chunk):
    chunks = [
        make_chunk(ENGLISH_A, chunk_id="a", document_id="doc-a"),
        make_chunk(ENGLISH_B, chunk_id="b", document_id="doc-b"),
    ]
    vector_index.add(chunks, embedder.embed([c.text for c in chunks]))
    vector_index.remove_document("doc-a")

    found = vector_index.search(embedding=embedder.embed([ENGLISH_A])[0], limit=10)
    assert [c.chunk_id for c in found.candidates] == ["b"]


def test_index_version_changes_on_every_mutation(embedder, vector_index, make_chunk):
    """Provenance names an index build; a constant version is decoration."""
    versions = [vector_index.index_version]
    chunk = make_chunk(ENGLISH_A, document_id="doc-a")
    vector_index.add([chunk], embedder.embed([chunk.text]))
    versions.append(vector_index.index_version)
    vector_index.remove_document("doc-a")
    versions.append(vector_index.index_version)
    assert len(set(versions)) == 3


# --- lexical index ---------------------------------------------------------


def test_lexical_search_matches_arabic(make_chunk):
    """ADR-006: the audited arm ran an English analyzer over every language."""
    index = InMemoryLexicalIndex()
    index.add(
        [
            make_chunk(ARABIC_A, chunk_id="ar-1", language="ar"),
            make_chunk(ARABIC_B, chunk_id="ar-2", language="ar"),
        ]
    )
    found = index.search(text="القهوة العربية", limit=10)
    assert [c.chunk_id for c in found.candidates] == ["ar-2"]


def test_lexical_search_ignores_arabic_diacritics(make_chunk):
    index = InMemoryLexicalIndex()
    index.add([make_chunk("العربية", chunk_id="ar-1", language="ar")])
    # Same word written with tashkeel.
    found = index.search(text="الْعَرَبِيَّة", limit=10)
    assert [c.chunk_id for c in found.candidates] == ["ar-1"]


def test_lexical_search_is_case_insensitive(make_chunk):
    index = InMemoryLexicalIndex()
    index.add([make_chunk("Artificial Intelligence", chunk_id="a")])
    assert index.search(text="ARTIFICIAL", limit=5).candidates[0].chunk_id == "a"


def test_chunks_sharing_no_term_are_not_returned_at_all(make_chunk):
    """A non-match must not receive a rank.

    Fusion rewards rank, so a zero-relevance chunk that still appears at rank 1
    of the lexical arm would be promoted by RRF as if it were evidence.
    """
    index = InMemoryLexicalIndex()
    index.add(
        [
            make_chunk(ENGLISH_A, chunk_id="a"),
            make_chunk(ARABIC_B, chunk_id="b"),
        ]
    )
    found = index.search(text="artificial intelligence", limit=10)
    assert [c.chunk_id for c in found.candidates] == ["a"]


def test_lexical_ties_break_deterministically_on_chunk_id(make_chunk):
    index = InMemoryLexicalIndex()
    index.add([make_chunk(ENGLISH_A, chunk_id=cid) for cid in ("c", "a", "b")])
    found = index.search(text="artificial", limit=10)
    assert [c.chunk_id for c in found.candidates] == ["a", "b", "c"]


def test_readding_a_chunk_does_not_double_count_its_terms(make_chunk):
    """A document frequency inflated by re-ingestion silently skews every IDF."""
    index = InMemoryLexicalIndex()
    chunk = make_chunk(ENGLISH_A, chunk_id="a")
    index.add([chunk])
    once = index.search(text="artificial", limit=5).candidates[0].score
    index.add([chunk])
    twice = index.search(text="artificial", limit=5).candidates[0].score
    assert len(index) == 1
    assert once == pytest.approx(twice)


def test_lexical_removing_a_document_removes_its_chunks(make_chunk):
    index = InMemoryLexicalIndex()
    index.add(
        [
            make_chunk(ENGLISH_A, chunk_id="a", document_id="doc-a"),
            make_chunk(ENGLISH_A, chunk_id="b", document_id="doc-b"),
        ]
    )
    index.remove_document("doc-a")
    found = index.search(text="artificial", limit=10)
    assert [c.chunk_id for c in found.candidates] == ["b"]
    assert len(index) == 1


def test_an_empty_lexical_index_returns_an_empty_list(make_chunk):
    found = InMemoryLexicalIndex().search(text="anything", limit=10)
    assert found.candidates == ()
    assert found.method is RetrievalMethod.LEXICAL


def test_a_query_with_no_tokens_returns_an_empty_list(make_chunk):
    index = InMemoryLexicalIndex()
    index.add([make_chunk(ENGLISH_A, chunk_id="a")])
    assert index.search(text="!!! ???", limit=10).candidates == ()


@pytest.mark.parametrize("limit", [0, -1])
def test_both_indexes_reject_a_non_positive_limit(embedder, vector_index, limit):
    with pytest.raises(ValueError):
        vector_index.search(embedding=embedder.embed([ENGLISH_A])[0], limit=limit)
    with pytest.raises(ValueError):
        InMemoryLexicalIndex().search(text=ENGLISH_A, limit=limit)


@pytest.mark.parametrize("kwargs", [{"k1": -1.0}, {"b": -0.1}, {"b": 1.1}])
def test_impossible_bm25_parameters_are_rejected(kwargs):
    with pytest.raises(ValueError):
        InMemoryLexicalIndex(**kwargs)


# --- exactly which BM25 (audit finding 2) ----------------------------------


def test_bm25_parameters_are_the_stated_defaults():
    """k1 and b are choices; a change to either must be a visible one."""
    from personal_ai_core.knowledge.lexical_index import (
        BM25_B,
        BM25_K1,
        BM25_QUERY_TERM_FREQUENCY,
    )

    assert (BM25_K1, BM25_B) == (1.5, 0.75)
    assert BM25_QUERY_TERM_FREQUENCY == "linear"


def test_query_term_frequency_is_linear_not_deduplicated(make_chunk):
    """The variant the module docstring names, pinned.

    "BM25" alone does not pin down how a repeated query term is weighted. This
    implementation sums duplicates, which is the full Okapi formula with k3
    unbounded. Deduplicating would make these two scores equal; saturating with
    a finite k3 would put the ratio between 1 and 3.
    """
    index = InMemoryLexicalIndex()
    index.add([make_chunk(ENGLISH_A, chunk_id="a"), make_chunk(ARABIC_A, chunk_id="b")])

    once = index.search(text="artificial", limit=5).candidates[0].score
    thrice = index.search(text="artificial artificial artificial", limit=5).candidates[0].score

    assert thrice == pytest.approx(once * 3)


def test_a_repeated_query_term_outweighs_a_single_one_in_ranking(make_chunk):
    """The user-visible consequence of linear query term frequency."""
    index = InMemoryLexicalIndex()
    index.add(
        [
            make_chunk("artificial systems", chunk_id="artificial-only"),
            make_chunk("intelligence systems", chunk_id="intelligence-only"),
        ]
    )
    # Weighting one term more heavily by repeating it changes which wins.
    plain = index.search(text="artificial intelligence", limit=5).candidates
    weighted = index.search(
        text="intelligence intelligence intelligence artificial", limit=5
    ).candidates

    assert {c.chunk_id for c in plain} == {"artificial-only", "intelligence-only"}
    assert weighted[0].chunk_id == "intelligence-only"


def test_the_per_term_formula_is_textbook_okapi(make_chunk):
    """Recomputed here from the formula rather than trusted from the name."""
    import math

    docs = {"d1": "the cat sat on the mat the cat", "d2": "a dog sat on a log"}
    index = InMemoryLexicalIndex()
    index.add([make_chunk(text, chunk_id=cid) for cid, text in docs.items()])

    from personal_ai_core.knowledge.text import tokenize

    tokens = {cid: tokenize(text) for cid, text in docs.items()}
    n = len(tokens)
    avgdl = sum(len(t) for t in tokens.values()) / n
    query = tokenize("cat sat")

    expected = {}
    for cid, terms in tokens.items():
        score = 0.0
        for term in query:
            frequency = terms.count(term)
            if frequency == 0:
                continue
            df = sum(1 for t in tokens.values() if term in t)
            idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
            score += (
                idf
                * (frequency * (1.5 + 1.0))
                / (frequency + 1.5 * (1 - 0.75 + 0.75 * len(terms) / avgdl))
            )
        expected[cid] = score

    actual = {
        c.chunk_id: c.score for c in index.search(text="cat sat", limit=9).candidates
    }
    for cid, score in expected.items():
        if score > 0:
            assert actual[cid] == pytest.approx(score, abs=1e-12)
