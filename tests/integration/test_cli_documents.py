"""`pac --documents`: retrieval reaches the entry point -- Finding F-2.

Before this, `pac` composed identity and conversation only. Retrieval was
built, tested and unreachable: `build_grounded_in_memory_service` had no
caller outside the tests, so the one path a user runs never grounded a turn.

The open question F-2 had to answer was the one `build_persistent_service`
wrote down: when is the corpus re-ingested, given that knowledge is derived
and not stored (ADR-010 R4)? The answer these tests pin: **on every run, from
the paths named on the command line.** The conversation is durable; the
corpus is not; and the command says so.
"""
from __future__ import annotations

import io
from typing import Any, Mapping

from personal_ai_core.app.cli import _document_id, main
from personal_ai_core.context import ScriptAwareTokenEstimator
from personal_ai_core.conversation.factory import build_persistent_service
from personal_ai_core.conversation.grounding import GROUNDING_PREAMBLE
from personal_ai_core.core.domain import EventType

NOTE = "Reciprocal rank fusion combines rankings using ranks, not scores."
ARABIC = "دمج الرتب المتبادلة يوحد الترتيبين باستخدام الرتب وليس الدرجات."


class Recorder:
    def __init__(self):
        self.payloads: list[Mapping[str, Any]] = []

    def __call__(self, url, payload, timeout):
        self.payloads.append(payload)
        return {"model": payload["model"], "message": {"content": "ok"}}

    def evidence(self):
        """The evidence message of the last call, or None."""
        return next(
            (
                m["content"]
                for m in self.payloads[-1]["messages"]
                if m["role"] == "system" and GROUNDING_PREAMBLE in m["content"]
            ),
            None,
        )


def run(argv, *, lines=("how does reciprocal rank fusion combine rankings?",), env=None):
    transport = Recorder()
    out = io.StringIO()
    code = main(
        argv, transport=transport, stdin=iter(lines), stdout=out, env=env or {}
    )
    return code, out.getvalue(), transport


def session_of(output: str) -> str:
    return next(
        line.split("session:")[1].strip()
        for line in output.splitlines()
        if line.startswith("session:")
    )


def notes(tmp_path):
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "rrf.md").write_text(NOTE + "\n", encoding="utf-8")
    return folder


# --- it grounds -----------------------------------------------------------


def test_documents_reach_the_prompt(tmp_path):
    folder = notes(tmp_path)
    code, output, transport = run(
        ["--database", str(tmp_path / "core.db"), "--documents", str(folder)]
    )
    assert code == 0
    evidence = transport.evidence()
    assert evidence is not None, "the prompt carries no evidence block"
    assert NOTE in evidence
    # The citation resolves to the file that was read.
    assert (folder / "rrf.md").resolve().as_uri() in evidence
    assert "documents: 1 file(s)" in output


def test_without_documents_nothing_is_retrieved(tmp_path):
    """The path that already worked is unchanged: no flag, no retrieval."""
    code, _, transport = run(["--database", str(tmp_path / "core.db")])
    assert code == 0
    assert transport.evidence() is None
    assert [m["role"] for m in transport.payloads[-1]["messages"]] == ["system", "user"]


def test_ephemeral_runs_ground_too(tmp_path):
    code, _, transport = run(["--ephemeral", "--documents", str(notes(tmp_path))])
    assert code == 0
    assert transport.evidence() is not None


def test_an_arabic_document_answers_an_arabic_question(tmp_path):
    (tmp_path / "ar.txt").write_text(ARABIC, encoding="utf-8")
    code, _, transport = run(
        ["--ephemeral", "--documents", str(tmp_path / "ar.txt")],
        lines=["كيف يعمل دمج الرتب المتبادلة؟"],
    )
    assert code == 0
    evidence = transport.evidence()
    assert evidence is not None and ARABIC in evidence


# --- the corpus is read every run, and only then ---------------------------


def test_the_conversation_is_kept_and_the_corpus_is_not(tmp_path):
    """The decision F-2 records, pinned from both sides."""
    database = str(tmp_path / "core.db")
    folder = notes(tmp_path)
    _, first, _ = run(["--database", database, "--documents", str(folder)])
    session = session_of(first)

    # Same database, same session, no --documents: the conversation continues
    # and nothing is retrieved -- the index did not survive the process.
    code, _, transport = run(["--database", database, "--session", session])
    assert code == 0
    assert transport.evidence() is None

    # Named again, the documents are read again.
    code, _, transport = run(
        ["--database", database, "--session", session, "--documents", str(folder)]
    )
    assert code == 0
    assert transport.evidence() is not None


