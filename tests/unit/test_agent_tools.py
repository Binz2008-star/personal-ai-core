"""The agent's tools, run directly -- the executor is tested separately."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from personal_ai_core.agent import tools as tools_module
from personal_ai_core.agent.commands import CommandRejected
from personal_ai_core.agent.sandbox import SandboxError, Workspace
from personal_ai_core.agent.tools import (
    Shell,
    MAX_OUTPUT_CHARS,
    ListDirectory,
    ReadFile,
    RunCommand,
    FindFiles,
    SearchText,
    WriteFile,
    default_tools,
    run_bounded,
)
from personal_ai_core.core.agent import RiskLevel
from personal_ai_core.core.contracts import Tool


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "notes.md").write_text("alpha\nReciprocal Rank Fusion\nomega\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('fusion')\n", encoding="utf-8")
    (root / ".env").write_text("API_KEY=secret-fusion\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("fusion in git\n", encoding="utf-8")
    return Workspace(root)


def test_every_default_tool_satisfies_the_contract(ws):
    for tool in default_tools(ws):
        assert isinstance(tool, Tool)


def test_the_declared_risk_levels_are_the_designs(ws):
    """Section 3's examples: read/search/list LOW, write MEDIUM, command HIGH."""
    assert {t.spec.name: t.spec.risk_level for t in default_tools(ws)} == {
        "read_file": RiskLevel.LOW,
        "list_directory": RiskLevel.LOW,
        "find_files": RiskLevel.LOW,
        "search_text": RiskLevel.LOW,
        "write_file": RiskLevel.MEDIUM,
        "run_command": RiskLevel.HIGH,
    }


# --- read ---------------------------------------------------------------------


def test_read_returns_the_text(ws):
    result = ReadFile(ws).run({"path": "notes.md"})
    assert result.ok and "Reciprocal Rank Fusion" in result.output


def test_read_refuses_a_secret(ws):
    """A secret read into the model's context is a secret disclosed."""
    with pytest.raises(SandboxError, match="may not read"):
        ReadFile(ws).run({"path": ".env"})


def test_read_refuses_an_escape(ws):
    with pytest.raises(SandboxError):
        ReadFile(ws).run({"path": "../outside.txt"})


def test_read_reports_a_missing_file(ws):
    result = ReadFile(ws).run({"path": "nope.md"})
    assert not result.ok and "not a file" in (result.error or "")


def test_read_is_bounded_and_says_so(ws):
    (ws.root / "big.txt").write_text("x" * (MAX_OUTPUT_CHARS + 10), encoding="utf-8")
    result = ReadFile(ws).run({"path": "big.txt"})
    assert result.truncated and len(result.output) == MAX_OUTPUT_CHARS


def test_read_refuses_binary(ws):
    (ws.root / "blob.bin").write_bytes(b"\xff\xfe\x00")
    assert not ReadFile(ws).run({"path": "blob.bin"}).ok


# --- list and search -------------------------------------------------------------


def test_list_marks_directories_and_hides_git(ws):
    result = ListDirectory(ws).run({})
    assert result.output.splitlines() == [".env", "notes.md", "src/"]


def test_search_finds_lines_and_skips_secrets_and_git(ws):
    result = SearchText(ws).run({"text": "fusion"})
    lines = result.output.splitlines()
    assert "notes.md:2: Reciprocal Rank Fusion" in lines
    assert "src/app.py:1: print('fusion')" in lines
    assert not any(line.startswith((".env", ".git")) for line in lines)


def test_search_can_be_case_sensitive(ws):
    result = SearchText(ws).run({"text": "fusion", "case_sensitive": True})
    assert result.output == "src/app.py:1: print('fusion')"


def test_search_says_when_nothing_matched(ws):
    assert SearchText(ws).run({"text": "zzz"}).output == "no matches"


