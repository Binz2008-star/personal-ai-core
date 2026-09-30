"""ADR-017 Unit 2: `derive_observations` against its required tests (§15.5).

Every test builds its input list in the intended `seq` order and passes it as
the repository would; none needs `Event` or `FeedbackRecord` to expose `seq`.
"""
from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from personal_ai_core.core.domain import Event, EventType
from personal_ai_core.core.feedback import (
    CORRECTION_KEY,
    FeedbackOutcome,
    FeedbackRecord,
    feedback_idempotency_key,
)
from personal_ai_core.core.observation import Observation
from personal_ai_core.learning import derive_observations, effect_for
from personal_ai_core.learning.outcomes import CANDIDATE_STRENGTHEN, EVALUATION_SIGNAL

T0 = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
G, C = FeedbackOutcome.GOOD, FeedbackOutcome.CORRECTION


def reply(event_id: str, *, session: str = "s1", message: str | None = "m-a") -> Event:
    return Event(session_id=session, type=EventType.GENERATION_COMPLETED,
                 message_id=message, id=event_id, occurred_at=T0)


def fb(fid: str, source: str, outcome: FeedbackOutcome, *, actor: str = "user",
       session: str = "s1", at: datetime = T0, effect: str | None = None,
       payload: dict | None = None) -> FeedbackRecord:
    return FeedbackRecord(
        idempotency_key=feedback_idempotency_key(
            session_id=session, source_event_id=source, outcome=outcome, actor=actor),
        source_event_id=source, session_id=session, actor=actor, outcome=outcome,
        effect=effect_for(outcome) if effect is None else effect,
        id=fid, occurred_at=at, payload=payload or {},
    )


# --- determinism ---------------------------------------------------------------


def test_equal_input_gives_equal_output_and_the_input_is_not_mutated():
    events = [reply("e1"), reply("e2")]
    feedback = [fb("f1", "e1", G), fb("f2", "e2", C, payload={CORRECTION_KEY: "x"})]
    before = (list(events), list(feedback))

    first = derive_observations(events, feedback)
    second = derive_observations(list(events), list(feedback))

    assert first == second
    assert (events, feedback) == before


def test_an_observation_is_frozen_and_has_no_random_id():
    (obs,), _ = derive_observations([reply("e1")], [fb("f1", "e1", G)])
    with pytest.raises(FrozenInstanceError):
        obs.conflicted = True  # type: ignore[misc]
    assert not hasattr(obs, "id")
    assert hash(obs) == hash(derive_observations([reply("e1")], [fb("f1", "e1", G)])[0][0])


# --- one per judged source -------------------------------------------------------


def test_one_observation_per_judged_source_event_in_one_session():
    events = [reply("e1", message="m1"), reply("e2", message="m2"), reply("e3", message="m3")]
    feedback = [fb("f1", "e1", G), fb("f2", "e2", G), fb("f3", "e1", G, actor="owner")]
    observations, unobserved = derive_observations(events, feedback)

    assert [o.source_event_id for o in observations] == ["e1", "e2"]  # e3: no feedback, none
    assert observations[0].feedback_ids == ("f1", "f3")
    assert observations[0].source_message_id == "m1"
    assert observations[1].session_id == "s1"
    assert unobserved == ()


# --- D7: supplied order is authoritative ----------------------------------------


def test_supplied_order_wins_over_occurred_at():
    later, earlier = T0 + timedelta(hours=1), T0
    # Supplied (seq) order: GOOD then CORRECTION, but their clocks say the reverse.
    feedback = [fb("f1", "e1", G, at=later), fb("f2", "e1", C, at=earlier,
                                                 payload={CORRECTION_KEY: "fixed"})]
    (obs,), _ = derive_observations([reply("e1")], feedback)

    assert obs.feedback_ids == ("f1", "f2")
    assert obs.effective_outcome is C
    assert obs.conflicts == (("f1", G, later),)


def test_output_order_follows_first_reference_in_feedback_not_event_order():
    events = [reply("e1"), reply("e2")]
    feedback = [fb("f1", "e2", G), fb("f2", "e1", G), fb("f3", "x-missing", G),
                fb("f4", "e2", G, actor="owner")]
    observations, unobserved = derive_observations(events, feedback)
    shuffled, _ = derive_observations(list(reversed(events)), feedback)

    assert [o.source_event_id for o in observations] == ["e2", "e1"]
    assert shuffled == observations
    assert [u.source_event_id for u in unobserved] == ["x-missing"]


# --- D1: conflict rule --------------------------------------------------------------


