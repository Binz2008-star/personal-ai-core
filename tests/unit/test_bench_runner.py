"""The benchmark runner end to end, on a scripted fake model (ADR-022 §5).

The fake solves one task and fails the other in known ways, so a broken check
or a broken record cannot pass: the solved task must come out PASS with its
test run recorded, and the failed ones FAIL with the reason they failed.
"""
from __future__ import annotations

import io
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

from personal_ai_core.app.bench.runner import CallLog, main, report

BENCH = Path(__file__).resolve().parents[2] / "evals" / "bench"
FIXED = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
FIXED_CALC = (BENCH / "fixtures" / "off_by_one" / "calc.py").read_text(
    encoding="utf-8").replace("range(1, len(prices))", "range(len(prices))")
_ARABIC = re.compile(r"[؀-ۿ]")


def _reply(content: str) -> dict[str, Any]:
    return {"message": {"role": "assistant", "content": content}, "model": "fake",
            "prompt_eval_count": 100, "eval_count": 10, "done_reason": "stop"}


class ScriptedModel:
    """A transport that plays one script per task, step by step.

    `scripts` maps a substring of the task's instruction to the replies the
    model gives, in order; a script is chosen by the first user message.
    """

    def __init__(self, scripts: Mapping[str, list[str]]) -> None:
        self.scripts = scripts

    def __call__(self, url: str, payload: Mapping[str, Any], timeout: int) -> Mapping[str, Any]:
        messages = payload["messages"]
        users = [m["content"] for m in messages if m["role"] == "user"]
        turn = sum(1 for m in messages if m["role"] == "assistant")
        arabic = bool(_ARABIC.search(users[0]))
        for key, script in self.scripts.items():
            if key in users[0]:
                text = script[min(turn, len(script) - 1)]
                return _reply(text.replace("{lang}", "ar" if arabic else "en"))
        raise AssertionError(f"no script for {users[0][:60]!r}")


def _tool(tool: str, **arguments: Any) -> str:
    return json.dumps({"tool": tool, "arguments": arguments})


def _answer(text: str) -> str:
    return json.dumps({"answer": text})


SOLVES = {
    "test_calc.py": [
        _tool("read_file", path="calc.py"),
        _tool("write_file", path="calc.py", content=FIXED_CALC),
        _tool("shell", command="python -m pytest -q -p no:cacheprovider"),
        _answer("Fixed the loop start; the tests pass."),
    ],
    # The knowledge task, answered wrongly: 24 is the allowance, not the carry-over.
    "leave": ["You can carry over 24 days (annual-leave.md)."],
    "الإجازة": ["يمكنك ترحيل 24 يوماً (annual-leave.md)."],
}


def _run(tmp_path: Path, scripts=SOLVES, *argv: str) -> tuple[int, str, list[dict]]:
    out = io.StringIO()
    # The two original tasks: the scripts below are written for them.
    code = main(["--tasks", str(BENCH), "--out", str(tmp_path), "--runs", "1",
                 "--only", "verify-off-by-one", "kb-leave-carryover", *argv],
                transport=ScriptedModel(scripts), stdout=out, env={}, now=lambda: FIXED,
                commit="abc1234", probe=lambda url, body=None: {"models": []})
    files = sorted(tmp_path.glob("bench-*.jsonl"))
    lines = [json.loads(x) for x in files[0].read_text(encoding="utf-8").splitlines()] if files else []
    return code, out.getvalue(), lines


def test_a_solved_task_passes_and_a_wrong_answer_fails(tmp_path):
    code, output, lines = _run(tmp_path)
    assert code == 0, output
    header, runs, end = lines[0], [x for x in lines if x["kind"] == "run"], lines[-1]
    assert header["kind"] == "header" and end["kind"] == "end"
    assert len(runs) == 4  # 2 tasks x 2 languages x 1 run
    by = {(r["task"], r["language"]): r for r in runs}
    for language in ("en", "ar"):
        solved = by[("verify-off-by-one", language)]
        assert solved["success"], solved["checks"]
        assert solved["stop"] == "answered"
        assert [s["tool"] for s in solved["steps"]] == ["read_file", "write_file", "shell"]
        assert solved["commands"] == ["python -m pytest -q -p no:cacheprovider"]
        assert solved["verification"]["verdict"] == "PASS"
        assert solved["approvals"][0]["approved"] is True
        assert solved["steps"][2]["approval"] == {
            "by": "benchmark policy", "approved": True,
            "reason": "shell: within the benchmark command policy"}
        assert "approval" not in solved["steps"][0], "read_file is not asked"
        assert solved["final_state"]["files"]["calc.py"]
        assert solved["signals"] == []
        wrong = by[("kb-leave-carryover", language)]
        assert not wrong["success"]
        assert "check:answer_contains" in wrong["signals"]
    assert "overall: 2/4 (50%)" in output


