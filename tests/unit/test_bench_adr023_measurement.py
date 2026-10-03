"""The ADR-023 measurement of 2026-10-03, re-derived from the committed files.

Two runs on the rig at 4f63f73 (#189), against the rescored baseline (#166):
unit 1 alone, then unit 1 with the environment context (unit 2). The handoff
quotes the figures below; this file re-derives each one from the records, so
the document cannot drift from the evidence.

These are counts of what was recorded. Whether a change is an improvement is
not decided here: ADR-023 section 5 requires the deciding rule to be written
before a comparison is read, and for this measurement it was not.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from personal_ai_core.app.bench.compare import classify

REPO = Path(__file__).resolve().parents[2]
RESULTS = REPO / "evals" / "results" / "bench"
BASELINE = RESULTS / "bench-20261002T081707Z.rescored-bench-checks-v2.jsonl"
UNIT1 = RESULTS / "bench-20261003T132852Z.jsonl"
UNIT1_AND_2 = RESULTS / "bench-20261003T135502Z.jsonl"
MEASURED_AT = "4f63f73bd28a5ed1bfd73909d826e5de3d63075d"


def _lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]


def _agent(path: Path) -> dict[tuple[str, str, int], dict]:
    return {
        (r["task"], r["language"], r["run"]): r
        for r in _lines(path)
        if r.get("kind") == "run" and r["track"] == "agent"
    }


def _classes(path: Path) -> Counter:
    return Counter((classify(r), r["language"]) for r in _agent(path).values())


def test_both_runs_are_the_instrument_they_claim_to_be():
    for path, environment in ((UNIT1, False), (UNIT1_AND_2, True)):
        header = _lines(path)[0]
        assert header["kind"] == "header"
        assert header["commit"] == MEASURED_AT
        assert header["scorer"] == "bench-checks-v2"
        assert header["runs"] == 5 and header["languages"] == ["en", "ar"]
        assert "contract" in header
        assert header["environment_context"] is environment
        assert len(_agent(path)) == 120


def test_unit_1_removed_the_class_it_was_built_for():
    """Class 1, 'answers without executing': 52 in the baseline, 0 after."""
    before, after = _classes(BASELINE), _classes(UNIT1)
    assert sum(n for (c, _), n in before.items() if c == "answered_without_executing") == 52
    assert sum(n for (c, _), n in after.items() if c == "answered_without_executing") == 0


def test_unit_1_agent_success_by_language():
    for path, en, ar in ((BASELINE, 9, 0), (UNIT1, 15, 11), (UNIT1_AND_2, 8, 16)):
        runs = _agent(path).values()
        assert sum(r["success"] for r in runs if r["language"] == "en") == en, path.name
        assert sum(r["success"] for r in runs if r["language"] == "ar") == ar, path.name


def test_where_the_52_went():
    """18 to success, 23 to class 2, 7 to class 3, 4 rejected until the budget ended."""
    before, after = _agent(BASELINE), _agent(UNIT1)
    moved = Counter(
        classify(after[key])
        for key, run in before.items()
        if classify(run) == "answered_without_executing"
    )
    assert moved == {
        "success": 18,
        "executed_unverified": 23,
        "refused_commands": 7,
        "rejected_to_budget": 4,
    }


def test_the_cost_of_unit_1_is_budget():
    """Budget stops 33 -> 57; 54 attempts had an answer rejected, 58 rejections."""
    assert sum(r["stop"] == "budget" for r in _agent(BASELINE).values()) == 33
    unit1 = _agent(UNIT1).values()
    assert sum(r["stop"] == "budget" for r in unit1) == 57
    assert sum(1 for r in unit1 if r["action_rejections"]) == 54
    assert sum(r["action_rejections"] for r in unit1) == 58


def test_unit_2_moved_class_3_and_not_the_total():
    """Class 3 23 -> 11; agent success 26 -> 24; English 15 -> 8, Arabic 11 -> 16."""
    one, two = _classes(UNIT1), _classes(UNIT1_AND_2)
    assert sum(n for (c, _), n in one.items() if c == "refused_commands") == 23
    assert sum(n for (c, _), n in two.items() if c == "refused_commands") == 11
    assert sum(r["success"] for r in _agent(UNIT1).values()) == 26
    assert sum(r["success"] for r in _agent(UNIT1_AND_2).values()) == 24


def test_with_unit_2_english_answers_were_rejected_until_the_budget_ended():
    """'Rejected until the budget ended, never acted': English 2 -> 10.
    Attempts with a rejected answer 54 -> 74, rejections 58 -> 82."""
    assert _classes(UNIT1)[("rejected_to_budget", "en")] == 2
    assert _classes(UNIT1_AND_2)[("rejected_to_budget", "en")] == 10
    both = _agent(UNIT1_AND_2).values()
    assert sum(1 for r in both if r["action_rejections"]) == 74
    assert sum(r["action_rejections"] for r in both) == 82