def test_search_does_not_follow_a_symlink_out(tmp_path, ws):
    """A symlinked FILE: os.walk already declines to descend into a linked
    directory, so a linked directory would not test the sandbox at all."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.txt").write_text("fusion outside\n", encoding="utf-8")
    try:
        (ws.root / "leak.txt").symlink_to(outside / "leak.txt")
    except OSError as exc:
        pytest.skip(f"cannot create a symlink here: {exc}")
    assert "outside" not in SearchText(ws).run({"text": "fusion"}).output


# --- write -----------------------------------------------------------------------


def test_write_creates_and_reports(ws):
    result = WriteFile(ws).run({"path": "out/new.md", "content": "hi"})
    assert result.ok and result.output == "created out/new.md (2 characters)"
    assert (ws.root / "out" / "new.md").read_text(encoding="utf-8") == "hi"


def test_write_says_when_it_overwrote(ws):
    assert WriteFile(ws).run({"path": "notes.md", "content": "x"}).output.startswith("overwrote")


def test_write_refuses_a_secret(ws):
    with pytest.raises(SandboxError, match="protected"):
        WriteFile(ws).run({"path": ".env", "content": "API_KEY=mine"})
    assert "secret-fusion" in (ws.root / ".env").read_text(encoding="utf-8")


def test_write_refuses_git(ws):
    with pytest.raises(SandboxError):
        WriteFile(ws).run({"path": ".git/hooks/pre-commit", "content": "x"})


# --- command -----------------------------------------------------------------------


def test_a_rejected_command_never_reaches_a_process(ws, monkeypatch):
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
    with pytest.raises(CommandRejected):
        RunCommand(ws).run({"command": "python evil.py"})
    assert called == []


def test_a_command_runs_without_a_shell_in_the_workspace(ws, monkeypatch):
    seen = {}

    def fake_run(args, **kwargs):
        seen.update(args=args, shell=False, **kwargs)
        return 0, "ok\n", ""

    monkeypatch.setattr(tools_module, "run_bounded", fake_run)
    result = RunCommand(ws).run({"command": "git status"})
    assert result.ok and result.output == "ok\n"
    assert seen["args"] == ["git", "status"]
    assert seen["shell"] is False
    assert seen["cwd"] == ws.root


def test_the_command_environment_is_built_from_nothing(ws, monkeypatch):
    """The source blanked four named secrets and passed everything else."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    seen = {}

    def fake_run(args, **kwargs):
        seen.update(kwargs)
        return 0, "", ""

    monkeypatch.setattr(tools_module, "run_bounded", fake_run)
    RunCommand(ws).run({"command": "git status"})
    assert "OPENAI_API_KEY" not in seen["env"]
    assert seen["env"]["HOME"] == str(ws.root)


def test_a_failing_command_is_a_failed_result_with_its_output(ws, monkeypatch):
    monkeypatch.setattr(tools_module, "run_bounded", lambda args, **k: (128, "", "not a repo"))
    result = RunCommand(ws).run({"command": "git status"})
    assert not result.ok and result.error == "exit code 128" and "not a repo" in result.output


def test_a_timeout_is_reported(ws, monkeypatch):
    def slow(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(tools_module, "run_bounded", slow)
    result = RunCommand(ws, timeout_seconds=3).run({"command": "git log"})
    assert not result.ok and result.error == "timed out after 3s"


def test_a_real_command_really_runs(ws):
    """One end-to-end run, no fakes. git exists on every CI platform; the
    workspace is not a repository, so git exits non-zero -- which is still a
    process that ran, was captured, and reported its failure."""
    result = RunCommand(ws).run({"command": "git status"})
    assert not result.ok and result.error and result.error.startswith("exit code")


# --- find_files ---------------------------------------------------------------


def _tree(ws):
    root = ws.root
    (root / "evals" / "cases").mkdir(parents=True)
    (root / "evals" / "results").mkdir(parents=True)
    (root / "evals" / "README.md").write_text("x\n", encoding="utf-8")
    for name in ("contract_v0.json", "contract_v1.json", "refusal_v1.json"):
        (root / "evals" / "cases" / name).write_text("{}\n", encoding="utf-8")
    for i in range(4):
        (root / "evals" / "results" / f"raw-{i}.JSON").write_text("{}\n", encoding="utf-8")
    return root


def test_find_files_counts_by_name_in_every_subdirectory(ws):
    """The 2026-10-01 rig run: asked to count the JSON files in evals, the
    agent searched contents and answered 'none'. There were files below."""
    _tree(ws)
    result = FindFiles(ws).run({"pattern": "*.json", "path": "evals"})
    assert result.ok
    lines = result.output.splitlines()
    assert lines[-1] == "7 file(s) match *.json"  # case-insensitive: .JSON counts
    assert "evals/cases/refusal_v1.json" in lines
    assert "evals/README.md" not in result.output


def test_find_files_reports_no_match_and_rejects_an_empty_pattern(ws):
    assert FindFiles(ws).run({"pattern": "*.xyz"}).output == "no files match *.xyz"
    assert not FindFiles(ws).run({"pattern": "  "}).ok


def test_find_files_skips_git_and_never_leaves_the_workspace(ws):
    result = FindFiles(ws).run({"pattern": "config"})
    assert "no files match" in result.output  # .git/config is not walked


def test_search_text_says_it_searches_contents_not_names(ws):
    assert "find_files" in SearchText(ws).spec.description



# --- no input, and a timeout that ends the whole tree (rig, 2026-10-02) -------------


def _run_with_open_stdin(ws, command: str, timeout_seconds: int) -> subprocess.CompletedProcess:
    """Run the Shell tool in a child whose own stdin is a pipe kept open, as the
    agent's is in a terminal. Without that, an inherited stdin may already be at
    end-of-file and the test could not tell a closed stdin from an open one."""
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from personal_ai_core.agent.sandbox import Workspace\n"
        "from personal_ai_core.agent.tools import Shell\n"
        f"r = Shell(Workspace(Path({str(ws.root)!r})), timeout_seconds={timeout_seconds})"
        ".run({'command': sys.argv[1]})\n"
        "print('OK' if r.ok else 'FAIL', r.error)\n"
    )
    import os

    # A raw pipe, not stdin=PIPE: communicate() would close a PIPE and hand the
    # child end-of-file, which is exactly what is under test. The write end
    # stays open here until the child is done.
    read_end, write_end = os.pipe()
    try:
        child = subprocess.Popen(
            [sys.executable, "-c", code, command], stdin=read_end, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
        )
        os.close(read_end)
        try:
            out, err = child.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate()
            pytest.fail(f"the command waited for input: {command}")
    finally:
        os.close(write_end)
    return subprocess.CompletedProcess(child.args, child.returncode, out, err)


