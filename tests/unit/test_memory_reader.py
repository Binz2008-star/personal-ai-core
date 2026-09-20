"""MemoryReader: a nominal, read-only boundary.

The point of these tests is that the boundary is structural rather than
conventional. A Protocol would have been satisfied by `SealedMemoryStore`,
which has both method names and raises on both -- leaving "technically a
reader, never use it as one" as a rule enforced by comment.
"""
from __future__ import annotations

import inspect
import typing

import pytest

from personal_ai_core.core.errors import InvariantViolation
from personal_ai_core.core.memory import (
    MemoryProvenance,
    MemoryReader,
    MemoryRecord,
    MemoryStatus,
    MemoryType,
)
from personal_ai_core.persistence.in_memory import SealedMemoryStore
from personal_ai_core.persistence.memory_store import InMemoryMemoryRepository


def _record(*, session_id: str = "s1", content: str = "prefers Arabic") -> MemoryRecord:
    return MemoryRecord(
        session_id=session_id,
        type=MemoryType.PREFERENCES,
        content=content,
        language="en",
        provenance=MemoryProvenance(
            session_id=session_id, event_id="e1", promoted_by="rule:test"
        ),
        status=MemoryStatus.ACTIVE,
        confidence=0.9,
    )


def test_memory_reader_is_a_class_not_a_protocol():
    """Nominal typing is the boundary; a Protocol would not be one."""
    assert inspect.isclass(MemoryReader)
    assert not issubclass(MemoryReader, typing.Protocol)  # type: ignore[arg-type]
    # A Protocol would carry this marker; a plain class must not.
    assert getattr(MemoryReader, "_is_protocol", False) is False


def test_list_active_for_session_is_a_hard_filter():
    repo = InMemoryMemoryRepository()
    mine = _record(session_id="s1", content="mine")
    theirs = _record(session_id="s2", content="theirs")
    repo.write(mine)
    repo.write(theirs)

    reader = MemoryReader(source=repo)
    visible = reader.list_active_for_session("s1")

    assert [r.content for r in visible] == ["mine"]


def test_list_active_for_session_excludes_non_active():
    repo = InMemoryMemoryRepository()
    active = _record(content="kept")
    rejected = MemoryRecord(
        session_id="s1",
        type=MemoryType.PREFERENCES,
        content="dropped",
        language="en",
        provenance=MemoryProvenance(
            session_id="s1", event_id="e1", promoted_by="rule:test"
        ),
        status=MemoryStatus.REJECTED,
        confidence=0.2,
    )
    repo.write(active)
    repo.write(rejected)

    reader = MemoryReader(source=repo)
    assert [r.content for r in reader.list_active_for_session("s1")] == ["kept"]


def test_read_delegates_to_the_source():
    repo = InMemoryMemoryRepository()
    record = _record()
    repo.write(record)

    reader = MemoryReader(source=repo)
    assert reader.read(record.id) == record
    assert reader.read("nope") is None


def test_memory_reader_exposes_no_write_surface():
    """The read view has no way to express a write."""
    reader = MemoryReader(source=InMemoryMemoryRepository())
    assert not hasattr(reader, "write")
    assert not hasattr(reader, "supersede")


def test_a_sealed_store_wrapped_in_a_reader_still_refuses():
    """The seal survives wrapping; it does not become a silent empty read.

    Nothing in the composition root wraps a SealedMemoryStore. This pins
    what happens if anything ever did: a loud refusal, not a quiet zero.
    """
    reader = MemoryReader(source=SealedMemoryStore())
    with pytest.raises(InvariantViolation, match="Event != Memory"):
        reader.list_active_for_session("s1")
    with pytest.raises(InvariantViolation, match="Event != Memory"):
        reader.read("anything")
