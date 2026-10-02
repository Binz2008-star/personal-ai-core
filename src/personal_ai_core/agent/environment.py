"""Environment context for the agent loop (ADR-023 §2.1, unit 2).

Code, not the model, gathers authoritative facts about where the agent is working
and states them to it: the operating system and what its shell does with quotes
and wildcards, the project's runtime, whether the workspace is a git repository and
its state, the supported test command, and what `run_command` accepts. In the
benchmark baseline 14 of 111 failed agent attempts ended on refused commands the
model could not have known were wrong (`python -m unittest`, `dir`, single-quoted
arguments on Windows `cmd`, `dotnet test` in a Python project), and nothing in the
loop's prompt told it the system, the shell, the language, or how tests run.

Two rules from the ADR are kept by construction:

- **The supported test command is resolved in the ADR's order** (§6, decision 3):
  the task contract, then an explicit command from the caller, then verified
  repository configuration, then constrained discovery as a fallback. A discovered
  or configured command is offered only if `validate_command` already accepts it, so
  this module can never widen what the command tools allow.
- **The block has a bounded budget.** `render` drops the lowest-priority lines until
  it fits, and raises if the lines that cannot be dropped do not fit, so the block
  cannot crowd the task out of an 8192-token window. Tokens are counted with an
  estimator the caller passes in (the project's `ScriptAwareTokenEstimator`), because
  the agent layer may import only `core`.

Nothing here runs a model, a network call or any program. It reads files only. In
particular it does not run `git status`: that command can execute programs the
repository's own configuration names (`core.fsmonitor`, clean filters), which is why
`commands.py` refuses `git -c`, and a workspace may be a repository the user did not
write. Whether the workspace is a repository is read from `.git`; its state is left to
`run_command` with `git status`, which the user confirms. The loop does not use this
unless it is given it: with the context off, the loop is byte-for-byte what it was.
"""
from __future__ import annotations

import configparser
import platform
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .commands import ALLOWED_EXECUTABLES, GIT_READ_ONLY, CommandRejected, validate_command

# The ADR leaves the figure to measurement. The longest block this module can produce is
# about 300 tokens by the project's (conservative) estimator; 400 is about 5% of an
# 8192-token window, room for it without letting a future line crowd the task.
DEFAULT_MAX_TOKENS = 400

# Where a supported test command came from, in the ADR's order of precedence.
CONTRACT, CALLER, CONFIGURATION, DISCOVERY = (
    "task contract", "caller", "repository configuration", "discovery")

_PYTEST_SECTIONS: tuple[tuple[str, str], ...] = (
    ("pytest.ini", "pytest"), ("tox.ini", "pytest"), ("setup.cfg", "tool:pytest"))


@dataclass(frozen=True, slots=True)
class SupportedTestCommand:
    command: str
    source: str
    evidence: str


@dataclass(frozen=True, slots=True)
class EnvironmentFacts:
    system: str
    shell: str
    runtime: str | None
    runtime_evidence: str | None
    git: str
    test_command: SupportedTestCommand | None


@dataclass(frozen=True, slots=True)
class Rendered:
    text: str
    tokens: int
    dropped: tuple[str, ...]


def _accepted(command: str) -> bool:
    try:
        validate_command(command)
    except CommandRejected:
        return False
    return True


def _pytest_configuration(workspace: Path) -> str | None:
    """The file that proves pytest is configured, only if it is present AND parses."""
    for name, section in _PYTEST_SECTIONS:
        path = workspace / name
        if not path.is_file():
            continue
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read_string(path.read_text(encoding="utf-8"))
        except (configparser.Error, UnicodeDecodeError, OSError):
            continue
        if parser.has_section(section):
            return f"{name} [{section}]"
    pyproject = workspace / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError):
            return None
        if "pytest" in data.get("tool", {}) and "ini_options" in data["tool"]["pytest"]:
            return "pyproject.toml [tool.pytest.ini_options]"
    return None


def _pytest_discovery(workspace: Path) -> str | None:
    """Constrained discovery: pytest-style test files, and nothing wider."""
    tests = workspace / "tests"
    for directory in (tests, workspace):
        if not directory.is_dir():
            continue
        pattern = "**/*.py" if directory == tests else "*.py"
        for path in directory.glob(pattern):
            if path.name.startswith("test_") or path.stem.endswith("_test"):
                return f"{path.relative_to(workspace).as_posix()}"
    return None


def resolve_test_command(
    workspace: Path, *, contract: str | None = None, caller: str | None = None
) -> SupportedTestCommand | None:
    """The supported test command, in the ADR's order, or None.

    A command from the contract or the caller is the owner's to give, but it is still
    only offered if the command policy accepts it: an environment block that told the
    model to run something `run_command` refuses would be the failure it exists to end.
    """
    for source, command in ((CONTRACT, contract), (CALLER, caller)):
        if command is not None and command.strip():
            return (SupportedTestCommand(command.strip(), source, f"given by the {source}")
                    if _accepted(command.strip()) else None)
    if not _accepted("pytest"):
        return None
    evidence = _pytest_configuration(workspace)
    if evidence is not None:
        return SupportedTestCommand("pytest", CONFIGURATION, evidence)
    found = _pytest_discovery(workspace)
    if found is not None:
        return SupportedTestCommand("pytest", DISCOVERY, f"test file {found}")
    return None


