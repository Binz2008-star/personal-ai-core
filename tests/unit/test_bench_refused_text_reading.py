"""The re-run of #189 at 1861756, with refused-reply text (#198), re-derived from its files.

Two runs on the rig, unit 1 then unit 1 + 2, at the commit where #193 began
recording what the loop refused. The handoff quotes the figures below; each is
re-derived here from the committed records.

Three kinds of figure, kept apart:

1. The instrument check (#196) and the reader (#195): rules fixed before the
   data, applied as written.
2. Hypotheses H1-H4, written at 16:47:50Z before any refused text was read
   (PROJECT_STATE, Next 6b2): decided by the reader's counts, including the
   ones that failed.
3. Readings made AFTER the text was read, named so: they are new rules, found
   in the data, and are leads for a measured experiment, not results.

Counts of what was recorded. None of this decides whether a control is an
improvement; ADR-023 amendment 1 (#191) is not yet approved.
"""
from __future__ import annotations

import json
import re
import tempfile
from collections import Counter
from functools import lru_cache
from pathlib import Path

from personal_ai_core.app.bench import refusals, replication
from personal_ai_core.app.bench.tasks import load

REPO = Path(__file__).resolve().parents[2]
RESULTS = REPO / "evals" / "results" / "bench"
EARLIER_UNIT1 = RESULTS / "bench-20261003T132852Z.jsonl"
EARLIER_UNIT1_AND_2 = RESULTS / "bench-20261003T135502Z.jsonl"
UNIT1 = RESULTS / "bench-20261003T161348Z.jsonl"
UNIT1_AND_2 = RESULTS / "bench-20261003T164631Z.jsonl"
MEASURED_AT = "18617569d3da6e73d2b943903ee367dee4a24bbd"
TASKS = {t.id: t for t in load(REPO / "evals" / "bench")}
ANSWERABLE = {"git-last-commit-file", "tests-count-failures", "toolsel-count-json"}


def _lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]


def _agent(path: Path) -> list[dict]:
    return [r for r in _lines(path) if r.get("kind") == "run" and r["track"] == "agent"]


def _protocol_errors(path: Path):
    """(run, refused reply, done_reason of its call) for every protocol error."""
    for run in _agent(path):
        calls = run.get("model_calls") or []
        for refused in run["refused_replies"]:
            if refused["kind"] == "protocol_error":
                index = refused["call"] - 1
                done = calls[index].get("done_reason") if 0 <= index < len(calls) else None
                yield run, refused, done


def test_both_runs_are_the_instrument_they_claim_to_be():
    for path, environment in ((UNIT1, False), (UNIT1_AND_2, True)):
        header = _lines(path)[0]
        assert header["commit"] == MEASURED_AT
        assert header["environment_context"] is environment
        assert "refused_replies" in header and "not scored" in header["refused_replies"]
        assert len(_agent(path)) == 120
        assert all("refused_replies" in r for r in _agent(path))


# --- 1. Rules fixed before the data ------------------------------------------


def test_the_rig_did_not_differ_between_189_and_its_re_run():
    """#196: no cell moved by 15 or more. Agent cells moved by at most 4."""
    pairs = ((EARLIER_UNIT1, UNIT1, {("agent", "en"): (15, 14), ("agent", "ar"): (11, 11),
                                      ("knowledge", "en"): (60, 60), ("knowledge", "ar"): (55, 50)}),
             (EARLIER_UNIT1_AND_2, UNIT1_AND_2, {("agent", "en"): (8, 12), ("agent", "ar"): (16, 15),
                                                  ("knowledge", "en"): (59, 60),
                                                  ("knowledge", "ar"): (53, 52)}))
    for earlier, later, expected in pairs:
        lines, flagged = replication.compare(_lines(earlier), _lines(later))
        assert not flagged, lines
        a, b = replication.cells(_lines(earlier)), replication.cells(_lines(later))
        assert {key: (a[key][0], b[key][0]) for key in expected} == expected


@lru_cache(maxsize=None)
def _reading(path: Path) -> refusals.Reading:
    """Judging a rejected answer rebuilds its task's fixture; once per file."""
    with tempfile.TemporaryDirectory() as tmp:
        return refusals.read(_lines(path), TASKS, Path(tmp))


def test_answers_rejected_and_the_few_the_checks_would_have_passed():
    """Unit 1: 64 rejected, 1 would have passed. Unit 1 + 2: 68 rejected, 3."""
    for path, rejected, false in ((UNIT1, {"en": 15, "ar": 49}, {"ar": 1}),
                                  (UNIT1_AND_2, {"en": 28, "ar": 40}, {"en": 2, "ar": 1})):
        reading = _reading(path)
        by_language = Counter()
        for (language, _), count in reading.rejected.items():
            by_language[language] += count
        assert dict(by_language) == rejected
        false_by_language = Counter()
        for (language, _), count in reading.false_rejections.items():
            false_by_language[language] += count
        assert dict(false_by_language) == false
        assert reading.withheld == 0


