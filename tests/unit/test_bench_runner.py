"""The benchmark runner end to end, on a scripted fake model (ADR-022 §5).

The fake solves one task and fails the other in known ways, so a broken check
or a broken record cannot pass: the solved task must come out PASS with its
test run recorded, and the failed ones FAIL with the reason they failed.
"""
from __future__ import annotations

import io
import json
import re
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
        "fixture": "fixtures/repo", "git": True,
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
        "id": "t", "track": "agent", "category": "git", "fixture": "fixtures/repo",
        "git": True, "instruction": {"en": "x", "ar": "س"},
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
