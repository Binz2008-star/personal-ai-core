"""The read-only pre-migration audit for `events_feedback_idem_unique`.

What each finding means, which is not the same for all three:

  duplicates        CREATE UNIQUE INDEX cannot be built; `connect` raises.
  missing keys      extracts to NULL, never collides, index builds fine --
                    and the row is left invisible to every duplicate check.
  unparseable       blocks ONLY on a feedback row, because the index is
                    partial and its predicate is evaluated before its
                    expression. A corrupt payload on any other event type is
                    reported but does not stop a deployment.

`classify_feedback_rows` is pure logic over already-fetched rows, so every
finding is testable with no database and no server. The SQLite wrapper is then
tested against a real file, including the property that matters most: it must
not apply the schema it is auditing.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from personal_ai_core.core.feedback import (
    FEEDBACK_IDEMPOTENCY_KEY,
    classify_feedback_rows,
)
from personal_ai_core.persistence.sqlite import (
    audit_feedback_rows,
    connect,
)

FEEDBACK = "feedback.recorded"
OTHER = "generation.completed"


def _row(row_id: str, row_type: str, key: str | None) -> tuple[str, str, str]:
    """A well-formed row; `key=None` omits the reserved key entirely."""
    payload: dict[str, object] = {"text": "x"}
    if key is not None:
        payload[FEEDBACK_IDEMPOTENCY_KEY] = key
    return row_id, row_type, json.dumps(payload)


def _raw(row_id: str, row_type: str, payload_text: str) -> tuple[str, str, str]:
    """A row whose payload text is supplied verbatim, valid or not."""
    return row_id, row_type, payload_text


def _feedback_payload(key: str | None) -> str:
    body: dict[str, object] = {"actor": "user", "outcome": "bad"}
    if key is not None:
        body[FEEDBACK_IDEMPOTENCY_KEY] = key
    return json.dumps(body)


# --- the classifier -------------------------------------------------------


def test_a_clean_table_is_clean():
    result = classify_feedback_rows(
        [
            _row("e1", OTHER, None),
            _row("f1", FEEDBACK, "feedback:s1:e1:bad:user"),
            _row("f2", FEEDBACK, "feedback:s1:e2:good:user"),
        ]
    )
    assert result.clean
    assert result.duplicate_keys == ()
    assert result.rows_missing_key == ()
    assert result.rows_with_unparseable_payload == ()


def test_duplicate_keys_are_reported_with_every_row_id():
    result = classify_feedback_rows(
        [
            _row("f1", FEEDBACK, "same-key"),
            _row("f2", OTHER, None),
            _row("f3", FEEDBACK, "same-key"),
            _row("f4", FEEDBACK, "unique"),
        ]
    )
    assert not result.clean
    assert result.duplicate_keys == (("same-key", ("f1", "f3")),)
    assert result.rows_missing_key == ()


def test_a_feedback_row_with_no_key_is_reported_as_missing():
    result = classify_feedback_rows(
        [_row("f1", FEEDBACK, None), _row("f2", FEEDBACK, "k")]
    )
    assert not result.clean
    assert result.rows_missing_key == ("f1",)
    assert result.duplicate_keys == ()


def test_an_empty_key_counts_as_missing_not_as_a_duplicate():
    """An empty string is falsy, so it must not become a colliding value."""
    result = classify_feedback_rows(
        [_row("f1", FEEDBACK, ""), _row("f2", FEEDBACK, "")]
    )
    assert result.duplicate_keys == ()
    assert result.rows_missing_key == ("f1", "f2")


def test_a_non_object_payload_is_missing_not_unparseable():
    """`->>` and `json_extract` return NULL for a non-object without raising.

    Calling this "unparseable" would be a false alarm: the index builds fine.
    """
    rows = [("f1", FEEDBACK, "[1, 2, 3]"), ("f2", FEEDBACK, '"just a string"')]
    result = classify_feedback_rows(rows)
    assert result.rows_with_unparseable_payload == ()
    assert result.rows_missing_key == ("f1", "f2")


def test_a_malformed_payload_on_a_NON_feedback_row_is_still_reported():
    """Reported for data hygiene, even though it does not block the index.

    The index's partial predicate means a non-feedback row's payload is never
    parsed, so this cannot fail a deployment. It is still a corrupt row worth
    naming, and the audit reports every event type rather than only the ones
    that happen to block.
    """
    result = classify_feedback_rows(
        [
            _raw("g1", OTHER, "not json at all {"),
            _row("f1", FEEDBACK, "k"),
        ]
    )
    assert not result.clean
    assert result.rows_with_unparseable_payload == ("g1",)
    # It must not be reported as a feedback problem as well.
    assert result.duplicate_keys == ()
    assert result.rows_missing_key == ()


def test_a_malformed_payload_is_reported_even_on_a_feedback_row():
    """A feedback row that will not parse cannot be a duplicate or a gap."""
    result = classify_feedback_rows([_raw("f1", FEEDBACK, '{"key": ')])
    assert result.rows_with_unparseable_payload == ("f1",)
    assert result.duplicate_keys == ()
    assert result.rows_missing_key == ()


def test_findings_are_ordered_by_storage_order():
    result = classify_feedback_rows(
        [
            _row("z", FEEDBACK, None),
            _raw("a", OTHER, "broken {"),
            _row("m", FEEDBACK, None),
        ]
    )
    assert result.rows_missing_key == ("z", "m")
    assert result.rows_with_unparseable_payload == ("a",)


# --- the SQLite wrapper ---------------------------------------------------


def _feedback_db(path) -> sqlite3.Connection:
    return connect(path)


def _seed_raw(path, rows: list[tuple[str, str, str]]) -> None:
    """Create a database holding `rows`, with the index REMOVED.

    Every audit fixture needs the same starting state: a table that predates
    the index. Dropping it first is not optional -- the index is precisely
    what forbids a duplicate row, so the duplicates have to be inserted into a
    table that is not yet protected.
    """
    db = connect(path)
    db.execute("DROP INDEX events_feedback_idem_unique")
    for row_id, row_type, payload_text in rows:
        db.execute(
            "INSERT INTO events (id, session_id, type, payload, actor, "
            "occurred_at) VALUES (?, 's1', ?, ?, 'user', '2024-01-01')",
            (row_id, row_type, payload_text),
        )
    db.commit()
    db.close()


def _index_names(path) -> set[str]:
    db = sqlite3.connect(path)
    try:
        return {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
    finally:
        db.close()


def test_the_audit_does_not_apply_the_schema_it_is_auditing(tmp_path):
    """The property that makes the audit meaningful.

    `connect` creates the index. If the audit reached for `connect`, the very
    `CREATE UNIQUE INDEX` it is checking for would already have run -- and on a
    table with duplicates, that would raise before any question was answered.
    So the index must still be absent afterwards.
    """
    path = tmp_path / "unaudited.db"
    _seed_raw(
        path,
        [(row_id, FEEDBACK, _feedback_payload("k")) for row_id in ("g1", "g2")],
    )

    result = audit_feedback_rows(path)
    assert result.duplicate_keys == (("k", ("g1", "g2")),)

    assert "events_feedback_idem_unique" not in _index_names(path)


def test_the_audit_reports_clean_on_a_properly_feedback_database(tmp_path):
    path = tmp_path / "clean.db"
    _seed_raw(
        path,
        [
            ("g1", OTHER, json.dumps({"text": "hi"})),
            ("f1", FEEDBACK, _feedback_payload("k1")),
        ],
    )
    assert audit_feedback_rows(path).clean


def test_the_audit_writes_nothing(tmp_path):
    """`mode=ro` means the filesystem refuses a write, not merely that we skip one."""
    path = tmp_path / "readonly.db"
    _seed_raw(path, [("f1", FEEDBACK, _feedback_payload("k1"))])
    before = path.read_bytes()

    assert audit_feedback_rows(path).clean
    assert path.read_bytes() == before


def test_the_audit_does_not_delete_or_choose_a_winner(tmp_path):
    """It reports the pair. Which one survives is the owner's decision.

    `DATA_MIGRATION_DECISION_REQUIRED` is a human's call; an audit that
    deduplicated for them would have already made it.
    """
    path = tmp_path / "duplicates.db"
    _seed_raw(
        path,
        [(row_id, FEEDBACK, _feedback_payload("k")) for row_id in "f1 f2 f3".split()],
    )

    result = audit_feedback_rows(path)
    assert result.duplicate_keys == (("k", ("f1", "f2", "f3")),)

    db = sqlite3.connect(path)
    try:
        assert db.execute(
            "SELECT COUNT(*) FROM events WHERE type = 'feedback.recorded'"
        ).fetchone()[0] == 3
    finally:
        db.close()


def test_a_feedback_row_missing_its_key_does_not_block_the_index(tmp_path):
    """The silent case, and the reason `clean` is not just a duplicate check.

    A missing key extracts to NULL, a NULL never collides, so the index builds
    without complaint -- and that row is then invisible to every duplicate
    check it was supposed to participate in. An audit that only looked for
    duplicates would report CLEAN here and be wrong.
    """
    path = tmp_path / "asymmetry.db"
    _seed_raw(path, [("f1", FEEDBACK, _feedback_payload(None))])

    assert audit_feedback_rows(path).rows_missing_key == ("f1",)

    # The index builds anyway: NULL keys do not collide.
    rebuilt = connect(path)
    rebuilt.close()
    assert "events_feedback_idem_unique" in _index_names(path)


def test_a_malformed_payload_on_a_feedback_row_does_block_the_index(tmp_path):
    """The loud counterpart: this one really does stop the index being built.

    The index is PARTIAL, so the predicate is applied before the expression.
    A feedback row matches the predicate, so its payload is parsed -- and
    fails.
    """
    path = tmp_path / "malformed.db"
    _seed_raw(path, [("f1", FEEDBACK, "{not json")])

    assert audit_feedback_rows(path).rows_with_unparseable_payload == ("f1",)

    with pytest.raises(sqlite3.OperationalError, match="malformed JSON"):
        connect(path)


def test_a_malformed_payload_on_a_NON_feedback_row_does_NOT_block_the_index(
    tmp_path,
):
    """The predicate runs first, so a non-feedback payload is never parsed.

    This is the opposite of what an "evaluate the expression for every row"
    reading predicts, and it is worth pinning: the scope of the malformed
    finding is real, but it is a data-hygiene report rather than a deployment
    blocker. Asserted so nobody "fixes" the audit into over-reporting.
    """
    path = tmp_path / "harmless-malformed.db"
    _seed_raw(
        path,
        [
            ("g1", OTHER, "{not json"),
            ("f1", FEEDBACK, _feedback_payload("k1")),
        ],
    )

    # Reported, so a real data defect is still surfaced...
    assert audit_feedback_rows(path).rows_with_unparseable_payload == ("g1",)
    # ...and `clean` is False even though the index would build fine.
    assert not audit_feedback_rows(path).clean

    rebuilt = connect(path)
    rebuilt.close()
    assert "events_feedback_idem_unique" in _index_names(path)


def test_connect_raises_rather_than_migrating_when_duplicates_block_the_index(
    tmp_path,
):
    """Duplicates surface as a refusal at `connect`, not a silent dedup.

    The refusal is loud but unadorned: a raw `sqlite3.IntegrityError` naming
    the index, with no pointer to the audit that would have predicted it, and
    no mention of the remediation. Asserted here so the behaviour is recorded
    rather than discovered in production.
    """
    path = tmp_path / "blocked.db"
    _seed_raw(
        path,
        [(row_id, FEEDBACK, _feedback_payload("k")) for row_id in ("f1", "f2")],
    )

    with pytest.raises(sqlite3.IntegrityError, match="events_feedback_idem_unique"):
        connect(path)