# --- 2. Hypotheses written before the text was read ---------------------------


def test_h1_failed_english_first_replies_were_not_mostly_prose_echoing_git_status():
    reading = _reading(UNIT1_AND_2)
    first_en = {c: n for (lang, c), n in reading.first_reply.items() if lang == "en"}
    assert first_en == {"malformed_json": 5, "prose": 4, "wrong_shape": 5, "truncated": 1}
    git_prose = [r for run, r, done in _protocol_errors(UNIT1_AND_2)
                 if run["language"] == "en" and r["call"] == 1 and run["task"].startswith("git-")
                 and refusals.classify_protocol_error(r["text"], r["error"], done) == "prose"]
    assert not any("git status" in r["text"] or "run_command" in r["text"] for r in git_prose)


def test_h2_failed_several_objects_and_wrong_shape_are_a_third():
    first_en = Counter({c: n for (lang, c), n in _reading(UNIT1_AND_2).first_reply.items()
                        if lang == "en"})
    assert first_en["several_objects"] + first_en["wrong_shape"] == 5
    assert sum(first_en.values()) == 15


def test_h3_failed_truncation_is_nine_of_98_and_eight_hit_the_generation_limit():
    errors = list(_protocol_errors(UNIT1)) + list(_protocol_errors(UNIT1_AND_2))
    assert len(errors) == 98
    truncated = [(r, done) for _, r, done in errors
                 if refusals.classify_protocol_error(r["text"], r["error"], done) == "truncated"]
    assert len(truncated) == 9
    # The ninth is the brace rule over-reaching: a short reply, stopped normally.
    assert sum(done == "length" for _, done in truncated) == 8


def test_h4_held_with_one_exception_four_false_rejections_three_runs_passed_anyway():
    found = []
    with tempfile.TemporaryDirectory() as tmp:
        for path in (UNIT1, UNIT1_AND_2):
            # Only these three can pass without a change to the workspace; the
            # reader judged every task and found none elsewhere (test above).
            for run in (r for r in _agent(path) if r["task"] in ANSWERABLE):
                for refused in run["refused_replies"]:
                    if refused["kind"] == "action_required" and refusals.would_have_passed(
                            TASKS[run["task"]], refusals._answer_text(refused["text"]), Path(tmp)):
                        found.append((run["task"], run["success"]))
    assert Counter(task for task, _ in found) == {"toolsel-count-json": 3,
                                                  "git-last-commit-file": 1}
    assert sum(success for _, success in found) == 3


# --- 3. Read after the text was seen: leads, not results ----------------------

TOOLS = "|".join(refusals.TOOL_NAMES)
FUNCTION_CALL = re.compile(rf"^\W*({TOOLS})\s*\(")


def test_after_reading_function_call_syntax_appears_only_with_unit_2_and_only_in_english():
    """`write_file(VERSION, "1.5.0")`: the 13-token replies of #189's git-commit-release."""
    for path, expected in ((UNIT1, {}), (UNIT1_AND_2, {"en": 9})):
        counts = Counter(run["language"] for run, r, _ in _protocol_errors(path)
                         if FUNCTION_CALL.match(r["text"].strip()))
        assert dict(counts) == expected


def test_after_reading_a_numeric_answer_is_refused_and_twice_it_was_right():
    """`{"answer": 4}` breaks the protocol ("answer" must be a string)."""
    values = [re.search(r'"answer"\s*:\s*(\d+)', r["text"]).group(1)
              for path in (UNIT1, UNIT1_AND_2) for run, r, _ in _protocol_errors(path)
              if '"answer" must be a string' in (r["error"] or "")]
    assert sorted(values) == ["10", "4", "4", "5", "8"]


def test_after_reading_file_content_that_breaks_json_is_the_largest_single_cause():
    for path, expected in ((UNIT1, 20), (UNIT1_AND_2, 17)):
        errors = list(_protocol_errors(path))
        assert len(errors) == 49
        assert sum("not valid JSON" in (r["error"] or "") and '"write_file"' in r["text"]
                   for _, r, _ in errors) == expected


def test_after_reading_string_arguments_for_a_command_are_refused():
    """`"arguments": "type words.py"` -- a command given as a string, not an object."""
    total = sum('"arguments" must be an object' in (r["error"] or "")
                for path in (UNIT1, UNIT1_AND_2) for _, r, _ in _protocol_errors(path))
    assert total == 8
