"""The owner's profile: written once, read into every conversation and task.

The profile is what makes the Core the owner's rather than anyone's: a
Markdown file beside the database, composed into the identity message of
every turn -- conversation and agent alike -- so it is not tied to a session.
"""
from __future__ import annotations

import io

from personal_ai_core.app.cli import MAX_PROFILE_CHARS, main
from personal_ai_core.identity import DefaultIdentityComposer
from personal_ai_core.identity.text import CONTRACT_HEADING, PROFILE_HEADING

PROFILE = "# About me\n\n- I build Rico Hunt, a job-search platform for the UAE.\n"


def recording(reply='{"answer": "ok"}'):
    sent = []

    def transport(url, payload, timeout):
        sent.append(payload["messages"])
        return {"model": payload["model"], "message": {"content": reply}}

    transport.sent = sent  # type: ignore[attr-defined]
    return transport


def run(tmp_path, *argv, lines=("hello",), transport=None, env=None):
    out = io.StringIO()
    transport = transport or recording("hi")
    code = main(
        ["--database", str(tmp_path / "data" / "core.db"), *argv],
        transport=transport,
        stdin=iter(lines),
        stdout=out,
        env=env if env is not None else {},
    )
    return code, out.getvalue(), transport


def write_profile(tmp_path, text=PROFILE):
    path = tmp_path / "data" / "profile.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def system(transport):
    return transport.sent[0][0]["content"]


# --- read into every turn ------------------------------------------------------------


def test_the_profile_beside_the_database_reaches_the_model(tmp_path):
    path = write_profile(tmp_path)
    code, output, transport = run(tmp_path)
    assert code == 0
    assert f"profile: {path} (" in output
    assert "I build Rico Hunt" in system(transport)
    assert PROFILE_HEADING in system(transport)


def test_the_profile_is_read_before_the_rules(tmp_path):
    """The contract stays last, nearest the turn it governs."""
    write_profile(tmp_path)
    _, _, transport = run(tmp_path)
    text = system(transport)
    assert text.index("I build Rico Hunt") < text.index(CONTRACT_HEADING)


def test_the_profile_is_there_in_a_new_session_too(tmp_path):
    """Not tied to a session: a second run, a new session, the same profile."""
    write_profile(tmp_path)
    run(tmp_path)
    _, _, transport = run(tmp_path)
    assert "I build Rico Hunt" in system(transport)


def test_the_agent_knows_the_owner_too(tmp_path):
    write_profile(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    transport = recording('{"answer": "ok"}')
    code, _, _ = run(
        tmp_path, "--agent", "--workspace", str(workspace), transport=transport
    )
    assert code == 0 and "I build Rico Hunt" in system(transport)


def test_a_named_profile_and_the_environment_are_honoured(tmp_path):
    named = tmp_path / "me.md"
    named.write_text("- named profile\n", encoding="utf-8")
    _, _, transport = run(tmp_path, "--profile", str(named))
    assert "named profile" in system(transport)

    from_env = tmp_path / "env.md"
    from_env.write_text("- from the environment\n", encoding="utf-8")
    _, _, transport = run(tmp_path, env={"PAC_PROFILE": str(from_env)})
    assert "from the environment" in system(transport)


def test_without_a_profile_nothing_is_added_and_the_user_is_told_where(tmp_path):
    code, output, transport = run(tmp_path)
    assert code == 0
    assert "profile: none yet" in output and "profile.md" in output
    assert PROFILE_HEADING not in system(transport)


def test_an_ephemeral_run_has_no_default_profile(tmp_path):
    out = io.StringIO()
    transport = recording("hi")
    main(["--ephemeral"], transport=transport, stdin=iter(["hi"]), stdout=out, env={})
    assert PROFILE_HEADING not in system(transport)


def test_an_oversized_profile_is_refused_before_anything_is_opened(tmp_path):
    write_profile(tmp_path, "x" * (MAX_PROFILE_CHARS + 1))
    code, output, transport = run(tmp_path)
    assert code == 2 and "Shorten" in output
    assert transport.sent == []  # type: ignore[attr-defined]
    assert not (tmp_path / "data" / "core.db").exists()


# --- --remember ------------------------------------------------------------------------


def test_remember_starts_a_profile_and_adds_to_it(tmp_path):
    code, output, transport = run(tmp_path, "--remember", "I prefer answers in Arabic")
    assert code == 0 and "remembered" in output
    assert transport.sent == []  # type: ignore[attr-defined]
    run(tmp_path, "--remember", "  my   CV is   up to date  ")
    text = (tmp_path / "data" / "profile.md").read_text(encoding="utf-8")
    assert text == "# About me\n- I prefer answers in Arabic\n- my CV is up to date\n"

    _, _, transport = run(tmp_path)
    assert "I prefer answers in Arabic" in system(transport)


def test_remember_keeps_what_was_written_by_hand(tmp_path):
    write_profile(tmp_path, "# Me\nno trailing newline")
    run(tmp_path, "--remember", "one more")
    text = (tmp_path / "data" / "profile.md").read_text(encoding="utf-8")
    assert text == "# Me\nno trailing newline\n- one more\n"


def test_remember_needs_text_and_a_place(tmp_path):
    code, output, _ = run(tmp_path, "--remember", "   ")
    assert code == 2 and "needs something" in output
    out = io.StringIO()
    code = main(["--ephemeral", "--remember", "x"], stdin=iter([]), stdout=out, env={})
    assert code == 2 and "no profile to add to" in out.getvalue()


# --- the composer ----------------------------------------------------------------------


def test_the_profile_is_paid_for_in_the_identity_budget():
    """The budget funds identity from `tokens()`; the profile must be in it."""
    from personal_ai_core.context import ScriptAwareTokenEstimator

    estimator = ScriptAwareTokenEstimator()
    bare = DefaultIdentityComposer()
    profiled = DefaultIdentityComposer(profile=PROFILE)
    assert profiled.tokens(estimator) > bare.tokens(estimator)
    assert profiled.profile == PROFILE.strip()
    assert DefaultIdentityComposer(profile="  \n ").text == bare.text


# --- projects.md, beside the profile -------------------------------------------------


def test_projects_beside_the_profile_are_read_with_it(tmp_path):
    path = write_profile(tmp_path)
    projects = path.parent / "projects.md"
    projects.write_text("# My projects\n- LVYY: a WhatsApp sales agent\n", encoding="utf-8")
    _, output, transport = run(tmp_path)
    text = system(transport)
    assert "I build Rico Hunt" in text and "LVYY: a WhatsApp sales agent" in text
    assert text.index("I build Rico Hunt") < text.index("LVYY")
    assert f"profile: {path} + {projects} (" in output


def test_projects_alone_are_enough(tmp_path):
    projects = tmp_path / "data" / "projects.md"
    projects.parent.mkdir(parents=True)
    projects.write_text("- only projects\n", encoding="utf-8")
    _, _, transport = run(tmp_path)
    assert "only projects" in system(transport)


def test_the_limit_is_on_profile_and_projects_together(tmp_path):
    half = MAX_PROFILE_CHARS // 2 + 1
    path = write_profile(tmp_path, "p" * half)
    (path.parent / "projects.md").write_text("q" * half, encoding="utf-8")
    code, output, _ = run(tmp_path)
    assert code == 2 and "projects.md" in output
