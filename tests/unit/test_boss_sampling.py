"""The Boss model's sampling reaches every generation (owner decision 2026-10-01).

Through the composition root, with a transport that keeps each payload, so
the assertion is on what Ollama would receive.
"""
from __future__ import annotations

import dataclasses

from personal_ai_core.conversation.factory import build_in_memory_service
from personal_ai_core.core.config import DEFAULT_BOSS_SAMPLING, Settings
from personal_ai_core.core.domain import EventType


class Recorder:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def __call__(self, url, payload, timeout):
        self.payloads.append(payload)
        return {"model": payload["model"], "message": {"content": "ok"}, "done_reason": "stop"}


def turn(settings: Settings | None = None, **send):
    model = Recorder()
    service, events = build_in_memory_service(settings or Settings.from_env({}), transport=model)
    session = service.start_session(service.create_user().id)
    service.send(session_id=session.id, content="hello", **send)
    return model.payloads[-1]["options"], events.list_for_session(session.id)


def test_every_turn_carries_the_configured_sampling_and_the_budget():
    options, _ = turn()
    for key, value in DEFAULT_BOSS_SAMPLING.items():
        assert options[key] == value
    assert isinstance(options["num_predict"], int)


def test_the_sampling_sent_is_recorded_on_the_turn():
    _, events = turn()
    requested = next(e for e in events if e.type is EventType.GENERATION_REQUESTED)
    assert requested.payload["sampling"] == dict(DEFAULT_BOSS_SAMPLING)


def test_a_caller_option_wins_over_the_configured_one():
    options, _ = turn(options={"temperature": 0.0})
    assert options["temperature"] == 0.0
    assert options["top_k"] == DEFAULT_BOSS_SAMPLING["top_k"]


def test_empty_sampling_sends_only_the_budget():
    settings = dataclasses.replace(Settings.from_env({}), boss_sampling={})
    options, events = turn(settings)
    assert set(options) == {"num_predict"}
    requested = next(e for e in events if e.type is EventType.GENERATION_REQUESTED)
    assert requested.payload["sampling"] == {}


def test_the_default_cannot_be_mutated_by_a_caller():
    """A shared default that one caller could edit would change every later turn."""
    try:
        DEFAULT_BOSS_SAMPLING["temperature"] = 2.0  # type: ignore[index]
    except TypeError:
        pass
    assert DEFAULT_BOSS_SAMPLING["temperature"] == 0.7
