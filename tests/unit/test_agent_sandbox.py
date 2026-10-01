"""The workspace sandbox -- adapted PathResolver plus protected files.

The first half repeats, against the Core, the escapes the characterization
tests show the source refusing: parity. The second half pins what changed.
"""
from __future__ import annotations

import pytest

from personal_ai_core.agent.sandbox import (
    SandboxError,
    Workspace,
    is_protected,
    windows_alias_reason,
)


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


# --- other spellings of a protected name -----------------------------------
#
# On Windows, `.git.` and `.git ` are `.git`, `.git::$INDEX_ALLOCATION` is
# `.git`, and `.env::$DATA` is `.env`. Measured on the owner's machine: through
# the real executor and the default policy, with no confirmation asked,
# `write_file` created `.git/hooks/pre-commit` by way of `.git./hooks/...` and
# created a protected `.env` by way of `.env::$DATA`, and `read_file` read
# `.git/config`. The name checks compare spellings, so each spelling had to be
# refused, not only the plain one.


@pytest.fixture
def windows_names(monkeypatch):
    """Switch the Windows spelling rule on, so the rule is tested on every
    platform rather than only where the filesystem would have aliased the name."""
    monkeypatch.setattr("personal_ai_core.agent.sandbox._windows_names_apply", lambda: True)


@pytest.mark.parametrize(
    "part",
    [".git.", ".git ", "notes.", "notes ", "...", ".env::$DATA", ".git::$INDEX_ALLOCATION",
     "file.txt:stream", "a:b"],
)
def test_a_name_windows_would_read_as_another_has_a_reason(part):
    assert windows_alias_reason(part)


@pytest.mark.parametrize(
    "part",
    ["", ".", "notes.txt", ".env", ".git", ".github", "a b", "v1.2", "my notes.md", "ملاحظات.txt",
     "file-name_1", ".gitignore"],
)
def test_an_ordinary_name_has_no_reason(part):
    assert windows_alias_reason(part) is None


@pytest.mark.parametrize(
    "path",
    [".git./config", ".git /config", ".git::$INDEX_ALLOCATION/config", "sub/.git./hooks/pre-commit",
     "sub/.GIT ./config"],
)
def test_other_spellings_of_git_are_refused(workspace, windows_names, path):
    with pytest.raises(SandboxError):
        workspace.resolve(path)


@pytest.mark.parametrize(
    "path",
    [".env::$DATA", "config/.env::$DATA", "credentials.json::$DATA", "config/.env.", "config/.env ",
     "server.pem::$DATA"],
)
def test_other_spellings_of_a_protected_file_cannot_be_written(workspace, windows_names, path):
    with pytest.raises(SandboxError):
        workspace.resolve_for_write(path)


@pytest.mark.parametrize(
    "path",
    ["notes.txt", "a b/c d.txt", "v1.2/readme.md", ".github/workflows/tests.yml", ".gitignore",
     "ملاحظات/ملف.txt", "src/./main.py"],
)
def test_ordinary_paths_still_resolve_with_the_rule_on(workspace, windows_names, path):
    assert workspace.resolve(path).is_relative_to(workspace.root)


def test_a_link_to_git_inside_the_workspace_is_refused(workspace):
    """What a spelling check cannot see through is judged by where it resolves.
    The same property holds for an 8.3 short name such as `GIT~1`."""
    (workspace.root / ".git").mkdir()
    link(workspace.root / "alias", workspace.root / ".git")
    with pytest.raises(SandboxError, match=r"\.git"):
        workspace.resolve("alias/config")


def test_a_short_name_for_git_is_refused_where_the_volume_has_one(workspace):
    """Only meaningful on a Windows volume that makes 8.3 names; elsewhere
    `GIT~1` does not exist and there is nothing to refuse. Not a skip: an
    unmet condition here must not read as a skipped test in the audit."""
    (workspace.root / ".git").mkdir()
    if (workspace.root / "GIT~1").exists():
        with pytest.raises(SandboxError, match=r"\.git"):
            workspace.resolve("GIT~1/config")


def test_an_existing_protected_file_reached_by_its_short_name_is_still_refused(workspace):
    """`is_protected` judges the RESOLVED path, so the long name decides and
    `ENV~1` is the same file as `.env`. Same condition as above: only where the
    volume makes 8.3 names, and not a skip."""
    config = workspace.root / "config"
    config.mkdir()
    (config / ".env").write_text("x", encoding="utf-8")
    if (config / "ENV~1").exists():
        with pytest.raises(SandboxError, match="protected"):
            workspace.resolve_for_write("config/ENV~1")
