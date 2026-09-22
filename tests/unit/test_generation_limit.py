"""The generation reserve reaches the provider (ADR-011 prerequisite B).

Before this, `generation_reserve` was accounting-only: computed, recorded on
`ContextAllocation`, and never sent. A number the provider never sees reserves
nothing. These tests assert on the payload the transport actually received,
because that is the only place the claim can be checked -- asserting on the
allocation would re-check the accounting that was already true.
"""
from __future__ import annotations

from typing import Any, Mapping

from personal_ai_core.context.budget import (
    DEFAULT_GENERATION_RESERVE,
    ReserveBasedBudgetPolicy,
)
from personal_ai_core.conversation.factory import (
    build_grounded_in_memory_service,
    build_in_memory_service,
)
from personal_ai_core.core.config import Settings
from personal_ai_core.core.domain import EventType


class RecordingTransport:
    """Captures the payload, so the assertion is on what was sent."""

    def __init__(self) -> None:
        self.payloads: list[Mapping[str, Any]] = []

    def __call__(self, url: str, payload: Mapping[str, Any], timeout: int):
        self.payloads.append(dict(payload))
        return {"model": payload["model"], "message": {"content": "ok"}}

    @property
    def last(self) -> Mapping[str, Any]:
        assert self.payloads, "the provider was never called"
        return self.payloads[-1]


def _turn(service, transport, **send_kwargs):
    user = service.create_user()
    session = service.start_session(user.id)
    service.send(session_id=session.id, content="مرحبا", **send_kwargs)
    return transport.last


# --- the ungrounded path: no allocation exists, the limit must still be sent -

def test_the_ungrounded_path_sends_the_reserve_as_num_predict():
    """The path with no retrieval wired, and the one the live smoke test uses.

    It has no `ContextAllocation` to read, which is exactly why enforcement
    could have been missed here.
    """
    transport = RecordingTransport()
    service, _ = build_in_memory_service(transport=transport)
    payload = _turn(service, transport)

    assert payload["options"]["num_predict"] == DEFAULT_GENERATION_RESERVE


def test_the_grounded_path_sends_the_reserve_as_num_predict():
    transport = RecordingTransport()
    slice_ = build_grounded_in_memory_service(transport=transport)
    payload = _turn(slice_.service, transport)

    assert payload["options"]["num_predict"] == DEFAULT_GENERATION_RESERVE


def test_both_paths_agree_on_the_limit():
    """The equivalence `_generation_limit`'s docstring asserts.

    One path reads the turn's allocation, the other asks the policy directly.
    They must not drift, and stating it in a docstring is not checking it.
    """
    ungrounded_transport = RecordingTransport()
    ungrounded, _ = build_in_memory_service(transport=ungrounded_transport)

    grounded_transport = RecordingTransport()
    grounded = build_grounded_in_memory_service(transport=grounded_transport)

    a = _turn(ungrounded, ungrounded_transport)["options"]["num_predict"]
    b = _turn(grounded.service, grounded_transport)["options"]["num_predict"]
    assert a == b


# --- the number is derived, not hard-coded ---------------------------------

def test_the_limit_follows_the_policy_rather_than_a_constant():
    """A different reserve must produce a different limit.

    Without this, `num_predict = 1024` written as a literal would pass every
    other test in this file.
    """
    transport = RecordingTransport()
    service, _ = build_in_memory_service(transport=transport)
    service._budget_policy = ReserveBasedBudgetPolicy(generation_reserve=77)

    payload = _turn(service, transport)
    assert payload["options"]["num_predict"] == 77
    assert payload["options"]["num_predict"] != DEFAULT_GENERATION_RESERVE


# --- caller precedence ------------------------------------------------------

def test_an_explicit_caller_limit_wins():
    """A caller naming num_predict has said something more specific.

    Overriding it silently would make the `options` parameter a lie.
    """
    transport = RecordingTransport()
    service, _ = build_in_memory_service(transport=transport)
    payload = _turn(service, transport, options={"num_predict": 5})

    assert payload["options"]["num_predict"] == 5


def test_caller_options_are_preserved_alongside_the_limit():
    transport = RecordingTransport()
    service, _ = build_in_memory_service(transport=transport)
    payload = _turn(service, transport, options={"temperature": 0.1})

    assert payload["options"]["temperature"] == 0.1
    assert payload["options"]["num_predict"] == DEFAULT_GENERATION_RESERVE


def test_the_caller_mapping_is_not_mutated():
    """`options` is a Mapping the caller owns; writing into it is a side effect."""
    transport = RecordingTransport()
    service, _ = build_in_memory_service(transport=transport)
    caller_options: dict[str, Any] = {"temperature": 0.1}
    _turn(service, transport, options=caller_options)

    assert caller_options == {"temperature": 0.1}


# --- the limit is recoverable from the record -------------------------------

def test_the_limit_is_recorded_on_generation_requested():
    """What was sent must be recoverable without reading the transport."""
    transport = RecordingTransport()
    service, events = build_in_memory_service(transport=transport)
    _turn(service, transport)

    requested = [
        e for e in events.all() if e.type is EventType.GENERATION_REQUESTED
    ]
    assert requested, "no GENERATION_REQUESTED event was recorded"
    assert requested[-1].payload["generation_limit"] == DEFAULT_GENERATION_RESERVE


def test_the_boss_model_is_unchanged_by_this_path():
    """Prerequisite B must not touch model selection."""
    transport = RecordingTransport()
    service, _ = build_in_memory_service(transport=transport)
    payload = _turn(service, transport)

    assert payload["model"] == Settings().boss_model
