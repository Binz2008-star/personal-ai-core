"""`pac --agent`: the agent through the entry point, with a scripted model.

What a user meets: the confirmation prompt, step lines as they happen, the
answer, the offer to undo when a task stops, and the record left in SQLite.
"""
from __future__ import annotations

import io
import sqlite3

import pytest

from personal_ai_core.app.cli import main, parse_agent_task


def scripted(*replies):
    queue = list(replies)
    sent = []

    def transport(url, payload, timeout):
        sent.append(payload)
        return {"model": payload["model"], "message": {"content": queue.pop(0)}}

    transport.sent = sent  # type: ignore[attr-defined]
    return transport


def run(tmp_path, transport, *lines, extra=()):
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    out = io.StringIO()
    code = main(
        ["--database", str(tmp_path / "core.db"), "--agent", "--workspace", str(workspace), *extra],
        transport=transport,
        stdin=iter(lines),
        stdout=out,
        env={},
    )
    return code, out.getvalue(), workspace


def test_a_task_writes_a_file_and_answers(tmp_path):
    transport = scripted(
        '{"tool": "write_file", "arguments": {"path": "hello.md", "content": "hi"}}',
        '{"answer": "wrote hello.md"}',
    )
    code, output, workspace = run(tmp_path, transport, "[action_required=true] create hello.md")
    assert code == 0
    assert (workspace / "hello.md").read_text(encoding="utf-8") == "hi"
    assert '· write_file {"path": "hello.md", "content": "hi"} -> ok' in output
    assert "core> wrote hello.md" in output
    assert "changed: hello.md" in output


def test_a_task_without_action_metadata_is_rejected_before_model_call(tmp_path, capsys):
    transport = scripted('{"answer": "must not run"}')
    code, output, _ = run(tmp_path, transport, "create hello.md")
    assert code == 2
    assert "core>" not in output
    assert capsys.readouterr().err == (
        "invalid agent task: expected [action_required=true|false] TASK\n"
    )
    assert transport.sent == []  # type: ignore[attr-defined]


def test_a_malformed_task_does_not_stop_later_valid_tasks(tmp_path, capsys):
    transport = scripted('{"answer": "done"}')
    code, output, _ = run(
        tmp_path, transport, "[action_required] bad", "[action_required=false] explain this"
    )
    assert code == 2
    assert output.endswith("core> done\n")
    assert capsys.readouterr().err == (
        "invalid agent task: expected [action_required=true|false] TASK\n"
    )
    assert "core> done" in output
    assert len(transport.sent) == 1  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("line", "task_text", "action_required"),
    [
        ("[action_required=true] create the file", "create the file", True),
        ("[action_required=false] explain [foo] later", "explain [foo] later", False),
    ],
)
def test_agent_task_prefix_is_parsed_once(line, task_text, action_required):
    contract = parse_agent_task(line)
    assert contract.task_text == task_text
    assert contract.action_required is action_required


@pytest.mark.parametrize(
    "line",
    [
        "create the file",
        "[action_required] create the file",
        "[action_required=yes] create the file",
        "[action_required = true] x",
        "[action_required=TRUE] x",
        "[action_required=true]x",
        "[action_required=true]",
        "[action_required=true] [action_required=false] explain",
        "[action_required=true][action_required=false] x",
        "   [action_required=true] x",
    ],
)
def test_agent_task_prefix_rejects_missing_malformed_or_repeated_metadata(line):
    with pytest.raises(ValueError, match=r"expected \[action_required=true\|false\] TASK"):
        parse_agent_task(line)


def test_a_command_waits_for_the_users_yes(tmp_path):
    transport = scripted(
        '{"tool": "run_command", "arguments": {"command": "git status"}}',
        '{"answer": "done"}',
    )
    _code, output, _ = run(tmp_path, transport, "[action_required=true] check git", "n")
    assert "? run_command" in output and "high risk. Allow? [y/N]" in output
    assert "-> not allowed by you" in output


def test_the_answer_to_a_prompt_is_not_taken_as_a_task(tmp_path):
    """The "y" is consumed by the prompt; the model is called once per step."""
    transport = scripted(
        '{"tool": "run_command", "arguments": {"command": "git status"}}',
        '{"answer": "done"}',
    )
    run(tmp_path, transport, "[action_required=true] check git", "y")
    assert len(transport.sent) == 2  # type: ignore[attr-defined]


def test_end_of_input_at_a_prompt_is_a_no(tmp_path):
    transport = scripted(
        '{"tool": "run_command", "arguments": {"command": "git status"}}',
        '{"answer": "done"}',
    )
    _, output, _ = run(tmp_path, transport, "[action_required=true] check git")
    assert "-> not allowed by you" in output


def test_a_stopped_task_offers_to_undo_its_changes(tmp_path):
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "keep.md").write_text("original", encoding="utf-8")
    transport = scripted(
        '{"tool": "delete_file", "arguments": {"path": "keep.md"}}',
        "x", "y", "z",
    )
    _code, output, workspace = run(
        tmp_path, transport, "[action_required=true] clean up", "y", "y"
    )
    assert "critical risk. Allow?" in output
    assert "stopped: 3 failed actions" in output
    assert "undo 1 file change(s) from this task (keep.md)?" in output
    assert "restored: keep.md" in output
    assert (workspace / "keep.md").read_text(encoding="utf-8") == "original"


