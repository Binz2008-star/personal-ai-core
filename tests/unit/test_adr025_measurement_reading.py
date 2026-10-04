"""The ADR-025 measurement (#208), re-derived from its files.

Two arms on the rig at 2d9b569, 10 runs each, the same model and the same
loaded context; the only difference is `--native-tools`. The handoff and
ADR-025 §12 quote the figures below; each is re-derived here from the
committed records.

Three kinds of figure, kept apart:

1. The instrument: what each file says it measured.
2. The verdict, by the rule fixed before the runs (ADR-025 §6.1, ADR-023
   amendment 2): PASS. The owner accepted it on 2026-10-04 as "measured, PASS
   on the pre-declared target, NOT adopted"; native tool calls stay opt-in.
3. Observations made AFTER the verdict was read, named so. In the owner's
   words, they are "observed evidence / likely mechanism, not yet a proven
   causal fix": a hypothesis that only a fix and a new measurement can test.
"""
from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from pathlib import Path

from personal_ai_core.app.bench import refusals
from personal_ai_core.app.bench.compare import Side, compare
from personal_ai_core.app.bench.verdict import decide

REPO = Path(__file__).resolve().parents[2]
RESULTS = REPO / "evals" / "results" / "bench"
TEXT = RESULTS / "bench-20261004T004107Z.jsonl"
NATIVE = RESULTS / "bench-20261004T013805Z.jsonl"
MEASURED_AT = "2d9b56994cd1455acc8507cd744cf9c9c0ad9046"
EMPTY = "the reply is empty"


def _lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]


def _runs(path: Path) -> list[dict]:
    return [r for r in _lines(path) if r.get("kind") == "run"]


def _agent(path: Path) -> list[dict]:
    return [r for r in _runs(path) if r["track"] == "agent"]


def _key(run: dict) -> tuple[str, str, int]:
    return run["task"], run["language"], run["run"]


# --- 1. The instrument ----------------------------------------------------------


BOSS = "huihui_ai/qwen2.5-abliterate:7b"
WEIGHTS = "sha256:212345411ab817a8d7669ed63f24a30f2e2a2cd297455a6aad1de50c82f7691c"
MANIFEST = "sha256:103482475c9b4d4032999dd5d7383478cad8b966ae6ae984d525daf7352c2219"


def _header_and_end(path: Path) -> tuple[dict, dict]:
    lines = _lines(path)
    return lines[0], next(r for r in lines if r.get("kind") == "end")


def test_both_arms_are_the_instrument_they_claim_to_be():
    for path, native in ((TEXT, False), (NATIVE, True)):
        header, end = _header_and_end(path)
        assert header["commit"] == MEASURED_AT
        assert header["model"] == BOSS and header["role"] == "boss"
        assert header["native_tools"] is native
        assert header["environment_context"] is False
        assert header["lenient_protocol"] is False
        assert header["num_ctx_measured_by_owner"] == 8192
        assert header["runs"] == 10
        assert end["context_mismatch"] is False
        assert len(_runs(path)) == 480 and len(_agent(path)) == 240
        assert not any(r["stop"] == "error" for r in _runs(path))
        # What the runner read from Ollama after the arm: the Boss's weights by
        # digest, verified against the blob, no adapter, at 8192.
        for weights in (end["weights"], end["ollama_loaded"]["weights"]):
            assert weights["digest"] == WEIGHTS and weights["manifest_digest"] == MANIFEST
            assert weights["verified"] is True and weights["adapters"] == []
        assert end["ollama_loaded"]["probed"] is True
        assert end["ollama_loaded"]["context_length"] == 8192


def test_the_arms_differ_only_by_the_native_flag():
    """Every header field but `native_tools` and the start time is equal, and so is
    the end record: the same model, weights, machine, policy, sampling and tasks."""
    (text_header, text_end), (native_header, native_end) = map(_header_and_end, (TEXT, NATIVE))
    differ = {key for key in text_header.keys() | native_header.keys()
              if text_header.get(key) != native_header.get(key)}
    assert differ == {"native_tools", "started_at"}
    assert text_end == native_end
    assert len(text_header["tasks"]) == 24


# --- 2. The verdict, by the rule fixed before the runs ----------------------------


