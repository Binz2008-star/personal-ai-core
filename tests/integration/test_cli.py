"""The command works — including when the model server is not there.

An entry point is the only part of a system every user touches, and the only
part whose failures they meet before they have read anything. So these tests
are as much about the failure paths as the happy one: an unknown session, an
unreachable model, a directory that does not exist yet.

Everything the outside world provides is injected -- argv, stdin, stdout, the
environment and the transport -- so the whole command runs without a terminal,
a home directory or a model server. That is the same reason `transport` is
injectable on the factories, applied one level up.
"""
from __future__ import annotations

import io
from typing import Any, Mapping

import pytest

from personal_ai_core.app.cli import DEFAULT_DATABASE_ENV, main
from personal_ai_core.core.errors import ProviderError


def transport_saying(text: str):
    def _transport(url: str, payload: Mapping[str, Any], timeout: int):
        return {"model": payload["model"], "message": {"content": text}}

    return _transport


def failing_transport(url: str, payload: Mapping[str, Any], timeout: int):
    raise ProviderError("connection refused to http://127.0.0.1:11434")


def run(argv, *, lines=(), transport=None, env=None):
    out = io.StringIO()
    code = main(
        argv,
        transport=transport or transport_saying("ok"),
        stdin=iter(lines),
        stdout=out,
        env=env if env is not None else {},
    )
    return code, out.getvalue()


# --- it answers -----------------------------------------------------------


def test_a_turn_gets_a_reply(tmp_path):
    code, output = run(
        ["--database", str(tmp_path / "core.db")],
        lines=["hello"],
        transport=transport_saying("hi there"),
    )
    assert code == 0
    assert "core> hi there" in output


def test_it_says_where_the_state_is_kept(tmp_path):
    """A user cannot back up, inspect or delete a file they were never told
    about."""
    database = tmp_path / "core.db"
    code, output = run([f"--database={database}"], lines=[])
    assert code == 0
    assert str(database) in output
    assert "session:" in output


def test_blank_lines_are_not_turns(tmp_path):
    code, output = run(
        ["--database", str(tmp_path / "core.db")],
        lines=["", "   ", "real"],
        transport=transport_saying("once"),
    )
    assert code == 0
    assert output.count("core> once") == 1


# --- and it remembers -----------------------------------------------------


def test_a_session_can_be_continued_in_a_later_run(tmp_path):
    """The whole point of the durable store, expressed in the interface."""
    database = str(tmp_path / "core.db")
    _, first = run([f"--database={database}"], lines=["my name is Rico"])
    session_id = next(
        line.split("session:")[1].strip()
        for line in first.splitlines()
        if line.startswith("session:")
    )

    sent: list[list[str]] = []

    def recording(url: str, payload: Mapping[str, Any], timeout: int):
        sent.append([m["content"] for m in payload["messages"]])
        return {"model": payload["model"], "message": {"content": "ok"}}

    code, _ = run(
        [f"--database={database}", f"--session={session_id}"],
        lines=["what did I say?"],
        transport=recording,
    )
    assert code == 0
    assert "my name is Rico" in sent[-1]


def test_the_invitation_to_continue_names_the_session(tmp_path):
    _, output = run([f"--database={tmp_path / 'core.db'}"], lines=[])
    assert "--session " in output


def test_ephemeral_keeps_nothing(tmp_path):
    database = tmp_path / "core.db"
    code, output = run(["--ephemeral", f"--database={database}"], lines=["hello"])
    assert code == 0
    assert not database.exists()
    assert "--session" not in output


# --- and it fails in sentences --------------------------------------------


def test_an_unknown_session_is_reported_not_raised(tmp_path):
    code, output = run(
        [f"--database={tmp_path / 'core.db'}", "--session=does-not-exist"],
        lines=["hello"],
    )
    assert code == 2
    assert "no such session: does-not-exist" in output


def test_an_unreachable_model_is_reported_not_raised(tmp_path):
    """The commonest first-run failure. A traceback here teaches the user
    nothing they can act on; the variable name does."""
    code, output = run(
        [f"--database={tmp_path / 'core.db'}"],
        lines=["hello"],
        transport=failing_transport,
    )
    assert code == 1
    assert "could not be reached" in output
    assert "PAC_OLLAMA_HOST" in output


# --- where the file goes --------------------------------------------------


def test_the_database_directory_is_created(tmp_path):
    database = tmp_path / "nowhere" / "yet" / "core.db"
    code, _ = run([f"--database={database}"], lines=["hello"])
    assert code == 0
    assert database.exists()


def test_the_environment_names_the_database_when_the_flag_does_not(tmp_path):
    database = tmp_path / "from-env.db"
    code, output = run(
        [], lines=["hello"], env={DEFAULT_DATABASE_ENV: str(database)}
    )
    assert code == 0
    assert database.exists()
    assert str(database) in output


def test_the_flag_wins_over_the_environment(tmp_path):
    flagged = tmp_path / "flag.db"
    from_env = tmp_path / "env.db"
    code, _ = run(
        [f"--database={flagged}"],
        lines=["hello"],
        env={DEFAULT_DATABASE_ENV: str(from_env)},
    )
    assert code == 0
    assert flagged.exists()
    assert not from_env.exists()


# --- the model is not guessed ---------------------------------------------


def test_the_language_is_undetermined_unless_given(tmp_path):
    """Guessing it would put a claim in the record that nothing measured."""
    database = str(tmp_path / "core.db")
    run([f"--database={database}"], lines=["مرحبا"])

    from personal_ai_core.persistence.sqlite import connect

    connection = connect(database)
    try:
        rows = connection.execute(
            "SELECT language FROM messages WHERE role = 'user'"
        ).fetchall()
        assert [row["language"] for row in rows] == ["und"]
    finally:
        connection.close()


def test_a_given_language_reaches_the_stored_message(tmp_path):
    database = str(tmp_path / "core.db")
    run([f"--database={database}", "--language=ar"], lines=["مرحبا"])

    from personal_ai_core.persistence.sqlite import connect

    connection = connect(database)
    try:
        rows = connection.execute(
            "SELECT language FROM messages WHERE role = 'user'"
        ).fetchall()
        assert [row["language"] for row in rows] == ["ar"]
    finally:
        connection.close()


@pytest.mark.parametrize("flag", ["--help"])
def test_help_exits_cleanly(flag, capsys):
    with pytest.raises(SystemExit) as exit_info:
        main([flag], env={})
    assert exit_info.value.code == 0