def test_every_model_call_is_recorded_with_what_was_sent(tmp_path):
    _, _, lines = _run(tmp_path)
    solved = next(x for x in lines if x.get("task") == "verify-off-by-one")
    assert len(solved["model_calls"]) == 4
    # The agent loop sends num_predict and nothing else: recorded, not assumed.
    assert solved["model_calls"][0]["options_sent"] == {"num_predict": 1024}
    assert solved["prompt_tokens"] == 400 and solved["completion_tokens"] == 40


def test_the_header_binds_the_result(tmp_path):
    _, _, lines = _run(tmp_path)
    header = lines[0]
    assert header["commit"] == "abc1234" and header["role"] == "boss"
    assert header["runs"] == 1 and header["languages"] == ["en", "ar"]
    assert set(header["tasks"]) == {"verify-off-by-one", "kb-leave-carryover"}
    assert "Not a security sandbox" in header["policy"]
    assert "not isolated from the network" in header["environment"]
    assert header["judge_model"].startswith("none")


def test_a_network_tool_is_denied_and_the_run_says_so(tmp_path):
    scripts = dict(SOLVES)
    scripts["test_calc.py"] = [_tool("web_search", query="python off by one"),
                               _answer("I could not search.")]
    _, _, lines = _run(tmp_path, scripts, "--only", "verify-off-by-one", "--languages", "en")
    run = next(x for x in lines if x["kind"] == "run")
    assert not run["success"]
    assert run["steps"][0]["executed"] is False
    assert run["approvals"] == [{"tool": "web_search", "arguments": {"query": "python off by one"},
                                 "approved": False,
                                 "reason": "network tools are denied during a benchmark"}]
    assert "tool_refused" in run["signals"]


def test_an_edit_never_tested_is_labelled(tmp_path):
    scripts = dict(SOLVES)
    scripts["test_calc.py"] = [_tool("write_file", path="calc.py", content=FIXED_CALC),
                               _answer("Fixed.")]
    _, _, lines = _run(tmp_path, scripts, "--only", "verify-off-by-one", "--languages", "en")
    run = next(x for x in lines if x["kind"] == "run")
    # The code is right, but the task asked for a confirmed fix: it fails on that.
    assert not run["success"]
    assert "not_tested_after_edit" in run["signals"]
    assert "check:tested_after_last_edit" in run["signals"]


def test_a_shell_command_outside_the_policy_is_refused(tmp_path):
    scripts = dict(SOLVES)
    scripts["test_calc.py"] = [_tool("shell", command="curl https://example.com"),
                               _answer("No network.")]
    _, _, lines = _run(tmp_path, scripts, "--only", "verify-off-by-one", "--languages", "en")
    run = next(x for x in lines if x["kind"] == "run")
    assert run["approvals"][0]["approved"] is False
    assert "outside the benchmark policy" in run["approvals"][0]["reason"]


def test_a_run_out_of_budget_has_no_answer(tmp_path):
    scripts = dict(SOLVES)
    scripts["test_calc.py"] = ["not json at all"]
    _, _, lines = _run(tmp_path, scripts, "--only", "verify-off-by-one", "--languages", "en")
    run = next(x for x in lines if x["kind"] == "run")
    assert run["stop"] == "budget" and run["answer"] is None
    assert {"no_answer", "protocol_errors"} <= set(run["signals"])


def test_resume_skips_what_was_measured_and_refuses_another_setup(tmp_path):
    _, _, lines = _run(tmp_path, SOLVES, "--only", "verify-off-by-one", "--languages", "en")
    path = next(tmp_path.glob("bench-*.jsonl"))
    out = io.StringIO()
    code = main(["--tasks", str(BENCH), "--runs", "2", "--only", "verify-off-by-one",
                 "--languages", "en", "--resume", str(path)],
                transport=ScriptedModel(SOLVES), stdout=out, env={}, commit="abc1234",
                probe=lambda url, body=None: {"models": []})
    assert code == 2 and "runs differs" in out.getvalue()
    out = io.StringIO()
    code = main(["--tasks", str(BENCH), "--runs", "1", "--only", "verify-off-by-one",
                 "--languages", "en", "--resume", str(path)],
                transport=ScriptedModel(SOLVES), stdout=out, env={}, commit="abc1234",
                probe=lambda url, body=None: {"models": []})
    assert code == 0
    runs = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()
            if json.loads(x)["kind"] == "run"]
    assert len(runs) == 1, "a measured run is not measured twice"


