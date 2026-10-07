"""The workspace sandbox: every path the agent touches is resolved here.

ADAPTED from unified-llm-local @ 21a36b0 (ADR-004):
  - path_security.py `PathResolver.resolve` and `_check_symlink_escape`
  - protected_path_policy.py `PROTECTED_BASENAMES`, `PROTECTED_SUFFIXES`

Carried over unchanged in behaviour (pinned by
tests/characterization/test_source_validators.py): empty, null-byte, absolute
(POSIX, drive-letter and UNC), traversal and `.git` paths are refused; a path
that resolves outside the root is refused; so is one that passes through a
symlink pointing outside it.

Changed from the source, deliberately:
  - One resolver per `Workspace` object. The source kept a module-global
    resolver defaulting to the directory the module lived in -- a sandbox
    whose root is wherever the code happens to be installed.
  - Protected files are refused for WRITE here, at resolution, rather than by
    each tool remembering to ask. The source's template carve-out
    (`.env.example` writable) is kept.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Iterable


class SandboxError(PermissionError):
    """A path the agent may not use. The message says why, and is shown."""


# From protected_path_policy.py: secrets and credentials. The agent may not
# write, overwrite or create them, whatever it is asked.
PROTECTED_BASENAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.development",
        ".env.test",
        ".env.production",
        ".env.prod",
        ".env.staging",
        ".env.backup",
        "credentials.json",
        "secrets.json",
        "token.json",
        "client_secret.json",
        "id_rsa",
        "id_ed25519",
        "id_dsa",
        "id_ecdsa",
    }
)
PROTECTED_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks")
WRITEABLE_TEMPLATES = frozenset({".env.example", ".env.template", ".env.sample"})

_DRIVE = re.compile(r"^[a-zA-Z]:[\\/]")


def _windows_names_apply() -> bool:
    """Whether this platform reads a path component as something other than its
    spelling. A function, so a test can switch it on anywhere."""
    return os.name == "nt"


def windows_alias_reason(part: str) -> str | None:
    """Why Windows would read `part` as another name, or None for an ordinary one.

    The checks below compare names as written, and Windows maps several
    spellings onto one file: it drops trailing dots and spaces (`.git.` is
    `.git`), and reads `name:stream` as an NTFS alternate data stream of `name`
    (`.env::$DATA` is `.env`, `.git::$INDEX_ALLOCATION` is `.git`). None of
    these is a name a file can really have, so none is allowed through.
    """
    if part in ("", "."):
        return None
    if ":" in part:
        return "a ':' in a path component names an NTFS stream, not a file"
    if part.endswith((".", " ")):
        return "Windows drops a trailing dot or space, so this is another name"
    return None


def is_protected(path: Path) -> bool:
    name = path.name.lower()
    if name in WRITEABLE_TEMPLATES:
        return False
    return name in PROTECTED_BASENAMES or name.endswith(PROTECTED_SUFFIXES)


# A live SQLite database is up to four files, and each of them IS the
# database: overwriting the journal or the WAL corrupts it as surely as
# overwriting the main file.
SQLITE_COMPANIONS = ("", "-journal", "-wal", "-shm")


class Workspace:
    """A directory the agent is confined to.

    `reserved` names files the Core itself owns -- its database -- that the
    agent must not reach even when they lie inside the workspace (F-1: with
    the default database under `~/.personal-ai-core/`, `--workspace ~`
    contains it). Each is reserved together with its SQLite companion files,
    and a reserved file is refused by `resolve`, so for every tool and every
    purpose: read, search, write and delete. Listing hides it.

    `owner_files` are the owner's profile and projects files (N4). They are
    composed into every later turn's instructions, so an agent that could
    write one -- MEDIUM risk, never asked -- could rewrite what every later
    conversation obeys. They are reserved exactly as the database is, but
    alone: they have no companion files. A path that does not exist yet is
    reserved too, so the agent cannot create a profile where there was none.
    """

    def __init__(
        self,
        root: Path,
        *,
        reserved: Iterable[Path] = (),
        owner_files: Iterable[Path] = (),
    ) -> None:
        resolved = Path(root).resolve()
        if not resolved.is_dir():
            raise ValueError(f"workspace is not a directory: {root}")
        self.root = resolved
        self._reserved = tuple(
            Path(str(Path(path).resolve()) + companion)
            for path in reserved
            for companion in SQLITE_COMPANIONS
        )
        self._owner_files = tuple(Path(path).resolve() for path in owner_files)

    def is_reserved(self, path: Path) -> bool:
        """Whether `path` is, or is the same file as, a reserved one -- the
        database (with its companions) or one of the owner's files.

        Compared by resolved name (case-folded where the platform folds case)
        and, for files that exist, by identity: a hard link, or a spelling a
        case-insensitive filesystem maps to the same file, is the same file.
        """
        return self._matches(path, self._reserved) or self._matches(path, self._owner_files)

    @staticmethod
    def _matches(path: Path, reserved_paths: tuple[Path, ...]) -> bool:
        candidate = Path(path).resolve()
        folded = os.path.normcase(str(candidate))
        for reserved in reserved_paths:
            if folded == os.path.normcase(str(reserved)):
                return True
            try:
                if candidate.exists() and reserved.exists() and os.path.samefile(
                    candidate, reserved
                ):
                    return True
            except OSError:
                continue
        return False

    def is_hard_link_to_protected(self, path: Path) -> bool:
        """Whether `path` is the same file as a protected-named file in the root.

        Name protection alone misses a hard link: `alias.txt -> .env` carries an
        ordinary name, so `is_protected("alias.txt")` is False and the link's
        contents reach the model. A hard link IS the same file, so this compares
        by identity instead of spelling -- but only for regular files that could
        have a link (st_nlink > 1), so ordinary files pay no scan.
        """
        try:
            info = path.stat()
        except OSError:
            return False
        if not stat.S_ISREG(info.st_mode) or info.st_nlink < 2:
            return False
        for protected in self._protected_files():
            try:
                if os.path.samefile(path, protected):
                    return True
            except OSError:
                continue
        return False

    def _protected_files(self) -> Iterable[Path]:
        """Protected-named files under the root, for identity comparison."""
        for directory, subdirectories, files in os.walk(self.root):
            subdirectories[:] = [d for d in subdirectories if d.lower() != ".git"]
            for name in files:
                if is_protected(Path(name)):
                    yield Path(directory) / name

    def resolve(self, path: str) -> Path:
        """A path inside the workspace, or SandboxError. For reading."""
        if not path:
            raise SandboxError("empty path")
        normalized = path.replace("\\", "/")
        if "\x00" in normalized:
            raise SandboxError(f"null byte in path: {path!r}")
        if normalized.startswith("/") or normalized.startswith("//") or _DRIVE.match(path):
            raise SandboxError(f"absolute path not allowed: {path}")
        parts = normalized.split("/")
        if ".." in parts:
            raise SandboxError(f"path traversal not allowed: {path}")
        if any(part.lower() == ".git" for part in parts):
            raise SandboxError(f".git is not reachable from the workspace: {path}")
        if _windows_names_apply():
            for part in parts:
                reason = windows_alias_reason(part)
                if reason is not None:
                    raise SandboxError(f"{reason}: {path}")

        # Symlinks first, so an escape through one is reported as that and not
        # as a generic escape: the fix for the two is different.
        self._refuse_symlink_escape(normalized, path)
        resolved = (self.root / normalized).resolve()
        if not resolved.is_relative_to(self.root):
            raise SandboxError(f"path escapes the workspace: {path}")
        # The spelling was checked above; this checks what it resolved to. A
        # name the spelling check cannot see through -- an 8.3 short name
        # (`GIT~1`), or a link to the directory -- resolves to the real one.
        if any(part.lower() == ".git" for part in resolved.relative_to(self.root).parts):
            raise SandboxError(f".git is not reachable from the workspace: {path}")
        if self._matches(resolved, self._reserved):
            raise SandboxError(
                f"the Core's own database is not reachable from the workspace: {path}"
            )
        if self._matches(resolved, self._owner_files):
            raise SandboxError(
                f"the owner's profile is not reachable from the workspace: {path}"
            )
        return resolved

    def resolve_for_write(self, path: str) -> Path:
        """As `resolve`, and the target is not a protected file."""
        resolved = self.resolve(path)
        if is_protected(resolved) or self.is_hard_link_to_protected(resolved):
            raise SandboxError(f"protected file, the agent may not write it: {path}")
        return resolved

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def _refuse_symlink_escape(self, normalized: str, original: str) -> None:
        # Walk the UNRESOLVED components: `resolve()` has already collapsed
        # every link, so the resolved path cannot say whether it went through
        # one. lstat does not follow the link it describes.
        current = self.root
        for part in Path(normalized).parts:
            current = current / part
            try:
                mode = os.lstat(current).st_mode
            except OSError:
                return  # does not exist yet, so it cannot be a link
            if stat.S_ISLNK(mode) and not current.resolve().is_relative_to(self.root):
                raise SandboxError(
                    f"symlink escapes the workspace: {original} (via {part!r})"
                )
