"""What unified-llm-local @ 21a36b0 ACTUALLY does -- ADR-004, step 1 of ADAPT.

COMPONENT_EXTRACTION_MATRIX.md row 1: "Characterize -- write tests against
tool_security.py at 21a36b0 pinning its current behaviour", before anything is
adapted. These tests assert the source's behaviour, not the behaviour we want.
Where the two differ, the test here pins the SOURCE, and names the fix in
src/personal_ai_core/agent/ that departs from it.

The source's own ~962 lines of tests are evidence the logic is sound where it
was tested. These found where it was not.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from . import source_21a36b0 as source
from .source_cases import SOURCE_LETS_THROUGH, SOURCE_REFUSES

SOURCE_CHECKOUT = Path(
    os.environ.get("UNIFIED_LLM_LOCAL", "/home/user/binz2008-star/unified-llm-local")
)
PINNED = "21a36b0765d89390eed63da094977e4bb8e5b4c2"


def allowed(command: str, workspace: Path) -> bool:
    try:
        source.validate_command(command, workspace)
    except PermissionError:
        return False
    return True


# --- the snapshot is the source -------------------------------------------


# Each block of the snapshot: where it came from, its first line, its length,
# and the SHA-256 of those lines at 21a36b0 -- computed from `git show` of the
# pinned commit when the snapshot was taken.
BLOCKS = (
    ("tool_security.py", 39, 136, "# ── Allowlist",
     "c87ef54a9ad0883e8963df73fbbfadd3346876b7133b18b209743519f0465188"),
    ("tool_security.py", 190, 274, "def validate_command(",
     "5fc933f640944da001eec7464d06a67b153a2a00744b7c861b31908e12137141"),
    ("path_security.py", 24, 196, "class PathSecurityError(",
     "bcde60df7d0064f418e6da0fcd5ec812b678778c467bdd3333eb6317135e93fb"),
)


def _digest(lines: list[str]) -> str:
    return hashlib.sha256("".join(line + "\n" for line in lines).encode("utf-8")).hexdigest()


def _snapshot_block(first_line: str, length: int) -> list[str]:
    snapshot = (Path(__file__).parent / "source_21a36b0.py").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(snapshot) if line.startswith(first_line))
    return snapshot[start : start + length]


def test_the_snapshot_is_the_pinned_source():
    """Runs everywhere, no skip. The hashes pin the snapshot to what 21a36b0
    held; where the source checkout exists (a developer's machine), the source
    itself is read as well, so the hashes are re-proven rather than trusted."""
    for path, start, end, first_line, expected in BLOCKS:
        block = _snapshot_block(first_line, end - start + 1)
        assert _digest(block) == expected, f"snapshot of {path} {start}-{end} was edited"
        if (SOURCE_CHECKOUT / ".git").exists():
            shown = subprocess.run(
                ["git", "-C", str(SOURCE_CHECKOUT), "show", f"{PINNED}:{path}"],
                capture_output=True, text=True, encoding="utf-8", check=True,
            ).stdout.splitlines()
            assert _digest(shown[start - 1 : end]) == expected, f"{path} at {PINNED} differs"


# --- what the source gets right, and the Core keeps ------------------------


@pytest.mark.parametrize(
    "command",
    ["ls -la", "git status", "git log --oneline", "pytest -q", "grep -r needle src"],
)
def test_source_allows_ordinary_inspection(command, tmp_path):
    assert allowed(command, tmp_path)


@pytest.mark.parametrize("command", SOURCE_REFUSES)
def test_source_refuses_the_obvious(command, tmp_path):
    assert not allowed(command, tmp_path)


@pytest.mark.parametrize("path", ["/etc/passwd", "C:\\x", "../up", "a/../../up", ".git/config"])
def test_source_path_validation_refuses_escapes(path, tmp_path):
    with pytest.raises(PermissionError):
        source.validate_path(path, tmp_path)


def test_source_path_validation_allows_a_nested_file(tmp_path):
    assert source.validate_path("a/b.txt", tmp_path) == (tmp_path / "a" / "b.txt").resolve()


@pytest.mark.parametrize("path", ["a\x00b", "//server/share", "C:/x"])
def test_source_resolver_refuses_null_bytes_unc_and_drives(path, tmp_path):
    with pytest.raises(source.PathSecurityError):
        source.PathResolver(tmp_path).resolve(path)


def test_source_resolver_catches_a_symlink_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    try:
        (root / "link").symlink_to(outside)
    except OSError as exc:  # Windows without the symlink privilege
        pytest.skip(f"cannot create a symlink here: {exc}")
    with pytest.raises(source.PathSecurityError):
        source.PathResolver(root).resolve("link/secret.txt")


# --- what the source lets through: each is closed in agent/commands.py ------
#
# These PASS against the source. That is the finding. Each names the numbered
# fix in agent/commands.py; tests/unit/test_agent_commands.py pins the fix.


@pytest.mark.parametrize("command, fix", SOURCE_LETS_THROUGH)
def test_source_lets_through(command, fix, tmp_path):
    assert allowed(command, tmp_path), f"the source now refuses {command!r} (fix {fix})"


def test_source_lists_contradict_each_other_on_git_branch(tmp_path):
    """`branch` is in GIT_SAFE_COMMANDS and in BLOCKED_ARGUMENT_PATTERNS."""
    assert "branch" in source.GIT_SAFE_COMMANDS
    assert "branch" in source.BLOCKED_ARGUMENT_PATTERNS
    assert not allowed("git branch", tmp_path)
