"""Retrieval-only gate over the bilingual bench corpora (B1).

Where each knowledge bench question's cited document ranks, measured on the
retrieval stack `pac --documents` composes, with no model in the loop. It is
the baseline a retrieval change -- a similarity floor, a chunker fix, a new
ranking -- is measured against: any change to a rank fails this test, and the
change updates BASELINE in the same PR, so the effect is in the diff.

How a rank is read. Chunk ids are minted per run, and RRF breaks equal fused
scores on the chunk id, so the ORDER of tied passages differs between runs
(`kb-incident-cause` in English came first in about half of 30 runs, second
in the rest). The gate reads a rank that does not depend on it: the cited
document's best passage ranks after every passage whose fused score is at
least its own. Ties count against it, so the number is the same on every run.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from personal_ai_core.app.bench.tasks import Task, load
from personal_ai_core.app.cli import _ingest
from personal_ai_core.conversation.factory import build_grounded_in_memory_service
from personal_ai_core.core.config import Settings
from personal_ai_core.core.domain import UNDETERMINED_LANGUAGE
from personal_ai_core.core.knowledge import RetrievalQuery
from personal_ai_core.knowledge import HashingEmbeddingProvider
from personal_ai_core.knowledge.retrieval import HybridRetriever

BENCH = Path(__file__).resolve().parents[2] / "evals" / "bench"
# The grounded path's default (`build_grounded_in_memory_service`).
EVIDENCE_LIMIT = 5

# (task id, language) -> the cited document's rank at worst, or None when it is
# not among the EVIDENCE_LIMIT passages retrieved. Measured at 6c35db3.
BASELINE: dict[tuple[str, str], int | None] = {
    ("kb-api-port", "en"): 1,
    ("kb-api-port", "ar"): 1,
    ("kb-deploy-day", "en"): 1,
    ("kb-deploy-day", "ar"): 3,
    ("kb-incident-cause", "en"): 2,  # tied with architecture.md for first
    ("kb-incident-cause", "ar"): 1,
    ("kb-leave-carryover", "en"): 1,
    ("kb-leave-carryover", "ar"): 3,
    ("kb-meal-limit", "en"): 1,
    ("kb-meal-limit", "ar"): 1,
    ("kb-remote-days", "en"): 1,
    ("kb-remote-days", "ar"): 2,
    ("kb-sick-certificate", "en"): 1,
    ("kb-sick-certificate", "ar"): 2,
    ("kb-travel-notice", "en"): 1,
    ("kb-travel-notice", "ar"): 1,
}

# What the per-case ranks add up to, written out so a reader of a diff sees
# the headline move. recall@2 is what a long session's evidence reserve
# guarantees room for (EVIDENCE_RESERVE_ITEMS, P1-3).
RECALL = {
    "en": {1: 7, 2: 8, 5: 8},
    "ar": {1: 4, 2: 6, 5: 8},
}


def _cited(task: Task) -> set[str]:
    return {check["document"] for check in task.checks if check["type"] == "cites"}


def _measured_tasks() -> list[Task]:
    return [t for t in load(BENCH) if t.track == "knowledge" and _cited(t)]


def _rank(task: Task, language: str) -> int | None:
    # The composition `pac --documents` and the bench use: the same builder,
    # the same ingestion of the same files, the same language (undetermined,
    # as the bench sends it). No model is called: nothing is sent.
    grounded = build_grounded_in_memory_service(Settings())
    assert task.corpus is not None
    _ingest(grounded.ingestion, sorted(p for p in task.corpus.iterdir() if p.is_file()),
            io.StringIO())
    # Over the slice's own indexes. The vector index refuses an embedding from
    # any other model, so a change of embedder in the factory fails here
    # loudly rather than measuring something else.
    embedder = HashingEmbeddingProvider()
    retriever = HybridRetriever(
        embedder=embedder,
        vector_index=grounded.vector_index,
        lexical_index=grounded.lexical_index,
        catalog=grounded.catalog,
    )
    results = retriever.retrieve(RetrievalQuery(
        text=task.instruction[language], limit=EVIDENCE_LIMIT,
        language=UNDETERMINED_LANGUAGE,
    ))
    cited = _cited(task)
    scores = [r.provenance.fused_score for r in results]
    hits = [
        score for result, score in zip(results, scores)
        if (result.provenance.source_uri or "").rsplit("/", 1)[-1] in cited
    ]
    if not hits:
        return None
    best = max(s for s in hits if s is not None)
    return sum(1 for s in scores if s is not None and s >= best)


@pytest.fixture(scope="module")
def measured() -> dict[tuple[str, str], int | None]:
    return {
        (task.id, language): _rank(task, language)
        for task in _measured_tasks()
        for language in ("en", "ar")
    }


def test_every_cited_knowledge_case_is_in_the_baseline():
    # A new knowledge case that cites a document is measured, not skipped; a
    # removed one leaves no stale row.
    expected = {(t.id, lang) for t in _measured_tasks() for lang in ("en", "ar")}
    assert set(BASELINE) == expected


def test_the_cited_document_ranks_where_the_baseline_says(measured):
    changed = {key: (BASELINE.get(key), rank) for key, rank in measured.items()
               if BASELINE.get(key) != rank}
    assert not changed, (
        "retrieval ranks moved (baseline, measured): "
        f"{dict(sorted(changed.items()))}. If the change is intended, update "
        "BASELINE and RECALL in the same PR."
    )


@pytest.mark.parametrize("language", ["en", "ar"])
def test_recall_matches_the_baseline(measured, language):
    ranks = [rank for (_, lang), rank in measured.items() if lang == language]
    recall = {k: sum(1 for r in ranks if r is not None and r <= k) for k in RECALL[language]}
    assert len(ranks) == 8
    assert recall == RECALL[language]


def test_the_rank_does_not_depend_on_the_run():
    # The reason the gate scores ties against the cited document: two builds
    # mint different chunk ids, and the rank read here must not move with them.
    task = next(t for t in _measured_tasks() if t.id == "kb-incident-cause")
    assert {_rank(task, "en") for _ in range(8)} == {BASELINE[("kb-incident-cause", "en")]}
