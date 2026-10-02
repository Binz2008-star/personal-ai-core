"""Every benchmark check type, on hand-built workspaces and answers (ADR-022 §5)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from personal_ai_core.app.bench.checks import CHECKS, RunEvidence, ToolCall, judge
from personal_ai_core.app.bench.tasks import GIT_ENV

PASS, FAIL = "PASS", "FAIL"


def _call(tool: str, ok: bool = True, executed: bool = True, **arguments: str) -> ToolCall:
    return ToolCall(tool=tool, arguments=arguments, decision="allow", executed=executed, ok=ok)


def _ev(tmp_path: Path, answer: str | None = None, calls=()) -> RunEvidence:
    return RunEvidence(workspace=tmp_path, answer=answer, calls=tuple(calls))


def _verdict(ev: RunEvidence, kind: str, **params) -> str:
    return CHECKS[kind](ev, **params)[0]


def test_file_checks(tmp_path):
    # Bytes, so the platform does not translate the line endings under test.
    (tmp_path / "a.txt").write_bytes(b"hello\r\nworld  \n")
    ev = _ev(tmp_path)
    assert _verdict(ev, "file_exists", path="a.txt") == PASS
    assert _verdict(ev, "file_exists", path="b.txt") == FAIL
    assert _verdict(ev, "file_absent", path="b.txt") == PASS
    assert _verdict(ev, "file_absent", path="a.txt") == FAIL
    assert _verdict(ev, "file_contains", path="a.txt", text="world") == PASS
    assert _verdict(ev, "file_contains", path="a.txt", text="moon") == FAIL
    assert _verdict(ev, "file_contains", path="b.txt", text="x") == FAIL
    # Line endings and trailing spaces do not decide equality; content does.
    assert _verdict(ev, "file_equals", path="a.txt", text="hello\nworld\n") == PASS
    assert _verdict(ev, "file_equals", path="a.txt", text="hello\nmoon") == FAIL


def test_a_check_path_outside_the_workspace_fails_the_check(tmp_path):
    ok, results = judge(_ev(tmp_path), [{"type": "file_exists", "path": "../etc"}])
    assert not ok and "escapes" in results[0]["detail"]


def test_command_passes_runs_the_fixture_check_with_this_interpreter(tmp_path):
    (tmp_path / "good.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "bad.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
    ev = _ev(tmp_path)
    assert _verdict(ev, "command_passes", argv=["python", "good.py"]) == PASS
    verdict, detail = CHECKS["command_passes"](ev, argv=["python", "bad.py"])
    assert verdict == FAIL and "exit 3" in detail
    assert _verdict(ev, "command_passes", argv=["no-such-program-xyz"]) == FAIL


def _repo(path: Path, message: str = "initial") -> None:
    env = {**__import__("os").environ, **GIT_ENV}
    (path / "f.txt").write_text("x\n", encoding="utf-8")
    for argv in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", message]):
        subprocess.run(["git", *argv], cwd=path, check=True, env=env, capture_output=True)


def test_git_checks(tmp_path):
    ev = _ev(tmp_path)
    assert _verdict(ev, "git_log_contains", text="x") == FAIL  # not a repository
    assert _verdict(ev, "git_clean") == FAIL
    _repo(tmp_path, "fix: off by one")
    assert _verdict(ev, "git_log_contains", text="off by one") == PASS
    assert _verdict(ev, "git_log_contains", text="other") == FAIL
    assert _verdict(ev, "git_clean") == PASS
    (tmp_path / "f.txt").write_text("changed\n", encoding="utf-8")
    assert _verdict(ev, "git_clean") == FAIL


def test_answer_contains_folds_case_and_arabic_forms(tmp_path):
    assert _verdict(_ev(tmp_path, "The answer is CALC.PY"), "answer_contains",
                    any_of=["calc.py"]) == PASS
    assert _verdict(_ev(tmp_path, "الملف هو الإعدادات"), "answer_contains",
                    any_of=["الاعدادات"]) == PASS
    assert _verdict(_ev(tmp_path, "nothing"), "answer_contains", any_of=["calc.py"]) == FAIL
    assert _verdict(_ev(tmp_path, None), "answer_contains", any_of=["x"]) == FAIL


@pytest.mark.parametrize("answer, verdict", [
    ("3 tests fail", PASS),
    # v2: the first baseline scored "port 8443." as stating no number at all.
    ("The answer is 3.", PASS),
    ("The answer is 3, I think", PASS),
    ("Is it 3?", PASS),
    ("It is 3!", PASS),
    ("فشلت ٣.", PASS),
    ("(3)", PASS),
    # ...and nothing else is widened: a decimal or a longer number is not 3.
    ("It is 3.5.", FAIL),
    ("It is 0.3.", FAIL),
    ("It is .3", FAIL),
    ("Room 33.", FAIL),
    ("In 2023, five tests failed.", FAIL),
    ("فشلت ٣ اختبارات", PASS),
    ("13 tests fail", FAIL),
    ("3.5 tests", FAIL),
    ("none fail", FAIL),
    (None, FAIL),
])
def test_answer_number_matches_whole_numbers_in_either_digit_set(tmp_path, answer, verdict):
    assert _verdict(_ev(tmp_path, answer), "answer_number", value=3) == verdict


def test_cites_names_the_document(tmp_path):
    assert _verdict(_ev(tmp_path, "See annual-leave.md."), "cites",
                    document="annual-leave.md") == PASS
    assert _verdict(_ev(tmp_path, "According to annual-leave, 5 days."), "cites",
                    document="annual-leave.md") == PASS
    assert _verdict(_ev(tmp_path, "See remote-work.md."), "cites",
                    document="annual-leave.md") == FAIL


def test_declines_reuses_the_harness_check_and_review_is_not_a_pass(tmp_path):
    decline = "The provided documents do not contain this information."
    assert _verdict(_ev(tmp_path, decline), "declines") == PASS
    assert _verdict(_ev(tmp_path, "The capital is Paris."), "declines") == FAIL
    assert _verdict(_ev(tmp_path, None), "declines") == FAIL


def test_tool_used_counts_only_executed_calls(tmp_path):
    calls = [_call("find_files", pattern="*.json"), _call("shell", executed=False, command="x")]
    ev = _ev(tmp_path, calls=calls)
    assert _verdict(ev, "tool_used", tool="find_files") == PASS
    assert _verdict(ev, "tool_used", tool="shell") == FAIL
    assert _verdict(ev, "tool_not_used", tool="shell") == PASS
    assert _verdict(ev, "tool_not_used", tool="find_files") == FAIL


@pytest.mark.parametrize("calls, verdict, detail", [
    ([], FAIL, "no edit"),
    ([_call("write_file", path="a.py")], FAIL, "no test run"),
    ([_call("run_command", command="pytest -q"), _call("write_file", path="a.py")], FAIL,
     "no test run"),
    ([_call("write_file", path="a.py"), _call("run_command", command="pytest -q")], PASS, ""),
    ([_call("write_file", path="a.py"), _call("shell", command="python -m pytest -q")], PASS, ""),
    ([_call("write_file", path="a.py"), _call("run_command", ok=False, command="pytest")], FAIL,
     "failed"),
    # A test run that was refused (not executed) is not a test run.
    ([_call("write_file", path="a.py"), _call("shell", executed=False, command="pytest")], FAIL,
     "no test run"),
    # The last test run decides: a failure fixed and re-run is a pass.
    ([_call("write_file", path="a.py"), _call("run_command", ok=False, command="pytest"),
      _call("write_file", path="a.py"), _call("run_command", command="pytest")], PASS, ""),
    ([_call("write_file", path="a.py"), _call("run_command", command="cat pytest.ini")], FAIL,
     "no test run"),
])
def test_tested_after_last_edit(tmp_path, calls, verdict, detail):
    got, text = CHECKS["tested_after_last_edit"](_ev(tmp_path, calls=calls))
    assert got == verdict and detail in text


def test_judge_needs_every_check_to_pass(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    ok, results = judge(_ev(tmp_path, "5"), [
        {"type": "file_exists", "path": "a.txt"}, {"type": "answer_number", "value": 5}])
    assert ok and [r["verdict"] for r in results] == [PASS, PASS]
    ok, results = judge(_ev(tmp_path, "4"), [
        {"type": "file_exists", "path": "a.txt"}, {"type": "answer_number", "value": 5}])
    assert not ok and results[1]["verdict"] == FAIL


def test_the_interpreter_is_the_one_running_the_benchmark(tmp_path):
    (tmp_path / "which.py").write_text(
        f"import sys; raise SystemExit(0 if sys.executable == {sys.executable!r} else 1)\n",
        encoding="utf-8")
    assert _verdict(_ev(tmp_path), "command_passes", argv=["python", "which.py"]) == PASS


def test_an_informational_check_is_recorded_but_does_not_decide(tmp_path):
    ok, results = judge(_ev(tmp_path, "5 days"), [
        {"type": "answer_number", "value": 5},
        {"type": "cites", "document": "annual-leave.md", "informational": True}])
    assert ok
    assert results[1] == {"check": "cites", "verdict": FAIL, "detail": "does not name annual-leave.md",
                          "informational": True}
    ok, _ = judge(_ev(tmp_path, "4 days (annual-leave.md)"), [
        {"type": "answer_number", "value": 5},
        {"type": "cites", "document": "annual-leave.md", "informational": True}])
    assert not ok, "a right citation does not rescue a wrong fact"


def test_a_number_ending_a_sentence_is_stated(tmp_path):
    """bench-checks-v1 missed every number followed by a full stop: 17 correct
    answers in the first baseline were scored wrong. These are their shapes."""
    assert _verdict(_ev(tmp_path, "The api service listens on port 8443."),
                    "answer_number", value=8443) == PASS
    assert _verdict(_ev(tmp_path, "خدمة api تستمع على المنفذ 8443."),
                    "answer_number", value=8443) == PASS
    assert _verdict(_ev(tmp_path, "It failed in the March 2026 outage and listens on port 9443."),
                    "answer_number", value=9443) == PASS


@pytest.mark.parametrize("answer", [
    "The api service listens on port 9443.",      # a wrong number, punctuated
    "Version 8443.1 of the service.",             # the number inside a decimal
    "Port 18443.",                                # inside a longer number
    "Port 84430!",
    "The service was released in 2026.",          # unrelated numbers only
])
def test_the_fix_adds_no_false_positive(tmp_path, answer):
    assert _verdict(_ev(tmp_path, answer), "answer_number", value=8443) == FAIL
