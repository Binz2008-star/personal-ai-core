"""The arithmetic behind ADR-020 amendment 1's thresholds. Pure; reads no result.

The owner set two targets (2026-10-01): an unchanged model is rejected at most
about 10% of the time across every gating case (alpha, family-wise), and a
case whose failure rate moves from 10% to 70% is caught at least 75% of the
time. The thresholds in `compare` are derived from these, not from any run:

- per case, failures on each side are binomial over the runs;
- the false-rejection bound takes the worst case, p = 0.5 for every case,
  and treats the cases as independent. That is a calibration assumption, not
  a guarantee: correlated cases can move the family-wise rate.

`tests/unit/test_gate_calibration.py` holds the thresholds to the targets, so
a later change to them has to meet the targets or change the ADR.
"""
from __future__ import annotations

from math import comb


def _binomial(n: int, p: float) -> list[float]:
    return [comb(n, k) * p**k * (1 - p) ** (n - k) for k in range(n + 1)]


def rise_probability(n: int, delta: int, p_baseline: float, p_candidate: float) -> float:
    """P(candidate failures - baseline failures >= delta), n runs per side."""
    b, c = _binomial(n, p_baseline), _binomial(n, p_candidate)
    return sum(b[x] * c[y] for x in range(n + 1) for y in range(n + 1) if y - x >= delta)


def worst_case_false_rejection(n: int, delta: int) -> float:
    """The largest per-case false-regression probability over every base rate."""
    return max(rise_probability(n, delta, p / 100, p / 100) for p in range(101))


def family_false_rejection(groups: list[tuple[int, int, int]]) -> float:
    """At least one false regression across groups of (runs, delta, cases)."""
    keep = 1.0
    for n, delta, cases in groups:
        keep *= (1 - worst_case_false_rejection(n, delta)) ** cases
    return 1 - keep
