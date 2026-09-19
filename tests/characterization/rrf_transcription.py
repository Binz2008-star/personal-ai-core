"""Faithful transcription of the legacy hybrid_search() RRF algorithm.

SOURCE (pinned, read-only, never imported at runtime):
    unified-llm-local @ 21a36b0765d89390eed63da094977e4bb8e5b4c2
    chunker_v4.py L460-532  --  CREATE FUNCTION hybrid_search(...)
    brain_agent_v4.py L222-286  --  the only call site

This is NOT Core code and NOT a proposed implementation. It exists so the
legacy behaviour can be pinned by tests without importing the legacy package
or requiring Postgres, pgvector or Neon.

It transcribes what the SQL *does*, including its defects. Where the SQL is
non-deterministic, this transcription is explicitly marked and the tests assert
the ambiguity rather than inventing a resolution.

Nothing here may be promoted into `personal_ai_core` without going through the
Core contract first.
"""
from __future__ import annotations

from dataclasses import dataclass

RRF_K_DEFAULT = 60
"""Observed: `rrf_k INT DEFAULT 60` in the function signature, and the call
site passes 60 explicitly."""


def candidate_k(match_count: int) -> int:
    """Observed: `candidate_k := GREATEST(match_count * 8, 50);`

    A floor of 50, so small match_counts still scan a wide candidate pool.
    """
    return max(match_count * 8, 50)


@dataclass(frozen=True)
class Row:
    """A chunk as the legacy schema sees it."""

    id: int
    distance: float       # cosine distance: `embedding <=> query_embedding`
    ts_rank: float | None # ts_rank_cd(content_tsv, kw); None means no @@ match


def vector_candidates(rows: list[Row], k: int) -> dict[int, tuple[int, float]]:
    """Observed vec_cand CTE.

    - ORDER BY embedding <=> query_embedding  (ascending distance, closest first)
    - ROW_NUMBER() -> 1-based rank
    - LIMIT candidate_k
    - NO WHERE clause and no similarity threshold: every chunk is eligible.

    Returns {id: (rank, similarity)} where similarity = 1 - distance.
    """
    ordered = sorted(rows, key=lambda r: r.distance)[:k]
    return {r.id: (i + 1, 1.0 - r.distance) for i, r in enumerate(ordered)}


def lexical_candidates(rows: list[Row], k: int) -> dict[int, int]:
    """Observed kw_cand CTE.

    - WHERE content_tsv @@ kw   (filtered: non-matching rows are excluded)
    - ORDER BY ts_rank_cd(...) DESC
    - ROW_NUMBER() -> 1-based rank
    - LIMIT candidate_k
    - Returns rank only; no lexical score reaches the final output.
    """
    matching = [r for r in rows if r.ts_rank is not None]
    ordered = sorted(matching, key=lambda r: -r.ts_rank)[:k]
    return {r.id: i + 1 for i, r in enumerate(ordered)}


def rrf_score(vec_rank: int | None, kw_rank: int | None, rrf_k: int = RRF_K_DEFAULT) -> float:
    """Observed combined CTE scoring.

        COALESCE(1.0 / (rrf_k + v.vec_rank), 0.0)
      + COALESCE(1.0 / (rrf_k + k.kw_rank), 0.0)

    Absence from a list contributes exactly 0.0 -- not 1/(k+infinity), and not
    a penalty.
    """
    score = 0.0
    if vec_rank is not None:
        score += 1.0 / (rrf_k + vec_rank)
    if kw_rank is not None:
        score += 1.0 / (rrf_k + kw_rank)
    return score


def hybrid_search(
    rows: list[Row], *, match_count: int = 10, rrf_k: int = RRF_K_DEFAULT
) -> list[tuple[int, float, float]]:
    """Observed end-to-end behaviour of hybrid_search().

    Returns [(id, similarity, rrf_score)] ordered by rrf_score DESC,
    limited to match_count.

    NON-DETERMINISM (observed, not fixed here): the SQL's final clause is
    `ORDER BY cb.rrf_score DESC` with no tie-breaker, and both ROW_NUMBER()
    windows also lack tie-breakers. Equal scores therefore have an
    unspecified relative order. This transcription preserves Python's stable
    sort so tests can *detect* ties; it does not claim the SQL behaves that
    way.
    """
    k = candidate_k(match_count)
    vec = vector_candidates(rows, k)
    kw = lexical_candidates(rows, k)

    by_id = {r.id: r for r in rows}
    combined: list[tuple[int, float, float]] = []

    # Observed: FULL OUTER JOIN vec_cand / kw_cand -> union of both id sets,
    # merged on id, so a chunk found by both paths appears once.
    for cid in set(vec) | set(kw):
        vec_entry = vec.get(cid)
        vec_rank = vec_entry[0] if vec_entry else None
        # Observed: COALESCE(v.vec_sim, recomputed from chunks_v4) -- a
        # lexical-only hit still reports a similarity, recomputed via the join.
        similarity = vec_entry[1] if vec_entry else 1.0 - by_id[cid].distance
        combined.append((cid, similarity, rrf_score(vec_rank, kw.get(cid), rrf_k)))

    combined.sort(key=lambda t: -t[2])
    return combined[:match_count]
