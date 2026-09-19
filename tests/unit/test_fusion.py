"""ReciprocalRankFusion.

Three inherited behaviours (k=60, rank-based, missing=0) and one deliberate
departure (deterministic tie ordering) are each pinned separately, so that a
future change has to say which one it is changing.
"""
import pytest

from personal_ai_core.core.knowledge import (
    Candidate,
    CandidateList,
    RetrievalMethod,
)
from personal_ai_core.knowledge import RRF_K, ReciprocalRankFusion


def vector_list(*chunk_ids):
    return CandidateList(
        method=RetrievalMethod.VECTOR,
        candidates=tuple(
            Candidate(
                chunk_id=chunk_id,
                rank=rank,
                score=1.0 / rank,
                method=RetrievalMethod.VECTOR,
            )
            for rank, chunk_id in enumerate(chunk_ids, start=1)
        ),
    )


def lexical(*chunk_ids):
    return CandidateList(
        method=RetrievalMethod.LEXICAL,
        candidates=tuple(
            Candidate(
                chunk_id=chunk_id,
                rank=rank,
                # Deliberately on a wildly different scale from the vector
                # arm: if fusion ever starts reading scores, this breaks it.
                score=1000.0 / rank,
                method=RetrievalMethod.LEXICAL,
            )
            for rank, chunk_id in enumerate(chunk_ids, start=1)
        ),
    )


@pytest.fixture
def fusion():
    return ReciprocalRankFusion()


# --- inherited from the characterized legacy behaviour ---------------------


def test_k_is_sixty(fusion):
    assert RRF_K == 60
    assert fusion.k == 60


def test_score_is_the_sum_of_one_over_k_plus_rank(fusion):
    fused = fusion.fuse_detailed([vector_list("a"), lexical("a")], limit=5)
    assert fused[0].fused_score == pytest.approx(1 / 61 + 1 / 61)


def test_a_candidate_missing_from_a_list_contributes_zero_not_a_penalty(fusion):
    """Absence is absence -- no imputed worst rank, no subtraction."""
    fused = {f.chunk_id: f.fused_score for f in fusion.fuse_detailed(
        [vector_list("a", "b"), lexical("a")], limit=5
    )}
    assert fused["b"] == pytest.approx(1 / 62)
    assert fused["a"] == pytest.approx(1 / 61 + 1 / 61)


def test_fusion_reads_ranks_and_ignores_scores(fusion):
    """The lexical arm's scores here are a thousand times larger.

    If they were being summed, the lexical top hit would win. It does not,
    because scores from two arms share no scale -- which is the reason RRF
    exists at all.
    """
    order = fusion.fuse([vector_list("a", "b"), lexical("b", "a")], limit=5)
    # Perfectly symmetric input: both score 1/61 + 1/62, so the tie rule
    # decides -- not the lexical arm's larger numbers.
    assert order == ["a", "b"]


def test_appearing_in_both_arms_beats_appearing_first_in_one(fusion):
    order = fusion.fuse([vector_list("only-top", "both"), lexical("both")], limit=5)
    assert order == ["both", "only-top"]


# --- deliberately NOT inherited: undefined tie ordering --------------------


def test_equal_scores_order_by_ascending_chunk_id(fusion):
    order = fusion.fuse([vector_list("z"), vector_list("a"), vector_list("m")], limit=5)
    assert order == ["a", "m", "z"]


def test_ordering_does_not_depend_on_input_order(fusion):
    """The legacy query left ties to the store's row order, which is not a
    guarantee. A ranking that can differ between two identical runs cannot be
    tested, which is how it stayed unproven."""
    first = fusion.fuse([vector_list("b", "a"), lexical("a", "b")], limit=5)
    second = fusion.fuse([lexical("a", "b"), vector_list("b", "a")], limit=5)
    assert first == second


# --- structure and contract ------------------------------------------------


def test_contributions_record_what_each_arm_supplied(fusion):
    fused = fusion.fuse_detailed([vector_list("a", "b"), lexical("b")], limit=5)
    by_id = {f.chunk_id: f for f in fused}
    assert by_id["b"].methods() == (RetrievalMethod.VECTOR, RetrievalMethod.LEXICAL)
    assert by_id["a"].methods() == (RetrievalMethod.VECTOR,)
    assert {c.rank for c in by_id["b"].contributions} == {2, 1}


def test_fuse_returns_the_same_order_as_fuse_detailed(fusion):
    lists = [vector_list("a", "b", "c"), lexical("c", "a")]
    assert fusion.fuse(lists, limit=3) == [
        f.chunk_id for f in fusion.fuse_detailed(lists, limit=3)
    ]


def test_limit_truncates_after_ranking_not_before(fusion):
    """`b` is only reachable because the lexical arm also has it."""
    assert fusion.fuse([vector_list("a", "b"), lexical("b", "b2")], limit=1) == ["b"]


def test_no_lists_produces_no_results(fusion):
    assert fusion.fuse([], limit=5) == []


def test_empty_lists_produce_no_results(fusion):
    assert fusion.fuse([CandidateList(method=RetrievalMethod.VECTOR)], limit=5) == []


@pytest.mark.parametrize("limit", [0, -1])
def test_a_non_positive_limit_is_rejected(fusion, limit):
    with pytest.raises(ValueError):
        fusion.fuse([vector_list("a")], limit=limit)


def test_k_must_be_positive():
    with pytest.raises(ValueError):
        ReciprocalRankFusion(k=0)


def test_a_smaller_k_sharpens_the_influence_of_rank():
    """Documents why 60 is not arbitrary-but-harmless.

    k controls how much rank 1 outweighs rank 10. Changing it changes every
    ranking, which is why it is inherited rather than re-picked.
    """
    lists = [vector_list("top", "tenth")]
    gap = lambda k: (  # noqa: E731
        lambda fused: fused[0].fused_score - fused[1].fused_score
    )(ReciprocalRankFusion(k=k).fuse_detailed(lists, limit=2))
    assert gap(5) > gap(60)
