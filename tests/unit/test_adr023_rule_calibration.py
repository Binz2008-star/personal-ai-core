"""ADR-023 amendment 1 (PROPOSED): the arithmetic its table quotes.

The amendment proposes the rule that decides "better" for the next ADR-023
comparison. Its thresholds are derived with ADR-020 amendment 1's targets and
`app/gate_calibration.py`, not taken from any run. This file re-derives every
number the table states, so the document cannot drift from the arithmetic.
It encodes no policy: which row applies is the owner's decision (D1).
"""
from __future__ import annotations

from personal_ai_core.app.gate_calibration import family_false_rejection, rise_probability

CELLS = 48  # 24 tasks x 2 languages
GROUPS = 4  # 2 tracks x 2 languages
TASKS_PER_TRACK = 12


def _threshold(runs: int, units: int) -> int:
    """The smallest failure rise that keeps the family-wise rate at or below 10%."""
    return next(d for d in range(1, runs + 1) if family_false_rejection([(runs, d, units)]) <= 0.10)


def test_at_five_runs_a_per_cell_gate_is_close_to_blind():
    assert _threshold(5, CELLS) == 5
    assert round(family_false_rejection([(5, 5, CELLS)]), 4) == 0.0458
    assert round(rise_probability(5, 5, 0.10, 0.70), 3) == 0.099


def test_seventeen_runs_meet_adr_020s_targets_per_cell():
    assert _threshold(17, CELLS) == 9
    assert round(family_false_rejection([(17, 9, CELLS)]), 4) == 0.0681
    assert round(rise_probability(17, 9, 0.10, 0.70), 3) == 0.779
    # And it is the fewest runs that do.
    for runs in range(5, 17):
        d = _threshold(runs, CELLS)
        assert rise_probability(runs, d, 0.10, 0.70) < 0.75


def test_pooled_by_track_and_language():
    five, ten = 5 * TASKS_PER_TRACK, 10 * TASKS_PER_TRACK
    assert (five, ten) == (60, 120)
    assert _threshold(five, GROUPS) == 12
    assert round(family_false_rejection([(five, 12, GROUPS)]), 4) == 0.0688
    assert round(rise_probability(five, 12, 0.50, 0.70), 3) == 0.540
    assert _threshold(ten, GROUPS) == 16
    assert round(family_false_rejection([(ten, 16, GROUPS)]), 4) == 0.0873
    assert round(rise_probability(ten, 16, 0.50, 0.70), 3) == 0.873