def _runtime(workspace: Path) -> tuple[str | None, str | None]:
    for marker in ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"):
        if (workspace / marker).is_file():
            return "Python", marker
    first = next(iter(sorted(workspace.glob("*.py"))), None)
    if first is not None:
        return "Python", first.name
    return None, None


def _git(workspace: Path) -> str:
    """Whether the workspace is a repository, from `.git` alone (a file in a worktree)."""
    return "repository" if (workspace / ".git").exists() else "not a repository"


def gather(
    workspace: Path,
    *,
    system: str | None = None,
    contract_test_command: str | None = None,
    caller_test_command: str | None = None,
) -> EnvironmentFacts:
    system = system or platform.system() or "unknown"
    runtime, evidence = _runtime(workspace)
    return EnvironmentFacts(
        system=system,
        shell="cmd" if system == "Windows" else "sh",
        runtime=runtime,
        runtime_evidence=evidence,
        git=_git(workspace),
        test_command=resolve_test_command(
            workspace, contract=contract_test_command, caller=caller_test_command),
    )


def _shell_line(facts: EnvironmentFacts) -> str:
    if facts.system == "Windows":
        return ("System: Windows. The `shell` tool uses cmd: single quotes are not quote "
                "characters (use double quotes), and the shell does not expand wildcards.")
    return (f"System: {facts.system}. The `shell` tool uses sh: single and double quotes "
            "work, and the shell expands wildcards.")


def _tools_line() -> str:
    git = ", ".join(sorted(GIT_READ_ONLY))
    names = ", ".join(sorted(ALLOWED_EXECUTABLES - {"git"}))
    return (f"`run_command` has no shell, no pipes, no redirects and no wildcards. It runs "
            f"only: {names}, and git with only these subcommands: {git}.")


def render(
    facts: EnvironmentFacts,
    *,
    estimate: Callable[[str], int],
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> Rendered:
    """The block the model is shown, within its budget.

    Lines are listed from the most to the least important; the least important are
    dropped first, and each drop is reported so a run can record it. The first two
    lines are never dropped: if they alone exceed the budget the caller has asked for
    something impossible, and that is an error and not a silently shorter block.
    """
    if facts.test_command is not None:
        tests = (f"Tests: the supported test command is `{facts.test_command.command}` "
                 f"({facts.test_command.source}: {facts.test_command.evidence}). Run tests "
                 "with it; do not choose another.")
    else:
        tests = "Tests: no supported test command was found in the workspace."
    project = (f"Project: {facts.runtime} (found {facts.runtime_evidence})."
               if facts.runtime else "Project: no known runtime was found in the workspace.")
    header = "Environment (facts gathered by the program from this machine and workspace):"
    required = [header, _shell_line(facts)]
    git = ("Git: this workspace is a repository. Its state is not read for you; run "
           "`git status` with `run_command` to see it." if facts.git == "repository"
           else "Git: this workspace is not a repository.")
    # Most to least important. What `run_command` accepts is second: refused commands
    # were the whole of the baseline's third failure class.
    optional = [("tests", tests), ("commands", _tools_line()), ("project", project),
                ("git", git)]
    kept = list(optional)
    dropped: list[str] = []
    while True:
        text = "\n".join([*required, *(line for _, line in kept)])
        tokens = estimate(text)
        if tokens <= max_tokens:
            return Rendered(text=text, tokens=tokens, dropped=tuple(dropped))
        if not kept:
            raise ValueError(
                f"the environment context needs {tokens} tokens; its budget is {max_tokens}")
        dropped.append(kept.pop()[0])


@dataclass(frozen=True, slots=True)
class Composed:
    """What the loop needs for one run: the text to show, and what to record about it."""

    text: str
    record: Mapping[str, Any]


class EnvironmentContext:
    """Gathers and renders the environment for one run, afresh each time.

    A session runs several tasks in one workspace and the agent may create the tests
    or the configuration in the first, so the facts are read when a run starts and not
    when the loop is built. The record carries counts and names only: no paths, no
    file contents, nothing from the model.
    """

    def __init__(
        self,
        workspace: Path,
        *,
        estimate: Callable[[str], int],
        max_tokens: int = DEFAULT_MAX_TOKENS,
        system: str | None = None,
        contract_test_command: str | None = None,
        caller_test_command: str | None = None,
    ) -> None:
        self._workspace = workspace
        self._estimate = estimate
        self._max_tokens = max_tokens
        self._system = system
        self._contract_test_command = contract_test_command
        self._caller_test_command = caller_test_command

    def compose(self) -> Composed:
        facts = gather(
            self._workspace,
            system=self._system,
            contract_test_command=self._contract_test_command,
            caller_test_command=self._caller_test_command,
        )
        rendered = render(facts, estimate=self._estimate, max_tokens=self._max_tokens)
        command = facts.test_command
        return Composed(
            text=rendered.text,
            record={
                "tokens": rendered.tokens,
                "max_tokens": self._max_tokens,
                "dropped": list(rendered.dropped),
                "system": facts.system,
                "runtime": facts.runtime,
                "git": facts.git,
                "test_command": command.command if command else None,
                "test_command_source": command.source if command else None,
            },
        )
