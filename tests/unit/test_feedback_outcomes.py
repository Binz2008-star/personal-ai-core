"""The outcome effect table, and the backend agreement it depends on (ADR-017 §3.2).

ADR-017 §3.2 states the rule this module enforces: *"a feedback label with no
effect is a bug"*, and claims *"the effect table lives in `src/` so the
enum-producer guard sees a producer for each"*. Both halves of that were false
when the change was written -- the table was markdown, and `FeedbackOutcome`
was absent from the guard's target set, so all eleven members were dead. These
tests are the difference between the claim and the code.

The last two tests are the reason the table is worth persisting at all: they pin
that a label's disposition survives a durable round-trip, and that the two
shipped backends answer "is this feedback valid?" identically. A protocol whose
implementations disagree is a backend-dependent question, and that was a real
divergence here -- `InMemoryFeedbackRepository` checked the idempotency key
before validating the source event, `SqliteFeedbackRepository` did the
opposite, and the outcome depended on which backend was configured.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from personal_ai_core.core.domain import Event, EventType
from personal_ai_core.core.feedback import (
    FeedbackOutcome,
    FeedbackRecord,
    feedback_idempotency_key,
)
from personal_ai_core.learning.outcomes import (
    CANDIDATE_PREFERENCES,
    CANDIDATE_STRENGTHEN,
    CANDIDATE_SUPERSEDE,
    EVALUATION_SIGNAL,
    KNOWLEDGE_GAP_OBSERVATION,
    OUTCOME_EFFECTS,
    effect_for,
)
from personal_ai_core.persistence.in_memory import (
    InMemoryEventRepository,
    InMemoryFeedbackRepository,
)
from personal_ai_core.persistence.sqlite import (
    SqliteFeedbackRepository,
    connect,
)

_SRC = Path(__file__).resolve().parents[2] / "src" / "personal_ai_core"


def _declared_members() -> set[str]:
    source = (_SRC / "core" / "feedback.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "FeedbackOutcome":
            return {
                item.targets[0].id
                for item in node.body
                if isinstance(item, ast.Assign)
                and isinstance(item.targets[0], ast.Name)
            }
    raise AssertionError("FeedbackOutcome not found in core/feedback.py")


# ── totality ────────────────────────────────────────────────────────────────


def test_every_declared_outcome_has_an_effect():
    """The anti-rot property: no member may exist without a row.

    Asserted against the ENUM's own declared members rather than against a
    hand-copied list, so adding a member to `FeedbackOutcome` without an effect
    here fails. A literal list of eleven names would keep passing forever,
    which is how the original claim went stale: the doc and the code drifted
    apart with nothing checking.
    """
    declared = _declared_members()
    assert declared, "no members parsed -- the guard below would be vacuous"
    missing = declared - {m.name for m in OUTCOME_EFFECTS}
    assert not missing, (
        f"FeedbackOutcome member(s) with no entry in OUTCOME_EFFECTS: "
        f"{sorted(missing)}. ADR-017 §3.2: a feedback label with no effect is a "
        "bug. Add the effect, or drop the member."
    )


def test_the_table_covers_nothing_that_is_not_declared():
    """The other direction. A row for a member the enum does not have is
    either a typo or a member that was renamed; both are silent otherwise."""
    declared = _declared_members()
    extra = {m.name for m in OUTCOME_EFFECTS} - declared
    assert not extra, f"OUTCOME_EFFECTS has rows for undeclared members: {sorted(extra)}"


def test_the_table_is_read_only():
    """It is module-level state consulted by a live code path. A caller that
    could add a row would let a judgement carry an effect nothing in the ADR
    describes."""
    with pytest.raises(TypeError):
        OUTCOME_EFFECTS[FeedbackOutcome.GOOD] = "something_new"  # type: ignore[index]


def test_effect_for_refuses_an_outcome_with_no_row(monkeypatch):
    """A label with no effect must be loud, not defaulted.

    Returning a placeholder would turn the exact defect §3.2 forbids into
    persisted data that looks valid and means nothing.

    The row is removed by rebinding the module attribute rather than by
    mutating the mapping, because the mapping is deliberately read-only --
    which `test_the_table_is_read_only` covers from the other side.
    """
    import personal_ai_core.learning.outcomes as outcomes

    partial = {k: v for k, v in outcomes.OUTCOME_EFFECTS.items()
               if k is not FeedbackOutcome.GOOD}
    assert FeedbackOutcome.GOOD not in partial
    monkeypatch.setattr(outcomes, "OUTCOME_EFFECTS", partial)

    with pytest.raises(ValueError, match="has no entry in OUTCOME_EFFECTS"):
        outcomes.effect_for(FeedbackOutcome.GOOD)
    # a member that IS present still resolves
    assert outcomes.effect_for(FeedbackOutcome.BAD) == EVALUATION_SIGNAL


@pytest.mark.parametrize(
    "outcome,expected",
    [
        (FeedbackOutcome.GOOD, EVALUATION_SIGNAL),
        (FeedbackOutcome.BAD, EVALUATION_SIGNAL),
        (FeedbackOutcome.WRONG, EVALUATION_SIGNAL),
        (FeedbackOutcome.WRONG_SOURCE, EVALUATION_SIGNAL),
        (FeedbackOutcome.TOO_SLOW, EVALUATION_SIGNAL),
        (FeedbackOutcome.FALSE_REFUSAL, EVALUATION_SIGNAL),
        (FeedbackOutcome.CORRECTION, CANDIDATE_STRENGTHEN),
        (FeedbackOutcome.REMEMBER_THIS, CANDIDATE_PREFERENCES),
        (FeedbackOutcome.PREFERENCE, CANDIDATE_PREFERENCES),
        (FeedbackOutcome.FORGET_THIS, CANDIDATE_SUPERSEDE),
        (FeedbackOutcome.KNOWLEDGE_GAP, KNOWLEDGE_GAP_OBSERVATION),
    ],
)
def test_each_outcome_resolves_to_the_effect_the_adr_states(
    outcome: FeedbackOutcome, expected: str
):
    """The five ADR rows, member by member.

    Written out rather than derived from the table, because a test that reads
    its expectation out of the code under test checks only that the code equals
    itself. This is the transcription check: if ADR §3.2 changes, this fails.
    """
    assert effect_for(outcome) == expected


def test_forget_this_records_a_supersede_disposition_and_touches_nothing():
    """`FORGET_THIS` commits to superseding a memory THROUGH THE PIPELINE.

    The table records that commitment. It must not perform it: no
    `MemoryStore.supersede` call may originate in `learning/` (review point 5),
    and the sole-writer guard in `test_experience_pipeline.py` would catch one
    -- but only if it looked, and this module is the natural place for such a
    call to appear later. Asserted here at the point of temptation.
    """
    assert effect_for(FeedbackOutcome.FORGET_THIS) == CANDIDATE_SUPERSEDE
    for path in sorted((_SRC / "learning").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert ".supersede(" not in source, (
            f"learning/{path.name} calls .supersede(; supersession is a memory "
            "write and only ExperiencePipeline may perform it (ADR-017 review "
            "point 5)"
        )
        assert ".write(" not in source, (
            f"learning/{path.name} calls .write(; the memory store's sole writer "
            "is ExperiencePipeline (ADR-017 review point 5)"
        )


# ── the disposition is persisted, not recomputed ────────────────────────────


def test_the_effect_survives_a_sqlite_round_trip(request):
    from personal_ai_core.learning.feedback import FeedbackRecorder

    db = connect(":memory:")
    request.addfinalizer(db.close)
    source = Event(session_id="s1", type=EventType.GENERATION_COMPLETED, payload={})
    db.execute(
        "INSERT INTO events (id, session_id, type, payload, actor, occurred_at) "
        "VALUES (?,?,?,?,?,?)",
        (source.id, "s1", source.type.value, "{}", "user", source.occurred_at.isoformat()),
    )

    recorder = FeedbackRecorder(SqliteFeedbackRepository(db))
    written = recorder.record(
        source_event_id=source.id, session_id="s1", outcome=FeedbackOutcome.CORRECTION
    )
    read_back = SqliteFeedbackRepository(db).list_for_source(source.id)[0]

    assert written.effect == CANDIDATE_STRENGTHEN
    assert read_back.effect == CANDIDATE_STRENGTHEN


def test_a_record_written_before_the_effect_field_reads_back_as_unresolved():
    """History is read, not repaired.

    A `FEEDBACK_RECORDED` row predating this field has no `effect` key. It
    rebuilds as a record with none rather than raising or inventing a code:
    re-deriving one here would mean the durable row and the rebuilt record
    disagree about the same event, which is the failure mode the audit exists
    to prevent.
    """
    from personal_ai_core.core.feedback import feedback_record_from_event

    event = Event(
        session_id="s1",
        type=EventType.FEEDBACK_RECORDED,
        payload={
            "feedback_idempotency_key": "feedback:s1:e1:good:user",
            "source_event_id": "e1",
            "session_id": "s1",
            "actor": "user",
            "outcome": "good",
            "feedback_id": "f1",
            "occurred_at": "2026-01-01T00:00:00+00:00",
            "payload": {},
        },
    )
    rebuilt = feedback_record_from_event(event)
    assert rebuilt.effect == ""
    assert rebuilt.outcome is FeedbackOutcome.GOOD


def test_a_hand_written_effect_with_whitespace_is_refused():
    """Persisted codes are matched by equality, so a padded one matches nothing.

    ``""`` is the documented sentinel for "not resolved here" and is allowed;
    a padded code is a bug and is refused at construction, while the record that
    carries it is still on the stack.
    """
    with pytest.raises(ValueError, match="bare effect code"):
        FeedbackRecord(
            idempotency_key=feedback_idempotency_key(
                session_id="s1", source_event_id="e1", outcome=FeedbackOutcome.GOOD,
                actor="user",
            ),
            source_event_id="e1",
            session_id="s1",
            actor="user",
            outcome=FeedbackOutcome.GOOD,
            effect=" evaluation_signal ",
        )

    # the sentinel itself is still constructible
    unresolved = FeedbackRecord(
        idempotency_key=feedback_idempotency_key(
            session_id="s1", source_event_id="e1", outcome=FeedbackOutcome.GOOD,
            actor="user",
        ),
        source_event_id="e1",
        session_id="s1",
        actor="user",
        outcome=FeedbackOutcome.GOOD,
    )
    assert unresolved.effect == ""


# ── the two shipped backends must answer the same question ──────────────────


def _contradicting_record() -> FeedbackRecord:
    """A record that reuses a STORED key AND names a source that cannot exist.

    Only reachable with a hand-written key: `feedback_idempotency_key` derives
    the key from `source_event_id`, so a derived key cannot contradict it.
    """
    return FeedbackRecord(
        idempotency_key="feedback:s1:does-not-exist:good:user",
        source_event_id="does-not-exist",
        session_id="s1",
        actor="user",
        outcome=FeedbackOutcome.GOOD,
    )


def test_both_backends_refuse_a_record_that_names_no_source_and_reuses_a_key():
    """The divergence this change removed.

    Previously: `InMemoryFeedbackRepository` checked the idempotency key first
    and returned the stored record, while `SqliteFeedbackRepository` validated
    the source first and raised. The same call therefore produced a
    `FeedbackRecord` or a `ValueError` depending only on backend
    configuration. Both must raise.
    """
    # in memory: store one good record, then offer the contradicting one
    events = InMemoryEventRepository()
    in_memory = InMemoryFeedbackRepository(events)
    seed = Event(session_id="s1", type=EventType.GENERATION_COMPLETED, payload={})
    events.append(seed)
    good = FeedbackRecord(
        idempotency_key=feedback_idempotency_key(
            session_id="s1", source_event_id=seed.id, outcome=FeedbackOutcome.GOOD,
            actor="user",
        ),
        source_event_id=seed.id,
        session_id="s1",
        actor="user",
        outcome=FeedbackOutcome.GOOD,
    )
    in_memory.append(good)

    # the stored key IS "feedback:s1:<seed>:good:user", so re-offering that key
    # with a bogus source is the corner under test
    contradicting = FeedbackRecord(
        idempotency_key=good.idempotency_key,
        source_event_id="does-not-exist",
        session_id="s1",
        actor="user",
        outcome=FeedbackOutcome.GOOD,
    )
    with pytest.raises(ValueError, match="does not exist in session"):
        in_memory.append(contradicting)

    # sqlite, same shape
    db = connect(":memory:")
    try:
        db.execute(
            "INSERT INTO events (id, session_id, type, payload, actor, occurred_at) "
            "VALUES (?,?,?,?,?,?)",
            (seed.id, "s1", seed.type.value, "{}", "user", seed.occurred_at.isoformat()),
        )
        sqlite_repo = SqliteFeedbackRepository(db)
        sqlite_repo.append(good)
        with pytest.raises(ValueError, match="does not exist in session"):
            sqlite_repo.append(contradicting)
        # and the good record is still the only one stored: the refusal
        # appended nothing
        assert len(sqlite_repo.list_for_source(seed.id)) == 1
    finally:
        db.close()


def test_a_duplicate_with_a_valid_source_is_a_no_op_on_both_backends():
    """The other half of the ordering: a legitimate retry still deduplicates.

    Fixing the order must not have turned every duplicate into an error.
    """
    events = InMemoryEventRepository()
    in_memory = InMemoryFeedbackRepository(events)
    seed = Event(session_id="s1", type=EventType.GENERATION_COMPLETED, payload={})
    events.append(seed)
    key = feedback_idempotency_key(
        session_id="s1", source_event_id=seed.id, outcome=FeedbackOutcome.BAD, actor="user"
    )

    def make() -> FeedbackRecord:
        return FeedbackRecord(
            idempotency_key=key,
            source_event_id=seed.id,
            session_id="s1",
            actor="user",
            outcome=FeedbackOutcome.BAD,
        )

    first, second = in_memory.append(make()), in_memory.append(make())
    assert second.id == first.id
    assert len(in_memory.list_for_source(seed.id)) == 1

    db = connect(":memory:")
    try:
        db.execute(
            "INSERT INTO events (id, session_id, type, payload, actor, occurred_at) "
            "VALUES (?,?,?,?,?,?)",
            (seed.id, "s1", seed.type.value, "{}", "user", seed.occurred_at.isoformat()),
        )
        sqlite_repo = SqliteFeedbackRepository(db)
        a, b = sqlite_repo.append(make()), sqlite_repo.append(make())
        assert b.id == a.id
        assert len(sqlite_repo.list_for_source(seed.id)) == 1
    finally:
        db.close()