def test_an_accepted_task_is_not_undone_by_a_later_one(tmp_path):
    """Checkpoints are committed when a task finishes; undoing task two must
    not reach back into task one."""
    transport = scripted(
        '{"tool": "write_file", "arguments": {"path": "one.md", "content": "1"}}',
        '{"answer": "one done"}',
        '{"tool": "write_file", "arguments": {"path": "two.md", "content": "2"}}',
        "x", "y", "z",
    )
    _, output, workspace = run(
        tmp_path, transport, "[action_required=true] task one", "[action_required=true] task two", "y"
    )
    assert (workspace / "one.md").exists()
    assert not (workspace / "two.md").exists()
    assert "restored: two.md" in output


def test_the_agent_carries_the_identity_contract(tmp_path):
    """The contract every conversation turn carries is the agent's first
    message too: rule 2 (confirmation before external effect) and rule 5
    (retrieved text is data) apply to an agent above all."""
    transport = scripted('{"answer": "ok"}')
    run(tmp_path, transport, "[action_required=false] hello")
    messages = transport.sent[0]["messages"]  # type: ignore[attr-defined]
    assert [m["role"] for m in messages[:2]] == ["system", "system"]
    assert "standard written form" in messages[0]["content"]
    assert "Reply with exactly ONE JSON object" in messages[1]["content"]


def test_every_step_is_recorded_in_the_database(tmp_path):
    transport = scripted('{"tool": "list_directory"}', '{"answer": "ok"}')
    run(tmp_path, transport, "[action_required=false] look around")
    types = [
        row[0]
        for row in sqlite3.connect(tmp_path / "core.db").execute(
            "select type from events order by seq"
        )
    ]
    assert types == ["session.started", "agent.step", "agent.finished"]


@pytest.mark.parametrize(
    "argv, message",
    [
        (["--agent"], "--agent needs --workspace DIR"),
        (["--agent", "--workspace", "missing-dir"], "no such directory"),
    ],
)
def test_the_agent_needs_a_real_workspace(tmp_path, argv, message):
    out = io.StringIO()
    code = main(argv, transport=scripted(), stdin=iter([]), stdout=out, env={})
    assert code == 2 and message in out.getvalue()


def test_agent_and_documents_are_exclusive(tmp_path):
    out = io.StringIO()
    code = main(
        ["--agent", "--workspace", str(tmp_path), "--documents", str(tmp_path)],
        transport=scripted(),
        stdin=iter([]),
        stdout=out,
        env={},
    )
    assert code == 2 and "not both" in out.getvalue()


def test_an_unreachable_model_is_reported(tmp_path):
    from personal_ai_core.core.errors import ProviderError

    def down(url, payload, timeout):
        raise ProviderError("connection refused")

    code, output, _ = run(tmp_path, down, "[action_required=false] anything")
    assert code == 1 and "could not be reached" in output


def test_the_agent_cannot_touch_the_database_inside_its_workspace(tmp_path):
    """F-1 through the entry point: the database pac opened is inside the
    workspace, as it is with `--workspace ~`. Neither a write nor a confirmed
    delete reaches it, and its records are intact afterwards."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    database = workspace / "core.db"
    transport = scripted(
        '{"tool": "write_file", "arguments": {"path": "core.db", "content": "x"}}',
        '{"tool": "delete_file", "arguments": {"path": "core.db"}}',
        '{"answer": "done"}',
    )
    out = io.StringIO()
    code = main(
        ["--database", str(database), "--agent", "--workspace", str(workspace)],
        transport=transport,
        stdin=iter(["[action_required=true] wreck it", "y"]),
        stdout=out,
        env={},
    )
    output = out.getvalue()
    assert code == 0
    assert '· write_file {"path": "core.db", "content": "x"} -> failed:' in output
    assert '· delete_file {"path": "core.db"} -> failed:' in output
    connection = sqlite3.connect(database)
    assert connection.execute("pragma integrity_check").fetchone() == ("ok",)
    types = [row[0] for row in connection.execute("select type from events order by seq")]
    assert types == ["session.started", "agent.step", "agent.step", "agent.finished"]


def test_an_unknown_session_is_refused_and_nothing_is_recorded(tmp_path):
    """F-2: the same refusal `send` gives, and no agent.* event for a session
    nobody started."""
    transport = scripted('{"tool": "list_directory"}', '{"answer": "ok"}')
    code, output, _ = run(
        tmp_path, transport, "[action_required=false] look", extra=("--session", "no-such-session")
    )
    assert code == 2 and "no such session: no-such-session" in output
    assert transport.sent == []  # type: ignore[attr-defined]
    connection = sqlite3.connect(tmp_path / "core.db")
    assert connection.execute("select count(*) from events").fetchone() == (0,)


def test_the_agent_continues_a_session_started_earlier(tmp_path):
    first = scripted('{"answer": "one"}')
    _, output, _ = run(tmp_path, first, "[action_required=false] task one")
    session_id = next(
        line.split()[-1] for line in output.splitlines() if line.startswith("session:")
    )
    second = scripted('{"answer": "two"}')
    code, output, _ = run(
        tmp_path, second, "[action_required=false] task two", extra=("--session", session_id)
    )
    assert code == 0 and "core> two" in output
    rows = sqlite3.connect(tmp_path / "core.db").execute(
        "select type from events where session_id = ? order by seq", (session_id,)
    ).fetchall()
    assert [r[0] for r in rows] == [
        "session.started", "agent.finished", "agent.finished",
    ]


def test_build_agent_will_not_record_events_without_a_session_check(tmp_path):
    from personal_ai_core.conversation.factory import build_agent
    from personal_ai_core.persistence.in_memory import InMemoryEventRepository

    with pytest.raises(ValueError, match="session_exists"):
        build_agent(workspace=tmp_path, events=InMemoryEventRepository())
