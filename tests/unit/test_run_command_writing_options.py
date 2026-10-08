"""run_command stays a CHECKING command: options that make it write are refused.

A rollback restores what the file tools wrote and nothing else (recovery.py).
`ruff check --fix` run in a task that is then undone left its fixes behind,
and `pytest --log-file=calc.py` left calc.py empty. The options refused here
are the ones ruff 0.15, pytest 9.1 and mypy 1.20 document as writing, most of
them run and seen to; the plain checks still pass. Validation runs before
anything executes, so none of these tests needs the tools installed.
"""
from __future__ import annotations

import pytest

from personal_ai_core.agent.commands import CommandRejected, validate_command
from personal_ai_core.agent.sandbox import Workspace
from personal_ai_core.agent.tools import RunCommand


@pytest.mark.parametrize(
    "command",
    [
        # ruff applies fixes in place, in every spelling of the request
        "ruff check --fix",
        "ruff check . --fix",
        "ruff check --fix .",
        "ruff check --fix=true .",
        "ruff check --unsafe-fixes .",
        "ruff check --unsafe-fixes=true .",
        "ruff check --fix-only .",
        "ruff check --fix-only=true .",
        "ruff check src --fix --unsafe-fixes",
        "ruff --fix check .",
        # ...and writes in two other ways
        "ruff check --add-noqa .",
        "ruff check --add-noqa=reason .",
        "ruff check --output-file out.txt .",
        "ruff check --output-file=out.txt .",
        "ruff check -o out.txt .",
        "ruff check -oout.txt .",
        "ruff check -no out.txt .",  # -n -o out.txt
        # pytest empties --basetemp, truncates --log-file and --debug files,
        # writes --junitxml; -o can set log_file, or addopts to any of these
        "pytest --basetemp=src",
        "pytest --basetemp src",
        "pytest --log-file=calc.py",
        "pytest --debug",
        "pytest --debug=calc.py",
        "pytest --junitxml=report.xml",
        "pytest --junit-xml=report.xml",
        "pytest -o log_file=calc.py",
        "pytest -olog_file=calc.py",
        "pytest -qo addopts=--basetemp=src",
        "pytest --override-ini=addopts=--basetemp=src",
        # mypy writes reports, and --install-types runs pip
        "mypy --install-types --non-interactive .",
        "mypy --html-report out .",
        "mypy --txt-report=out .",
        "mypy --junit-xml=report.xml .",
        # mypy reads a unique prefix as the whole option
        "mypy --junit-x=report.xml .",
        "mypy --install .",
    ],
)
def test_an_option_that_writes_files_is_refused(command):
    with pytest.raises(CommandRejected, match="write files"):
        validate_command(command)


def test_the_refusal_says_why_and_what_to_do_instead():
    """The message is shown to the model, so it carries the reason (a rollback
    would not undo it) and the alternative (write_file, which it would)."""
    with pytest.raises(CommandRejected) as refused:
        validate_command("ruff check . --fix")
    message = str(refused.value)
    assert "--fix makes ruff write files" in message
    assert "a rollback restores what the file tools wrote, not what a command writes" in message
    assert "write_file" in message


@pytest.mark.parametrize("command", ["ruff check @args.txt", "pytest @args.txt", "mypy @args.txt ."])
def test_an_argument_file_is_refused(command):
    """All three read further arguments from `@file`, and that file -- which
    write_file can create -- could hold --fix where this check cannot see it."""
    with pytest.raises(CommandRejected, match="arguments from args.txt"):
        validate_command(command)


@pytest.mark.parametrize(
    "command",
    ['ruff check --config "fix = true" .', "ruff check --config=fix=true .",
     "ruff check --config 'unsafe-fixes = true' --config 'fix = true' ."],
)
def test_ruff_inline_configuration_is_refused(command):
    """`--config "fix = true"` fixes exactly as --fix does."""
    with pytest.raises(CommandRejected, match="inline setting"):
        validate_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "ruff check",
        "ruff check .",
        "ruff check src tests",
        "ruff check --diff .",  # shows each fix, writes nothing
        "ruff check --no-fix .",
        "ruff check --show-fixes --statistics .",
        "ruff check --fixable F401 --select F401 .",  # eligibility only
        "ruff check --config ruff.toml .",  # a file, not an inline setting
        "ruff check -e -n -q .",
        "pytest",
        "pytest -q",
        "pytest -x -q tests",
        "pytest -vv test_words.py --tb=short",
        "pytest -Werror -q",  # -W takes the rest as its value
        "pytest -p no:cacheprovider -q",
        "pytest -rfE",
        "pytest -k fix",  # a test name, not an option
        "pytest --collect-only",
        "mypy .",
        "mypy --strict src",
        "mypy -v .",
        "grep -o needle notes.md",  # grep's -o is --only-matching
        "grep @decorator notes.py",  # for grep, "@" is part of a pattern
    ],
)
def test_the_plain_checks_are_still_admitted(command):
    assert validate_command(command)[0] == command.split()[0]


def test_the_tool_tells_the_model_what_a_rollback_covers(tmp_path):
    description = RunCommand(Workspace(tmp_path)).spec.description
    assert "ruff --fix" in description
    assert "write_file" in description
    assert "restores what the file tools wrote, not what a command wrote" in description


def test_run_command_refuses_before_anything_runs(tmp_path):
    (tmp_path / "a.py").write_text("import os\n", encoding="utf-8")
    with pytest.raises(CommandRejected, match="write files"):
        RunCommand(Workspace(tmp_path)).run({"command": "ruff check . --fix"})
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "import os\n"
