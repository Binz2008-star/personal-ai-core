"""`pac --observations`: what recorded feedback amounts to, read and printed.

The first consumer of ADR-017 Unit 2. Each test drives the real entry point
end to end -- a stored conversation, feedback through `pac --feedback`, then
`--observations` -- and the database file is checked byte for byte to be
unchanged by the read.
"""
from __future__ import annotations

import hashlib
import io
import re
from typing import Any, Mapping

from personal_ai_core.app.cli import main
from personal_ai_core.core.errors import ProviderError


def transport_saying(text: str):
    def _transport(url: str, payload: Mapping[str, Any], timeout: int):
        return {"model": payload["model"], "message": {"content": text}}

    return _transport


def no_model(url: str, payload: Mapping[str, Any], timeout: int):
    raise ProviderError("--observations must not call the model")


def run(argv, *, lines=(), transport=None):
    out = io.StringIO()
    code = main(argv, transport=transport or transport_saying("Sydney"),
                stdin=iter(lines), stdout=out, env={})
    return code, out.getvalue()


def converse(db, *lines: str) -> str:
    code, output = run(["--database", str(db)], lines=lines)
    assert code == 0, output
    match = re.search(r"^session: (\S+)", output, re.MULTILINE)
    assert match is not None, output
    return match.group(1)


def feedback(db, session_id, label, *extra):
    code, output = run(["--database", str(db), "--session", session_id, "--feedback", label, *extra],
                       transport=no_model)
    assert code == 0, output


def observations(db, session_id):
    return run(["--database", str(db), "--session", session_id, "--observations"],
               transport=no_model)


def digest(db) -> str:
    return hashlib.sha256(db.read_bytes()).hexdigest()


def test_good_then_correction_shows_one_conflicted_observation_with_its_text(tmp_path):
    db = tmp_path / "core.db"
    session = converse(db, "What is the capital of Australia?")
    feedback(db, session, "good")
    feedback(db, session, "correction", "--correction", "Canberra, not Sydney")
    before = digest(db)

    code, output = observations(db, session)

    assert code == 0, output
    assert "observations for session" in output and ": 1" in output
    assert "correction -> candidate_strengthen (2 judgement(s))" in output
    assert "conflicted: earlier good; not promotable" in output
    assert "correction: Canberra, not Sydney" in output
    assert digest(db) == before


def test_a_single_judgement_is_not_conflicted(tmp_path):
    db = tmp_path / "core.db"
    session = converse(db, "hello")
    feedback(db, session, "good")

    code, output = observations(db, session)

    assert code == 0
    assert "good -> evaluation_signal (1 judgement(s))" in output
    assert "conflicted" not in output


def test_a_session_without_feedback_says_so(tmp_path):
    db = tmp_path / "core.db"
    session = converse(db, "hello")
    code, output = observations(db, session)
    assert code == 0
    assert "no feedback recorded" in output


def test_refusals_happen_before_anything_is_read(tmp_path):
    db = tmp_path / "core.db"
    code, output = run(["--ephemeral", "--session", "x", "--observations"], transport=no_model)
    assert code == 2 and "--ephemeral" in output
    code, output = run(["--database", str(db), "--observations"], transport=no_model)
    assert code == 2 and "needs --session" in output
    code, output = run(["--database", str(db), "--session", "x", "--observations"], transport=no_model)
    assert code == 2 and "no stored conversations" in output
    assert not db.exists()  # a read never creates the store
    session = converse(db, "hello")
    code, output = run(["--database", str(db), "--session", session, "--observations",
                        "--feedback", "good"], transport=no_model)
    assert code == 2 and "cannot be combined" in output
    code, output = observations(db, "no-such-session")
    assert code == 2 and "no such session" in output
