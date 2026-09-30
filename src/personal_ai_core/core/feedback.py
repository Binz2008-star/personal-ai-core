"""Phase 7 feedback domain type and its durable form (ADR-017 §3.1).

Pure values: a frozen dataclass and a str-Enum, matching the convention in
`core.domain` and `core.memory`. No I/O, no persistence, no provider, no
framework.

A `FeedbackRecord` is not a conversation event and is never a memory
(ADR-017 §3.1, review point 6). Its durable form is a `FEEDBACK_RECORDED`
event; `as_feedback_event` / `feedback_record_from_event` are the single
place that serialisation contract lives, so every backend round-trips the
same field set.

Only the persistence boundary is in scope here: the record, its deterministic
idempotency key, and the event mapping. The observation builder, the
evaluation contract and the extraction rules belong to the rest of Phase 7
and are not built.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .domain import Event, EventType, _check_json_value, new_id, utcnow


class FeedbackOutcome(str, Enum):
    """Structured judgement labels (LEARNING_ARCHITECTURE §4, ADR-017 §3.2).

    The labels are the vocabulary the promotion pipeline will later read; this
    phase persists them and keys them, and defines no downstream effect for
    them yet.
    """

    GOOD = "good"
    BAD = "bad"
    WRONG = "wrong"
    WRONG_SOURCE = "wrong_source"
    TOO_SLOW = "too_slow"
    FALSE_REFUSAL = "false_refusal"
    CORRECTION = "correction"
    REMEMBER_THIS = "remember_this"
    FORGET_THIS = "forget_this"
    PREFERENCE = "preference"
    KNOWLEDGE_GAP = "knowledge_gap"


# The reserved payload key under which a FeedbackRecord's deterministic
# idempotency key travels on its durable FEEDBACK_RECORDED event, so the
# duplicate predicate survives every backend round-trip (review point 1).
FEEDBACK_IDEMPOTENCY_KEY = "feedback_idempotency_key"

# The one payload key a FeedbackRecord may carry, and only on CORRECTION: the
# user's corrected text (ADR-017 A1, D5). Defined here rather than in
# `app/cli.py` so `learning/` can read it without importing the entry point
# (D6). The value is unchanged, so stored data is unaffected. Anything wider
# would be a payload schema, which ADR-017 has not designed.
CORRECTION_KEY = "correction"

# The one EventType member this phase adds, held here so the repositories can
# filter feedback events without naming the enum member and muddying the
# "exactly one producer" claim (ADR-017 §3.1): only `as_feedback_event`
# references `EventType.FEEDBACK_RECORDED`.
FEEDBACK_EVENT_TYPE = EventType.FEEDBACK_RECORDED


@dataclass(frozen=True, slots=True)
class FeedbackRecord:
    """A judgement of one prior event (ADR-017 §3.1).

    Append-only, like an event: a correction is a NEW record, never an edit.
    `source_event_id` is required and must already exist in the same session
    -- a judgement of nothing is not feedback -- which the repository
    implementations enforce at the boundary.
    """

    idempotency_key: str
    source_event_id: str
    session_id: str
    actor: str
    outcome: FeedbackOutcome
    # The downstream disposition `outcome` commits to (ADR-017 §3.2), resolved
    # by `learning.outcomes.effect_for` and persisted with the record so the
    # future observation builder reads a code instead of re-deriving one.
    #
    # Optional on a directly-constructed record because the enum and the effect
    # table live in different layers by construction: the enum-producer guard
    # excludes an enum's own defining module when counting producers, so a
    # table in this file would not count and every member would still read as
    # dead. The production path is `FeedbackRecorder`, which always resolves
    # one. An effect that IS supplied must be a real code, not padding.
    effect: str = ""
    id: str = field(default_factory=new_id)
    occurred_at: datetime = field(default_factory=utcnow)
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("FeedbackRecord.session_id must be non-empty")
        if not self.source_event_id:
            raise ValueError("FeedbackRecord.source_event_id must be non-empty")
        if not self.actor:
            raise ValueError("FeedbackRecord.actor must be non-empty")
        if self.effect and self.effect != self.effect.strip():
            # "" is the documented "not resolved here" sentinel and is allowed.
            # " " or " evaluation_signal " is a bug: it persists a code no
            # consumer can match, which is the failure this field exists to
            # make impossible.
            raise ValueError(
                f"FeedbackRecord.effect {self.effect!r} must be a bare effect "
                "code with no surrounding whitespace; resolve it with "
                "learning.outcomes.effect_for(outcome) rather than writing "
                "one by hand."
            )
        if not self.idempotency_key.startswith("feedback:"):
            raise ValueError(
                "FeedbackRecord.idempotency_key must have the deterministic "
                "'feedback:<session>:<source>:<outcome>:<actor>' shape; build "
                "it with feedback_idempotency_key()"
            )
        # The same payload serialisation contract as Event.payload (PR #41):
        # checked here, while the caller that built the record is on the
        # stack, so an unstorable payload cannot reach a durable store.
        for key, value in self.payload.items():
            if not isinstance(key, str):
                raise ValueError(
                    f"feedback payload has a non-string key {key!r}. JSON "
                    "object keys are strings, and converting silently would "
                    "change what is read back."
                )
            _check_json_value(value, f"payload[{key!r}]")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


def feedback_idempotency_key(
    *,
    session_id: str,
    source_event_id: str,
    outcome: FeedbackOutcome,
    actor: str,
) -> str:
    """The deterministic key identifying one judgement (review point 1).

    Same (session, source, outcome, actor) => same key; appending a record
    whose key already exists is an atomic no-op at the repository boundary,
    so retries and duplicate submissions can never double-record.
    """
    return (
        "feedback:"
        + session_id
        + ":"
        + source_event_id
        + ":"
        + outcome.value
        + ":"
        + actor
    )


def as_feedback_event(record: FeedbackRecord) -> Event:
    """The durable form of a FeedbackRecord: one FEEDBACK_RECORDED event.

    The single production reference to `EventType.FEEDBACK_RECORDED`: every
    backend stores feedback through this mapping, so the durability contract
    (the field set below) lives in exactly one place and every backend
    round-trips the same record.
    """
    return Event(
        session_id=record.session_id,
        type=FEEDBACK_EVENT_TYPE,
        payload={
            FEEDBACK_IDEMPOTENCY_KEY: record.idempotency_key,
            "source_event_id": record.source_event_id,
            "session_id": record.session_id,
            "actor": record.actor,
            "outcome": record.outcome.value,
            "effect": record.effect,
            "feedback_id": record.id,
            "occurred_at": record.occurred_at.isoformat(),
            "payload": dict(record.payload),
        },
        actor=record.actor,
        occurred_at=record.occurred_at,
    )


def feedback_record_from_event(event: Event) -> FeedbackRecord:
    """Rebuild the FeedbackRecord a FEEDBACK_RECORDED event carries."""
    if event.type is not FEEDBACK_EVENT_TYPE:
        raise ValueError(
            "feedback_record_from_event requires a FEEDBACK_RECORDED event, "
            f"got {event.type.value!r}"
        )
    payload = event.payload
    return FeedbackRecord(
        idempotency_key=payload[FEEDBACK_IDEMPOTENCY_KEY],
        source_event_id=payload["source_event_id"],
        session_id=payload["session_id"],
        actor=payload["actor"],
        outcome=FeedbackOutcome(payload["outcome"]),
        # Absent in rows written before the effect field existed. `.get` rather
        # than `[...]` so this stays a pure read of durable history: an old
        # event rebuilds as a record with no resolved effect, which is the
        # truth about it, and the recorder will resolve one for anything new.
        effect=payload.get("effect", ""),
        id=payload["feedback_id"],
        occurred_at=datetime.fromisoformat(payload["occurred_at"]),
        payload=dict(payload["payload"]),
    )


FEEDBACK_TYPE = FEEDBACK_EVENT_TYPE.value


@dataclass(frozen=True, slots=True)
class FeedbackAudit:
    """What a read-only pre-migration audit found. Reports; it never repairs.

    `clean` is true only when all three findings are empty. A non-clean result
    is a DECISION for the owner, not a defect this code may quietly fix: which
    record of a duplicated pair should survive is a policy question, and
    inventing an answer here is exactly the thing ADR-017 forbids.

    Note that `clean` is stricter than "the index will build". A missing key
    does not stop the index and still makes this not clean, because the row it
    describes is left unprotected. See `classify_feedback_rows`.
    """

    duplicate_keys: tuple[tuple[str, tuple[str, ...]], ...]
    rows_missing_key: tuple[str, ...]
    rows_with_unparseable_payload: tuple[str, ...]

    @property
    def clean(self) -> bool:
        return not (
            self.duplicate_keys
            or self.rows_missing_key
            or self.rows_with_unparseable_payload
        )


def classify_feedback_rows(rows: Iterable[tuple[str, str, str]]) -> FeedbackAudit:
    """Classify stored `(id, type, payload_json)` rows against the index.

    Pure logic over already-fetched rows, so both backends audit identically
    and this is testable with no database at all. Rows must arrive in `seq`
    order; the findings are reported in that same order so a human reads them
    in storage order.

    Three findings. What they MEAN differs, and the difference is the whole
    reason to run this before deploying rather than after:

    1. Duplicate keys. `CREATE UNIQUE INDEX` cannot be created while these
       exist, so `connect` raises and the deployment is blocked. Loud, and the
       one most likely to be noticed anyway.
    2. Missing keys. A FEEDBACK_RECORDED row with no usable
       `feedback_idempotency_key` extracts to NULL, and a NULL never collides
       -- so the index is created happily and the row is then invisible to
       every duplicate check it was supposed to take part in. SILENT. This is
       the finding worth the most attention precisely because nothing fails.
    3. Unparseable payloads. A TEXT column carries no JSON validity
       constraint, so `payload` can be anything.

    On (3), note WHERE the malformed payload sits, because it decides whether
    the index can be built at all. `events_feedback_idem_unique` is PARTIAL,
    and both backends evaluate a partial index's predicate BEFORE the indexed
    expression. A malformed payload is therefore only ever parsed for a row
    that already matches `type = 'feedback.recorded'`:

      malformed on a feedback row     -> index creation fails
      malformed on any other row      -> never parsed; index builds fine

    Verified on SQLite. The same predicate-then-expression order is how
    PostgreSQL plans a partial index, but that has NOT been verified against a
    live server here, so treat the non-feedback case as advisory rather than
    proven. Both variants are reported because a malformed payload is a real
    data defect either way; only one of them blocks a deployment.

    A payload that parses to a non-object (a JSON array or scalar) is NOT
    unparseable: both backends' `json_extract` / `->>` return NULL for a
    non-object without raising. It lands in `rows_missing_key` if the row is
    feedback, and is otherwise unremarkable.
    """
    duplicates: dict[str, list[str]] = {}
    missing: list[str] = []
    unparseable: list[str] = []

    for row_id, row_type, payload_text in rows:
        try:
            parsed = json.loads(payload_text)
        except (TypeError, ValueError):
            unparseable.append(row_id)
            continue

        if row_type != FEEDBACK_TYPE:
            continue

        key = parsed.get(FEEDBACK_IDEMPOTENCY_KEY) if isinstance(parsed, dict) else None
        if not isinstance(key, str) or not key:
            missing.append(row_id)
            continue

        duplicates.setdefault(key, []).append(row_id)

    return FeedbackAudit(
        duplicate_keys=tuple(
            (key, tuple(ids))
            for key, ids in duplicates.items()
            if len(ids) > 1
        ),
        rows_missing_key=tuple(missing),
        rows_with_unparseable_payload=tuple(unparseable),
    )