def test_the_smoke_case_good_then_correction_is_a_conflict():
    feedback = [fb("f1", "e1", G), fb("f2", "e1", C, payload={CORRECTION_KEY: "Canberra"})]
    (obs,), _ = derive_observations([reply("e1")], feedback)

    assert obs.effective_outcome is C
    assert obs.conflicted is True
    assert obs.conflicts == (("f1", G, T0),)
    assert obs.correction == "Canberra"


def test_a_single_record_is_not_a_conflict():
    (obs,), _ = derive_observations([reply("e1")], [fb("f1", "e1", G)])
    assert obs.conflicted is False and obs.conflicts == ()


def test_the_same_outcome_from_two_actors_is_not_a_conflict():
    feedback = [fb("f1", "e1", G, actor="user"), fb("f2", "e1", G, actor="owner")]
    (obs,), _ = derive_observations([reply("e1")], feedback)
    assert obs.conflicted is False and obs.feedback_ids == ("f1", "f2")


# --- D2: source-event rule ----------------------------------------------------------


def test_feedback_on_a_non_generation_event_is_unobserved_with_its_stored_type():
    received = Event(session_id="s1", type=EventType.MESSAGE_RECEIVED, id="u1", occurred_at=T0)
    observations, unobserved = derive_observations([received], [fb("f1", "u1", G)])

    assert observations == ()
    assert len(unobserved) == 1
    assert unobserved[0].source_event_type == "message.received"
    assert unobserved[0].feedback_ids == ("f1",)


def test_feedback_on_a_missing_event_is_unobserved_with_no_type():
    observations, unobserved = derive_observations([reply("e1")], [fb("f1", "gone", G)])
    assert observations == ()
    assert unobserved[0].source_event_id == "gone"
    assert unobserved[0].source_event_type is None


# --- D3: across actors --------------------------------------------------------------


def test_two_actors_with_different_outcomes_give_one_conflicted_observation():
    feedback = [fb("f1", "e1", G, actor="user"), fb("f2", "e1", FeedbackOutcome.BAD, actor="owner")]
    observations, _ = derive_observations([reply("e1")], feedback)

    assert len(observations) == 1
    assert observations[0].conflicted is True
    assert observations[0].effective_outcome is FeedbackOutcome.BAD


# --- D5: correction text -----------------------------------------------------------


def test_correction_present_on_the_effective_record():
    (obs,), _ = derive_observations(
        [reply("e1")], [fb("f1", "e1", C, payload={CORRECTION_KEY: "  verbatim text "})])
    assert obs.correction == "  verbatim text "


def test_correction_absent_when_the_effective_outcome_is_not_correction():
    (obs,), _ = derive_observations([reply("e1")], [fb("f1", "e1", G)])
    assert obs.correction is None


def test_a_correction_on_a_non_effective_record_is_not_carried():
    feedback = [fb("f1", "e1", C, payload={CORRECTION_KEY: "old"}), fb("f2", "e1", G)]
    (obs,), _ = derive_observations([reply("e1")], feedback)
    assert obs.correction is None
    assert "f1" in obs.feedback_ids  # still reachable


def test_a_correction_without_text_is_none():
    (obs,), _ = derive_observations([reply("e1")], [fb("f1", "e1", C)])
    assert obs.correction is None


# --- effective_effect ------------------------------------------------------------------


def test_the_stored_effect_code_is_carried_not_re_derived():
    # A stored code that differs from what effect_for(GOOD) would give proves
    # the value was read from the record rather than recomputed.
    (obs,), _ = derive_observations([reply("e1")], [fb("f1", "e1", G, effect=CANDIDATE_STRENGTHEN)])
    assert obs.effective_effect == CANDIDATE_STRENGTHEN


def test_an_empty_stored_code_falls_back_to_effect_for():
    (obs,), _ = derive_observations([reply("e1")], [fb("f1", "e1", G, effect="")])
    assert obs.effective_effect == EVALUATION_SIGNAL


# --- no write ----------------------------------------------------------------------


def test_derivation_over_read_only_inputs():
    events = (reply("e1"),)
    feedback = (fb("f1", "e1", G),)
    observations, _ = derive_observations(iter(events), iter(feedback))
    assert isinstance(observations[0], Observation)


def test_the_module_imports_core_and_learning_only():
    source = Path(__file__).resolve().parents[2] / "src" / "personal_ai_core" / "learning" / "observations.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    targets = {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level > 0
    }
    assert targets <= {"core", "outcomes"}, targets
    assert "memory" not in source.read_text(encoding="utf-8").replace("reach memory", "")
