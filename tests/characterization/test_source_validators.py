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


def _source_lines(path: str, start: int, end: int) -> list[str]:
    shown = subprocess.run(
        ["git", "-C", str(SOURCE_CHECKOUT), "show", f"{PINNED}:{path}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return shown[start - 1 : end]


@pytest.mark.skipif(
    not (SOURCE_CHECKOUT / ".git").exists(), reason="source checkout not present"
)
def test_the_snapshot_matches_the_source():
    snapshot = (Path(__file__).parent / "source_21a36b0.py").read_text().splitlines()
    for path, start, end in (
        ("tool_security.py", 39, 136),
        ("tool_security.py", 190, 274),
        ("path_security.py", 24, 196),
    ):
        block = _source_lines(path, start, end)
        joined = "\n".join(snapshot)
        assert "\n".join(block) in joined, f"{path} {start}-{end} drifted from {PINNED}"


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
