"""What the owner adds to every prompt holds no secret.

The profile is composed into the identity message of every turn, conversation
and agent alike, and sent to whatever host PAC_OLLAMA_HOST names. `--remember`
wrote whatever it was given into it, so a key typed there went with every turn
from then on. Now `--remember` refuses a secret-shaped entry, and one that would
push the profile past its limit, before writing anything; a secret already in
the file is withheld from what the model reads, and a line says where it is.
Every secret below is obviously fake.
"""
from __future__ import annotations

import io

import pytest

from personal_ai_core.app import cli
from personal_ai_core.app.cli import MAX_PROFILE_CHARS, main
from personal_ai_core.core.redaction import Redaction, RedactionError

GITHUB = "ghp_" + "PLANTED" + "a" * 29
AWS = "AKIA" + "IOSFODNN7EXAMPLE"
PROFILE = "# About me\n\n- I build Rico Hunt, a job-search platform for the UAE.\n"


class Recording:
    def __init__(self) -> None:
        self.sent: list[list[dict]] = []

    def __call__(self, url, payload, timeout):
        self.sent.append(payload["messages"])
        return {"model": payload["model"], "message": {"content": "hi"}}


def run(tmp_path, *argv, lines=("hello",)):
    out = io.StringIO()
    transport = Recording()
    code = main(["--database", str(tmp_path / "data" / "core.db"), *argv],
                transport=transport, stdin=iter(lines), stdout=out, env={})
    return code, out.getvalue(), transport


def profile(tmp_path):
    return tmp_path / "data" / "profile.md"


def write_profile(tmp_path, text):
    path = profile(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- --remember ----------------------------------------------------------------------


@pytest.mark.parametrize(("entry", "shape"), [
    (f"my github token is {GITHUB}", "GitHub token"),
    (f"aws key {AWS}", "AWS access key"),
    ("the database is postgresql://owner:hunter2-secret@db.example/core", "connection string"),
])
def test_remember_refuses_a_secret_and_writes_nothing(tmp_path, entry, shape):
    path = write_profile(tmp_path, PROFILE)
    code, output, transport = run(tmp_path, "--remember", entry)
    assert code == 2
    assert shape in output and "not remembered" in output
    assert path.read_text(encoding="utf-8") == PROFILE
    # The value is not repeated back, and nothing was sent anywhere.
    for value in (GITHUB, AWS, "hunter2-secret"):
        assert value not in output
    assert transport.sent == []


def test_remember_refuses_a_secret_without_creating_the_file(tmp_path):
    code, _, _ = run(tmp_path, "--remember", f"token {GITHUB}")
    assert code == 2
    assert not profile(tmp_path).exists()


def test_remember_refuses_an_entry_that_would_pass_the_limit(tmp_path):
    full = "# About me\n" + "- " + "x" * (MAX_PROFILE_CHARS - 20) + "\n"
    path = write_profile(tmp_path, full)
    code, output, _ = run(tmp_path, "--remember", "one more thing about me")
    assert code == 2
    assert f"the limit is {MAX_PROFILE_CHARS}" in output and str(path) in output
    assert path.read_text(encoding="utf-8") == full


def test_remember_still_remembers_an_ordinary_fact(tmp_path):
    path = write_profile(tmp_path, PROFILE)
    code, output, _ = run(tmp_path, "--remember", "I prefer short answers in Arabic")
    assert code == 0 and "remembered" in output
    assert path.read_text(encoding="utf-8") == PROFILE + "- I prefer short answers in Arabic\n"


# --- a profile that already holds one ----------------------------------------------------


def test_a_secret_in_the_profile_is_withheld_from_the_model(tmp_path):
    path = write_profile(tmp_path, PROFILE + f"- my github token is {GITHUB}\n")
    code, output, transport = run(tmp_path)
    assert code == 0
    sent = " ".join(m["content"] for m in transport.sent[0])
    assert GITHUB not in sent
    # The rest of the profile still reaches the model.
    assert "I build Rico Hunt" in sent
    assert "secret-shaped" in output and "GitHub token" in output and str(path) in output
    assert GITHUB not in output
    # The owner's file is not changed.
    assert GITHUB in path.read_text(encoding="utf-8")


def test_a_clean_profile_is_sent_as_written_and_says_nothing(tmp_path):
    write_profile(tmp_path, PROFILE)
    code, output, transport = run(tmp_path)
    assert code == 0
    assert "I build Rico Hunt, a job-search platform for the UAE." in transport.sent[0][0]["content"]
    assert "secret-shaped" not in output


# --- a check that fails is not a pass (ADR-018 section 3.8) ------------------------------


class Failing:
    def redact(self, text: str) -> Redaction:
        raise RedactionError(RedactionError.INTERNAL)


def test_remember_writes_nothing_when_it_cannot_check(tmp_path, monkeypatch):
    path = write_profile(tmp_path, PROFILE)
    monkeypatch.setattr(cli, "build_reply_redactor", Failing)
    code, output, _ = run(tmp_path, "--remember", "an ordinary fact")
    assert code == 2 and "could not be checked" in output
    assert path.read_text(encoding="utf-8") == PROFILE


def test_a_profile_that_cannot_be_checked_is_not_sent(tmp_path, monkeypatch):
    write_profile(tmp_path, PROFILE)
    monkeypatch.setattr(cli, "build_reply_redactor", Failing)
    code, output, transport = run(tmp_path)
    assert code == 2 and "could not be checked" in output
    assert transport.sent == []
