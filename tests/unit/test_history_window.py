"""A long session sends its newest messages, keeps them all, and says what it left out.

Gap analysis P1-3. Every turn read and sent the whole session. Since P0-1 a
turn that does not fit the window is refused rather than cut by the server,
so a long enough session refused every turn from then on, for good. Now the
oldest messages are left out of the prompt -- only of the prompt: the store
keeps every one -- and CONTEXT_ASSEMBLED records how many, their estimated
tokens and the first message that was sent. The window is deterministic, and
the turn is still refused when this turn's own message does not fit alone.
"""
from __future__ import annotations

from typing import Any, Mapping

import pytest

from personal_ai_core.context.token_estimator import ScriptAwareTokenEstimator
from personal_ai_core.conversation.factory import (
    build_grounded_in_memory_service,
    build_in_memory_service,
)
from personal_ai_core.conversation.service import window_history
from personal_ai_core.core.config import Settings
from personal_ai_core.core.domain import EventType, Message, Role
from personal_ai_core.core.errors import ContextOverflowError

# Room for about five exchanges of the turns below: small enough to fill in a
# few turns, large enough for the identity and the reserves.
SMALL = Settings.from_env({"PAC_BOSS_CONTEXT_WINDOW": "3072"})
REPLY = "reply " * 40
TURNS = 12

estimate = ScriptAwareTokenEstimator().estimate


class RecordingTransport:
    def __init__(self) -> None:
        self.payloads: list[Mapping[str, Any]] = []

    def __call__(self, url: str, payload: Mapping[str, Any], timeout: int):
        self.payloads.append(dict(payload))
        return {"model": payload["model"], "message": {"content": REPLY}}


def _question(i: int) -> str:
    return f"question {i} " + "word " * 60


def _assembled(events, session_id) -> list[Mapping[str, Any]]:
    return [e.payload for e in events.list_for_session(session_id)
            if e.type is EventType.CONTEXT_ASSEMBLED]


def _long_session(build: str = "default"):
    transport = RecordingTransport()
    if build == "default":
        service, events = build_in_memory_service(SMALL, transport=transport)
    else:
        grounded = build_grounded_in_memory_service(SMALL, transport=transport)
        service, events = grounded.service, grounded.events
    session = service.start_session(service.create_user().id)
    for i in range(TURNS):
        service.send(session_id=session.id, content=_question(i))
    return service, events, session, transport


# --- the session -------------------------------------------------------------------------


@pytest.mark.parametrize("build", ["default", "grounded"])
def test_a_long_session_keeps_answering_and_keeps_every_message(build):
    service, events, session, transport = _long_session(build)
    assert len(transport.payloads) == TURNS
    stored = service.history(session.id)
    assert len(stored) == 2 * TURNS
    assembled = _assembled(events, session.id)
    assert assembled[0]["history_left_out"] == 0
    assert assembled[-1]["history_left_out"] > 0
    assert not any(payload["overcommitted"] for payload in assembled)


@pytest.mark.parametrize("build", ["default", "grounded"])
def test_what_is_sent_is_the_newest_messages_from_a_question(build):
    service, events, session, transport = _long_session(build)
    last = _assembled(events, session.id)[-1]
    # The last turn saw every message but its own reply.
    seen = list(service.history(session.id))[:-1]
    sent = seen[last["history_left_out"]:]

    assert last["history_messages"] == len(sent)
    assert last["history_first_sent"] == sent[0].id
    assert sent[0].role is Role.USER
    assert last["history_tokens"] == sum(estimate(m.content) for m in sent)
    left_out = seen[:last["history_left_out"]]
    assert last["history_left_out_tokens"] == sum(estimate(m.content) for m in left_out)
    # The prompt is the identity (and any evidence), then exactly those messages.
    prompt = transport.payloads[-1]["messages"]
    conversation = prompt[len(prompt) - len(sent):]
    assert [(m["role"], m["content"]) for m in conversation] == [
        (m.role.value, m.content) for m in sent]
    assert all(m["role"] == "system" for m in prompt[:len(prompt) - len(sent)])
    assert _question(0) not in [m["content"] for m in prompt]