def test_another_model_is_refused(tmp_path):
    out = io.StringIO()
    code = main(["--tasks", str(BENCH), "--out", str(tmp_path)], transport=ScriptedModel(SOLVES),
                stdout=out, env={"PAC_BOSS_MODEL": "other:7b"}, commit="x")
    assert code == 2 and "refusing" in out.getvalue()
    assert not list(tmp_path.glob("*.jsonl"))


def test_an_unproved_task_stops_the_benchmark_before_it_starts(tmp_path):
    import shutil

    tasks = tmp_path / "tasks"
    shutil.copytree(BENCH, tasks)
    data = json.loads((tasks / "verify-off-by-one.json").read_text(encoding="utf-8"))
    data["reference"]["writes"] = {}
    (tasks / "verify-off-by-one.json").write_text(json.dumps(data), encoding="utf-8")
    out = io.StringIO()
    code = main(["--tasks", str(tasks), "--out", str(tmp_path / "out"),
                 "--only", "verify-off-by-one"],
                transport=ScriptedModel(SOLVES), stdout=out, env={}, commit="x")
    assert code == 2 and "not admitted: verify-off-by-one" in out.getvalue()
    assert not (tmp_path / "out").exists()


def test_show_policy_prints_the_allowlist():
    out = io.StringIO()
    assert main(["--show-policy"], stdout=out) == 0
    assert "git {add, commit" in out.getvalue()


def test_the_call_log_records_a_failed_call_and_re_raises():
    def broken(url, payload, timeout):
        raise OSError("connection refused")

    log = CallLog(broken)
    with pytest.raises(OSError):
        log("u", {"options": {"num_predict": 5}}, 1)
    assert log.take()[0]["error"] == "connection refused"
    assert log.take() == []


def test_report_counts_and_signals():
    rows = [{"kind": "run", "task": "t", "track": "agent", "category": "debugging",
             "language": lang, "success": ok, "signals": [] if ok else ["no_answer"],
             "seconds": 10.0} for lang, ok in (("en", True), ("en", False), ("ar", False))]
    text = report(rows)
    assert "overall: 1/3 (33%)" in text
    assert "debugging" in text and "no_answer" in text
    assert report([]) == "no runs"


def test_a_commit_through_shell_lands_with_the_fixed_identity(tmp_path):
    """Local git through `shell`, end to end: approved by the policy, committed
    under the fixtures' identity, seen by the git check. The identity is in the
    repository's config: `shell` strips GIT_AUTHOR_* from its environment
    (the name matches "AUTH"), and CI has no global git identity to fall
    back on -- which is how this was found."""
    tasks = tmp_path / "tasks"
    (tasks / "fixtures" / "repo").mkdir(parents=True)
    (tasks / "fixtures" / "repo" / "notes.txt").write_text("draft\n", encoding="utf-8")
    task = {
        "id": "git-commit-notes", "track": "agent", "category": "git",
        "action_required": True, "fixture": "fixtures/repo", "git": True,
        "instruction": {"en": "Change notes.txt to say final and commit it with the message "
                              "finalize notes.",
                        "ar": "غيّر notes.txt ليقول final ثم احفظه في git برسالة finalize notes."},
        "checks": [{"type": "git_log_contains", "text": "finalize notes"},
                   {"type": "git_clean"}, {"type": "file_contains", "path": "notes.txt",
                                           "text": "final"}],
        "reference": {"writes": {"notes.txt": "final\n"},
                      "commands": [["git", "add", "-A"],
                                   ["git", "commit", "-q", "-m", "finalize notes"]]},
    }
    (tasks / "git-commit-notes.json").write_text(json.dumps(task, ensure_ascii=False),
                                                encoding="utf-8")
    script = {"notes.txt": [_tool("write_file", path="notes.txt", content="final\n"),
                            _tool("shell", command="git add -A"),
                            _tool("shell", command='git commit -q -m "finalize notes"'),
                            _answer("Committed.")]}
    out = io.StringIO()
    code = main(["--tasks", str(tasks), "--out", str(tmp_path / "out"), "--runs", "1",
                 "--languages", "en"], transport=ScriptedModel(script), stdout=out, env={},
                now=lambda: FIXED, commit="abc1234", probe=lambda url, body=None: {"models": []})
    assert code == 0, out.getvalue()
    lines = [json.loads(x) for x in next((tmp_path / "out").glob("*.jsonl"))
             .read_text(encoding="utf-8").splitlines()]
    run = next(x for x in lines if x["kind"] == "run")
    assert run["success"], (run["checks"], run["steps"])
    assert [a["approved"] for a in run["approvals"]] == [True, True]
    assert run["final_state"]["git_log"][0].endswith("finalize notes")
    assert run["final_state"]["git_status"] == ""


