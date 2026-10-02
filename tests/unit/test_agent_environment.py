"""The environment context (ADR-023 §2.1, unit 2): facts read by code, a bounded block.

What these pin, because each is a way the control could quietly stop being what the ADR
says: the supported test command is resolved in the ADR's order and never widens the
command policy; a command given by the contract or caller is never overridden by a
discovered one; nothing is executed to gather the facts; the block stays inside its
budget and drops its least important lines first.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from personal_ai_core.agent.commands import ALLOWED_EXECUTABLES, GIT_READ_ONLY
from personal_ai_core.agent.environment import (
    CALLER,
    CONFIGURATION,
    CONTRACT,
    DEFAULT_MAX_TOKENS,
    DISCOVERY,
    EnvironmentContext,
    EnvironmentFacts,
    SupportedTestCommand,
    gather,
    render,
    resolve_test_command,
)
from personal_ai_core.context.token_estimator import ScriptAwareTokenEstimator

ESTIMATE = ScriptAwareTokenEstimator().estimate


def _resolved(root: Path, **given: str) -> SupportedTestCommand:
    """The supported command where the test expects one; a missing one fails the test."""
    found = resolve_test_command(root, **given)
    assert found is not None, "expected a supported test command"
    return found


def _write(root: Path, name: str, text: str = "") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- the system -------------------------------------------------------------------


@pytest.mark.parametrize("system, shell", [("Windows", "cmd"), ("Linux", "sh"), ("Darwin", "sh")])
def test_the_shell_follows_the_system(tmp_path, system, shell):
    facts = gather(tmp_path, system=system)
    assert (facts.system, facts.shell) == (system, shell)


def test_windows_is_told_what_cmd_does_with_quotes_and_wildcards(tmp_path):
    text = render(gather(tmp_path, system="Windows"), estimate=ESTIMATE).text
    assert "cmd" in text and "single quotes are not quote characters" in text
    assert "does not expand wildcards" in text


def test_a_posix_system_is_told_its_shell_expands_wildcards(tmp_path):
    text = render(gather(tmp_path, system="Linux"), estimate=ESTIMATE).text
    assert "sh" in text and "expands wildcards" in text and "single quotes are not" not in text


# --- the project ------------------------------------------------------------------


@pytest.mark.parametrize("marker", ["pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"])
def test_python_is_found_by_its_project_files(tmp_path, marker):
    _write(tmp_path, marker, "")
    facts = gather(tmp_path)
    assert (facts.runtime, facts.runtime_evidence) == ("Python", marker)


def test_a_lone_python_file_is_enough_and_an_empty_workspace_has_no_runtime(tmp_path):
    assert gather(tmp_path).runtime is None
    _write(tmp_path, "calc.py", "x = 1\n")
    assert gather(tmp_path).runtime == "Python"


@pytest.mark.parametrize("make, expected", [
    (lambda root: None, "not a repository"),
    (lambda root: (root / ".git").mkdir(), "repository"),
    (lambda root: _write(root, ".git", "gitdir: ../elsewhere\n"), "repository"),   # a worktree
])
def test_a_repository_is_read_from_dot_git(tmp_path, make, expected):
    make(tmp_path)
    assert gather(tmp_path).git == expected


def test_nothing_is_executed_to_gather_the_facts(tmp_path, monkeypatch):
    """`git status` can run programs a repository's config names (core.fsmonitor, filters).
    The workspace may be a repository the user did not write, so no program is run."""
    git = tmp_path / ".git"
    git.mkdir()
    (git / "config").write_text('[core]\n\tfsmonitor = "touch pwned"\n', encoding="utf-8")
    _write(tmp_path, "tests/test_a.py", "def test_a(): pass\n")

    def refuse(*args, **kwargs):
        raise AssertionError("the environment context ran a program")

    for target in ("run", "Popen", "check_output", "call", "check_call"):
        monkeypatch.setattr(subprocess, target, refuse)
    monkeypatch.setattr(os, "system", refuse)
    facts = gather(tmp_path)
    render(facts, estimate=ESTIMATE)
    assert facts.git == "repository" and not (tmp_path / "pwned").exists()


# --- the supported test command: the ADR's order, and never wider -----------------


def test_the_order_is_contract_then_caller_then_configuration_then_discovery(tmp_path):
    _write(tmp_path, "pytest.ini", "[pytest]\n")
    _write(tmp_path, "tests/test_a.py")
    both = _resolved(tmp_path, contract="pytest -q", caller="pytest -x")
    assert (both.command, both.source) == ("pytest -q", CONTRACT)
    caller = _resolved(tmp_path, caller="pytest -x")
    assert (caller.command, caller.source) == ("pytest -x", CALLER)
    configured = _resolved(tmp_path)
    assert (configured.command, configured.source) == ("pytest", CONFIGURATION)
    (tmp_path / "pytest.ini").unlink()
    discovered = _resolved(tmp_path)
    assert (discovered.command, discovered.source) == ("pytest", DISCOVERY)
    (tmp_path / "tests" / "test_a.py").unlink()
    assert resolve_test_command(tmp_path) is None


@pytest.mark.parametrize("given", ["python -m unittest", "dotnet test", "pytest | tee out",
                                   "pytest /etc/passwd", "git commit -m x", "./pytest"])
def test_a_command_the_policy_refuses_is_never_offered_and_never_replaced(tmp_path, given):
    """Discovery never overrides a command from 1-3 (§2.1), and nothing widens the
    command policy. A refused contract command means no supported command, not a quiet
    fallback to something the contract did not choose."""
    _write(tmp_path, "pytest.ini", "[pytest]\n")
    assert resolve_test_command(tmp_path, contract=given) is None
    assert resolve_test_command(tmp_path, caller=given) is None


def test_a_blank_contract_command_is_not_a_command(tmp_path):
    _write(tmp_path, "pytest.ini", "[pytest]\n")
    assert _resolved(tmp_path, contract="  ").source == CONFIGURATION


@pytest.mark.parametrize("name, text, evidence", [
    ("pytest.ini", "[pytest]\naddopts = -q\n", "pytest.ini [pytest]"),
    ("tox.ini", "[pytest]\n", "tox.ini [pytest]"),
    ("setup.cfg", "[tool:pytest]\ntestpaths = tests\n", "setup.cfg [tool:pytest]"),
    ("pyproject.toml", "[tool.pytest.ini_options]\naddopts = '-q'\n",
     "pyproject.toml [tool.pytest.ini_options]"),
])
def test_configuration_counts_only_when_it_is_present_and_parses(tmp_path, name, text, evidence):
    _write(tmp_path, name, text)
    found = _resolved(tmp_path)
    assert (found.command, found.source, found.evidence) == ("pytest", CONFIGURATION, evidence)


@pytest.mark.parametrize("name, text", [
    ("pytest.ini", "this is not an ini file\n"),             # no section: nothing configured
    ("pytest.ini", "[unrelated]\nx = 1\n"),
    ("pyproject.toml", "[tool.pytest.ini_options\nbroken"),   # does not parse
    ("pyproject.toml", "[tool.ruff]\nline-length = 100\n"),   # parses, no pytest table
    ("setup.cfg", "[metadata]\nname = x\n"),
])
def test_configuration_that_is_absent_or_broken_is_not_verified(tmp_path, name, text):
    _write(tmp_path, name, text)
    assert resolve_test_command(tmp_path) is None


def test_a_broken_configuration_falls_through_to_discovery_not_to_a_crash(tmp_path):
    _write(tmp_path, "pyproject.toml", "[tool.pytest.ini_options\nbroken")
    _write(tmp_path, "tests/test_a.py")
    assert _resolved(tmp_path).source == DISCOVERY


@pytest.mark.parametrize("path, found", [
    ("tests/test_a.py", True), ("tests/unit/deep/test_b.py", True), ("test_c.py", True),
    ("calc_test.py", True),
    ("tests/helpers.py", False), ("src/test_not_at_the_root.py", False), ("notes.md", False),
])
def test_discovery_is_pytest_style_files_and_nothing_wider(tmp_path, path, found):
    _write(tmp_path, path)
    assert (resolve_test_command(tmp_path) is not None) is found


# --- the block and its budget -----------------------------------------------------


def _facts(**overrides) -> EnvironmentFacts:
    values: dict[str, Any] = dict(system="Linux", shell="sh", runtime="Python", runtime_evidence="pyproject.toml",
                  git="repository",
                  test_command=SupportedTestCommand("pytest", CONFIGURATION,
                                                    "pyproject.toml [tool.pytest.ini_options]"))
    values.update(overrides)
    return EnvironmentFacts(**values)


def test_the_block_states_the_facts_and_the_one_test_command():
    text = render(_facts(), estimate=ESTIMATE).text
    assert "System: Linux" in text
    assert "Project: Python (found pyproject.toml)" in text
    assert "`pytest`" in text and "do not choose another" in text
    assert "Git: this workspace is a repository" in text and "run `git status`" in text


def test_no_test_command_is_said_plainly_and_nothing_is_invented():
    text = render(_facts(test_command=None, runtime=None, runtime_evidence=None, git="not a repository"),
                  estimate=ESTIMATE).text
    assert "no supported test command was found" in text
    assert "no known runtime was found" in text and "not a repository" in text


def test_what_run_command_accepts_comes_from_the_command_policy_not_a_copy():
    """So the block cannot drift from `commands.py`: every executable and every read-only
    git subcommand the policy has is named, and `git` itself is described by its subcommands."""
    text = render(_facts(), estimate=ESTIMATE).text
    assert "no shell, no pipes, no redirects and no wildcards" in text
    for name in ALLOWED_EXECUTABLES - {"git"}:
        assert name in text, name
    for sub in GIT_READ_ONLY:
        assert sub in text, sub


def test_the_heaviest_block_fits_the_default_budget_with_nothing_dropped():
    heaviest = _facts(system="Windows", shell="cmd")
    rendered = render(heaviest, estimate=ESTIMATE)
    assert rendered.dropped == () and rendered.tokens <= DEFAULT_MAX_TOKENS
    assert DEFAULT_MAX_TOKENS <= 8192 * 0.05 + 1, "about 5% of the window, not more"


def test_the_least_important_lines_go_first_and_each_drop_is_reported():
    def chars(text: str) -> int:
        return len(text)

    full = render(_facts(), estimate=chars, max_tokens=10_000)
    assert full.dropped == ()
    git_line = len("Git: this workspace is a repository. Its state is not read for you; run "
                   "`git status` with `run_command` to see it.") + 1
    no_git = render(_facts(), estimate=chars, max_tokens=full.tokens - 1)
    assert no_git.dropped == ("git",)
    no_project = render(_facts(), estimate=chars, max_tokens=full.tokens - git_line - 1)
    assert no_project.dropped[:2] == ("git", "project")
    # What cannot be dropped is the header and the shell line; the tests line goes last.
    bare = render(_facts(), estimate=chars, max_tokens=300)
    assert bare.dropped[-1] == "tests" or "tests" not in bare.dropped
    assert "System: Linux" in bare.text


def test_a_budget_the_required_lines_cannot_meet_is_an_error_not_a_shorter_block():
    with pytest.raises(ValueError, match="needs .* tokens; its budget is 5"):
        render(_facts(), estimate=ESTIMATE, max_tokens=5)


# --- per run, and what is recorded ------------------------------------------------


def test_the_facts_are_read_when_a_run_starts_not_when_the_loop_is_built(tmp_path):
    context = EnvironmentContext(tmp_path, estimate=ESTIMATE, system="Linux")
    assert context.compose().record["test_command"] is None
    _write(tmp_path, "tests/test_new.py")
    after = context.compose().record
    assert (after["test_command"], after["test_command_source"]) == ("pytest", DISCOVERY)


def test_the_record_has_counts_and_names_only_never_paths_or_contents(tmp_path):
    _write(tmp_path, "pyproject.toml", "[tool.pytest.ini_options]\naddopts = 'secret-looking-flag'\n")
    record = EnvironmentContext(tmp_path, estimate=ESTIMATE, system="Windows").compose().record
    assert set(record) == {"tokens", "max_tokens", "dropped", "system", "runtime", "git",
                           "test_command", "test_command_source"}
    dumped = json.dumps(record)
    assert str(tmp_path) not in dumped and "secret-looking-flag" not in dumped
    assert record["max_tokens"] == DEFAULT_MAX_TOKENS and record["dropped"] == []
