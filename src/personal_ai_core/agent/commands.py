"""Which commands the agent may run -- validation only, nothing executes here.

ADAPTED from unified-llm-local @ 21a36b0 `tool_security.validate_command`
(ADR-004). Kept: an allowlist of executables rather than a denylist, the source's
blocked-argument list, no shell metacharacters, `shlex` parsing, and never
`shell=True` downstream.

Characterizing the source (tests/characterization/test_source_validators.py)
found that it let through commands it was meant to stop. Each of these is
closed here, and each has a test in tests/unit/test_agent_commands.py that
fails if it reopens:

  1. `python`, `pip`, `node`, `npm`, `npx`, `pnpm`, `yarn`, `make`, `cargo`
     and `go` were allowed. Each runs arbitrary code (`python script.py`),
     which makes an allowlist around it decorative. Dropped.
  2. `find . -delete` passed: `delete` was not on the list. `find`'s acting
     primaries (-delete, -exec, -execdir, -ok, -okdir, -fprint*, -fls) are
     refused.
  3. The git "read-only" list was not enforced: `git commit -m x` and
     `git add .` passed, because only arguments matching the blocklist were
     refused. Now the subcommand must BE on the read-only list.
  4. `git stash` was on that list, and it rewrites the working tree. So were
     `tag` (creates) and `branch` (creates, deletes) -- which the blocklist
     then refused anyway, a contradiction between the two lists. All three
     removed.
  5. `git -c key=value log` passed: `-c` became `c` after `lstrip("-")`, and
     `-c core.pager=...` runs a program. An option before the subcommand is
     refused, and so are `--output` (writes a file) and `--ext-diff` (runs
     one).
  6. `./ls` passed as `ls`: the source checked the executable's NAME and then
     ran the given PATH, so a script called `ls` in the workspace ran. The
     executable must be a bare name.
  7. Arguments were never contained: `cat /etc/passwd` and `grep -r key ~`
     passed, because only FILE TOOLS went through path validation. An
     argument that is absolute, starts with `~`, or has a `..` segment is
     refused -- the command runs in the workspace and stays there.
  8. `git remote add` / `set-url` rewrite repository config; `remote` is not
     on the read-only list. `<` and `>` join the refused metacharacters:
     harmless without a shell, but a command containing them was written for
     one, and it should be refused as written rather than run as something else.

`pytest`, `ruff` and `mypy` stay. pytest runs the project's own test code; a
command tool is HIGH risk, so every run is ASKed first (AGENT_ARCHITECTURE.md
section 3) and the allowlist is the second gate, not the only one.
"""
from __future__ import annotations

import shlex

ALLOWED_EXECUTABLES = frozenset(
    {
        # Read-only inspection
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
        # Version control, read-only subcommands only (below)
        "git",
        # Project checks. They run project code; ASK is what gates that.
        "pytest",
        "ruff",
        "mypy",
    }
)

GIT_READ_ONLY = frozenset(
    {
        "status",
        "log",
        "diff",
        "show",
        "rev-parse",
        "rev-list",
        "describe",
        "blame",
        "ls-files",
    }
)

# From the source, unchanged. Matched against each argument with leading
# dashes stripped, as the source did.
BLOCKED_ARGUMENTS = frozenset(
    {
        "push", "force", "hard", "force-with-lease", "reset", "clean", "checkout",
        "branch", "d", "D", "rm", "rmdir", "del", "format", "mkfs", "dd",
        "sudo", "chmod", "chown", "shutdown", "reboot", "halt", "poweroff", "init",
        "curl", "wget", "nc", "ncat", "socat", "ssh", "scp", "rsync",
        "eval", "exec",
    }
)

FIND_ACTIONS = frozenset(
    {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}
)
GIT_WRITING_OPTIONS = ("--output", "--ext-diff")
SHELL_METACHARACTERS = frozenset("|;&$`!{}()[]<>")


class CommandRejected(PermissionError):
    """A command the agent may not run. The message says why, and is shown."""


def validate_command(command: str) -> list[str]:
    """The command as an argument list, or CommandRejected."""
    if not command or not command.strip():
        raise CommandRejected("empty command")
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise CommandRejected(f"cannot parse the command: {exc}") from None
    if not parts:
        raise CommandRejected("empty command")

    executable = parts[0]
    if "/" in executable or "\\" in executable:
        raise CommandRejected(
            f"give the command by name, not by path: {executable}"
        )
    if executable not in ALLOWED_EXECUTABLES:
        raise CommandRejected(f"not an allowed command: {executable}")

    for part in parts:
        if any(character in SHELL_METACHARACTERS for character in part):
            raise CommandRejected(f"shell metacharacter in: {part}")

    arguments = parts[1:]
    for argument in arguments:
        if argument.lstrip("-") in BLOCKED_ARGUMENTS:
            raise CommandRejected(f"blocked argument: {argument}")
        _refuse_escaping_path(argument)

    if executable == "git":
        _validate_git(arguments)
    elif executable == "find":
        for argument in arguments:
            if argument in FIND_ACTIONS:
                raise CommandRejected(f"find may search, not act: {argument}")
    return parts


def _refuse_escaping_path(argument: str) -> None:
    # An option's value may carry a path too: `--file=/etc/x`, and a short
    # option may have its value attached: `-f/etc/x`.
    candidates = [argument]
    if argument.startswith("--") and "=" in argument:
        candidates.append(argument.split("=", 1)[1])
    elif argument.startswith("-") and not argument.startswith("--") and len(argument) > 2:
        candidates.append(argument[2:])
    for value in candidates:
        normalized = value.replace("\\", "/")
        if (
            normalized.startswith("/")
            or normalized.startswith("~")
            or (len(normalized) > 1 and normalized[1] == ":")
            or ".." in normalized.split("/")
        ):
            raise CommandRejected(f"argument reaches outside the workspace: {argument}")


def _validate_git(arguments: list[str]) -> None:
    if not arguments:
        raise CommandRejected("git needs a read-only subcommand")
    subcommand = arguments[0]
    if subcommand.startswith("-"):
        raise CommandRejected(f"no git option before the subcommand: {subcommand}")
    if subcommand not in GIT_READ_ONLY:
        raise CommandRejected(f"not a read-only git subcommand: {subcommand}")
    for argument in arguments[1:]:
        if argument.startswith(GIT_WRITING_OPTIONS):
            raise CommandRejected(f"git option that writes or runs a program: {argument}")