def test_the_fixture_repository_carries_its_own_identity(tmp_path):
    import subprocess

    from personal_ai_core.app.bench.tasks import load, materialize

    tasks = tmp_path / "tasks"
    (tasks / "fixtures" / "repo").mkdir(parents=True)
    (tasks / "fixtures" / "repo" / "a.txt").write_text("x\n", encoding="utf-8")
    (tasks / "t.json").write_text(json.dumps({
        "id": "t", "track": "agent", "category": "git", "action_required": True,
        "fixture": "fixtures/repo", "git": True, "instruction": {"en": "x", "ar": "س"},
        "checks": [{"type": "git_clean"}], "reference": {}}), encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    materialize(load(tasks)[0], workspace)
    name = subprocess.run(["git", "config", "--local", "user.name"], cwd=workspace,
                          capture_output=True, text=True).stdout.strip()
    assert name == "bench"


def _fixture_text(*parts: str) -> str:
    return (BENCH / "fixtures" / Path(*parts)).read_text(encoding="utf-8")


PYTEST_SHELL = _tool("shell", command="python -m pytest -q -p no:cacheprovider")
# One scripted solve per agent task, through the agent's own tools under the
# benchmark policy. If a task needs a tool or command the policy refuses, or
# the tools cannot reach what the checks look at, it fails here, not on the rig.
AGENT_SOLVES = {
    "Create config/settings.json": [
        _tool("write_file", path="config/settings.json",
              content='{"debug": false, "workers": 4}\n'),
        _answer("Created.")],
    "Add a function slugify": [
        _tool("write_file", path="text_utils.py",
              content=_fixture_text("slugify", "text_utils.py").replace(
                  '"""Text helpers."""\n', '"""Text helpers."""\nimport re\n')
              + '\n\ndef slugify(text):\n'
                '    return "-".join(re.findall(r"[a-z0-9]+", text.lower()))\n'),
        _answer("Added.")],
    "Change price_after_discount": [
        _tool("write_file", path="pricing.py",
              content=_fixture_text("discount", "pricing.py").replace(
                  "    return round(", "    percent = min(percent, 50)\n    return round(")),
        _answer("Capped.")],
    "test_words.py fail": [
        _tool("write_file", path="words.py",
              content=_fixture_text("word_count", "words.py").replace(
                  'text.split(" ")', "text.split()")),
        _answer("Fixed.")],
    "how many of them fail": [PYTEST_SHELL, _answer("3 of the 8 tests fail.")],
    "total_sales in report.py": [
        _tool("write_file", path="report.py",
              content=_fixture_text("sales", "report.py").replace("rows[:-1]", "rows")),
        PYTEST_SHELL, _answer("Fixed; the tests pass.")],
    "Change VERSION": [
        _tool("write_file", path="VERSION", content="1.5.0\n"),
        _tool("shell", command="git add -A"),
        _tool("shell", command='git commit -q -m "Release 1.5.0"'),
        _answer("Committed.")],
    "most recent commit": [
        _tool("run_command", command="git show --stat --format=%s HEAD"),
        _answer("README.md")],
    "sets the server port": [
        _tool("search_text", text="port"),
        _tool("write_file", path="config/app.ini",
              content="[server]\nhost = 127.0.0.1\nport = 8081\n"),
        _tool("shell", command="python check.py"),
        _answer("I changed config/app.ini.")],
    "How many .json files": [_tool("find_files", pattern="*.json"),
                             _answer("There are 4 .json files.")],
    "Fixed the date parser": [
        _tool("read_file", path="docs/CHANGELOG.md"),
        _tool("find_files", pattern="CHANGELOG*"),
        _tool("write_file", path="CHANGELOG.md",
              content="# Changelog\n\n## 2.0.0\n- New export format.\n"
                      "- Fixed the date parser.\n"),
        _answer("Added.")],
    "test_calc.py": SOLVES["test_calc.py"],
}


def test_every_agent_task_is_solvable_through_the_tools_under_the_policy(tmp_path):
    from personal_ai_core.app.bench.tasks import load

    agent_ids = [t.id for t in load(BENCH) if t.track == "agent"]
    out = io.StringIO()
    code = main(["--tasks", str(BENCH), "--out", str(tmp_path), "--runs", "1",
                 "--languages", "en", "--only", *agent_ids],
                transport=ScriptedModel(AGENT_SOLVES), stdout=out, env={}, now=lambda: FIXED,
                commit="abc1234", probe=lambda url, body=None: {"models": []})
    assert code == 0, out.getvalue()
    runs = [json.loads(x) for x in next(tmp_path.glob("*.jsonl")).read_text(
        encoding="utf-8").splitlines() if json.loads(x)["kind"] == "run"]
    failed = {r["task"]: (r["checks"], r["approvals"], [s.get("error") for s in r["steps"]])
              for r in runs if not r["success"]}
    assert not failed, failed
    assert len(runs) == 12


def test_a_right_fact_without_a_citation_succeeds_and_the_citation_is_its_own_rate(tmp_path):
    scripts = dict(SOLVES)
    scripts["leave"] = ["You can carry over up to 5 days."]
    _, output, lines = _run(tmp_path, scripts, "--only", "kb-leave-carryover", "--languages", "en")
    run = next(x for x in lines if x["kind"] == "run")
    assert run["success"] and run["signals"] == []
    assert "informational checks (not part of success):" in output
    assert "cites                0/1 (0%)" in output


def _tasks_with_contract(tmp_path: Path, task_id: str, action_required: bool) -> Path:
    """A copy of the shipped tasks in which one agent task states another contract."""
    tasks = tmp_path / "tasks"
    shutil.copytree(BENCH, tasks)
    path = tasks / f"{task_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["action_required"] = action_required
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return tasks


def test_an_agent_that_answers_without_acting_is_labelled(tmp_path):
    # Under action_required=false an answer with no tool call is accepted, and the
    # run says so: the label survives for the tasks the gate does not hold.
    tasks = _tasks_with_contract(tmp_path, "verify-off-by-one", action_required=False)
    scripts = dict(SOLVES)
    scripts["test_calc.py"] = [_answer("I will look at calc.py and test_calc.py.")]
    _, _, lines = _run(tmp_path / "out", scripts, "--tasks", str(tasks),
                       "--only", "verify-off-by-one", "--languages", "en")
    run = next(x for x in lines if x["kind"] == "run")
    assert not run["success"] and run["stop"] == "answered"
    assert run["action_required"] is False and run["action_rejections"] == 0
    assert "answered_without_acting" in run["signals"]


def test_an_action_required_task_rejects_an_answer_before_any_tool_call(tmp_path):
    """ADR-023 §8.3: the runner passes the task file's contract, so the unit 1 gate engages."""
    scripts = dict(SOLVES)
    scripts["test_calc.py"] = [_answer("It looks fine to me.")]
    code, _, lines = _run(tmp_path, scripts, "--only", "verify-off-by-one", "--languages", "en")
    run = next(x for x in lines if x["kind"] == "run")
    assert code == 0
    assert run["action_required"] is True
    # Three answers, three rejections, the third failure of the budget stops the run
    # with no answer; the unit-1 message is the last thing the model was told.
    assert run["stop"] == "budget" and run["answer"] is None
    assert run["action_rejections"] == 3
    assert len(run["model_calls"]) == 3
    assert "action_rejected" in run["signals"] and "no_answer" in run["signals"]
    assert "answered_without_acting" not in run["signals"]
    assert not run["success"]


def test_a_run_records_the_text_of_every_reply_the_loop_refused(tmp_path):
    """Handoff, Next 6b2: the reply that broke the protocol and the answer
    rejected for not having acted, as the model wrote them. Recorded only."""
    scripts = dict(SOLVES)
    scripts["test_calc.py"] = ["Let me look at calc.py first.", _answer("It looks fine to me."),
                               *SOLVES["test_calc.py"]]
    _, _, lines = _run(tmp_path, scripts, "--only", "verify-off-by-one", "--languages", "en")
    run = next(x for x in lines if x["kind"] == "run")
    assert run["refused_replies"] == [
        {"call": 1, "kind": "protocol_error", "error": "the reply contains no JSON object",
         "text": "Let me look at calc.py first."},
        {"call": 2, "kind": "action_required", "error": None,
         "text": _answer("It looks fine to me.")},
    ]
    # Scoring did not look at them: the run still solved the task.
    assert run["success"] and run["protocol_errors"] == 1 and run["action_rejections"] == 1
    assert "not scored" in lines[0]["refused_replies"]


def test_a_long_refused_reply_is_clipped_and_says_so(tmp_path):
    scripts = dict(SOLVES)
    scripts["test_calc.py"] = ["x" * 5_000, *SOLVES["test_calc.py"]]
    _, _, lines = _run(tmp_path, scripts, "--only", "verify-off-by-one", "--languages", "en")
    (refused,) = next(x for x in lines if x["kind"] == "run")["refused_replies"]
    assert refused["text"] == "x" * 4_000 + "... [1000 more chars]"


def test_a_run_with_nothing_refused_records_an_empty_list(tmp_path):
    _, _, lines = _run(tmp_path, SOLVES, "--only", "verify-off-by-one", "--languages", "en")
    assert next(x for x in lines if x["kind"] == "run")["refused_replies"] == []


def test_an_action_required_task_that_acts_is_not_rejected(tmp_path):
    _, _, lines = _run(tmp_path, SOLVES, "--only", "verify-off-by-one", "--languages", "en")
    run = next(x for x in lines if x["kind"] == "run")
    assert run["success"] and run["action_required"] is True
    assert run["action_rejections"] == 0 and "action_rejected" not in run["signals"]


def test_a_knowledge_run_has_no_contract_keys(tmp_path):
    _, _, lines = _run(tmp_path, SOLVES, "--only", "kb-leave-carryover", "--languages", "en")
    run = next(x for x in lines if x["kind"] == "run")
    assert run["track"] == "knowledge"
    assert "action_required" not in run and "action_rejections" not in run


def test_the_header_says_a_contract_was_passed(tmp_path):
    _, _, lines = _run(tmp_path, SOLVES, "--only", "verify-off-by-one", "--languages", "en")
    assert "AgentTaskContract" in lines[0]["contract"]


def test_a_refused_step_carries_the_policy_reason(tmp_path):
    scripts = dict(SOLVES)
    scripts["test_calc.py"] = [_tool("shell", command="code . -g"), _answer("Could not open it.")]
    _, _, lines = _run(tmp_path, scripts, "--only", "verify-off-by-one", "--languages", "en")
    step = next(x for x in lines if x["kind"] == "run")["steps"][0]
    assert step["executed"] is False
    assert step["approval"]["approved"] is False
    assert step["approval"]["reason"] == "shell: code is outside the benchmark policy"


def _v1_file(tmp_path: Path, answer: str) -> Path:
    """A result file as bench-checks-v1 wrote it: a right answer scored wrong."""
    _, _, lines = _run(tmp_path, {**SOLVES, "leave": [answer]}, "--only", "kb-leave-carryover",
                       "--languages", "en")
    lines[0].pop("scorer")
    run = next(x for x in lines if x["kind"] == "run")
    run["checks"][0].update(verdict="FAIL", detail="states [], not 5")
    run["success"] = False
    run["signals"] = ["check:answer_contains"]
    path = tmp_path / "bench-v1.jsonl"
    path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines),
                    encoding="utf-8")
    return path


