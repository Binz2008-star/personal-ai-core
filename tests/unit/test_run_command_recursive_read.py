"""Fix 9: run_command does not read files recursively.

Found in the RC bug hunt: `_refuse_protected_arguments` tries each argument
as a FILE, so a directory argument passed it, and a confirmed
`grep -rao -e PATTERN .` with the workspace at the home directory put the
owner's profile, the Core's database and a plain `.env` password into the
next request to the model. The secret-shape check (N7) does not catch prose or
a password that looks like nothing. Outside a git work tree `git diff a b`
compares the two paths directly and read the same files. Both are refused;
the model is pointed to search_text, which leaves those files out.
"""
from __future__ import annotations

import json
import subprocess
from typing import Any, Mapping

import pytest

from personal_ai_core.agent.sandbox import Workspace
from personal_ai_core.agent.tools import RunCommand
from personal_ai_core.conversation.factory import build_agent
from personal_ai_core.core.config import Settings

DATA = ".personal-ai-core"
PROFILE = "I walk my dog Biscuit along the canal at six\n"
DATABASE = "ledger balance 48213 for the quarterly account\n"
PASSWORD = "PASSWORD=hunter2plain\n"
PLANTED = ("Biscuit", "48213", "hunter2plain")


class Transport:
    """Scripts each reply and keeps each payload."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.payloads: list[Mapping[str, Any]] = []

    def __call__(self, url: str, payload: Mapping[str, Any], timeout: int):
        self.payloads.append(dict(payload))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return {"model": payload["model"], "message": {"content": reply}, "done_reason": "stop"}


@pytest.fixture
def home(tmp_path):
    """The workspace is the home directory, the data directory inside it."""
    root = tmp_path / "home"
    (root / DATA).mkdir(parents=True)
    (root / DATA / "core.db").write_text(DATABASE, encoding="utf-8")
    (root / DATA / "core.db-wal").write_text(DATABASE, encoding="utf-8")
    (root / DATA / "profile.md").write_text(PROFILE, encoding="utf-8")
    (root / ".env").write_text(PASSWORD, encoding="utf-8")
    (root / "notes.md").write_text("an ordinary note about the canal\n", encoding="utf-8")
    return root


def run_agent(root, command: str):
    transport = Transport(
        json.dumps({"tool": "run_command", "arguments": {"command": command}}),
        '{"answer": "done"}',
    )
    agent = build_agent(
        Settings.from_env({}),
        workspace=root,
        transport=transport,
        confirm=lambda request, spec: True,  # the owner says yes
        database=root / DATA / "core.db",
        owner_files=(root / DATA / "profile.md", root / DATA / "projects.md"),
    )
    outcome = agent.loop.run("which of my files mention the canal?", session_id="s1")
    return outcome, transport


@pytest.mark.parametrize(
    "command",
    ["grep -r -e a -e = .", "grep -rn -e a -e =", "grep --directories=recurse -e a -e = ."],
)
def test_a_confirmed_recursive_grep_sends_the_model_nothing_it_may_not_read(home, command):
    outcome, transport = run_agent(home, command)
    assert len(transport.payloads) == 2, "the model saw the refusal and answered"
    sent = json.dumps(transport.payloads)
    for planted in PLANTED:
        assert planted not in sent, f"{planted!r} reached the model"
    result = outcome.steps[0].record.result
    assert result is not None and not result.ok
    assert "may not search recursively" in (result.error or "")
    assert "use search_text" in (result.error or "")
    assert "use search_text" in json.dumps(transport.payloads[1])


def test_a_confirmed_git_diff_outside_a_repository_sends_nothing_either(home):
    (home / "empty").mkdir()
    (home / "empty" / "x").write_text("x\n", encoding="utf-8")
    outcome, transport = run_agent(home, "git diff empty .")
    sent = json.dumps(transport.payloads)
    for planted in PLANTED:
        assert planted not in sent, f"{planted!r} reached the model"
    result = outcome.steps[0].record.result
    assert result is not None and "outside a git work tree" in (result.error or "")


def test_a_confirmed_grep_of_a_named_file_still_reaches_the_model(home):
    outcome, transport = run_agent(home, "grep -n canal notes.md")
    result = outcome.steps[0].record.result
    assert result is not None and result.ok
    assert "ordinary note about the canal" in json.dumps(transport.payloads[1])


def test_grep_on_a_named_ordinary_file_still_runs(home):
    result = RunCommand(Workspace(home)).run({"command": "grep -n canal notes.md"})
    assert result.ok and "ordinary note" in result.output


def test_git_diff_inside_a_work_tree_still_runs(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "notes.md").write_text("hello\n", encoding="utf-8")
    result = RunCommand(Workspace(root)).run({"command": "git diff --stat"})
    assert result.ok, result.error
