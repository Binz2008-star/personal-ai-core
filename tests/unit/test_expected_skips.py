"""Which skips this repository allows, enforced rather than described.

A skipped test reads like a passing one in every summary line. Three skips are
deliberate here, plus one whole file expected to skip until a reference
tokenizer is installed; anything else is a test that silently stopped running.

The CI step that prints skip reasons makes them *visible*. This makes them
*enforced*, because "visible in a 380-line log" is how a skip becomes normal.

Allowed, and why each is a decision rather than an omission:

  - Two in the layering check. They mark what the dependency rule does not
    apply to -- the composition root, whose job is wiring concrete adapters,
    and `config.py`, the one place a provider may be named. Recording them as
    skips is how the exemption stays visible in the output.
  - The token estimator validation harness, which needs a reference tokenizer
    this project deliberately does not vendor. Its skip reason states that the
    one-sided-error claim is unverified in that environment.

Recursion
---------

This file inspects the suite by running it in a subprocess, because a pytest
hook would only observe the run it is part of and this needs to see the suite
the way CI sees it. The first version of this file omitted to exclude itself
and recursed until it was killed.

Two independent guards now prevent that, deliberately belt and braces: the
subprocess is told to ignore this file, *and* it is run with an environment
marker that makes this file skip itself if it is ever collected anyway.
Either alone would be enough; relying on one would mean a single careless
edit reintroduces a fork bomb.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SELF = "tests/unit/test_expected_skips.py"
NESTED_MARKER = "PAC_SKIP_AUDIT_CHILD"

ALLOWED_SKIP_SOURCES = {
    "tests/unit/test_dependency_direction.py",
    "tests/integration/test_token_estimator_validation.py",
}

# Skips outside the tokenizer harness are individually accounted for.
MAX_NON_HARNESS_SKIPS = 2

pytestmark = pytest.mark.skipif(
    os.environ.get(NESTED_MARKER) == "1",
    reason="nested run started by the skip audit itself; not re-entered",
)


def _skip_report() -> list[str]:
    environment = {**os.environ, NESTED_MARKER: "1"}
    completed = subprocess.run(
        [
            sys.executable, "-m", "pytest", "tests/", "-q", "-rs",
            f"--ignore={SELF}", "-p", "no:cacheprovider",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        env=environment,
        timeout=300,
    )
    return [
        line.strip()
        for line in completed.stdout.splitlines()
        if line.strip().startswith("SKIPPED")
    ]


@pytest.fixture(scope="module")
def skips() -> list[str]:
    return _skip_report()


def test_every_skip_comes_from_an_accounted_for_file(skips):
    unexpected = [
        line
        for line in skips
        if not any(source in line for source in ALLOWED_SKIP_SOURCES)
    ]
    assert not unexpected, (
        "tests are being skipped from files with no recorded reason to skip. "
        "A skip reads like a pass in every summary line, so each one has to be "
        f"a decision: {unexpected}"
    )


def test_the_number_of_structural_skips_has_not_grown(skips):
    """The tokenizer harness may grow samples; the exemptions may not grow."""
    structural = [
        line for line in skips if "test_token_estimator_validation.py" not in line
    ]
    total = sum(_count(line) for line in structural)
    assert total <= MAX_NON_HARNESS_SKIPS, (
        f"expected at most {MAX_NON_HARNESS_SKIPS} structural skips, found "
        f"{total}: {structural}"
    )


def test_every_skip_states_a_usable_reason(skips):
    """A bare `pytest.skip()` explains nothing to whoever reads the log."""
    for line in skips:
        _, _, reason = line.partition(": ")
        assert len(reason.strip()) > 10, f"skip without a usable reason: {line}"


def test_the_audit_does_not_re_enter_itself(skips):
    """The fork bomb, pinned.

    If this file ever appears in its own child run, the subprocess is
    collecting the test that spawned it.
    """
    assert all(SELF not in line for line in skips)


def _count(line: str) -> int:
    """`SKIPPED [3] path:line: reason` -> 3."""
    if "[" in line and "]" in line:
        try:
            return int(line[line.index("[") + 1 : line.index("]")])
        except ValueError:
            return 1
    return 1