def test_rescore_rejudges_answer_checks_into_a_new_file(tmp_path):
    path = _v1_file(tmp_path, "Up to 5 days.")
    source_before = path.read_bytes()
    out = io.StringIO()
    assert main(["--tasks", str(BENCH), "--rescore", str(path)], stdout=out) == 0
    assert path.read_bytes() == source_before, "the source is never modified"
    target = path.with_name("bench-v1.rescored-bench-checks-v2.jsonl")
    lines = [json.loads(x) for x in target.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["rescored_from"] == "bench-v1.jsonl"
    assert lines[0]["rescored_with"] == "bench-checks-v2"
    assert lines[0]["scorer_before"] == "bench-checks-v1"
    run = next(x for x in lines if x["kind"] == "run")
    assert run["success"] and run["signals"] == []
    assert "1 check verdict(s) changed" in out.getvalue()


def test_rescore_never_overwrites_and_refuses_a_current_file(tmp_path):
    path = _v1_file(tmp_path, "Up to 5 days.")
    assert main(["--tasks", str(BENCH), "--rescore", str(path)], stdout=io.StringIO()) == 0
    out = io.StringIO()
    assert main(["--tasks", str(BENCH), "--rescore", str(path)], stdout=out) == 2
    assert "never overwritten" in out.getvalue()
    current = path.with_name("bench-v1.rescored-bench-checks-v2.jsonl")
    out = io.StringIO()
    assert main(["--tasks", str(BENCH), "--rescore", str(current)], stdout=out) == 2
    assert "nothing to do" in out.getvalue()


def test_rescore_refuses_a_changed_task_whose_answers_it_would_rejudge(tmp_path):
    import shutil

    path = _v1_file(tmp_path, "Up to 5 days.")
    tasks = tmp_path / "tasks"
    shutil.copytree(BENCH, tasks)
    data = json.loads((tasks / "kb-leave-carryover.json").read_text(encoding="utf-8"))
    data["checks"][0]["any_of"].append("V")
    (tasks / "kb-leave-carryover.json").write_text(json.dumps(data, ensure_ascii=False),
                                                   encoding="utf-8")
    out = io.StringIO()
    assert main(["--tasks", str(tasks), "--rescore", str(path)], stdout=out) == 2
    assert "changed since the run" in out.getvalue()


def test_a_new_result_file_names_its_scorer(tmp_path):
    _, _, lines = _run(tmp_path, SOLVES, "--only", "kb-leave-carryover", "--languages", "en")
    assert lines[0]["scorer"] == "bench-checks-v2"


def test_the_task_digest_does_not_depend_on_the_platforms_file_order(tmp_path):
    """file-create-settings had digest 150017ff... on the rig and 96fce741... on
    Linux: Windows sorts paths without regard to case, so check_settings.py came
    before README.md there. The order is now the relative path as text."""
    import hashlib

    from personal_ai_core.app.bench.runner import _task_digest
    from personal_ai_core.app.bench.tasks import Task

    fixture = tmp_path / "fixture"
    (fixture / "sub").mkdir(parents=True)
    contents = {"README.md": b"# r\r\n", "check_settings.py": b"x = 1\n", "sub/Z.txt": b"z"}
    for name, data in contents.items():
        (fixture / name).write_bytes(data)
    task = Task(id="t", track="agent", category="c", instruction={"en": "do"},
                checks=[], reference={}, base=tmp_path, fixture=fixture)

    expected = hashlib.sha256(json.dumps(
        {"instruction": task.instruction, "checks": task.checks, "git": task.git,
         "git_commits": task.git_commits},
        sort_keys=True, ensure_ascii=False).encode("utf-8"))
    for name in sorted(contents):  # code-point order: README.md, check_settings.py, sub/Z.txt
        expected.update(name.encode("utf-8"))
        expected.update(contents[name].replace(b"\r\n", b"\n"))
    assert _task_digest(task) == expected.hexdigest()[:16]


# --- ADR-023 §8.3: the contract is part of the task a run measured -------------


def _contract_task(tmp_path: Path, action_required: bool | None):
    from personal_ai_core.app.bench.tasks import Task

    return Task(id="t", track="agent", category="c", instruction={"en": "do"},
                checks=[], reference={}, base=tmp_path, action_required=action_required)


def test_the_contract_is_part_of_the_task_digest(tmp_path):
    from personal_ai_core.app.bench.runner import _task_digest

    required, optional = _contract_task(tmp_path, True), _contract_task(tmp_path, False)
    assert _task_digest(required) != _task_digest(optional)
    # Without the field it is the digest from before the field existed, and a task
    # that states no contract (a knowledge task) never had a different one.
    assert _task_digest(required, with_contract=False) == _task_digest(
        optional, with_contract=False) == _task_digest(_contract_task(tmp_path, None))


def test_only_the_contract_changed_the_shipped_tasks_digests():
    """Against the immutable baseline's header: a knowledge task keeps its digest, and an
    agent task keeps it once the contract is left out. Nothing else about a task moved."""
    from personal_ai_core.app.bench.runner import _task_digest
    from personal_ai_core.app.bench.tasks import load

    raw = (BENCH.parents[1] / "evals" / "results" / "bench" / "bench-20261002T081707Z.jsonl")
    recorded = json.loads(raw.read_text(encoding="utf-8").splitlines()[0])["tasks"]
    tasks = {t.id: t for t in load(BENCH)}
    assert set(recorded) == set(tasks)
    for task_id, task in tasks.items():
        if task.track == "knowledge":
            assert _task_digest(task) == recorded[task_id]["digest"], task_id
        elif task_id == "file-create-settings":
            # The baseline header keeps the pre-#170 Windows digest (ADR-022 §10); the
            # task digests to 96fce7417d36a103 on every platform now.
            assert _task_digest(task, with_contract=False) == "96fce7417d36a103"
        else:
            assert _task_digest(task, with_contract=False) == recorded[task_id]["digest"], task_id
            assert _task_digest(task) != recorded[task_id]["digest"], task_id


def test_a_file_from_before_the_contract_is_not_resumed(tmp_path):
    """The baseline ran without a contract. Resuming it would mix two experiments."""
    from personal_ai_core.app.bench.runner import _task_digest
    from personal_ai_core.app.bench.tasks import load

    _run(tmp_path, SOLVES, "--only", "verify-off-by-one", "--languages", "en")
    path = next(tmp_path.glob("bench-*.jsonl"))
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    task = next(t for t in load(BENCH) if t.id == "verify-off-by-one")
    lines[0]["tasks"]["verify-off-by-one"]["digest"] = _task_digest(task, with_contract=False)
    path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines),
                    encoding="utf-8")
    out = io.StringIO()
    code = main(["--tasks", str(BENCH), "--runs", "1", "--only", "verify-off-by-one",
                 "--languages", "en", "--resume", str(path)],
                transport=ScriptedModel(SOLVES), stdout=out, env={}, commit="abc1234",
                probe=lambda url, body=None: {"models": []})
    assert code == 2 and "tasks differs" in out.getvalue()


