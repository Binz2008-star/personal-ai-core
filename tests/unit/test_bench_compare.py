"""The comparison of two benchmark result files (ADR-023 §5).

It describes what moved and decides nothing, so the tests pin two things: that its
classes are the ADR's (reproduced from the first baseline, exactly), and that each
thing it prints is read from the records and not invented.
"""
from __future__ import annotations

import io
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from personal_ai_core.app.bench.compare import Side, classify, compare, main
from personal_ai_core.app.bench.runner import _read, _task_digest
from personal_ai_core.app.bench.tasks import load

REPO = Path(__file__).resolve().parents[2]
BENCH = REPO / "evals" / "bench"
BASELINE = REPO / "evals" / "results" / "bench" / (
    "bench-20261002T081707Z.rescored-bench-checks-v2.jsonl")


def _header(**overrides: Any) -> dict[str, Any]:
    header = {"kind": "header", "model": "m", "scorer": "s", "runs": 1, "languages": ["en"],
              "num_ctx_measured_by_owner": 8192, "commit": "abc1234", "tasks": {}}
    header.update(overrides)
    return header


def _end(weights: Any = "sha256:w") -> dict[str, Any]:
    return {"kind": "end", "weights": weights}


def _run(task: str = "t", language: str = "en", run: int = 1, *, success: bool = False,
         stop: str = "answered", steps: tuple[tuple[bool, str], ...] = (),
         rejections: int | None = None, track: str = "agent") -> dict[str, Any]:
    record: dict[str, Any] = {
        "kind": "run", "task": task, "track": track, "language": language, "run": run,
        "success": success, "stop": stop,
        "steps": [{"executed": e, "decision": d} for e, d in steps]}
    if rejections is not None:
        record["action_rejections"] = rejections
    return record


def _side(*lines: dict[str, Any], name: str = "f.jsonl") -> Side:
    return Side(name, list(lines))


def _text(base: Side, cand: Side, tasks: Any = None) -> str:
    return compare(base, cand, tasks)


# --- the classes are the ADR's --------------------------------------------------


def test_the_classes_reproduce_adr_023_section_1_2_on_the_first_baseline():
    """52, 43, 14 and 2 of the 111 failed agent attempts, with the en/ar split."""
    side = Side(BASELINE, _read(BASELINE))
    failed = [r for r in side.agent if not r["success"]]
    counts = Counter((classify(r), r["language"]) for r in failed)
    assert len(side.agent) == 120 and len(failed) == 111
    assert {key: (counts[(key, "en")], counts[(key, "ar")]) for key in
            ("answered_without_executing", "executed_unverified", "refused_commands",
             "protocol_only")} == {
        "answered_without_executing": (15, 37), "executed_unverified": (27, 16),
        "refused_commands": (8, 6), "protocol_only": (1, 1)}
    assert sum(counts.values()) == 111, "no attempt falls in a class the ADR does not have"


@pytest.mark.parametrize("run, expected", [
    (_run(success=True), "success"),
    (_run(stop="error"), "provider_error"),
    (_run(), "answered_without_executing"),
    # Class 1 is decided before Class 3: it answered, and nothing ran.
    (_run(steps=((False, "deny"),)), "answered_without_executing"),
    (_run(stop="budget", steps=((True, "allow"),)), "executed_unverified"),
    (_run(steps=((False, "deny"), (True, "allow"))), "executed_unverified"),
    (_run(stop="budget", steps=((False, "deny"),)), "refused_commands"),
    (_run(stop="budget", steps=((False, "ask"),)), "refused_commands"),
    (_run(stop="budget", rejections=3), "rejected_to_budget"),
    (_run(stop="budget"), "protocol_only"),
    (_run(stop="budget", rejections=0), "protocol_only"),
])
def test_one_attempt_is_placed_in_one_class_from_its_record(run, expected):
    assert classify(run) == expected


# --- what it prints is read from the records -------------------------------------


def test_a_file_compared_with_itself_moves_nothing():
    side = Side(BASELINE, _read(BASELINE))
    tasks = {t.id: t for t in load(BENCH)}
    text = _text(side, side, tasks)
    assert "120 paired, 0 changed class" in text
    assert "0 fell, 0 rose" in text
    assert "WARNING" not in text
    assert "24 identical" in text
    assert "52 (en=15 ar=37)" in text and "43 (en=27 ar=16)" in text


def test_a_changed_class_is_named_with_its_count():
    base = _side(_header(), _run(run=1), _run(run=2), _run(run=3), _end())
    cand = _side(_header(contract="x"),
                 _run(run=1, stop="budget", rejections=3),
                 _run(run=2, steps=((True, "allow"),)),
                 _run(run=3), _end())
    text = _text(base, cand)
    assert "3 paired, 2 changed class" in text
    # The labels carry the ADR's class numbers, so the count reads "1  1. answers ...".
    assert "1  1. answers without executing -> rejected until the budget ended, never acted" in text
    assert "1  1. answers without executing -> 2. executes without adequate verification" in text


