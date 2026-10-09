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
        "grep -n needle README.md",
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
    assert {fix for _, fix in SOURCE_LETS_THROUGH} == set(range(1, 10))


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


@pytest.mark.parametrize("command", ["git status | head", "pytest && git status", "git status > status.txt"])
def test_shell_syntax_refusal_explains_the_supported_recovery(command):
    with pytest.raises(CommandRejected) as rejected:
        validate_command(command)
    message = str(rejected.value)
    assert "shell metacharacter" in message
    assert "one command per tool call" in message
    assert "read the returned output directly" in message


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


# --- fix 9: recursive reading ----------------------------------------------
# A deliberate change: `grep -r` was pinned as ordinary inspection above until
# it was found reading the database and the owner's profile through a
# directory argument. search_text is the recursive search.


@pytest.mark.parametrize(
    "command",
    [
        "grep -r needle src",
        "grep -R needle src",
        "grep -rn needle .",
        "grep -Rli needle .",
        "grep -rao -e needle .",
        "grep -nr needle",  # no path: grep -r searches "."
        "grep -5r needle .",  # a context count, then -r
        "grep needle . -r",  # GNU grep reads options after operands too
        "grep --recursive needle .",
        "grep --recur needle .",  # an unambiguous prefix is the option
        "grep --dereference-recursive needle .",
        "grep --deref needle .",
        "grep -drecurse needle .",
        "grep -nd recurse needle .",
        "grep --directories=recurse needle .",
        "grep --directories recurse needle .",
        "grep --dir=rec needle .",
        "grep -e -- -r .",  # `--` is -e's value here, not the end of options
    ],
)
def test_grep_may_not_search_recursively(command):
    with pytest.raises(CommandRejected, match="may not search recursively.*use search_text"):
        validate_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "grep needle notes.md",
        "grep -n needle notes.md",
        "grep -in -e needle notes.md",
        "grep -c needle notes.md README.md",
        "grep -e-r notes.md",  # -e takes the rest, "-r", as its pattern
        "grep -f patterns.txt notes.md",
        "grep -m 3 needle notes.md",
        "grep --directories=skip needle notes.md",
        "grep --color=never needle notes.md",
    ],
)
def test_grep_on_named_files_is_still_allowed(command):
    assert validate_command(command)[0] == "grep"


def test_git_diff_no_index_is_refused():
    with pytest.raises(CommandRejected, match="--no-index.*use search_text"):
        validate_command("git diff --no-index empty .")
    assert validate_command("git diff --stat HEAD")


@pytest.mark.parametrize("command", ["ls -R", "find . -type f", "ls -R src"])
def test_listings_of_names_are_left_alone(command):
    """Names, not contents: they show at most that a file exists."""
    assert validate_command(command)
