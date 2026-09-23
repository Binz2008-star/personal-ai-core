"""The workspace sandbox -- adapted PathResolver plus protected files.

The first half repeats, against the Core, the escapes the characterization
tests show the source refusing: parity. The second half pins what changed.
"""
from __future__ import annotations

import pytest

from personal_ai_core.agent.sandbox import SandboxError, Workspace, is_protected


def link(path, target):
    """Windows grants symlink creation only with a privilege CI may lack."""
    try:
        path.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"cannot create a symlink here: {exc}")


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return Workspace(root)


@pytest.mark.parametrize(
    "path",
    ["", "a\x00b", "/etc/passwd", "C:\\x", "C:/x", "//server/share", "\\\\server\\share",
     "../up", "a/../../up", ".git/config", "src/.GIT/HEAD"],
)
def test_escapes_are_refused(workspace, path):
    with pytest.raises(SandboxError):
        workspace.resolve(path)


def test_traversal_is_refused_even_when_it_would_land_inside(workspace):
    """Parity with the source, which refuses any `..` segment. The escape
    check alone would let `a/../b.txt` through, since it resolves inside."""
    with pytest.raises(SandboxError, match="traversal"):
        workspace.resolve("a/../b.txt")


def test_a_nested_path_resolves_inside(workspace):
    assert workspace.resolve("a/b.txt") == workspace.root / "a" / "b.txt"


def test_a_symlink_out_of_the_workspace_is_refused(tmp_path, workspace):
    outside = tmp_path / "outside"
    outside.mkdir()
    link(workspace.root / "link", outside)
    with pytest.raises(SandboxError, match="symlink"):
        workspace.resolve("link/secret.txt")


def test_a_symlink_inside_the_workspace_is_fine(workspace):
    (workspace.root / "real").mkdir()
    link(workspace.root / "alias", workspace.root / "real")
    assert workspace.resolve("alias/x.txt") == workspace.root / "real" / "x.txt"


def test_the_root_must_exist(tmp_path):
    with pytest.raises(ValueError):
        Workspace(tmp_path / "missing")


def test_two_workspaces_do_not_share_a_root(tmp_path):
    """The source kept one module-global resolver; this is the change."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first, second = Workspace(tmp_path / "a"), Workspace(tmp_path / "b")
    assert first.resolve("x") != second.resolve("x")


# --- protected files -------------------------------------------------------


@pytest.mark.parametrize(
    "name", [".env", ".env.production", "credentials.json", "id_rsa", "server.pem", "cert.KEY"]
)
def test_secrets_cannot_be_written(workspace, name):
    with pytest.raises(SandboxError, match="protected"):
        workspace.resolve_for_write(f"config/{name}")


@pytest.mark.parametrize("name", [".env", "id_rsa"])
def test_secrets_are_still_refused_for_reading_only_by_their_tool(workspace, name):
    """`resolve` does not refuse them: whether a secret may be READ is the
    tool's decision, and stage 2's read tool refuses it."""
    assert workspace.resolve(name) == workspace.root / name


def test_templates_stay_writable(workspace):
    assert not is_protected(workspace.root / ".env.example")
    workspace.resolve_for_write(".env.example")


def test_the_relative_path_is_posix(workspace):
    assert workspace.relative(workspace.root / "a" / "b.txt") == "a/b.txt"
