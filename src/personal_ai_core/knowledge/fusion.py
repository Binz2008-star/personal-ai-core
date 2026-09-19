"""Reciprocal Rank Fusion.

Implements `core.contracts.RankFusion`.

The audited hybrid search was classified `implemented, unproven`: it had zero
dedicated tests. Rather than port it on trust or rewrite it on instinct, its
behaviour was transcribed and pinned first in
`tests/characterization/` (`docs/RRF_CHARACTERIZATION.md`), and this
implementation was then written against that evidence with each inheritance
decision made explicitly.

**Taken from the legacy behaviour, deliberately:**

  - `k = 60`. It is the constant from the original RRF paper and the value the
    audited system ran in production. Changing it would change every ranking
    for no reason anyone could point at.
  - Rank-based fusion. Scores from a vector arm and a lexical arm share no
    scale, so combining scores is meaningless; combining ranks is the reason
    RRF exists.
  - A candidate missing from a list contributes zero, not a penalty and not an
    imputed rank. Absence is absence.

**Not inherited, and why:**

  - *Undefined tie ordering.* The legacy query left equal fused scores to the
    database's row order, which is not a guarantee. Here ties break on
    ascending `chunk_id` via `FusedCandidate.sort_key`. Arbitrary, but fixed --
    and a ranking whose order can change between two identical runs cannot be
    tested, which is how it stayed unproven for as long as it did.
  - *Candidate depth.* The legacy `max(match_count * 8, 50)` formula coupled
    how deep each arm searched to how many results the caller wanted. That is
    a retrieval-quality decision driven by a presentation parameter. Depth is
    set explicitly by the retriever instead; see `retrieval.py`.
"""
from __future__ import annotations

from typing import Sequence

from ..core.knowledge import Candidate, CandidateList, FusedCandidate

RRF_K = 60


class ReciprocalRankFusion:
    """Fuses ranked candidate lists into one deterministic ranking.

    Score for a chunk is the sum over the lists that contain it of
    `1 / (k + rank)`, with 1-based ranks.
    """

    def __init__(self, *, k: int = RRF_K) -> None:
        if k < 1:
            raise ValueError("k must be at least 1")
        self._k = k

    @property
    def k(self) -> int:
        return self._k

    def fuse(self, lists: Sequence[CandidateList], *, limit: int) -> list[str]:
        """Return chunk ids, best first."""
        return [
            fused.chunk_id for fused in self.fuse_detailed(lists, limit=limit)
        ]

    def fuse_detailed(
        self, lists: Sequence[CandidateList], *, limit: int
    ) -> list[FusedCandidate]:
        """Fuse and keep what each arm contributed.

        Beyond the `RankFusion` protocol on purpose. The protocol returns ids
        because that is all a caller needs to rank; the retriever needs the
        contributions to build provenance, and recomputing them from the input
        lists would be a second implementation of the same logic waiting to
        disagree with this one.
        """
        if limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}")

        scores: dict[str, float] = {}
        contributions: dict[str, list[Candidate]] = {}

        for candidate_list in lists:
            for candidate in candidate_list.candidates:
                scores[candidate.chunk_id] = scores.get(candidate.chunk_id, 0.0) + (
                    1.0 / (self._k + candidate.rank)
                )
                contributions.setdefault(candidate.chunk_id, []).append(candidate)

        fused = [
            FusedCandidate(
                chunk_id=chunk_id,
                fused_score=score,
                contributions=tuple(contributions[chunk_id]),
            )
            for chunk_id, score in scores.items()
        ]
        fused.sort(key=lambda candidate: candidate.sort_key)
        return fused[:limit]