def test_the_verdict_is_pass_with_the_figures_recorded():
    """Every task digest is identical across the arms, so the task files change
    nothing in R0 to R2; without them only the false-rejection cost is left
    uncounted (rebuilding every fixture to count it takes over a minute). #208
    records that line verbatim: 2 of 113 -> 0 of 130."""
    verdict = decide(Side(TEXT, _lines(TEXT)), Side(NATIVE, _lines(NATIVE)), "native-tools", None)
    assert verdict.outcome == "PASS"
    assert not verdict.unreadable and not verdict.dropped
    assert (verdict.leaving, verdict.entering) == (53, 30)
    assert round(verdict.p_value, 4) == 0.0076
    assert verdict.failures == {("agent", "en"): (96, 92), ("agent", "ar"): (98, 94),
                                ("knowledge", "en"): (0, 1), ("knowledge", "ar"): (16, 14)}
    assert not verdict.regressions
    costs = {label: (a, b) for label, a, b in verdict.costs}
    assert costs["agent attempts stopped on the budget"] == ("123", "74")
    assert costs["answers rejected for not having acted"] == (
        "113 (0.47 per attempt)", "130 (0.54 per attempt)")
    assert costs["protocol errors"] == ("98 (0.41 per attempt)", "96 (0.40 per attempt)")


def test_agent_success_moved_by_eight_attempts_which_no_rule_tests():
    """R2 only guards against regression; a rise in success is not a gated result."""
    for path, expected in ((TEXT, {"en": 24, "ar": 22}), (NATIVE, {"en": 28, "ar": 26})):
        assert dict(Counter(r["language"] for r in _agent(path) if r["success"])) == expected
    printed = compare(Side(TEXT, _lines(TEXT)), Side(NATIVE, _lines(NATIVE)), None)
    assert "both tracks: 5 fell, 9 rose" in printed


# --- 3. Read after the verdict: observed evidence, not a causal result ------------


def _empty_replies(path: Path):
    """(run, its model call) for every reply refused as empty."""
    for run in _agent(path):
        for refused in run["refused_replies"]:
            if refused["kind"] == "protocol_error" and refused["error"] == EMPTY:
                yield run, run["model_calls"][refused["call"] - 1]


def test_after_reading_every_empty_native_reply_had_text_generated_and_none_returned():
    """The model generated 14 to 1024 tokens; the backend returned neither text nor a call."""
    assert not list(_empty_replies(TEXT))
    pairs = list(_empty_replies(NATIVE))
    assert len(pairs) == 82
    tokens = [call["completion_tokens"] for _, call in pairs]
    assert (min(tokens), statistics.median(tokens), max(tokens)) == (14, 37, 1024)
    assert all(call["tool_calls_returned"] == 0 for _, call in pairs)
    assert Counter(call["done_reason"] for _, call in pairs) == {"stop": 80, "length": 2}
    attempts = {_key(run): run["success"] for run, _ in pairs}
    assert len(attempts) == 71
    assert sum(not success for success in attempts.values()) == 59


def test_after_reading_the_native_arms_protocol_errors_are_mostly_empty_replies():
    errors = Counter(refused["error"] for run in _agent(NATIVE)
                     for refused in run["refused_replies"] if refused["kind"] == "protocol_error")
    assert errors == {EMPTY: 82,
                      "a tool call was written as text; use the tool-call interface": 8,
                      "2 tool calls in one reply; call one tool per reply": 6}


TOOLS = "|".join(map(re.escape, refusals.TOOL_NAMES))
NAME_THEN_OBJECT = re.compile(rf"^\s*({TOOLS})\s*:?\s*\{{")


def _text_calls(path: Path):
    """Answers refused for not acting that are a tool name, then a JSON object:
    `shell {"command": ...}`. The loop's detector reads a call written as text
    only when the object itself carries "name" or "tool" (ADR-025 §5)."""
    for run in _agent(path):
        for refused in run["refused_replies"]:
            if refused["kind"] == "action_required" and NAME_THEN_OBJECT.match(refused["text"]):
                yield run, refused


def test_after_reading_calls_written_as_name_then_object_were_refused_as_not_acting():
    assert not list(_text_calls(TEXT))
    found = list(_text_calls(NATIVE))
    assert len(found) == 25
    attempts = {_key(run): run["success"] for run, _ in found}
    assert len(attempts) == 9 and not any(attempts.values())
    assert Counter((task, language) for task, language, _ in attempts) == {
        ("git-commit-release", "en"): 8, ("multistep-change-port", "ar"): 1}


def test_after_reading_git_commit_release_fell_where_those_calls_were_refused():
    """5/10 -> 0/10. In 8 native runs no step ran and every refusal was such a call.
    This coincides with the fall; it does not prove the cause."""
    def runs(path: Path) -> list[dict]:
        return [r for r in _agent(path) if (r["task"], r["language"]) == ("git-commit-release", "en")]

    assert sum(r["success"] for r in runs(TEXT)) == 5
    native = runs(NATIVE)
    assert sum(r["success"] for r in native) == 0
    only_text_calls = [r for r in native if not r["steps"] and r["refused_replies"] and all(
        x["kind"] == "action_required" and NAME_THEN_OBJECT.match(x["text"])
        for x in r["refused_replies"])]
    assert len(only_text_calls) == 8
    assert all(r["stop"] == "budget" and len(r["refused_replies"]) == 3 for r in only_text_calls)