def test_the_same_session_gives_the_same_windows():
    def windows():
        _, events, session, transport = _long_session()
        recorded = [(p["history_messages"], p["history_left_out"],
                     p["history_left_out_tokens"], p["history_tokens"])
                    for p in _assembled(events, session.id)]
        return recorded, [p["messages"] for p in transport.payloads]

    assert windows() == windows()


def test_a_message_too_long_alone_is_refused_however_much_is_left_out():
    service, events, session, transport = _long_session()
    sent_before = len(transport.payloads)
    assembled_before = len(_assembled(events, session.id))
    huge = "word " * 60_000

    with pytest.raises(ContextOverflowError) as caught:
        service.send(session_id=session.id, content=huge)

    assert len(transport.payloads) == sent_before, "an overcommitted turn must not be sent"
    # R1: no history could make room, so the message is the cause, and it is
    # refused before it is stored: the session is as it was, and its next
    # turn is not refused because of it.
    assert caught.value.cause == "message"
    assert caught.value.history_tokens == estimate(huge)
    assert caught.value.fixed_tokens < caught.value.context_window
    assert f"your message is about {estimate(huge)} tokens; shorten it" in str(caught.value)
    assert "new session" not in str(caught.value)
    assert len(service.history(session.id)) == 2 * TURNS
    assert huge not in [m.content for m in service.history(session.id)]
    assert len(_assembled(events, session.id)) == assembled_before
    failed = [e.payload for e in events.list_for_session(session.id)
              if e.type is EventType.GENERATION_FAILED]
    assert failed[-1]["reason"] == "context_overcommitted"
    assert failed[-1]["cause"] == "message" and failed[-1]["stored"] is False
    assert failed[-1]["history_tokens"] == estimate(huge)

    service.send(session_id=session.id, content=_question(TURNS))
    assert len(transport.payloads) == sent_before + 1


# --- R1: which of the two does not fit ---------------------------------------------------


def _profiled(window: str, profile: str):
    import dataclasses

    transport = RecordingTransport()
    settings = dataclasses.replace(
        Settings.from_env({"PAC_BOSS_CONTEXT_WINDOW": window}), profile=profile)
    service, events = build_in_memory_service(settings, transport=transport)
    return service, events, transport


# Real prose, about 2,200 tokens, so it fills a 2048-token window on its own.
PROFILE = (
    "I am a structural engineer who works on bridges and long-span roofs. I keep my "
    "notes in plain text, I prefer short answers that show the arithmetic, and I "
    "check every load case by hand before I trust a program's output. "
) * 40


def test_a_profile_that_fills_the_window_says_so_before_anything_is_stored():
    service, events, transport = _profiled("2048", PROFILE)
    # Said before a session exists, so the CLI need not create one.
    with pytest.raises(ContextOverflowError) as before:
        service.check_room()
    assert before.value.cause == "fixed_reserves"

    session = service.start_session(service.create_user().id)
    with pytest.raises(ContextOverflowError) as caught:
        service.send(session_id=session.id, content="hello")

    error = caught.value
    assert error.cause == "fixed_reserves"
    assert error.fixed_tokens >= error.context_window == 2048
    assert str(error) == (
        f"the profile and fixed reserves need about {error.fixed_tokens} of the "
        "model's 2048 tokens; shorten the profile")
    assert "new session" not in str(error)
    assert transport.payloads == []
    assert list(service.history(session.id)) == []
    assert EventType.MESSAGE_RECEIVED not in [
        e.type for e in events.list_for_session(session.id)]


def test_a_profile_that_leaves_room_is_not_blamed_for_a_long_message():
    service, _, _ = _profiled("8192", "I prefer short answers.")
    service.check_room()  # room for a message: nothing raised
    session = service.start_session(service.create_user().id)
    with pytest.raises(ContextOverflowError) as caught:
        service.send(session_id=session.id, content="word " * 60_000)
    assert caught.value.cause == "message"
    assert "shorten the profile" not in str(caught.value)


def test_the_cli_names_the_profile_when_the_profile_is_the_cause():
    import io

    from personal_ai_core.app.cli import _converse
    from personal_ai_core.conversation.factory import build_reply_redactor

    service, _, _ = _profiled("2048", PROFILE)
    session = service.start_session(service.create_user().id)
    out = io.StringIO()
    code = _converse(service=service, session_id=session.id, language=None,
                     lines=iter(["hello"]), out=out, redactor=build_reply_redactor())
    assert code == 1
    (line,) = out.getvalue().splitlines()
    assert line.startswith("the turn was not sent: the profile and fixed reserves need about ")
    assert line.endswith(" of the model's 2048 tokens; shorten the profile.")
    assert "new session" not in out.getvalue()