def test_rejections_are_shown_only_for_a_side_that_had_a_contract():
    base = _side(_header(), _run(), _end())
    cand = _side(_header(contract="AgentTaskContract"),
                 _run(stop="budget", rejections=3), _run(run=2, rejections=0), _end())
    text = _text(base, cand)
    assert "no contract" in text
    assert "1 att, 3 rej" in text


def test_a_task_whose_success_fell_is_listed_across_both_tracks():
    base = _side(_header(), _run("kb-x", track="knowledge", success=True),
                 _run("a", success=True), _end())
    cand = _side(_header(), _run("kb-x", track="knowledge", success=False),
                 _run("a", success=True), _end())
    text = _text(base, cand)
    assert "1 fell, 0 rose" in text
    assert "fell  kb-x" in text and "1/1 -> 0/1" in text


def test_what_would_make_the_comparison_meaningless_is_a_warning():
    base = _side(_header(tasks={"a": {"digest": "1"}}), _run(), _end("sha256:one"))
    cand = _side(_header(model="other", scorer="newer", num_ctx_measured_by_owner=4096,
                         tasks={"b": {"digest": "1"}}), _run(), _end("sha256:two"))
    text = _text(base, cand)
    for expected in ("model differs", "scorer differs", "num_ctx differs",
                     "weights differ or are not recorded", "task sets differ"):
        assert f"WARNING: {expected}" in text, expected


def test_unrecorded_weights_are_a_warning_not_a_match():
    base = _side(_header(), _run())          # no end record at all
    cand = _side(_header(), _run())
    assert "WARNING: weights differ or are not recorded" in _text(base, cand)


def test_a_digest_that_differs_only_by_the_contract_is_explained_and_another_is_not():
    task = next(t for t in load(BENCH) if t.id == "verify-off-by-one")
    other = next(t for t in load(BENCH) if t.id == "verify-sales-total")
    tasks = {t.id: t for t in load(BENCH)}
    before = {"verify-off-by-one": {"digest": _task_digest(task, with_contract=False)},
              "verify-sales-total": {"digest": "0123456789abcdef"}}
    after = {"verify-off-by-one": {"digest": _task_digest(task)},
             "verify-sales-total": {"digest": _task_digest(other)}}
    text = _text(_side(_header(tasks=before), _run(), _end()),
                 _side(_header(tasks=after), _run(), _end()), tasks)
    assert "1 differ only by the contract" in text
    assert "1 differ and the contract does not explain it" in text
    assert "not explained: verify-sales-total" in text
    assert "WARNING: some tasks are not the same tasks" in text


def test_without_the_task_files_a_changed_digest_is_not_verified():
    task = next(t for t in load(BENCH) if t.id == "verify-off-by-one")
    base = _side(_header(tasks={"verify-off-by-one": {
        "digest": _task_digest(task, with_contract=False)}}), _run(), _end())
    cand = _side(_header(tasks={"verify-off-by-one": {"digest": _task_digest(task)}}),
                 _run(), _end())
    text = _text(base, cand, tasks=None)
    assert "0 differ only by the contract" in text and "not verified" in text


def test_it_decides_nothing():
    text = _text(*(_side(_header(), _run(), _end()),) * 2)
    assert "does not say whether the change is an improvement" in text
    assert "no target score" in text


# --- the command ----------------------------------------------------------------


def test_the_command_prints_a_comparison_and_exits_zero():
    out = io.StringIO()
    assert main([str(BASELINE), str(BASELINE), "--tasks", str(BENCH)], stdout=out) == 0
    assert "Failed agent attempts by class" in out.getvalue()


def test_the_command_refuses_a_missing_file_and_a_file_without_a_header(tmp_path):
    out = io.StringIO()
    assert main([str(tmp_path / "nope.jsonl"), str(BASELINE)], stdout=out) == 2
    assert "refusing to compare" in out.getvalue()
    headless = tmp_path / "h.jsonl"
    headless.write_text(json.dumps(_run()) + "\n", encoding="utf-8")
    out = io.StringIO()
    assert main([str(headless), str(BASELINE)], stdout=out) == 2
    assert "has no header" in out.getvalue()


def test_the_command_refuses_a_file_with_no_runs(tmp_path):
    empty = tmp_path / "e.jsonl"
    empty.write_text(json.dumps(_header()) + "\n", encoding="utf-8")
    out = io.StringIO()
    assert main([str(empty), str(BASELINE)], stdout=out) == 2
    assert "has no runs" in out.getvalue()


def test_the_command_still_compares_when_the_task_files_are_missing(tmp_path):
    out = io.StringIO()
    assert main([str(BASELINE), str(BASELINE), "--tasks", str(tmp_path / "none")], stdout=out) == 0
