"""ADR-018 unit 2: secrets are withheld before the model sees the evidence.

Driven through the real composition root (`build_grounded_in_memory_service`,
the path `pac --documents` uses), with a fake transport that keeps every
payload, so the assertion is on exactly what reached the model.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from personal_ai_core.app.cli import _ingest
from personal_ai_core.context import NullRedactor, PatternSecretRedactor
from personal_ai_core.context.token_estimator import ScriptAwareTokenEstimator
from personal_ai_core.conversation import grounding
from personal_ai_core.conversation.factory import build_grounded_in_memory_service
from personal_ai_core.conversation.grounding import (
    GROUNDING_PREAMBLE,
    MEMORY_PREAMBLE,
    RenderedEvidenceCost,
    render_evidence,
    render_memories,
)
from personal_ai_core.core.domain import EventType
from personal_ai_core.core.knowledge import (
    Chunk,
    RetrievalMethod,
    RetrievalProvenance,
    RetrievalResult,
)
from personal_ai_core.core.memory import (
    MemoryEvidence,
    MemoryProvenance,
    MemoryRecord,
    MemoryStatus,
    MemoryType,
)
from personal_ai_core.core.redaction import RedactionError

SECRET = "PLANTED-9d27c1f4"
DEPLOY = (
    "Deployment notes.\n"
    f"DATABASE_URL=postgres://app:{SECRET}@db.internal:5432/app\n"
    "The service restarts every night at 02:00.\n"
)
MARKER = "[withheld: secret]"


class Recorder:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def __call__(self, url, payload, timeout):
        self.payloads.append(payload)
        return {"model": payload["model"], "message": {"content": "ok"}, "done_reason": "stop"}

    def sent(self) -> str:
        return json.dumps(self.payloads, ensure_ascii=False)


def grounded_turn(tmp_path: Path, document: str, question: str):
    model = Recorder()
    slice_ = build_grounded_in_memory_service(transport=model)
    doc = tmp_path / "deployment.md"
    doc.write_text(document, encoding="utf-8")
    _ingest(slice_.ingestion, [doc], io.StringIO())
    service = slice_.service
    session = service.start_session(service.create_user().id)
    service.send(session_id=session.id, content=question)
    return model, slice_, session.id


def test_the_secret_never_reaches_the_model_and_the_rest_of_the_document_does(tmp_path):
    model, _, _ = grounded_turn(tmp_path, DEPLOY, "What is in my deployment notes?")
    sent = model.sent()
    assert SECRET not in sent
    assert SECRET.casefold() not in sent.casefold()
    assert MARKER in sent
    assert "db.internal:5432" in sent  # only the value is withheld
    assert "02:00" in sent  # the non-secret fact is still there


def test_the_audit_event_counts_withheld_values_and_never_carries_them(tmp_path):
    _, slice_, session_id = grounded_turn(tmp_path, DEPLOY, "What is in my deployment notes?")
    assembled = [
        e for e in slice_.events.list_for_session(session_id)
        if e.type is EventType.CONTEXT_ASSEMBLED
    ]
    assert len(assembled) == 1
    payload = assembled[0].payload
    assert payload["redactions"] == {"url_userinfo": 1}
    assert SECRET not in json.dumps(dict(payload))


def test_a_turn_with_nothing_to_withhold_records_empty_redactions(tmp_path):
    _, slice_, session_id = grounded_turn(
        tmp_path, "Project notes.\nThe codename is Nile.\n", "What is the codename in my notes?"
    )
    payload = next(
        e.payload for e in slice_.events.list_for_session(session_id)
        if e.type is EventType.CONTEXT_ASSEMBLED
    )
    assert payload["redactions"] == {}


def test_the_stored_chunk_is_untouched(tmp_path):
    """Redaction is a render step: the index keeps the user's own text."""
    _, slice_, _ = grounded_turn(tmp_path, DEPLOY, "What is in my deployment notes?")
    stored = " ".join(chunk.text for chunk in slice_.catalog)
    assert SECRET in stored


class Exploding:
    def redact(self, text):
        raise RedactionError(RedactionError.INTERNAL)


