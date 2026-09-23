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

from dataclasses import dataclass, field
from pathlib import Path

from .sandbox import Workspace


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
        """Restore every touched file. Returns what was restored, in order."""
        restored = []
        for path, content in reversed(list(self._before.items())):
            if content is None:
                if path.is_file():
                    path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            restored.append(self._workspace.relative(path))
        self._before.clear()
        return tuple(restored)
