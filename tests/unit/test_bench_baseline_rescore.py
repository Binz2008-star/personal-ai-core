"""The first capability baseline and its rescore, checked against each other.

The raw file (a609f69, #165) is immutable evidence. bench-checks-v1 scored 17
correct answers wrong because a number that ended a sentence was not read. The
derived file applies bench-checks-v2. This test re-derives it from the raw file
and pins exactly what changed, so the two can never drift apart silently.
"""
from __future__ import annotations

import hashlib
import io
import json
from collections import Counter
from pathlib import Path

from personal_ai_core.app.bench.runner import main

REPO = Path(__file__).resolve().parents[2]
RESULTS = REPO / "evals" / "results" / "bench"
RAW = RESULTS / "bench-20261002T081707Z.jsonl"
RESCORED = RESULTS / "bench-20261002T081707Z.rescored-bench-checks-v2.jsonl"
RAW_SHA256 = "46a82cf3a075fc3d28c76e907b36b0a4b57b833e2858809f5dda79d0d5a63e16"


def _recorded(run: dict) -> dict:
    """Everything a run recorded except its verdicts."""
    return {k: v for k, v in run.items() if k not in ("checks", "success", "signals")}


def _runs(path: Path) -> dict[tuple[str, str, int], dict]:
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    return {(r["task"], r["language"], r["run"]): r for r in lines if r.get("kind") == "run"}


def test_the_raw_baseline_is_the_file_the_rig_committed():
    data = RAW.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(data).hexdigest() == RAW_SHA256


def test_the_rescore_changes_exactly_the_17_misread_numbers():
    raw, rescored = _runs(RAW), _runs(RESCORED)
    assert len(raw) == len(rescored) == 240
    up = [k for k in raw if not raw[k]["success"] and rescored[k]["success"]]
    down = [k for k in raw if raw[k]["success"] and not rescored[k]["success"]]
    assert down == []
    assert Counter((t, lang) for t, lang, _ in up) == {
        ("kb-api-port", "en"): 5, ("kb-api-port", "ar"): 5,
        ("kb-crossdoc-failed-port", "en"): 5, ("kb-crossdoc-failed-port", "ar"): 2}
    for key in up:
        changed = [(a, b) for a, b in zip(raw[key]["checks"], rescored[key]["checks"])
                   if a["verdict"] != b["verdict"]]
        assert [(a["check"], a["verdict"], b["verdict"]) for a, b in changed] == [
            ("answer_number", "FAIL", "PASS")]
    # Nothing but the verdicts moved: answers, steps and model calls are the run's own.
    for key in raw:
        assert _recorded(raw[key]) == _recorded(rescored[key])
    assert sum(r["success"] for r in raw.values()) == 106
    assert sum(r["success"] for r in rescored.values()) == 123


def test_the_committed_rescore_is_what_rescore_produces_today(tmp_path):
    source = tmp_path / RAW.name
    source.write_bytes(RAW.read_bytes())
    assert main(["--tasks", str(REPO / "evals" / "bench"), "--rescore", str(source)],
                stdout=io.StringIO()) == 0
    produced = _runs(tmp_path / RESCORED.name)
    committed = _runs(RESCORED)
    assert {k: (r["success"], r["checks"]) for k, r in produced.items()} == {
        k: (r["success"], r["checks"]) for k, r in committed.items()}
