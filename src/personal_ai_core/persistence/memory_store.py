"""In-process memory repository.

The real implementation of `core.contracts.MemoryStore`. Named separately
from `in_memory.py` so that the sealed conversation-path store and the
promotion-path repository do not share a module: they exist for opposite
reasons and confusing them is exactly the mistake ADR-003 pins down.

Supersede is non-destructive. The old record is not removed; it is rewritten
with `status=SUPERSEDED` and the new record carries a `supersedes` link to
it. An audit that cannot see the old claim is not an audit.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Sequence

from ..core.domain import utcnow
from ..core.memory import MemoryRecord, MemoryStatus


class InMemoryMemoryRepository:
    """Process-memory implementation of `MemoryStore`.

    Phase 3 storage. A Postgres-backed implementation replaces this without
    changing anything above the `core.contracts.MemoryStore` boundary --
    that is why the contract exists.
    """

    def __init__(self) -> None:
        self._records: dict[str, MemoryRecord] = {}

    def write(self, record: MemoryRecord) -> MemoryRecord:
        self._records[record.id] = record
        return record

    def read(self, memory_id: str) -> MemoryRecord | None:
        return self._records.get(memory_id)

    def list_active(self) -> Sequence[MemoryRecord]:
        return tuple(
            r for r in self._records.values() if r.status is MemoryStatus.ACTIVE
        )

    def supersede(self, old_id: str, new_record: MemoryRecord) -> MemoryRecord:
        old = self._records.get(old_id)
        if old is None:
            raise KeyError(f"unknown memory record: {old_id}")
        if old.status is not MemoryStatus.ACTIVE:
            raise ValueError(
                f"cannot supersede a record whose status is {old.status.value!r}"
            )
        superseded_old = replace(
            old, status=MemoryStatus.SUPERSEDED, updated_at=utcnow()
        )
        linked_new = replace(new_record, supersedes=old.id)
        self._records[old_id] = superseded_old
        self._records[linked_new.id] = linked_new
        return linked_new
