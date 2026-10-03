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


def test_english_budget_stops_with_unit_2_held_fewer_refused_tools_more_rejections():
    """English budget stops, unit 1 -> unit 1 + 2: refused tool calls 39 -> 19,
    rejections 5 -> 21, protocol errors 20 -> 34. Arabic's rejections 22 -> 22.
    A shift in where the failures fell, between two runs; not a cause."""
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


# --- Which call failed, and how (the forensic read of git-commit-release) ----
# The reply text is not recorded, but each model call's prompt-token count is.
# The prompt of call i+1 is the prompt of call i, plus call i's reply, plus the
# message the loop sent back. So "what the loop sent back" after a failed
# reply can be read as a token count: the action-required message (a fixed
# text) left 37 or 38 tokens; a protocol-error message (whose wording varies
# with the error) left 24 to 31; a tool result is fenced and longer. This is
# an inference from arithmetic, not an observation of text, and the first test
# below is what licenses it: across all 360 runs the reading never disagrees
# with the counts the loop itself recorded.

REJECTION_FEEDBACK = (37, 38)
PROTOCOL_FEEDBACK = range(24, 32)


def _feedback(run: dict) -> list[int]:
    calls = run.get("model_calls") or []
    return [
        calls[i + 1]["prompt_tokens"] - calls[i]["prompt_tokens"] - calls[i]["completion_tokens"]
        for i in range(len(calls) - 1)
    ]


def test_the_token_reading_never_disagrees_with_the_recorded_counts():
    """Every reading accounts for no more failures than were recorded, and the
    remainder is at most the last call, which has no next prompt to read from
    and failed only if the budget stopped the attempt."""
    for path in (BASELINE, UNIT1, UNIT1_AND_2):
        for r in _agent(path).values():
            fed = _feedback(r)
            unread_rejections = (r.get("action_rejections") or 0) - sum(f in REJECTION_FEEDBACK for f in fed)
            unread_protocol = r["protocol_errors"] - sum(f in PROTOCOL_FEEDBACK for f in fed)
            assert unread_rejections >= 0 and unread_protocol >= 0, (path.name, r["task"], r["run"])
            assert unread_rejections + unread_protocol <= (1 if r["stop"] == "budget" else 0)


def _first_reply(path: Path, lang: str) -> Counter:
    out = Counter()
    for r in _agent(path).values():
        fed = _feedback(r)
        if r["language"] != lang or not fed:
            continue
        out["protocol"] += fed[0] in PROTOCOL_FEEDBACK
        out["rejected"] += fed[0] in REJECTION_FEEDBACK
    return out


def test_english_first_replies_broke_the_protocol_more_often_arabic_did_not():
    """The first reply is the one that sees the opening prompt, where the
    environment text is. English first replies that broke the protocol:
    3 -> 8 -> 15; Arabic 6 -> 5 -> 5. First replies that were an answer
    rejected for not having acted: English 0 -> 12 -> 17, Arabic 0 -> 34 -> 41."""
    assert [_first_reply(p, "en")["protocol"] for p in (BASELINE, UNIT1, UNIT1_AND_2)] == [3, 8, 15]
    assert [_first_reply(p, "ar")["protocol"] for p in (BASELINE, UNIT1, UNIT1_AND_2)] == [6, 5, 5]
    assert [_first_reply(p, "en")["rejected"] for p in (BASELINE, UNIT1, UNIT1_AND_2)] == [0, 12, 17]
    assert [_first_reply(p, "ar")["rejected"] for p in (BASELINE, UNIT1, UNIT1_AND_2)] == [0, 34, 41]


def test_git_commit_release_in_english_failed_at_the_first_reply_with_unit_2():
    """Unit 1: in all 5 attempts the first reply (27 or 28 tokens) was a
    write_file call. Unit 1 + 2: in all 5 the first reply (13 or 19 tokens)
    broke the protocol; in 3 the sequence was protocol error, rejected
    answer, protocol error; the prompt it answered was 181 tokens longer."""
    one = [_agent(UNIT1)[("git-commit-release", "en", i)] for i in range(1, 6)]
    two = [_agent(UNIT1_AND_2)[("git-commit-release", "en", i)] for i in range(1, 6)]
    assert all(r["steps"][0]["tool"] == "write_file" for r in one)
    assert {r["model_calls"][0]["completion_tokens"] for r in one} == {27, 28}
    assert all(_feedback(r)[0] in PROTOCOL_FEEDBACK for r in two)
    assert {r["model_calls"][0]["completion_tokens"] for r in two} == {13, 19}
    pattern = [
        ("protocol" if f in PROTOCOL_FEEDBACK else "rejected" if f in REJECTION_FEEDBACK else "tool")
        for r in two for f in _feedback(r)[:2]
    ]
    assert sum(1 for i in range(0, 10, 2) if pattern[i:i + 2] == ["protocol", "rejected"]) == 3
    assert {r["model_calls"][0]["prompt_tokens"] - o["model_calls"][0]["prompt_tokens"]
            for r, o in zip(two, one)} == {181}


def test_with_unit_2_english_reached_for_run_command_instead_of_shell():
    """What the environment text says it is for: English shell calls 65 -> 26,
    run_command 8 -> 20 (Arabic shell 58 -> 34, run_command 13 -> 23)."""
    def tools(path: Path, lang: str) -> Counter:
        return Counter(s["tool"] for r in _agent(path).values() if r["language"] == lang for s in r["steps"])
    en1, en2 = tools(UNIT1, "en"), tools(UNIT1_AND_2, "en")
    ar1, ar2 = tools(UNIT1, "ar"), tools(UNIT1_AND_2, "ar")
    assert (en1["shell"], en2["shell"], en1["run_command"], en2["run_command"]) == (65, 26, 8, 20)
    assert (ar1["shell"], ar2["shell"], ar1["run_command"], ar2["run_command"]) == (58, 34, 13, 23)