def test_a_file_named_twice_is_one_document(tmp_path):
    """Directly and through its directory. With a random document id it
    would be indexed twice and could take two of the few evidence slots."""
    database = tmp_path / "core.db"
    folder = notes(tmp_path)
    code, output, _ = run(
        [
            "--database", str(database),
            "--documents", str(folder),
            "--documents", str(folder / "rrf.md"),
        ]
    )
    assert code == 0
    assert "documents: 1 file(s)" in output

    slice_ = build_persistent_service(database=database)
    try:
        [assembled] = [
            e
            for e in slice_.events.list_for_session(session_of(output))
            if e.type is EventType.CONTEXT_ASSEMBLED
        ]
    finally:
        slice_.close()
    assert assembled.payload["retrieved"] == 1


def test_the_document_id_is_derived_from_the_uri():
    uri = "file:///notes/rrf.md"
    assert _document_id(uri) == _document_id(uri)
    assert _document_id(uri) != _document_id("file:///notes/other.md")


# --- what a directory contributes, and what is refused -------------------


def test_a_directory_contributes_only_text_files(tmp_path):
    folder = notes(tmp_path)
    (folder / "deep").mkdir()
    (folder / "deep" / "more.txt").write_text("more text here", encoding="utf-8")
    (folder / "image.png").write_bytes(b"\x89PNG\r\n")
    (folder / "data.json").write_text('{"a": 1}', encoding="utf-8")
    code, output, _ = run(["--ephemeral", "--documents", str(folder)])
    assert code == 0
    assert "documents: 2 file(s)" in output


def test_a_file_named_explicitly_is_read_whatever_its_suffix(tmp_path):
    named = tmp_path / "notes.rst"
    named.write_text(NOTE, encoding="utf-8")
    code, output, transport = run(["--ephemeral", "--documents", str(named)])
    assert code == 0
    assert "documents: 1 file(s)" in output
    assert transport.evidence() is not None


def test_a_missing_path_stops_the_command_before_anything_is_opened(tmp_path):
    """A typo must not quietly shrink the corpus -- or create a database."""
    database = tmp_path / "core.db"
    code, output, transport = run(
        ["--database", str(database), "--documents", str(tmp_path / "nope")]
    )
    assert code == 2
    assert "no such file or directory" in output
    assert not database.exists()
    assert transport.payloads == []


def test_a_file_that_is_not_utf8_is_skipped_and_said(tmp_path):
    folder = notes(tmp_path)
    (folder / "latin1.txt").write_bytes("caf\xe9".encode("latin-1"))
    code, output, transport = run(["--ephemeral", "--documents", str(folder)])
    assert code == 0
    assert "skipped:" in output and "latin1.txt" in output and "not UTF-8" in output
    assert "documents: 1 file(s)" in output
    assert transport.evidence() is not None


# --- F-4 holds through the entry point ------------------------------------


def test_the_evidence_pac_sends_fits_the_budget_it_recorded(tmp_path):
    """Through `pac`, on the durable store: the CONTEXT_ASSEMBLED event is
    read back from SQLite, and the evidence message the model received fits
    the budget that event records."""
    database = tmp_path / "core.db"
    folder = notes(tmp_path)
    for i in range(12):
        (folder / f"note{i}.md").write_text(
            "\n\n".join(f"Rank fusion note {i}-{j}: {NOTE}" for j in range(4)),
            encoding="utf-8",
        )
    # A window small enough that the budget binds. At the default window
    # everything fits, and "fits" would prove nothing.
    code, output, transport = run(
        ["--database", str(database), "--documents", str(folder)],
        env={"PAC_BOSS_CONTEXT_WINDOW": "2000"},
    )
    assert code == 0

    slice_ = build_persistent_service(database=database)
    try:
        [assembled] = [
            e
            for e in slice_.events.list_for_session(session_of(output))
            if e.type is EventType.CONTEXT_ASSEMBLED
        ]
    finally:
        slice_.close()

    assert assembled.payload["dropped"] >= 1, "the budget did not bind"
    evidence = transport.evidence()
    assert evidence is not None
    assert ScriptAwareTokenEstimator().estimate(evidence) <= assembled.payload["budget_tokens"]