def test_a_command_gets_no_input_so_a_debugger_cannot_wait_for_it(ws):
    """The baseline hung for 26 minutes on `pytest --pdb`: the debugger waited for
    input. With stdin closed it reads end-of-file and the command ends."""
    (ws.root / "test_fails.py").write_text("def test_x():\n    assert False\n", encoding="utf-8")
    done = _run_with_open_stdin(
        ws, f'"{sys.executable}" -m pytest -q -p no:cacheprovider --pdb test_fails.py', 50)
    assert done.stdout.startswith("FAIL exit code"), (done.stdout, done.stderr)


def test_a_prompt_reads_end_of_file(ws):
    done = _run_with_open_stdin(ws, f'"{sys.executable}" -c "input()"', 50)
    assert done.stdout.startswith("FAIL exit code"), (done.stdout, done.stderr)


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        listed = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                capture_output=True, text=True).stdout
        return str(pid) in listed
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_timeout_kills_the_grandchild_too(ws):
    """Killing only the shell left the python grandchild holding the pipes, and
    the call never returned. The whole tree goes: the call returns, and the
    grandchild is no longer running."""
    script = ws.root / "spawn.py"
    script.write_text(
        "import subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "open('grandchild.pid', 'w').write(str(child.pid))\n"
        "child.wait()\n",
        encoding="utf-8")
    started = time.monotonic()
    result = Shell(ws, timeout_seconds=3).run({"command": f'"{sys.executable}" spawn.py'})
    assert time.monotonic() - started < 45
    assert not result.ok and result.error == "timed out after 3s"
    pid = int((ws.root / "grandchild.pid").read_text())
    deadline = time.monotonic() + 10
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not _alive(pid), "the grandchild outlived the timeout"


def test_a_timeout_kills_a_descendant_after_the_leader_exits(ws):
    """The leader can exit while a descendant still holds the inherited pipes.

    `taskkill /T` walks the tree from the named process, so once the leader is
    gone it cannot reach the descendant: on Windows the descendant outlived the
    timeout and the two reader threads stayed blocked on its pipes (owner's
    machine, 2026-10-08). The grandchild test above keeps the leader alive with
    `child.wait()`, so it never covered this case. A job object kills the whole
    tree from the job handle instead, so the descendant must be gone.
    """
    metadata = ws.root / "descendant.pid"
    leader = (
        "import subprocess, sys\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "Path(sys.argv[1]).write_text(str(child.pid))\n"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded([sys.executable, "-c", leader, str(metadata)],
                    cwd=ws.root, env=dict(os.environ), timeout=2)
    deadline = time.monotonic() + 10
    while not metadata.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    pid = int(metadata.read_text())
    deadline = time.monotonic() + 10
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not _alive(pid), "a descendant outlived the leader's timeout"


def test_failed_process_creation_closes_the_prepared_job(ws, monkeypatch):
    closed = []
    monkeypatch.setattr(tools_module.sys, "platform", "win32")
    monkeypatch.setattr(tools_module, "_start_suspended_in_job", lambda: (101, 4))
    monkeypatch.setattr(tools_module, "_close_job", closed.append)

    def missing(*args, **kwargs):
        raise FileNotFoundError("missing executable")

    monkeypatch.setattr(tools_module.subprocess, "Popen", missing)
    with pytest.raises(FileNotFoundError, match="missing executable"):
        run_bounded(["missing"], cwd=ws.root, env={}, timeout=1)
    assert closed == [101]
    assert not tools_module._jobs


@pytest.mark.parametrize("failure", [RuntimeError("assignment failed"), KeyboardInterrupt()])
def test_interrupted_containment_kills_and_reaps_the_frozen_child(ws, monkeypatch, failure):
    actions = []

    class FrozenChild:
        def kill(self):
            actions.append("kill")

        def wait(self):
            actions.append("wait")

    monkeypatch.setattr(tools_module.sys, "platform", "win32")
    monkeypatch.setattr(tools_module, "_start_suspended_in_job", lambda: (101, 4))
    monkeypatch.setattr(tools_module.subprocess, "Popen", lambda *args, **kwargs: FrozenChild())
    monkeypatch.setattr(tools_module, "_close_job", lambda job: actions.append(("close", job)))

    def interrupted(job, process):
        raise failure

    monkeypatch.setattr(tools_module, "_contain_and_release", interrupted)
    with pytest.raises(type(failure)):
        run_bounded(["command"], cwd=ws.root, env={}, timeout=1)
    assert actions == ["kill", "wait", ("close", 101)]
    assert not tools_module._jobs
