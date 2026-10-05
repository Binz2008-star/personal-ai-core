"""Every turn is measured against the window, and an overcommitted turn is not sent.

P0-2: the default `pac` path (no retrieval) used to send its whole history
unmeasured, with no allocation and no CONTEXT_ASSEMBLED event, so a turn's size
against the window could not be read back. Now every turn has an allocation,
from the same policy and the same estimate of the history the grounded path
uses, and records it.

P0-1: `ContextAllocation.overcommitted` was computed and recorded but nothing
read it, so a conversation longer than the window was sent anyway and cut by
the server without a word (ADR-005: no silent overflow). Now such a turn is
refused before the provider is called, recorded as a failed generation, and
the caller gets ContextOverflowError.
"""
from __future__ import annotations

import io
from typing import Any, Mapping

import pytest

from personal_ai_core.app.cli import _converse
from personal_ai_core.context.token_estimator import ScriptAwareTokenEstimator
from personal_ai_core.conversation.factory import (
    build_grounded_in_memory_service,
    build_in_memory_service,
    build_reply_redactor,
)
from personal_ai_core.core.domain import EventType
from personal_ai_core.core.errors import ContextOverflowError


class RecordingTransport:
    def __init__(self) -> None:
        self.payloads: list[Mapping[str, Any]] = []

    def __call__(self, url: str, payload: Mapping[str, Any], timeout: int):
        self.payloads.append(dict(payload))
        return {"model": payload["model"], "message": {"content": "ok"}}


def _assembled(events, session_id):
    return [e.payload for e in events.list_for_session(session_id)
            if e.type is EventType.CONTEXT_ASSEMBLED]


# --- P0-2: the default path is measured -----------------------------------------------------


def test_the_default_path_records_an_allocation_from_the_actual_history():
    transport = RecordingTransport()
    service, events = build_in_memory_service(transport=transport)
    session = service.start_session(service.create_user().id)
    service.send(session_id=session.id, content="hello there")
    service.send(session_id=session.id, content="and a second question")

    estimate = ScriptAwareTokenEstimator().estimate
    first, second = _assembled(events, session.id)
    # Turn 1 sees one message; turn 2 sees user, reply, user.
    assert first["history_messages"] == 1 and second["history_messages"] == 3
    assert first["history_tokens"] == estimate("hello there")
    assert second["history_tokens"] == sum(
        estimate(t) for t in ("hello there", "ok", "and a second question"))
    for payload in (first, second):
        assert payload["grounded"] is False and payload["overcommitted"] is False
        assert payload["estimator"] == ScriptAwareTokenEstimator().model_id
        assert payload["context_window"] > 0 and payload["generation_reserve"] > 0


def test_the_limit_sent_is_the_turns_own_allocation_on_the_default_path():
    transport = RecordingTransport()
    service, events = build_in_memory_service(transport=transport)
    session = service.start_session(service.create_user().id)
    service.send(session_id=session.id, content="hello")
    (assembled,) = _assembled(events, session.id)
    assert transport.payloads[-1]["options"]["num_predict"] == assembled["generation_reserve"]


# --- P0-1: an overcommitted turn is refused, never sent ---------------------------------------


@pytest.mark.parametrize("build", ["default", "grounded"])
def test_an_overcommitted_turn_is_refused_before_the_provider_is_called(build):
    transport = RecordingTransport()
    if build == "default":
        service, events = build_in_memory_service(transport=transport)
    else:
        grounded = build_grounded_in_memory_service(transport=transport)
        service, events = grounded.service, grounded.events
    session = service.start_session(service.create_user().id)
    # Far longer than an 8192-token window, whatever the estimator's rate.
    huge = "word " * 60_000

    with pytest.raises(ContextOverflowError) as caught:
        service.send(session_id=session.id, content=huge)

    assert transport.payloads == [], "an overcommitted turn must not reach the provider"
    assert caught.value.spoken_for > caught.value.context_window
    recorded = [e.type for e in events.list_for_session(session.id)]
    assert EventType.GENERATION_REQUESTED not in recorded
    assert EventType.GENERATION_COMPLETED not in recorded
    failed = [e.payload for e in events.list_for_session(session.id)
              if e.type is EventType.GENERATION_FAILED]
    assert len(failed) == 1 and failed[0]["reason"] == "context_overcommitted"
    assert failed[0]["spoken_for"] == caught.value.spoken_for
    # The measurement that refused it is on the record (P1-3 R1: it is made
    # before anything is stored, so there is no CONTEXT_ASSEMBLED to flag).
    estimate = ScriptAwareTokenEstimator().estimate
    assert failed[0]["history_tokens"] == estimate(huge) == caught.value.history_tokens
    assert failed[0]["cause"] == "message" and failed[0]["stored"] is False
    assert failed[0]["spoken_for"] == failed[0]["fixed_tokens"] + estimate(huge)
    assert _assembled(events, session.id) == []
    # R1: the message is NOT kept. Storing it left a session with a message
    # in it that no turn could send; nothing was received into the session.
    assert list(service.history(session.id)) == []
    assert EventType.MESSAGE_RECEIVED not in recorded


def test_the_cli_says_what_happened_instead_of_a_traceback():
    service, _ = build_in_memory_service(transport=RecordingTransport())
    session = service.start_session(service.create_user().id)
    out = io.StringIO()
    code = _converse(service=service, session_id=session.id, language=None,
                     lines=iter(["word " * 60_000]), out=out,
                     redactor=build_reply_redactor())
    assert code == 1
    # Older messages are left out to make room (P1-3), so a refused turn is
    # the message itself (R1): a new session would not help.
    tokens = ScriptAwareTokenEstimator().estimate(("word " * 60_000).strip())  # as typed
    (line,) = out.getvalue().splitlines()
    assert line.startswith(
        f"the turn was not sent: your message is about {tokens} tokens; shorten it "
        "(the model's window is 8192 tokens, and the profile and fixed reserves need about ")
    assert line.endswith(" of them).")
    assert "new session" not in out.getvalue()
    assert "shorten the profile" not in out.getvalue()
