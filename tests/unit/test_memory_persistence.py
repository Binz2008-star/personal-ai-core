"""InMemoryMemoryRepository: non-destructive supersede, ACTIVE filtering."""
from __future__ import annotations

import pytest

from personal_ai_core.core.memory import (
    MemoryProvenance,
    MemoryRecord,
    MemoryStatus,
    MemoryType,
)
from personal_ai_core.persistence.memory_store import InMemoryMemoryRepository


def _record(**overrides) -> MemoryRecord:
    defaults = dict(
        session_id="s1",
        type=MemoryType.PREFERENCES,
        content="prefers Arabic",
        language="en",
        provenance=MemoryProvenance(
            session_id="s1", event_id="e1", promoted_by="rule:test"
        ),
        status=MemoryStatus.ACTIVE,
        confidence=0.9,
    )
    defaults.update(overrides)
    return MemoryRecord(**defaults)


def test_write_then_read():
    repo = InMemoryMemoryRepository()
    record = _record()
    repo.write(record)
    assert repo.read(record.id) == record


def test_list_active_excludes_rejected_and_superseded():
    repo = InMemoryMemoryRepository()
    active = _record(content="one")
    rejected = _record(content="two", status=MemoryStatus.REJECTED, confidence=0.4)
    repo.write(active)
    repo.write(rejected)
    listed = repo.list_active()
    assert active in listed
    assert rejected not in listed


def test_supersede_retains_old_record_with_superseded_status():
    repo = InMemoryMemoryRepository()
    old = _record(content="prefers Arabic short replies")
    repo.write(old)

    new = _record(content="prefers Arabic short replies with citations")
    returned = repo.supersede(old.id, new)

    # The returned record carries a link back to what it replaced.
    assert returned.supersedes == old.id
    assert returned.status is MemoryStatus.ACTIVE

    # Old record is retained but its status is now SUPERSEDED.
    still_there = repo.read(old.id)
    assert still_there is not None
    assert still_there.status is MemoryStatus.SUPERSEDED

    # list_active shows only the new record.
    listed = repo.list_active()
    assert returned in listed
    assert still_there not in listed


def test_supersede_rejects_unknown_old_id():
    repo = InMemoryMemoryRepository()
    with pytest.raises(KeyError):
        repo.supersede("does-not-exist", _record())


def test_supersede_refuses_to_supersede_a_non_active_record():
    repo = InMemoryMemoryRepository()
    already_rejected = _record(status=MemoryStatus.REJECTED, confidence=0.4)
    repo.write(already_rejected)
    with pytest.raises(ValueError):
        repo.supersede(already_rejected.id, _record(content="something else"))


def test_read_unknown_id_returns_none():
    repo = InMemoryMemoryRepository()
    assert repo.read("nope") is None