# --- R2: one line, once, when messages start being left out --------------------------------


def test_the_terminal_says_once_that_older_messages_are_left_out():
    import io

    from personal_ai_core.app.cli import LEFT_OUT_NOTE, _converse
    from personal_ai_core.conversation.factory import build_reply_redactor

    transport = RecordingTransport()
    service, events = build_in_memory_service(SMALL, transport=transport)
    session = service.start_session(service.create_user().id)
    out = io.StringIO()
    code = _converse(service=service, session_id=session.id, language=None,
                     lines=iter([_question(i) for i in range(TURNS)]), out=out,
                     redactor=build_reply_redactor())
    assert code == 0

    left_out = [p["history_left_out"] for p in _assembled(events, session.id)]
    first = next(i for i, n in enumerate(left_out) if n)
    assert first < TURNS - 1, "the session must keep leaving messages out after it starts"
    notes = [line for line in out.getvalue().splitlines() if "no longer sent" in line]
    assert notes == [f"         {LEFT_OUT_NOTE.format(count=left_out[first])}"]
    assert notes[0] == (
        "         [older messages are no longer sent to the model (they are kept); "
        f"{left_out[first]} left out so far]")
    # Printed right after the reply of the turn that started it.
    lines = out.getvalue().splitlines()
    assert lines.index(notes[0]) == first + 1
    # Every turn's count is still on the record.
    assert all(n > 0 for n in left_out[first:])


def test_the_line_comes_back_only_after_the_count_resets():
    service, _, session, _ = _long_session()
    # The last turn of a long session is not the first to leave messages out.
    assert service.left_out_started(session.id) is None

    # A fresh service over the same store: the record, not memory, decides.
    first_turns = []
    transport = RecordingTransport()
    service, events = build_in_memory_service(SMALL, transport=transport)
    session = service.start_session(service.create_user().id)
    for i in range(TURNS):
        service.send(session_id=session.id, content=_question(i))
        first_turns.append(service.left_out_started(session.id))
    announced = [n for n in first_turns if n is not None]
    assert len(announced) == 1 and announced[0] > 0


# --- the window ----------------------------------------------------------------------------


def _messages(*roles: Role) -> list[Message]:
    return [Message(session_id="s", role=role, content=f"{role.value} {i}")
            for i, role in enumerate(roles)]


USER, ASSISTANT = Role.USER, Role.ASSISTANT


def test_nothing_is_left_out_when_everything_fits():
    history = _messages(USER, ASSISTANT, USER)
    window = window_history(history, estimate=len, fits=lambda tokens: True)
    assert window.sent == tuple(history)
    assert (window.left_out, window.left_out_tokens) == (0, 0)
    assert window.recorded()["history_first_sent"] == history[0].id


def test_the_oldest_go_first_and_the_window_starts_at_a_question():
    history = _messages(USER, ASSISTANT, USER, ASSISTANT, USER)
    sizes = [len(m.content) for m in history]
    # Room for the last three, so leaving out the first two would be enough;
    # leaving out one more makes the window start at a question.
    room = sum(sizes[-3:])
    window = window_history(history, estimate=len, fits=lambda tokens: tokens <= room)
    assert window.sent == tuple(history[2:])
    assert window.left_out == 2 and window.left_out_tokens == sum(sizes[:2])

    room = sum(sizes[-4:])  # starts at an answer: that answer goes too
    window = window_history(history, estimate=len, fits=lambda tokens: tokens <= room)
    assert window.sent == tuple(history[2:])
    assert window.sent[0].role is USER


def test_this_turns_message_is_always_kept():
    history = _messages(USER, ASSISTANT, USER)
    window = window_history(history, estimate=len, fits=lambda tokens: False)
    assert window.sent == (history[-1],)
    assert window.left_out == 2


def test_an_empty_history_is_an_empty_window():
    window = window_history([], estimate=len, fits=lambda tokens: True)
    assert window.sent == () and window.left_out == 0
    assert window.recorded()["history_first_sent"] is None
