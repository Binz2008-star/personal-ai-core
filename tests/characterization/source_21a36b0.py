"""VERBATIM snapshot of the source validators, for characterization only.

Source: unified-llm-local @ 21a36b0765d89390eed63da094977e4bb8e5b4c2
  tool_security.py lines 39-136 (allowlist and blocklists), 190-274 (validate_command,
  validate_path); path_security.py lines 24-196 (PathSecurityError, PathResolver).

This is NOT production code and nothing in src/ imports it. It exists so the
characterization tests can pin what the source does -- including the weaknesses the
Core's adaptation deliberately does not carry over -- without the source checkout
being present in CI. test_the_snapshot_matches_the_source checks it against the
source file when the checkout is available.
"""
# ruff: noqa
# fmt: off
import os
import re
import shlex
import stat as _stat
from pathlib import Path

# ── Allowlist ────────────────────────────────────────────────────
# Only these executables may be invoked by the agent.
ALLOWED_EXECUTABLES: set[str] = {
    # Python
    "python",
    "python3",
    "pip",
    "pip3",
    "pytest",
    "ruff",
    "mypy",
    "bandit",
    # Node
    "node",
    "npm",
    "npx",
    "pnpm",
    "yarn",
    # Git (read-only operations handled separately)
    "git",
    # Make / build
    "make",
    "cargo",
    "go",
    # System utilities (safe)
    "cat",
    "head",
    "tail",
    "wc",
    "grep",
    "find",
    "ls",
    "echo",
    "pwd",
    "which",
    "uname",
    "date",
}

# ── Blocked arguments ────────────────────────────────────────────
# These patterns in command arguments cause rejection.
BLOCKED_ARGUMENT_PATTERNS: list[str] = [
    # Git destructive
    "push",
    "force",
    "--hard",
    "--force-with-lease",
    "reset",
    "clean",
    "checkout",
    "branch",
    "-d",
    "-D",
    # Destructive filesystem
    "rm",
    "rmdir",
    "del",
    "format",
    "mkfs",
    "dd",
    # System
    "sudo",
    "chmod",
    "chown",
    "shutdown",
    "reboot",
    "halt",
    "poweroff",
    "init",
    # Network (dangerous)
    "curl",
    "wget",
    "nc",
    "ncat",
    "socat",
    "ssh",
    "scp",
    "rsync",
    # Eval / code execution
    "eval",
    "exec",
]

# ── Git read-only commands (always allowed) ──────────────────────
GIT_SAFE_COMMANDS: set[str] = {
    "status",
    "log",
    "diff",
    "show",
    "branch",
    "remote",
    "rev-parse",
    "rev-list",
    "describe",
    "tag",
    "stash",
    "blame",
}


def validate_command(command: str, workspace: Path, trusted: bool = False) -> list[str]:
    """
    Validate and parse a command string.

    Returns parsed args list if allowed, raises PermissionError otherwise.

    Args:
        trusted: If True, bypasses argument checks for internal operations
                 (worktree cleanup, etc.). Never expose to agent/LLM.
    """
    if not command or not command.strip():
        raise PermissionError("Empty command")

    try:
        parts = shlex.split(command)
    except ValueError as e:
        raise PermissionError(f"Invalid command syntax: {e}")

    if not parts:
        raise PermissionError("Empty command after parsing")

    executable = Path(parts[0]).name.lower()

    # Check if executable is in allowlist
    if executable not in ALLOWED_EXECUTABLES:
        raise PermissionError(f"Executable not allowed: {executable}")

    # For non-trusted commands, check blocked arguments
    if not trusted:
        # For git, check if the subcommand is safe
        if executable == "git" and len(parts) > 1:
            subcmd = parts[1].lower().lstrip("-")
            if subcmd not in GIT_SAFE_COMMANDS:
                # Check if it's a blocked argument
                for arg in parts[1:]:
                    if arg.lower().lstrip("-") in BLOCKED_ARGUMENT_PATTERNS:
                        raise PermissionError(f"Blocked git argument: {arg}")

        # Check blocked argument patterns
        for arg in parts[1:]:
            arg_lower = arg.lower().lstrip("-")
            if arg_lower in BLOCKED_ARGUMENT_PATTERNS:
                raise PermissionError(f"Blocked argument: {arg}")

        # Check for shell metacharacters
        dangerous_chars = set("|;&$`!{}()[]")
        for part in parts:
            if any(c in part for c in dangerous_chars):
                raise PermissionError(f"Shell metacharacter not allowed in: {part}")

    return parts


def validate_path(file_path: str, workspace: Path) -> Path:
    """
    Validate that a file path stays within the workspace.
    """
    if not file_path:
        raise PermissionError("Empty path")

    # Normalize separators
    normalized = file_path.replace("\\", "/")

    # Reject absolute paths
    if normalized.startswith("/") or (len(normalized) > 1 and normalized[1] == ":"):
        raise PermissionError(f"Absolute path not allowed: {file_path}")

    # Reject traversal
    if ".." in normalized.split("/"):
        raise PermissionError(f"Path traversal not allowed: {file_path}")

    # Reject .git access
    parts = normalized.split("/")
    if any(p.lower() == ".git" for p in parts):
        raise PermissionError(f".git access not allowed: {file_path}")

    resolved = (workspace / normalized).resolve()

    # Verify inside workspace
    try:
        resolved.relative_to(workspace.resolve())
    except ValueError:
        raise PermissionError(f"Path escapes workspace: {file_path}")

    return resolved


