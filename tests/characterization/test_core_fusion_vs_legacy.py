"""The Core's RRF, checked against the characterized legacy behaviour.

`test_legacy_rrf.py` pins what the legacy algorithm does. This file answers the
next question: for each of those behaviours, did the Core keep it or drop it,
and was that on purpose?

Every assertion below corresponds to a decision recorded in
`docs/RRF_CHARACTERIZATION.md` and in `knowledge/fusion.py`. A future change to
either side breaks a test here that names which decision it broke.

Direction of the dependency is one way and stays that way: this test imports
both the transcription and the Core. The Core imports neither. There is a
structural check for that in `tests/unit/test_dependency_direction.py`.
"""
import ast
import inspect

import pytest

from personal_ai_core.core.knowledge import (
    Candidate,
    CandidateList,
    RetrievalMethod,
)
from personal_ai_core.knowledge import RRF_K, ReciprocalRankFusion

from . import rrf_transcription as legacy

# Modules that make up the Core's fusion and retrieval path.
CORE_RETRIEVAL_MODULES = (
    "personal_ai_core.knowledge.fusion",
    "personal_ai_core.knowledge.retrieval",
    "personal_ai_core.knowledge.lexical_index",
    "personal_ai_core.knowledge.vector_index",
    "personal_ai_core.knowledge.language",
    "personal_ai_core.knowledge.text",
)


def _docstring_ids(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ) or not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                found.add(id(first.value))
    return found


def parse_core_module(dotted: str) -> ast.AST:
    import importlib

    return ast.parse(inspect.getsource(importlib.import_module(dotted)))


def code_strings(tree: ast.AST) -> list[str]:
    """Every string literal that is not a docstring.

    Docstrings must be excluded, not stripped by line. Several of these modules
    deliberately quote the legacy behaviour in prose in order to explain why it
    was rejected -- and a check that treats that explanation as evidence of the
    defect punishes the documentation.
    """
    skip = _docstring_ids(tree)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in skip
    ]


def as_list(method, ranked_ids):
    return CandidateList(
        method=method,
        candidates=tuple(
            Candidate(chunk_id=chunk_id, rank=rank, score=0.0, method=method)
            for rank, chunk_id in enumerate(ranked_ids, start=1)
        ),
    )


# --- inherited: taken from the legacy behaviour on purpose -----------------


def test_core_k_matches_the_legacy_default():
    assert RRF_K == legacy.RRF_K_DEFAULT == 60


@pytest.mark.parametrize(
    "vector_rank, lexical_rank",
    [(1, 1), (1, None), (None, 1), (3, 7), (None, 10), (10, None)],
)
def test_core_scores_match_the_legacy_formula_exactly(vector_rank, lexical_rank):
    lists = []
    if vector_rank is not None:
        lists.append(as_list(RetrievalMethod.SEMANTIC, [""] * (vector_rank - 1) + ["x"]))
    if lexical_rank is not None:
        lists.append(as_list(RetrievalMethod.LEXICAL, [""] * (lexical_rank - 1) + ["x"]))

    fused = {f.chunk_id: f.fused_score for f in
             ReciprocalRankFusion().fuse_detailed(lists, limit=50)}
    assert fused["x"] == pytest.approx(legacy.rrf_score(vector_rank, lexical_rank))


def test_a_missing_arm_contributes_zero_in_both():
    assert legacy.rrf_score(None, None) == 0.0
    assert ReciprocalRankFusion().fuse([], limit=5) == []


def test_both_reward_appearing_in_two_arms_over_ranking_first_in_one():
    assert legacy.rrf_score(1, None) < legacy.rrf_score(2, 2)
    order = ReciprocalRankFusion().fuse(
        [
            as_list(RetrievalMethod.SEMANTIC, ["only-top", "both"]),
            as_list(RetrievalMethod.LEXICAL, ["filler", "both"]),
        ],
        limit=5,
    )
    assert order[0] == "both"


# --- deliberately not inherited --------------------------------------------


def test_the_core_resolves_the_tie_ordering_the_legacy_left_undefined():
    """Legacy: equal scores fell to the store's row order, which is not a
    guarantee. Core: ascending chunk_id, always."""
    assert legacy.rrf_score(1, None) == legacy.rrf_score(1, None)
    order = ReciprocalRankFusion().fuse(
        [as_list(RetrievalMethod.SEMANTIC, ["z"]),
         as_list(RetrievalMethod.SEMANTIC, ["a"])],
        limit=5,
    )
    assert order == ["a", "z"]


def test_the_core_has_no_candidate_depth_formula_at_all():
    """Legacy: `candidate_k := GREATEST(match_count * 8, 50)` inside the search
    function.

    The Core's fusion takes `limit` and nothing else; depth is the retriever's
    explicit setting, independent of what the caller asked to display. This
    asserts the formula was not quietly reimplemented, by looking for the
    multiplication in the syntax tree rather than for a string in the text.
    """
    assert legacy.candidate_k(3) == 50
    assert legacy.candidate_k(10) == 80

    for dotted in ("personal_ai_core.knowledge.fusion",
                   "personal_ai_core.knowledge.retrieval"):
        for node in ast.walk(parse_core_module(dotted)):
            if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Mult):
                continue
            operands = (node.left, node.right)
            assert not any(
                isinstance(side, ast.Constant) and side.value == 8
                for side in operands
            ), f"{dotted} multiplies by 8; the legacy depth formula is back"


@pytest.mark.parametrize("dotted", CORE_RETRIEVAL_MODULES)
def test_the_core_does_not_inherit_the_english_only_language_behaviour(dotted):
    """ADR-006. The legacy lexical arm was `to_tsvector('english', ...)`:
    English stemming and stop words applied to every language, silently.

    No module in the Core's retrieval path names a language in code, so there
    is no default that can be wrong. Prose that explains the rejected behaviour
    is exempt -- that reasoning is the most useful thing in those files.
    """
    tree = parse_core_module(dotted)

    for literal in code_strings(tree):
        assert "english" not in literal.lower(), (
            f"{dotted} names a language in code: {literal!r}"
        )

    for node in ast.walk(tree):
        name = None
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, ast.arg):
            name = node.arg
        elif isinstance(node, ast.Name):
            name = node.id
        if name and "english" in name.lower():
            raise AssertionError(f"{dotted} names English in an identifier: {name}")


def test_the_english_check_would_actually_catch_a_regression():
    """Adversarial: the check above is worthless if prose exemption swallows
    everything."""
    tree = ast.parse(
        '"""Explains to_tsvector(\'english\') and why it was rejected."""\n'
        "ANALYZER = 'english'\n"
    )
    literals = code_strings(tree)
    assert literals == ["english"], literals
