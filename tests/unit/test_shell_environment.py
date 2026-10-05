"""The shell sees a fixed set of environment variables, never the rest.

Gap analysis P0-5. The shell inherited the owner's environment minus names
that looked secret, so a secret named any other way -- AWS_ACCESS_KEY_ID, a
project's own token name -- was one `env` or `set` away from the model. Now
only SHELL_ENV_NAMES pass, with the owner's values, and the owner's tools
still run as they do in a terminal: git finds its global config, Python its
temporary folder.
"""
from __future__ import annotations

import json
import os
import shutil
import sys

import pytest

from personal_ai_core.agent.sandbox import Workspace
from personal_ai_core.agent.tools import SHELL_ENV_NAMES, Shell, shell_environment

# Shaped like secrets, named so no word in them says so. Obviously fake values.
UNLISTED = {
    "AWS_ACCESS_KEY_ID": "AKIAEXAMPLEEXAMPLE00",
    "AWS_SECRET_ACCESS_KEY": "fake-secret-for-tests",
    "ACME_DEPLOY_CREDS": "fake-creds-for-tests",
    "HTTPS_PROXY": "http://user:fake-password@proxy.invalid:8080",
    "GITHUB_TOKEN": "fake-token-for-tests",
    "DATABASE_URL": "postgresql://u:fake@db.invalid/x",
    "PAC_WORKSPACE_NOTE": "anything at all",
}


@pytest.fixture
def unlisted(monkeypatch):
    for name, value in UNLISTED.items():
        monkeypatch.setenv(name, value)
    return UNLISTED


def test_nothing_unlisted_is_passed_whatever_it_is_called(unlisted):
    env = shell_environment()
    assert not set(unlisted) & set(env)
    assert all(name.upper() in SHELL_ENV_NAMES for name in env if name != "GIT_TERMINAL_PROMPT")
    for value in unlisted.values():
        assert value not in env.values()


def test_the_listed_names_pass_with_the_owners_values(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TEMP", str(tmp_path / "temp"))
    monkeypatch.setenv("LANG", "ar_AE.UTF-8")
    env = shell_environment()
    assert env["HOME"] == str(tmp_path)
    assert env["TEMP"] == str(tmp_path / "temp")
    assert env["LANG"] == "ar_AE.UTF-8"
    assert env["PATH"] == os.environ["PATH"]


def test_a_listed_name_is_matched_without_regard_to_case(monkeypatch):
    # Windows names are case-insensitive: `Path` and `SystemRoot` are PATH
    # and SYSTEMROOT. A plain mapping keeps the case as written, on every
    # platform, so this runs everywhere (os.environ folds case on Windows).
    monkeypatch.setattr(os, "environ", {"SystemRoot": r"C:\Windows", "Acme_Token": "fake"})
    assert shell_environment() == {"SystemRoot": r"C:\Windows", "GIT_TERMINAL_PROMPT": "0"}


def test_git_never_waits_for_a_terminal():
    assert shell_environment()["GIT_TERMINAL_PROMPT"] == "0"


def test_no_listed_name_is_a_secret_or_a_proxy():
    """The list is locations, locale and system settings; a review guard."""
    for word in ("TOKEN", "SECRET", "PASSWORD", "KEY", "AUTH", "CRED", "PROXY", "URL"):
        assert not any(word in name for name in SHELL_ENV_NAMES), word


# --- through the shell, in a real child process ---------------------------------


def _shell(tmp_path) -> Shell:
    root = tmp_path / "ws"
    root.mkdir()
    return Shell(Workspace(root))


def test_a_command_in_the_shell_cannot_read_an_unlisted_variable(unlisted, tmp_path):
    script = "import json, os; print(json.dumps(sorted(os.environ)))"
    result = _shell(tmp_path).run({"command": f'"{sys.executable}" -c "{script}"'})
    assert result.ok, result.error
    seen = {name.upper() for name in json.loads(result.output.strip().splitlines()[-1])}
    assert not {name.upper() for name in unlisted} & seen


def test_python_in_the_shell_finds_the_owners_temporary_folder(tmp_path, monkeypatch):
    temp = tmp_path / "owner-temp"
    temp.mkdir()
    for name in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(name, str(temp))
    script = "import tempfile; print(tempfile.gettempdir())"
    result = _shell(tmp_path).run({"command": f'"{sys.executable}" -c "{script}"'})
    assert result.ok, result.error
    assert os.path.samefile(result.output.strip().splitlines()[-1], temp)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_in_the_shell_finds_the_owners_global_identity(tmp_path, monkeypatch):
    # The wip version of this fix set HOME to the workspace, and a commit lost
    # the owner's identity. The owner's home is passed as it is.
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[user]\n\tname = Owner Example\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    result = _shell(tmp_path).run({"command": "git config --global user.name"})
    assert result.ok, result.error
    assert result.output.strip() == "Owner Example"
