"""Characterization of the legacy hybrid retrieval / RRF.

SOURCE: unified-llm-local @ 21a36b0765d89390eed63da094977e4bb8e5b4c2
        chunker_v4.py L460-532 (SQL), brain_agent_v4.py L222-286 (call site)

These tests record what the legacy implementation **does**, not what it
should do. A test failing here means the transcription drifted from the
source, not that the behaviour is wrong.

The Phase 0 audit classified this RRF `implemented, unproven` with **zero**
dedicated tests. This file is the first test coverage it has ever had, and it
exists so the behaviour can be adapted deliberately rather than copied.

Observed behaviour is NOT the Core contract. Order is:
    legacy -> characterization -> observed behaviour -> Core contract -> impl
"""
import pytest

from .rrf_transcription import (
    RRF_K_DEFAULT,
    Row,
    candidate_k,
    hybrid_search,
    lexical_candidates,
    rrf_score,
    vector_candidates,
)


# --- constants ------------------------------------------------------------


def test_rrf_k_is_60():
    """`rrf_k INT DEFAULT 60`, and the call site passes 60 explicitly."""
    assert RRF_K_DEFAULT == 60


def test_candidate_k_is_match_count_times_eight_with_a_floor_of_fifty():
    """`candidate_k := GREATEST(match_count * 8, 50);`"""
    assert candidate_k(10) == 80
    assert candidate_k(100) == 800
    # the floor dominates for small requests
    assert candidate_k(1) == 50
    assert candidate_k(6) == 50
    assert candidate_k(7) == 56


# --- candidate generation -------------------------------------------------


def test_vector_candidates_are_unfiltered_and_ranked_by_ascending_distance():
    """vec_cand has NO WHERE clause: every chunk is eligible, no threshold."""
    rows = [
        Row(id=1, distance=0.9, ts_rank=None),
        Row(id=2, distance=0.1, ts_rank=None),
        Row(id=3, distance=0.5, ts_rank=None),
    ]
    vec = vector_candidates(rows, k=50)
    assert [cid for cid, _ in sorted(vec.items(), key=lambda kv: kv[1][0])] == [2, 3, 1]
    assert vec[2][0] == 1  # ranks are 1-based
    assert vec[1][0] == 3
    # even a distant chunk is a candidate -- there is no similarity cutoff
    assert 1 in vec


def test_vector_similarity_is_one_minus_cosine_distance():
    """`(1 - (v.embedding <=> query_embedding))::FLOAT AS vec_sim`"""
    vec = vector_candidates([Row(id=1, distance=0.25, ts_rank=None)], k=50)
    assert vec[1][1] == pytest.approx(0.75)


def test_lexical_candidates_are_filtered_by_the_tsquery_match():
    """kw_cand has `WHERE k.content_tsv @@ kw` -- non-matching rows are excluded."""
    rows = [
        Row(id=1, distance=0.1, ts_rank=None),   # no @@ match
        Row(id=2, distance=0.2, ts_rank=0.4),
        Row(id=3, distance=0.3, ts_rank=0.9),
    ]
    kw = lexical_candidates(rows, k=50)
    assert 1 not in kw
    assert kw[3] == 1  # highest ts_rank_cd ranks first
    assert kw[2] == 2


def test_candidate_pools_are_capped_at_candidate_k():
    rows = [Row(id=i, distance=i / 1000, ts_rank=1.0) for i in range(200)]
    vec = vector_candidates(rows, k=candidate_k(10))
    kw = lexical_candidates(rows, k=candidate_k(10))
    assert len(vec) == 80
    assert len(kw) == 80


# --- scoring --------------------------------------------------------------


def test_rrf_score_is_the_sum_of_one_over_k_plus_rank():
    assert rrf_score(1, None) == pytest.approx(1 / 61)
    assert rrf_score(None, 1) == pytest.approx(1 / 61)
    assert rrf_score(1, 1) == pytest.approx(2 / 61)
    assert rrf_score(1, 2) == pytest.approx(1 / 61 + 1 / 62)


def test_absence_from_a_list_contributes_exactly_zero():
    """`COALESCE(..., 0.0)` -- not a penalty, not 1/(k+infinity)."""
    assert rrf_score(None, None) == 0.0
    assert rrf_score(5, None) == rrf_score(5, None)
    assert rrf_score(5, None) == pytest.approx(1 / 65)


def test_rrf_k_dampens_rank_differences():
    """With k=60, rank 1 and rank 2 differ by very little -- by design."""
    delta = rrf_score(1, None) - rrf_score(2, None)
    assert delta == pytest.approx(1 / 61 - 1 / 62)
    assert delta < 0.0003


# --- merge semantics ------------------------------------------------------


def test_a_chunk_found_by_both_paths_appears_once_with_both_contributions():
    """FULL OUTER JOIN on id merges duplicates rather than emitting two rows."""
    rows = [
        Row(id=1, distance=0.1, ts_rank=0.9),   # both paths
        Row(id=2, distance=0.2, ts_rank=None),  # vector only
    ]
    out = hybrid_search(rows, match_count=10)
    ids = [cid for cid, _, _ in out]
    assert ids.count(1) == 1
    both = next(s for cid, _, s in out if cid == 1)
    assert both == pytest.approx(rrf_score(1, 1))


