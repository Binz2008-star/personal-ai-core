"""ADR-020 amendment 1: the gate's thresholds meet the owner's targets."""
import json
from pathlib import Path

import pytest

from personal_ai_core.app import compare as cmp
from personal_ai_core.app.gate_calibration import (
    family_false_rejection,
    rise_probability,
    worst_case_false_rejection,
)

CASES = Path(__file__).resolve().parents[2] / "evals" / "cases"
ALPHA = 0.10
ALPHA_TOLERANCE = 0.005  # the owner's check: 15/+8 and 9/+6 give about 10.45%
POWER = 0.75


def _case_count(name):
    return len(json.loads((CASES / name).read_text(encoding="utf-8"))["cases"])


def _gating_groups():
    return [
        (cmp.CONTRACT_MIN_RUNS, cmp.CONTRACT_DELTA, _case_count("contract_v1.json")),
        (cmp.REFUSAL_MIN_RUNS, cmp.REFUSAL_SCRIPT_DELTA, _case_count("refusal_v2.json")),
    ]


def test_an_unchanged_model_is_rejected_about_alpha_of_the_time_at_worst():
    rate = family_false_rejection(_gating_groups())
    assert rate <= ALPHA + ALPHA_TOLERANCE, rate
    assert rate == pytest.approx(0.1045, abs=0.001)


def test_a_case_moving_from_10_to_70_percent_failure_is_caught():
    power = rise_probability(cmp.CONTRACT_MIN_RUNS, cmp.CONTRACT_DELTA, 0.1, 0.7)
    assert power >= POWER, power


def test_the_per_case_worst_cases_match_the_review():
    assert worst_case_false_rejection(15, 8) == pytest.approx(0.00261, abs=0.00002)
    assert worst_case_false_rejection(9, 6) == pytest.approx(0.00377, abs=0.00002)


def test_the_first_rule_would_have_failed_these_targets():
    """Unit 4's rule (+2 in 5, +2 in 3) rejects an unchanged model almost surely."""
    first = [(5, 2, _case_count("contract_v1.json")), (3, 2, _case_count("refusal_v2.json"))]
    assert family_false_rejection(first) > 0.9


def test_an_easier_threshold_would_break_alpha():
    groups = _gating_groups()
    n, delta, cases = groups[0]
    assert family_false_rejection([(n, delta - 1, cases), groups[1]]) > ALPHA + ALPHA_TOLERANCE
