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


# --- The read-only look at English under unit 2 (handoff, Next 6b) ----------
# The records keep token counts for each model call, not the reply text, so
# these say where and how often English failed, not what the model wrote.


def _per_language(path: Path, field: str) -> dict[str, tuple[int, int]]:
    """(total, attempts with at least one) of a per-attempt count, by language."""
    out = {}
    for lang in ("en", "ar"):
        runs = [r for r in _agent(path).values() if r["language"] == lang]
        out[lang] = (sum(r.get(field) or 0 for r in runs), sum(1 for r in runs if r.get(field)))
    return out


def test_english_protocol_errors_rose_with_each_unit():
    """English 14 -> 27 -> 46 protocol errors (in 10 -> 17 -> 31 attempts);
    Arabic 9 -> 15 -> 20. A protocol error is a reply that is not one JSON object."""
    assert _per_language(BASELINE, "protocol_errors") == {"en": (14, 10), "ar": (9, 7)}
    assert _per_language(UNIT1, "protocol_errors") == {"en": (27, 17), "ar": (15, 12)}
    assert _per_language(UNIT1_AND_2, "protocol_errors") == {"en": (46, 31), "ar": (20, 14)}


def _spent(path: Path, lang: str) -> dict[str, int]:
    """What the attempts the budget stopped spent their failures on."""
    spent = Counter()
    for r in _agent(path).values():
        if r["stop"] != "budget" or r["language"] != lang:
            continue
        spent["rejections"] += r.get("action_rejections") or 0
        spent["protocol"] += r.get("protocol_errors") or 0
        spent["refused_tool"] += sum(1 for s in r["steps"] if not s.get("executed"))
        spent["failed_step"] += sum(
            1 for s in r["steps"] if s.get("executed") and not s.get("verified", s.get("ok"))
        )
    return dict(spent)


def test_in_english_unit_2_traded_refused_tools_for_rejections_and_protocol_errors():
    """English budget stops: refused tool calls 39 -> 19, but rejections 5 -> 21
    and protocol errors 20 -> 34. Arabic's rejections did not rise (22 -> 22)."""
    assert _spent(UNIT1, "en") == {"rejections": 5, "protocol": 20, "refused_tool": 39, "failed_step": 8}
    assert _spent(UNIT1_AND_2, "en") == {"rejections": 21, "protocol": 34, "refused_tool": 19, "failed_step": 16}
    assert _spent(UNIT1, "ar")["rejections"] == _spent(UNIT1_AND_2, "ar")["rejections"] == 22


def test_git_commit_release_in_english_mostly_never_acted_with_unit_2():
    """Unit 1: 2 of 5 passed, every attempt wrote VERSION. Unit 1 + 2: 4 of 5
    ran no tool at all; each spent its budget on 1 rejection and 2 protocol errors."""
    one = [_agent(UNIT1)[("git-commit-release", "en", i)] for i in range(1, 6)]
    two = [_agent(UNIT1_AND_2)[("git-commit-release", "en", i)] for i in range(1, 6)]
    assert sum(r["success"] for r in one) == 2 and all(r["steps"] for r in one)
    never = [r for r in two if not r["steps"]]
    assert len(never) == 4
    assert all((r["action_rejections"], r["protocol_errors"]) == (1, 2) for r in never)
