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
    huge = "word " * 60_000

    with pytest.raises(ContextOverflowError):
        service.send(session_id=session.id, content=huge)

    assert len(transport.payloads) == sent_before, "an overcommitted turn must not be sent"
    refused = _assembled(events, session.id)[-1]
    # Everything before it was left out, and it still did not fit.
    assert refused["overcommitted"] is True
    assert refused["history_messages"] == 1
    assert refused["history_left_out"] == 2 * TURNS
    assert service.history(session.id)[-1].content == huge


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
