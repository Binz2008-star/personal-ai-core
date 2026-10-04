"""RECOVER -- bounded automation and rollback points (AGENT_ARCHITECTURE.md section 6).

    VERIFY fails -> classify -> repair (bounded retries)
                             -> or roll back to the last safe point
                             -> or stop and report

"Stop and report" is a legitimate outcome. Silent failure is not.

ActionBudget
    The `allowed() -> record() -> summary()` shape of robin-content-engine's
    upload_budget.py, which the design names as the pattern (REWRITE, concept
    only). Every automated action spends from it, and exhausting it stops the
    agent rather than letting it loop. Unlike the source it is per-run and in
    memory: a budget that survived restarts would make one bad session's
    failures a later session's refusal.

Checkpoints
    The rollback point. Every file mutation records what the file held first
    -- or that it did not exist -- and `rollback()` restores all of them in
    reverse order. The design requires one BEFORE any CRITICAL action;
    `DeleteFile` cannot be built without one.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..core.errors import RollbackIncomplete
from .sandbox import Workspace


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Write through a temporary file in the same directory, then replace.

    A direct write leaves a half-written file when the process dies mid-write;
    `os.replace` on one filesystem is atomic, so a reader sees the old file or
    the new one, never a mix. The temporary file is removed if anything fails.
    """
    def fill(fd: int) -> None:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

    _replace_atomically(path, fill)


def atomic_write_text(path: Path, content: str) -> None:
    """`atomic_write_bytes` for text, written as `Path.write_text(content,
    encoding="utf-8")` writes it (text mode: newlines follow the platform)."""
    def fill(fd: int) -> None:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

    _replace_atomically(path, fill)


def _replace_atomically(path: Path, fill: Callable[[int], None]) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        fill(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@dataclass
class ActionBudget:
    max_actions: int = 12
    max_failures: int = 3
    actions: int = field(default=0, init=False)
    failures: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.max_actions < 1 or self.max_failures < 1:
            raise ValueError("a budget must allow at least one action and one failure")

    def allowed(self) -> bool:
        return self.actions < self.max_actions and self.failures < self.max_failures

    def record(self, *, ok: bool) -> None:
        self.actions += 1
        if not ok:
            self.failures += 1

    def summary(self) -> str:
        if self.failures >= self.max_failures:
            return (
                f"stopped: {self.failures} failed actions reached the limit of "
                f"{self.max_failures}"
            )
        if self.actions >= self.max_actions:
            return f"stopped: {self.actions} actions reached the limit of {self.max_actions}"
        return f"{self.actions}/{self.max_actions} actions, {self.failures} failed"


class Checkpoints:
    """What each touched file held before the agent touched it."""

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace
        # Insertion-ordered; the FIRST state of each path is kept, because a
        # rollback returns to before the run, not to before the last write.
        self._before: dict[Path, bytes | None] = {}

    def before_mutation(self, path: Path) -> None:
        if path in self._before:
            return
        self._before[path] = path.read_bytes() if path.is_file() else None

    def touched(self) -> tuple[str, ...]:
        return tuple(self._workspace.relative(path) for path in self._before)

    def commit(self) -> None:
        """Accept the changes so far: a later rollback will not undo them.

        Called when a task ends with its changes kept. Without it, rolling back
        a failed task would also undo every task before it in the session.
        """
        self._before.clear()

    def rollback(self) -> tuple[str, ...]:
        """Restore every touched file. Returns what was restored, in order.

        One file that cannot be restored does not strand the others: each is
        tried, and then RollbackIncomplete names what was not restored. Those
        files keep their checkpoints, so nothing about them is forgotten."""
        restored: list[str] = []
        unrestored: list[str] = []
        for path, content in reversed(list(self._before.items())):
            try:
                if content is None:
                    if path.is_file():
                        path.unlink()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_bytes(path, content)
            except OSError:
                unrestored.append(self._workspace.relative(path))
                continue
            restored.append(self._workspace.relative(path))
            del self._before[path]
        if unrestored:
            raise RollbackIncomplete(tuple(restored), tuple(unrestored))
        return tuple(restored)
