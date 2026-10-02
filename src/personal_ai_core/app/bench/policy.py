"""Who answers the agent's approval questions during a benchmark (ADR-022 D3).

The agent asks a person before a HIGH or CRITICAL tool call. A benchmark has
no person, so this policy answers instead and records every answer. LOW and
MEDIUM tools never reach it: `RiskPolicy` allows them, inside the workspace
sandbox, as it always does.

What it approves, and nothing else:

- `delete_file`: the workspace sandbox already confines the path.
- `run_command`: the agent's own allowlist already validates it (read-only
  git, inspection commands, pytest, ruff, mypy).
- `shell`: only a command that passes `check_shell_command` below, which
  allows local git and Python/pytest on workspace files only.

It denies `web_search`, `fetch_url` and any tool it does not know.

This is BENCHMARK CONTAINMENT, NOT A SECURITY SANDBOX. There is no OS-level
process isolation yet (roadmap item 8). A Python file the agent writes and
then runs, or a test it adds, executes with the user's network access and
file permissions. The policy makes sure the benchmark does not hand the agent
the network or the rest of the disk through its tools; it cannot stop code
the agent wrote from reaching them. Results must not describe the benchmark
environment as sandboxed or network-isolated.
"""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Mapping

from ...core.agent import ToolRequest, ToolSpec

APPROVED_TOOLS = frozenset({"delete_file", "run_command"})
DENIED_TOOLS = frozenset({"web_search", "fetch_url"})

# Local git only: nothing that talks to a remote, rewrites history, changes
# configuration or runs a program from an option.
GIT_SUBCOMMANDS = frozenset({
    "status", "log", "diff", "show", "rev-parse", "ls-files",
    "add", "commit", "mv", "rm", "restore",
})
GIT_REFUSED_OPTIONS = ("--exec", "--upload-pack", "--receive-pack", "--output",
                       "--ext-diff", "--git-dir", "--work-tree", "--template")
PYTHON_EXECUTABLES = frozenset({"python", "python3"})
# Modules `python -m` may run: the project checks `run_command` already allows
# by name (pytest, ruff, mypy). Refusing `python -m mypy` while allowing
# `mypy` was a contradiction the 2026-10-02 smoke run found.
PYTHON_MODULES = frozenset({"pytest", "ruff", "mypy"})
# Characters with a meaning in sh or in cmd. The shell tool runs through the
# platform shell, so a command is accepted only when no shell would read
# anything into it: it then means the same as its argument list.
SHELL_METACHARACTERS = frozenset("|;&$`!{}()[]<>%^*?\n\r")


_DRIVE = re.compile(r"^[A-Za-z]:(/|$)")


class ShellRefused(ValueError):
    """A shell command outside the benchmark's command policy."""


def _refuse_escaping_path(argument: str) -> None:
    value = argument.split("=", 1)[1] if argument.startswith("--") and "=" in argument else argument
    normalized = value.replace("\\", "/")
    if (normalized.startswith(("/", "~")) or _DRIVE.match(normalized)
            or ".." in normalized.split("/")):
        raise ShellRefused(f"argument reaches outside the workspace: {argument}")


def check_shell_command(command: str, *, windows: bool | None = None) -> list[str]:
    """The command as an argument list, or ShellRefused with the reason.

    Accepted forms:

    - `git SUBCOMMAND ...`, SUBCOMMAND in GIT_SUBCOMMANDS, no option before it;
    - `pytest ...`, and `python -m pytest|ruff|mypy ...`;
    - `python FILE.py ...`, FILE a relative path inside the workspace.

    On Windows the shell is cmd, where a single quote is not a quote, so a
    command containing one would mean something different there: refused.
    """
    windows = os.name == "nt" if windows is None else windows
    if not command.strip():
        raise ShellRefused("empty command")
    bad = sorted({c for c in command if c in SHELL_METACHARACTERS})
    if bad:
        raise ShellRefused(f"shell metacharacter {''.join(bad)!r}")
    if windows and "'" in command:
        raise ShellRefused("single quotes are not quotes in cmd; use double quotes")
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise ShellRefused(f"cannot parse the command: {exc}") from None
    if not parts:
        raise ShellRefused("empty command")
    executable, arguments = parts[0], parts[1:]
    if "/" in executable or "\\" in executable:
        raise ShellRefused(f"give the command by name, not by path: {executable}")
    for argument in arguments:
        _refuse_escaping_path(argument)

    if executable == "git":
        if not arguments or arguments[0].startswith("-"):
            raise ShellRefused("git needs a subcommand first, with no option before it")
        if arguments[0] not in GIT_SUBCOMMANDS:
            raise ShellRefused(f"git {arguments[0]} is outside the benchmark policy")
        for argument in arguments[1:]:
            if argument.startswith(GIT_REFUSED_OPTIONS):
                raise ShellRefused(f"git option outside the benchmark policy: {argument}")
    elif executable == "pytest":
        pass
    elif executable in PYTHON_EXECUTABLES:
        if arguments[:1] == ["-m"] and len(arguments) > 1 and arguments[1] in PYTHON_MODULES:
            pass
        elif arguments and not arguments[0].startswith("-") and arguments[0].endswith(".py"):
            pass
        else:
            raise ShellRefused("python may run `-m pytest|ruff|mypy` or a .py file in the "
                               "workspace, nothing else")
    else:
        raise ShellRefused(f"{executable} is outside the benchmark policy")
    return parts


@dataclass(frozen=True)
class Answer:
    """One approval question and the policy's answer, kept for the run record."""

    tool: str
    arguments: Mapping[str, Any]
    approved: bool
    reason: str


@dataclass
class BenchmarkConfirm:
    """`agent.executor.Confirm` for a benchmark run: answers, and remembers."""

    windows: bool | None = None
    answers: list[Answer] = field(default_factory=list)

    def __call__(self, request: ToolRequest, spec: ToolSpec) -> bool:
        approved, reason = self._decide(request)
        self.answers.append(Answer(request.tool, dict(request.arguments), approved, reason))
        return approved

    def _decide(self, request: ToolRequest) -> tuple[bool, str]:
        if request.tool in DENIED_TOOLS:
            return False, "network tools are denied during a benchmark"
        if request.tool in APPROVED_TOOLS:
            return True, "workspace-scoped; validated by the tool itself"
        if request.tool == "shell":
            try:
                check_shell_command(str(request.arguments.get("command", "")),
                                    windows=self.windows)
            except ShellRefused as exc:
                return False, f"shell: {exc}"
            return True, "shell: within the benchmark command policy"
        return False, f"{request.tool} is not approved during a benchmark"


def describe() -> str:
    """The policy as text, for the run record and for review before a baseline."""
    return "\n".join([
        "Benchmark containment policy (ADR-022 D3). Not a security sandbox.",
        "LOW/MEDIUM tools: allowed by RiskPolicy, confined to the workspace sandbox.",
        f"Approved without a person: {', '.join(sorted(APPROVED_TOOLS))}.",
        f"Denied: {', '.join(sorted(DENIED_TOOLS))}, and any tool not named here.",
        "shell, approved only as:",
        f"  git {{{', '.join(sorted(GIT_SUBCOMMANDS))}}} (no option before the subcommand;"
        f" refused options: {', '.join(GIT_REFUSED_OPTIONS)})",
        "  pytest ... | python -m {pytest, ruff, mypy} ... | python FILE.py ...",
        "  no shell metacharacters, no absolute, ~ or .. paths; no single quotes on Windows",
        "Limit: Python code the agent writes and runs is not isolated from the network.",
    ])