def test_a_redactor_failure_fails_the_turn_without_sending_or_recording_the_text(
    tmp_path, monkeypatch
):
    """D5: no fallback to the raw text; the failure event quotes nothing."""
    monkeypatch.setattr(
        "personal_ai_core.conversation.factory.PatternSecretRedactor", Exploding
    )
    model = Recorder()
    slice_ = build_grounded_in_memory_service(transport=model)
    doc = tmp_path / "deployment.md"
    doc.write_text(DEPLOY, encoding="utf-8")
    _ingest(slice_.ingestion, [doc], io.StringIO())
    service = slice_.service
    session = service.start_session(service.create_user().id)

    with pytest.raises(RedactionError):
        service.send(session_id=session.id, content="What is in my deployment notes?")

    assert model.payloads == []  # the model was never called
    events = json.dumps(
        [dict(e.payload) for e in slice_.events.list_for_session(session.id)],
        ensure_ascii=False,
    )
    assert SECRET not in events
    assert "internal" in events


# --- the budget is charged on the text that is sent -----------------------------

ESTIMATOR = ScriptAwareTokenEstimator()


def passage(text: str, chunk_id: str = "c1") -> RetrievalResult:
    chunk = Chunk(document_id="d1", version_id="v1", text=text, ordinal=0, start=0,
                  end=len(text), id=chunk_id)
    return RetrievalResult(
        chunk=chunk,
        provenance=RetrievalProvenance(
            document_id="d1", version_id="v1", chunk_id=chunk_id, start=0, end=len(text),
            methods=(RetrievalMethod.LEXICAL,), source_uri="file:///deploy.md",
        ),
    )


def test_a_marker_longer_than_its_secret_is_still_charged_in_full():
    """`DB_PASSWORD=a` becomes `DB_PASSWORD=[withheld: secret]`: the rendered
    text grows, and the charge must grow with it (ADR-018 §3.4)."""
    redactor = PatternSecretRedactor()
    results = [passage(f"DB_PASSWORD=a{i}\nnote {i}", chunk_id=f"c{i}") for i in range(12)]
    cost = RenderedEvidenceCost(ESTIMATOR, redactor)
    charged = cost.document_section() + sum(cost.document(r) for r in results)
    section = f"{GROUNDING_PREAMBLE}\n\n{render_evidence(results, redactor)}"
    assert MARKER in section
    assert ESTIMATOR.estimate(section) <= charged
    # and the redacted section really costs more than the raw one would have
    raw = f"{GROUNDING_PREAMBLE}\n\n{render_evidence(results, NullRedactor())}"
    assert ESTIMATOR.estimate(section) > ESTIMATOR.estimate(raw)


def test_memories_are_withheld_too():
    """D4: the memory section is in scope."""
    evidence = MemoryEvidence(
        record=MemoryRecord(
            session_id="s1", type=MemoryType.PREFERENCES,
            content=f"my staging DB_PASSWORD={SECRET}", language="en",
            provenance=MemoryProvenance(session_id="s1", event_id="e1",
                                        promoted_by="rule:explicit_instruction"),
            status=MemoryStatus.ACTIVE, confidence=0.9,
        ),
        relevance=0.9,
    )
    tally: dict[str, int] = {}
    rendered = f"{MEMORY_PREAMBLE}\n\n" + render_memories(
        [evidence], PatternSecretRedactor(), tally=tally
    )
    assert SECRET not in rendered and MARKER in rendered
    assert tally == {"assignment": 1}


def test_the_renderers_have_no_default_redactor():
    """I1: a forgotten redactor is a TypeError, never a silent raw path."""
    with pytest.raises(TypeError):
        render_evidence([passage("x")])  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        grounding.RenderedEvidenceCost(ESTIMATOR)  # type: ignore[call-arg]


def test_the_preamble_explains_the_marker_with_the_marker_itself():
    """D3: the sentence names the exact marker the redactor writes, so the two
    cannot drift apart."""
    from personal_ai_core.context.redaction import REDACTION_MARKER

    assert REDACTION_MARKER in GROUNDING_PREAMBLE
    assert "never guess" in GROUNDING_PREAMBLE


def test_a_secret_named_in_a_sentence_never_reaches_the_model(tmp_path):
    """ADR-018 amendment 1: contract_v1's secret-prose-en, which leaked in
    every rig run under version 1, through the real composition root."""
    staging = (
        "Staging notes.\n"
        "The staging server is rebuilt every Friday evening.\n"
        "The staging password is hunter2-PLANTED-71c3.\n"
    )
    model, slice_, session_id = grounded_turn(
        tmp_path, staging, "What is the staging password in my staging notes?"
    )
    sent = model.sent()
    assert "hunter2-PLANTED-71c3" not in sent
    assert MARKER in sent
    assert "Friday evening" in sent
    payload = next(
        e.payload for e in slice_.events.list_for_session(session_id)
        if e.type is EventType.CONTEXT_ASSEMBLED
    )
    assert payload["redactions"] == {"prose": 1}
