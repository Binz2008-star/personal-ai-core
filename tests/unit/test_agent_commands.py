"""Command validation -- the adapted `validate_command` (ADR-004, step 3).

Two halves. The first holds the Core to the source where the source was right:
every command the characterization tests show the source refusing, the Core
refuses too, and the ordinary inspection commands still pass. The second pins
each numbered fix in agent/commands.py; each command there passes the SOURCE
(tests/characterization/test_source_validators.py::test_source_lets_through).
"""
from __future__ import annotations

import pytest

from personal_ai_core.agent.commands import CommandRejected, validate_command

from characterization.source_cases import SOURCE_LETS_THROUGH, SOURCE_REFUSES


def refused(command: str) -> bool:
    try:
        validate_command(command)
    except CommandRejected:
        return True
    return False


# --- kept from the source --------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "git status",
        "git log --oneline",
        "git diff HEAD~1",
        "git show HEAD",
        "pytest -q",
        "ruff check .",
        "grep -r needle src",
        "find . -name '*.py'",
        "wc -l README.md",
    ],
)
def test_ordinary_inspection_is_allowed(command):
    assert validate_command(command)[0] == command.split()[0]


@pytest.mark.parametrize("command", SOURCE_REFUSES)
def test_everything_the_source_refuses_the_core_refuses(command):
    """No regression against the source: the parity half of ADAPT."""
    assert refused(command)


# --- the fixes -------------------------------------------------------------


@pytest.mark.parametrize("command, fix", SOURCE_LETS_THROUGH)
def test_every_command_the_source_let_through_is_refused(command, fix):
    assert refused(command), f"fix {fix} has reopened: {command!r} passes"


def test_the_fix_list_covers_every_numbered_fix():
    assert {fix for _, fix in SOURCE_LETS_THROUGH} == set(range(1, 9))


@pytest.mark.parametrize(
    "command",
    [
        "find . -exec cat {} +",
        "find . -execdir ls",
        "find . -ok ls",
        "find . -fprint out.txt",
        "git -C other log",
        "git log --ext-diff",
        "git blame --output x",
        "grep -f/etc/passwd x",
        "grep --file=/etc/passwd x",
        "cat ../../etc/passwd",
        "cat C:\\Windows\\win.ini",
        "ls > out.txt",
        "cat < in.txt",
        "/bin/ls",
        "git",
        "git tag v1",
        "git branch new",
    ],
)
def test_variants_of_each_fix_are_refused(command):
    assert refused(command)


def test_the_git_subcommand_must_come_first():
    assert refused("git --no-pager log")
    assert not refused("git log --no-pager")  # an option AFTER it is fine


def test_a_rejection_says_why():
    with pytest.raises(CommandRejected, match="read-only git subcommand: commit"):
        validate_command("git commit -m x")


def test_an_option_before_the_subcommand_is_named_as_such():
    """Fix 5. Fix 3 would refuse it too -- `-c` is never read-only -- but the
    user should be told the real reason, not "not a read-only subcommand: -c"."""
    with pytest.raises(CommandRejected, match="option before the subcommand"):
        validate_command("git -c core.pager=less log")


def test_an_executable_given_by_path_is_named_as_such():
    """Fix 6. The exact-name allowlist would refuse `./ls` too; the message
    says what to do instead."""
    with pytest.raises(CommandRejected, match="by name, not by path"):
        validate_command("./ls")


@pytest.mark.parametrize("command", ["grep -r push src", "ls -d src", "git log --force"])
def test_the_source_blocklist_still_applies_to_arguments(command):
    """Kept from the source for parity and depth, and its cost stated: it has
    false positives -- searching for the word "push" is refused. Loosening it
    is a separate, reviewed decision, not a side effect of the adaptation."""
    with pytest.raises(CommandRejected, match="blocked argument"):
        validate_command(command)