def test_rescore_accepts_a_file_written_before_the_contract(tmp_path):
    """The contract changes no check, so a file that recorded the older digest is still
    the same task for the answer checks `rescore` re-judges (the baseline depends on it)."""
    from personal_ai_core.app.bench.runner import _task_digest
    from personal_ai_core.app.bench.tasks import load

    path = _v1_file(tmp_path, "You can carry over 5 days (annual-leave.md).")
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    answer_task = next(t for t in load(BENCH) if t.id == "tests-count-failures")
    lines[0]["tasks"] = {"tests-count-failures": {
        "track": "agent", "category": answer_task.category,
        "digest": _task_digest(answer_task, with_contract=False)}}
    path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines),
                    encoding="utf-8")
    assert main(["--tasks", str(BENCH), "--rescore", str(path)], stdout=io.StringIO()) == 0


# --- ADR-023 §2.1: the environment context is a flag, off unless asked for -----------


class _Capture:
    """Wraps the scripted model and keeps every payload it was sent."""

    def __init__(self, scripts) -> None:
        self.inner, self.payloads = ScriptedModel(scripts), []

    def __call__(self, url, payload, timeout):
        self.payloads.append(payload)
        return self.inner(url, payload, timeout)


def _run_with(tmp_path, *argv):
    capture = _Capture(SOLVES)
    out = io.StringIO()
    code = main(["--tasks", str(BENCH), "--out", str(tmp_path), "--runs", "1",
                 "--only", "verify-off-by-one", "--languages", "en", *argv],
                transport=capture, stdout=out, env={}, now=lambda: FIXED, commit="abc1234",
                probe=lambda url, body=None: {"models": []})
    lines = [json.loads(x) for x in next(tmp_path.glob("bench-*.jsonl")).read_text(
        encoding="utf-8").splitlines()]
    return code, capture, lines, out.getvalue()


