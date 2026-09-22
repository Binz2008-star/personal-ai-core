"""An event that cannot be written down is not evidence.

ADR-010 names this as the prerequisite for any durable backend, and classifies
it precisely: it is a property of the event, not of the store, so it belongs in
`core/domain.py` and is settled before a backend exists rather than discovered
by one.

What it was:

    Event(payload={"obj": object(), "fn": len})   -> constructed happily
    json.dumps(dict(event.payload))               -> TypeError

The failure landed at write time, on a store, holding the event it was about to
lose. It lands at construction now, with the caller that built it on the stack.

The check is structural rather than `json.dumps`, and these tests are mostly
about that difference: json.dumps ACCEPTS a tuple and returns a list, and
ACCEPTS NaN and returns output no other parser reads. Both are rejected here,
because a store that quietly changes what you gave it is worse than one that
refuses it.
"""
from __future__ import annotations

import json

import pytest

from personal_ai_core.core.domain import Event, EventType


def event(payload):
    return Event(session_id="s", type=EventType.SESSION_STARTED, payload=payload)


# --- what a payload may contain -------------------------------------------


def test_the_json_shapes_are_accepted():
    recorded = event(
        {
            "text": "a",
            "count": 1,
            "ratio": 0.5,
            "flag": True,
            "absent": None,
            "list": [1, "two", None, {"nested": True}],
            "mapping": {"depth": {"two": [1, 2]}},
        }
    )
    # The point of accepting them: this must not raise.
    json.loads(json.dumps(dict(recorded.payload)))


def test_the_payloads_this_system_actually_records_are_accepted():
    """Guards against a check so strict it breaks the callers it protects.

    These are the real shapes from conversation/service.py and memory/pipeline.
    """
    event({"user_id": "u-1"})
    event({"role": "user", "language": "ar"})
    event({"model": "m:7b", "provider": "ollama", "message_count": 3, "grounded": True})
    event({"chunk_ids": ["c-1", "c-2"], "budget_source": "reserve-based(...)"})


# --- and what it may not ---------------------------------------------------


def test_an_unserialisable_object_is_refused_at_construction():
    with pytest.raises(ValueError, match="object"):
        event({"obj": object()})


def test_a_function_is_refused():
    with pytest.raises(ValueError, match="builtin_function_or_method|function"):
        event({"fn": len})


def test_the_message_names_the_key_that_is_wrong():
    """A payload can be large. "something is not serialisable" sends the reader
    looking; naming the path does not."""
    with pytest.raises(ValueError, match=r"payload\['outer'\]\['inner'\]\[1\]"):
        event({"outer": {"inner": [1, object()]}})


def test_a_tuple_is_refused_although_json_accepts_it():
    """THE case for a structural check. json.dumps turns a tuple into a list,
    so the event read back is not the event written -- silently."""
    assert json.dumps({"t": (1, 2)}) == '{"t": [1, 2]}'

    with pytest.raises(ValueError, match="tuple"):
        event({"t": (1, 2)})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nan_and_the_infinities_are_refused_although_json_emits_them(value):
    """The second case for a structural check. json.dumps emits bare NaN and
    Infinity, which are not JSON and which other parsers reject -- so the
    write succeeds and the read, somewhere else, does not."""
    assert "NaN" in json.dumps({"x": float("nan")}) or True  # documents the default

    with pytest.raises(ValueError, match="no JSON representation"):
        event({"x": value})


def test_a_non_string_key_is_refused():
    with pytest.raises(ValueError, match="non-string key"):
        event({1: "one"})


def test_a_non_string_key_nested_inside_is_refused():
    with pytest.raises(ValueError, match="non-string key"):
        event({"outer": {2: "two"}})


def test_a_set_is_refused():
    with pytest.raises(ValueError, match="set"):
        event({"s": {1, 2}})


# --- the guarantee this buys ----------------------------------------------


def test_every_accepted_payload_survives_a_round_trip_unchanged():
    """The actual promise. Not "it serialises" -- that a tuple also does --
    but that what comes back equals what went in."""
    original = {
        "text": "نص عربي",
        "count": 0,
        "ratio": 1.5,
        "flag": False,
        "absent": None,
        "list": [1, [2, [3]], {"k": "v"}],
    }
    recorded = event(original)
    assert json.loads(json.dumps(dict(recorded.payload))) == original


def test_the_default_empty_payload_is_still_allowed():
    assert dict(Event(session_id="s", type=EventType.SESSION_STARTED).payload) == {}
