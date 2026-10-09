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
    -- its bytes and mode, or that it did not exist -- and the directories the
    mutation is about to create; `rollback()` removes what the run created and
    restores the rest. The design requires one BEFORE any CRITICAL action;
    `DeleteFile` cannot be built without one.
"""
from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..core.errors import RollbackIncomplete
from .sandbox import SandboxError, Workspace


# Rollback snapshots live in memory only for the current task. A mutation is
# refused before it happens if preserving its original would exceed this
# shared budget; earlier rollback points are never evicted to make room.
MAX_CHECKPOINT_BYTES = 16 * 1024 * 1024


def _read_umask() -> int:
    # `os.umask` can only be read by setting it, and it is process-global, so
    # it is read once, at import, before the agent runs anything concurrently.
    # A umask changed later in the process is not seen; nothing here does that.
    current = os.umask(0o022)
    os.umask(current)
    return current


# What `open()` gives a new file: 0o666 less the umask (typically 0o644).
_NEW_FILE_MODE = 0o666 & ~_read_umask()


def _file_mode(path: Path) -> int | None:
    """The permission bits of an existing file, or None if there is none."""
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None


def atomic_write_bytes(path: Path, content: bytes, *, mode: int | None = None) -> None:
    """Write through a temporary file in the same directory, then replace.

    A direct write leaves a half-written file when the process dies mid-write;
    `os.replace` on one filesystem is atomic, so a reader sees the old file or
    the new one, never a mix. The temporary file is removed if anything fails.

    The file keeps the mode it had (a 0755 script stays executable); a new one
    gets the umask default, not the 0600 of the temporary file. `mode`, when
    given, is set instead -- a rollback restores the mode it recorded.
    """
    def fill(fd: int) -> None:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

    _replace_atomically(path, fill, mode)


def atomic_write_text(path: Path, content: str) -> None:
    """`atomic_write_bytes` for text, written as `Path.write_text(content,
    encoding="utf-8")` writes it (text mode: newlines follow the platform)."""
    def fill(fd: int) -> None:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

    _replace_atomically(path, fill, None)


def _replace_atomically(path: Path, fill: Callable[[int], None], mode: int | None) -> None:
    if mode is None:
        mode = _file_mode(path)
    if mode is None:
        mode = _NEW_FILE_MODE
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        fill(fd)
        # mkstemp creates the file 0600. On Windows the mode bits are only the
        # read-only flag, and a read-only temporary could be neither replaced
        # nor cleaned up, so the mode is left alone there.
        if os.name != "nt":
            os.chmod(tmp, mode)
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


@dataclass(frozen=True)
class _Before:
    content: bytes | None  # None: the file did not exist
    mode: int | None = None


class Checkpoints:
    """What each touched file held before the agent touched it, and which
    directories the agent's writes created.

    Original bytes share a 16 MiB task budget. A file that cannot fit is
    refused before mutation, while existing checkpoints remain available.
    """

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace
        # Insertion-ordered; the FIRST state of each path is kept, because a
        # rollback returns to before the run, not to before the last write.
        self._before: dict[Path, _Before] = {}
        # Directories that did not exist when a mutation under them was
        # recorded. Only these may be removed by a rollback, and only empty.
        self._created_dirs: list[Path] = []

    def before_mutation(self, path: Path) -> None:
        self._record_missing_parents(path)
        if path in self._before:
            return
        if path.is_file():
            retained = sum(len(before.content) for before in self._before.values()
                           if before.content is not None)
            remaining = max(0, MAX_CHECKPOINT_BYTES - retained)
            message = (
                f"rollback checkpoint budget exceeded ({MAX_CHECKPOINT_BYTES} bytes per task); "
                f"file was not changed: {self._workspace.relative(path)}"
            )
            if path.stat().st_size > remaining:
                raise SandboxError(message)
            # Size can change after stat. Bound the actual read as well, with
            # one extra byte to detect overflow before storing a snapshot.
            with path.open("rb") as handle:
                content = handle.read(remaining + 1)
            if len(content) > remaining:
                raise SandboxError(message)
            self._before[path] = _Before(content, _file_mode(path))
        else:
            self._before[path] = _Before(None)

    def _record_missing_parents(self, path: Path) -> None:
        root = self._workspace.root
        parent = path.parent
        while (
            parent != root
            and parent.is_relative_to(root)
            and not os.path.lexists(parent)
        ):
            if parent not in self._created_dirs:
                self._created_dirs.append(parent)
            parent = parent.parent

    def touched(self) -> tuple[str, ...]:
        return tuple(self._workspace.relative(path) for path in self._before)

    def commit(self) -> None:
        """Accept the changes so far: a later rollback will not undo them.

        Called when a task ends with its changes kept. Without it, rolling back
        a failed task would also undo every task before it in the session.
        """
        self._before.clear()
        self._created_dirs.clear()

    def rollback(self) -> tuple[str, ...]:
        """Restore every touched file. Returns what was restored, in order.

        Three passes, so a path the run turned from a file into a directory
        (`delete_file x`, then `write_file x/y.txt`) gets its file back: the
        files the run created are removed, then the directories it created
        (deepest first, and only when empty), then the originals are written
        back with their modes.

        One file that cannot be restored does not strand the others: each is
        tried, and then RollbackIncomplete names what was not restored. Those
        files keep their checkpoints, so nothing about them is forgotten."""
        outcome: dict[Path, bool] = {}
        entries = list(reversed(list(self._before.items())))
        for path, before in entries:
            if before.content is None:
                try:
                    if path.is_file():
                        path.unlink()
                    outcome[path] = True
                except OSError:
                    outcome[path] = False
        self._remove_created_dirs()
        for path, before in entries:
            if before.content is not None:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_bytes(path, before.content, mode=before.mode)
                    outcome[path] = True
                except OSError:
                    outcome[path] = False
        restored: list[str] = []
        unrestored: list[str] = []
        for path, _ in entries:
            if outcome[path]:
                restored.append(self._workspace.relative(path))
                del self._before[path]
            else:
                unrestored.append(self._workspace.relative(path))
        if unrestored:
            raise RollbackIncomplete(tuple(restored), tuple(unrestored))
        self._created_dirs.clear()
        return tuple(restored)

    def _remove_created_dirs(self) -> None:
        """Remove the directories the run created, deepest first, if empty.

        A directory that existed before the run is never on the list; one
        that holds anything the rollback did not remove is left, and kept on
        the list in case a later rollback attempt empties it."""
        kept: list[Path] = []
        for directory in sorted(self._created_dirs, key=lambda p: len(p.parts), reverse=True):
            if directory.is_symlink() or not directory.is_dir():
                continue
            try:
                directory.rmdir()
            except OSError:
                kept.append(directory)
        self._created_dirs = kept