def test_the_environment_context_is_off_by_default_and_the_file_says_so(tmp_path):
    code, capture, lines, _ = _run_with(tmp_path)
    run = next(x for x in lines if x["kind"] == "run")
    assert code == 0 and lines[0]["environment_context"] is False
    assert "environment" not in run
    assert not any(m["content"].startswith("Environment (")
                   for p in capture.payloads for m in p["messages"])


def test_with_the_flag_each_agent_run_is_given_and_records_the_environment(tmp_path):
    code, capture, lines, _ = _run_with(tmp_path, "--environment-context")
    run = next(x for x in lines if x["kind"] == "run")
    assert code == 0 and lines[0]["environment_context"] is True
    first = capture.payloads[0]["messages"]
    assert sum(m["content"].startswith("Environment (") for m in first) == 1
    assert run["environment"]["test_command"] == "pytest"
    assert run["environment"]["test_command_source"] == "discovery"
    assert run["environment"]["dropped"] == [] and run["environment"]["tokens"] > 0


def test_a_knowledge_run_is_never_given_the_environment(tmp_path):
    out = io.StringIO()
    capture = _Capture(SOLVES)
    main(["--tasks", str(BENCH), "--out", str(tmp_path), "--runs", "1", "--only",
          "kb-leave-carryover", "--languages", "en", "--environment-context"],
         transport=capture, stdout=out, env={}, now=lambda: FIXED, commit="abc1234",
         probe=lambda url, body=None: {"models": []})
    assert not any(m["content"].startswith("Environment (")
                   for p in capture.payloads for m in p["messages"])


def test_a_file_is_not_resumed_under_the_other_setting(tmp_path):
    _run_with(tmp_path)
    path = next(tmp_path.glob("bench-*.jsonl"))
    out = io.StringIO()
    code = main(["--tasks", str(BENCH), "--runs", "1", "--only", "verify-off-by-one",
                 "--languages", "en", "--environment-context", "--resume", str(path)],
                transport=ScriptedModel(SOLVES), stdout=out, env={}, commit="abc1234",
                probe=lambda url, body=None: {"models": []})
    assert code == 2 and "environment_context differs" in out.getvalue()


def test_a_file_from_before_the_flag_resumes_as_off(tmp_path):
    _run_with(tmp_path)
    path = next(tmp_path.glob("bench-*.jsonl"))
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    lines[0].pop("environment_context")
    path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines),
                    encoding="utf-8")
    out = io.StringIO()
    code = main(["--tasks", str(BENCH), "--runs", "1", "--only", "verify-off-by-one",
                 "--languages", "en", "--resume", str(path)],
                transport=ScriptedModel(SOLVES), stdout=out, env={}, commit="abc1234",
                probe=lambda url, body=None: {"models": []})
    assert code == 0