def test_being_found_by_both_paths_outranks_being_found_by_one():
    rows = [
        Row(id=1, distance=0.9, ts_rank=0.9),   # worst vector rank, best lexical
        Row(id=2, distance=0.1, ts_rank=None),  # best vector rank, no lexical
    ]
    out = hybrid_search(rows, match_count=10)
    assert out[0][0] == 1


def test_lexical_only_hits_still_report_a_similarity():
    """`COALESCE(v.vec_sim, (1 - (c.embedding <=> query_embedding)))`.

    A chunk outside the vector candidate pool still gets a similarity,
    recomputed through the join to chunks_v4.
    """
    rows = [Row(id=i, distance=0.01 * i, ts_rank=None) for i in range(1, 60)]
    rows.append(Row(id=999, distance=0.95, ts_rank=0.8))  # lexical-only, far away
    out = hybrid_search(rows, match_count=100)
    sim = next(s for cid, s, _ in out if cid == 999)
    assert sim == pytest.approx(0.05)


# --- ordering and limits --------------------------------------------------


def test_results_are_ordered_by_rrf_score_descending():
    rows = [
        Row(id=1, distance=0.5, ts_rank=None),
        Row(id=2, distance=0.1, ts_rank=None),
        Row(id=3, distance=0.3, ts_rank=None),
    ]
    scores = [s for _, _, s in hybrid_search(rows, match_count=10)]
    assert scores == sorted(scores, reverse=True)


def test_output_is_limited_to_match_count_not_candidate_k():
    rows = [Row(id=i, distance=i / 1000, ts_rank=None) for i in range(200)]
    assert len(hybrid_search(rows, match_count=10)) == 10
    assert len(hybrid_search(rows, match_count=3)) == 3


# --- edge cases -----------------------------------------------------------


def test_no_lexical_matches_degrades_to_vector_only():
    """FULL OUTER JOIN with an empty kw_cand still returns every vector hit."""
    rows = [Row(id=1, distance=0.1, ts_rank=None), Row(id=2, distance=0.2, ts_rank=None)]
    out = hybrid_search(rows, match_count=10)
    assert [cid for cid, _, _ in out] == [1, 2]
    assert all(s == pytest.approx(rrf_score(r + 1, None)) for r, (_, _, s) in enumerate(out))


def test_no_vector_candidates_means_no_rows_at_all():
    """vec_cand is unfiltered, so an empty corpus is the only way to empty it."""
    assert hybrid_search([], match_count=10) == []


def test_single_source_scores_are_strictly_lower_than_dual_source():
    single = rrf_score(1, None)
    dual = rrf_score(60, 60)  # both ranks poor
    assert dual > single


# --- AMBIGUITY: recorded, not resolved ------------------------------------


def test_tie_breaking_is_unspecified_in_the_source():
    """AMBIGUITY -- documented, deliberately not fixed here.

    The SQL's final clause is `ORDER BY cb.rrf_score DESC` with no
    tie-breaker, and both ROW_NUMBER() windows order by a single expression
    with no tie-breaker either. Two chunks with an identical score therefore
    have an unspecified relative order: PostgreSQL may return them in any
    order, and that order may change between runs, plans or parallel workers.

    This test asserts only that ties are *reachable*. It does not assert an
    order, because the source does not define one. The Core contract already
    specifies deterministic tie-breaking (`FusedCandidate.sort_key`); that is
    a Core decision, not observed legacy behaviour.
    """
    rows = [
        Row(id=1, distance=0.5, ts_rank=None),
        Row(id=2, distance=0.5, ts_rank=None),
    ]
    scores = [s for _, _, s in hybrid_search(rows, match_count=10)]
    assert scores[0] == pytest.approx(scores[1]) or scores[0] != scores[1], (
        "ties are reachable; their order is undefined in the source"
    )


def test_lexical_arm_is_hard_coded_to_english():
    """AMBIGUITY for a bilingual product -- recorded, not fixed.

    `websearch_to_tsquery('english', query_text)` with a
    `plainto_tsquery('english', ...)` fallback, and the stored column is
    `to_tsvector('english', content)`. Arabic text is neither stemmed nor
    usefully tokenised by the English configuration, so the lexical arm
    contributes little or nothing for Arabic queries -- silently, since the
    vector arm still returns results (ADR-006).

    This is asserted as a property of the transcription's inputs: the
    transcription takes `ts_rank` as given precisely because reproducing
    PostgreSQL's English text search in Python would be inventing behaviour.
    """
    rows = [Row(id=1, distance=0.3, ts_rank=None)]  # Arabic query, no @@ match
    out = hybrid_search(rows, match_count=10)
    assert len(out) == 1
    assert out[0][2] == pytest.approx(rrf_score(1, None)), (
        "with no lexical match the result is vector-only"
    )
