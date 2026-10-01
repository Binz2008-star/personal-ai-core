"""ADR-019 unit 2: the guard inside a conversation turn.

Through the composition root, with a transport that scripts each reply and
keeps each payload, so the assertions are on what Ollama would receive and
what the turn recorded.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from personal_ai_core.context.token_estimator import ScriptAwareTokenEstimator
from personal_ai_core.conversation.factory import (
    build_grounded_in_memory_service,
    build_in_memory_service,
)
from personal_ai_core.conversation.language_guard import GUARD_NOTE
from personal_ai_core.core.config import Settings
from personal_ai_core.core.domain import EventType
from personal_ai_core.core.errors import ProviderError

QUESTION = "ما الفرق بين الذاكرة قصيرة المدى والذاكرة طويلة المدى؟"
CHINESE = "。提供的信息中没有提到您的笔记本电脑的序列号。"
ARABIC = "الذاكرة قصيرة المدى تحتفظ بالمعلومات لفترة قصيرة، والطويلة لفترات أطول."


class Scripted:
    def __init__(self, *replies: str | Exception) -> None:
        self.replies = list(replies)
        self.payloads: list[dict] = []

    def __call__(self, url, payload, timeout):
        self.payloads.append(payload)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {"model": payload["model"], "message": {"content": reply}, "done_reason": "stop"}


def turn(model, content=QUESTION, settings=None):
    service, events = build_in_memory_service(settings or Settings.from_env({}), transport=model)
    session = service.start_session(service.create_user().id)
    reply = service.send(session_id=session.id, content=content)
    return reply, events.list_for_session(session.id), service, session.id


def guard_events(events):
    return [e for e in events if e.type is EventType.REPLY_LANGUAGE_GUARD]


def test_a_chinese_draft_is_generated_again_and_the_arabic_reply_delivered():
    model = Scripted(CHINESE, ARABIC)
    reply, events, _, _ = turn(model)
    assert reply.content == ARABIC
    assert len(model.payloads) == 2
    second = model.payloads[1]["messages"]
    assert second[-1] == {"role": "system", "content": GUARD_NOTE}
    # the rejected draft is never shown to the model
    assert CHINESE not in json.dumps(second, ensure_ascii=False)
    [event] = guard_events(events)
    assert event.payload["expected"] == "arabic"
    assert event.payload["delivered_passed"] is True
    assert event.payload["rejected_counts"]["han"] > 0
    assert "rejected_prompt_tokens" in event.payload
    assert "rejected_completion_tokens" in event.payload


def test_the_retry_is_requested_on_the_record_as_attempt_two():
    """ADR-019 §3.3, which the first implementation did not follow: the second
    generation records its own GENERATION_REQUESTED with attempt 2."""
    model = Scripted(CHINESE, ARABIC)
    _, events, _, _ = turn(model)
    requested = [e for e in events if e.type is EventType.GENERATION_REQUESTED]
    assert len(requested) == 2
    first, second = (dict(e.payload) for e in requested)
    assert "attempt" not in first and second["attempt"] == 2
    assert second["message_count"] == first["message_count"] + 1
    assert second["sampling"] == first["sampling"]


def test_a_good_reply_is_requested_once():
    model = Scripted(ARABIC)
    _, events, _, _ = turn(model)
    assert len([e for e in events if e.type is EventType.GENERATION_REQUESTED]) == 1


def test_the_guard_event_carries_counts_never_text():
    model = Scripted(CHINESE, ARABIC)
    _, events, _, _ = turn(model)
    text = json.dumps([dict(e.payload) for e in events], ensure_ascii=False)
    assert "提供" not in text
    assert "الذاكرة قصيرة المدى تحتفظ" not in text


def test_the_rejected_draft_is_not_stored_as_a_message():
    model = Scripted(CHINESE, ARABIC)
    _, _, service, session_id = turn(model)
    stored = [m.content for m in service._messages.list_for_session(session_id)]
    assert CHINESE not in stored and ARABIC in stored


def test_a_second_failure_is_delivered_and_recorded_with_no_third_try():
    model = Scripted(CHINESE, CHINESE + "!")
    reply, events, _, _ = turn(model)
    assert reply.content == CHINESE + "!"
    assert len(model.payloads) == 2
    [event] = guard_events(events)
    assert event.payload["delivered_passed"] is False


def test_a_good_reply_costs_one_generation_and_no_event():
    model = Scripted(ARABIC)
    reply, events, _, _ = turn(model)
    assert reply.content == ARABIC and len(model.payloads) == 1
    assert guard_events(events) == []


def test_a_request_for_another_language_is_left_alone():
    model = Scripted("早上好")
    reply, events, _, _ = turn(model, content="ترجم إلى الصينية: صباح الخير يا صديقي")
    assert reply.content == "早上好" and len(model.payloads) == 1
    assert guard_events(events) == []


def test_the_guard_can_be_turned_off():
    settings = dataclasses.replace(Settings.from_env({}), language_guard=False)
    model = Scripted(CHINESE)
    reply, events, _, _ = turn(model, settings=settings)
    assert reply.content == CHINESE and len(model.payloads) == 1


@pytest.mark.parametrize(("value", "expected"), [("0", False), ("off", False), ("1", True)])
def test_pac_language_guard_reads_from_the_environment(value, expected):
    assert Settings.from_env({"PAC_LANGUAGE_GUARD": value}).language_guard is expected
    assert Settings.from_env({}).language_guard is True


def test_a_failed_retry_is_recorded_as_attempt_two_and_raised():
    model = Scripted(CHINESE, ProviderError("ollama request failed: timed out"))
    service, events = build_in_memory_service(Settings.from_env({}), transport=model)
    session = service.start_session(service.create_user().id)
    with pytest.raises(ProviderError):
        service.send(session_id=session.id, content=QUESTION)
    recorded = events.list_for_session(session.id)
    failed = [e for e in recorded if e.type is EventType.GENERATION_FAILED]
    assert [e.payload["attempt"] for e in failed] == [2]
    # Review of 2026-10-01: the guard's trigger was lost on this path.
    [guard] = [e for e in recorded if e.type is EventType.REPLY_LANGUAGE_GUARD]
    assert guard.payload["retry_failed"] is True
    assert guard.payload["delivered_passed"] is None
    assert guard.payload["rejected_counts"]["han"] > 0


def test_the_note_is_reserved_in_the_budget_as_its_own_share(tmp_path):
    expected = ScriptAwareTokenEstimator().estimate(GUARD_NOTE)
    for language_guard, reserve in ((True, expected), (False, 0)):
        settings = dataclasses.replace(Settings.from_env({}), language_guard=language_guard)
        slice_ = build_grounded_in_memory_service(settings, transport=Scripted(ARABIC))
        session = slice_.service.start_session(slice_.service.create_user().id)
        slice_.service.send(session_id=session.id, content=QUESTION)
        assembled = next(
            e for e in slice_.events.list_for_session(session.id)
            if e.type is EventType.CONTEXT_ASSEMBLED
        )
        assert assembled.payload["guard_reserve"] == reserve
        if reserve:
            assert f"guard={reserve}" in assembled.payload["budget_source"]


def test_the_event_carries_the_counts_the_verdict_was_computed_on():
    """Review of 2026-10-01: raw counts alone cannot reproduce a share verdict
    once quoted Latin is set aside."""
    english = "The notes do not mention it at all, sorry."
    model = Scripted(english, ARABIC)
    _, events, _, _ = turn(model)
    [event] = guard_events(events)
    assert event.payload["rejected_assessed_counts"] == event.payload["rejected_counts"]
    assert event.payload["retry_failed"] is False