class PathSecurityError(Exception):
    """Raised when a path violates security constraints."""

    pass


class PathResolver:
    """Central path resolver for repository security."""

    def __init__(self, root: Path):
        """
        Initialize the path resolver.

        Args:
            root: The allowed root directory. All resolved paths must be inside this.
        """
        self.root = root.resolve()

    def resolve(self, file_path: str, must_exist: bool = False) -> Path:
        """
        Validate and resolve a file path against the root directory.

        Args:
            file_path: The path to validate (relative to root)
            must_exist: If True, raise if the resolved path does not exist

        Returns:
            Canonical Path inside root

        Raises:
            PathSecurityError: If the path violates security constraints
        """
        if not file_path:
            raise PathSecurityError("Empty path not allowed")

        # Normalize path separators
        normalized = file_path.replace("\\", "/")

        # Check for null bytes
        if "\x00" in normalized:
            raise PathSecurityError(f"Null bytes not allowed in path: {file_path}")

        # Reject absolute paths
        if self._is_absolute(normalized):
            raise PathSecurityError(f"Absolute path not allowed: {file_path}")

        # Check for path traversal
        if ".." in normalized.split("/"):
            raise PathSecurityError(f"Path traversal not allowed: {file_path}")

        # Check for .git traversal
        parts = normalized.split("/")
        if any(part.lower() == ".git" for part in parts):
            raise PathSecurityError(f".git traversal not allowed: {file_path}")

        # Resolve the path
        p = Path(normalized)
        resolved = (self.root / p).resolve()

        # Verify the path is inside root using relative_to (more robust than string prefix)
        try:
            resolved.relative_to(self.root)
        except ValueError:
            raise PathSecurityError(f"Path escapes root directory: {file_path}")

        # Check for symlink escapes
        self._check_symlink_escape(resolved, file_path)

        if must_exist and not resolved.exists():
            raise PathSecurityError(f"Path does not exist: {file_path}")

        return resolved

    def _is_absolute(self, path: str) -> bool:
        """Check if a path is absolute, handling Windows and UNC paths."""
        # Unix absolute path
        if path.startswith("/"):
            return True

        # Windows absolute path (C:\, D:\, etc.)
        if re.match(r"^[a-zA-Z]:[\\/]", path):
            return True

        # Windows UNC path (\\server\share)
        if path.startswith("\\\\") or path.startswith("//"):
            return True

        return False

    def _check_symlink_escape(self, resolved: Path, original_path: str):
        """Check if a resolved path escapes the root through symlinks.

        Checks the ORIGINAL (un-resolved) path components for symlinks,
        because resolve() collapses symlinks and is_symlink() would return False.
        We also verify the final resolved target is inside root.
        """
        try:
            # Verify the final resolved path is inside root
            try:
                resolved.relative_to(self.root)
            except ValueError:
                raise PathSecurityError(
                    f"Path escapes root directory after resolution: {original_path} -> {resolved}"
                )

            # Walk the ORIGINAL path components (before resolve) to detect symlinks
            # Use os.lstat which does NOT follow symlinks, unlike stat()
            original_parts = Path(original_path.replace("\\", "/")).parts
            current = self.root
            for part in original_parts:
                current = current / part
                try:
                    # lstat returns info without following symlinks
                    # If it's a symlink, st_mode will indicate it
                    st = os.lstat(str(current))
                    if _stat.S_ISLNK(st.st_mode):
                        # Resolve this specific symlink and check target
                        target = current.resolve()
                        try:
                            target.relative_to(self.root)
                        except ValueError:
                            raise PathSecurityError(
                                f"Symlink escape detected: {original_path} "
                                f"(component '{part}' -> {target})"
                            )
                except (OSError, FileNotFoundError):
                    # Path component doesn't exist yet — fine, can't be a symlink
                    pass
        except PathSecurityError:
            raise
        except OSError as e:
            raise PathSecurityError(f"Error checking symlinks: {e}")

    def validate_file_path(self, file_path: str) -> bool:
        """
        Validate a file path without resolving.

        Returns True if valid, raises PathSecurityError otherwise.
        """
        self.resolve(file_path)
        return True

    def safe_join(self, *parts: str) -> Path:
        """
        Safely join path parts and validate against root.

        Args:
            *parts: Path components to join

        Returns:
            Validated Path inside root
        """
        joined = "/".join(parts)
        return self.resolve(joined)

    def get_relative_path(self, absolute_path: Path) -> str:
        """
        Get a relative path from root for an absolute path.

        Args:
            absolute_path: Absolute path to convert

        Returns:
            Relative path string

        Raises:
            PathSecurityError: If the path is outside root
        """
        resolved = absolute_path.resolve()
        try:
            return str(resolved.relative_to(self.root))
        except ValueError:
            raise PathSecurityError(f"Path is outside root: {absolute_path}")
