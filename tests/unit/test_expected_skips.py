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
    # The server-gated conformance (ADR-016): the whole module skips when no
    # PostgreSQL is advertised (POSTGRES_TEST_URL unset, as in CI) and runs
    # against a real server locally. A server-gated suite that cannot reach a
    # server must read as a skip, not as a pass.
    "tests/unit/test_postgres_backend.py",
    # The server composition (ADR-016, wired): the same gate as the
    # conformance module, on the factory slice that composes the backend.
    "tests/integration/test_server_service.py",
}

# Skips outside the tokenizer harness are individually accounted for:
# the two layering-check exemptions, plus the 28 collected legs of the
# Postgres conformance module (10 parametrised tests x 2 substrates + 8
# server-only tests) and the 6 legs of the server-composition integration
# file, all when no server is advertised.
MAX_NON_HARNESS_SKIPS = 36

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


def _unaccounted_for(lines: list[str]) -> list[str]:
    return [
        line
        for line in lines
        if not any(source in _normalize(line) for source in ALLOWED_SKIP_SOURCES)
    ]


def _re_entries(lines: list[str]) -> list[str]:
    return [line for line in lines if SELF in _normalize(line)]


def test_every_skip_comes_from_an_accounted_for_file(skips):
    unexpected = _unaccounted_for(skips)
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
    assert not _re_entries(skips)


def _normalize(line: str) -> str:
    """Render a SKIPPED line with one path separator, whatever the platform.

    pytest prints skip locations with the *native* separator, so on Windows
    the lines read `tests\\unit\\test_dependency_direction.py:148: ...`. Every
    path constant in this file is written with `/`, so a plain `in` test
    matched nothing there.

    The consequence was not a cosmetic one. Two checks in this file are
    substring tests against those constants, and both failed open or closed
    in the wrong direction on Windows:

      - the allowed-sources check matched no source, so every legitimate
        exemption read as an unaccounted-for skip and the gate failed;
      - the fork-bomb canary looked for `tests/unit/test_expected_skips.py`
        in lines that could only ever say `tests\\unit\\...`, so it could
        never fire. The guard most worth having was the one that had
        quietly stopped working.

    Normalizing at the single point where lines enter the assertions fixes
    both, and is a no-op on any platform whose separator is already `/`.
    """
    return line.replace("\\", "/")


def _count(line: str) -> int:
    """`SKIPPED [3] path:line: reason` -> 3."""
    if "[" in line and "]" in line:
        try:
            return int(line[line.index("[") + 1 : line.index("]")])
        except ValueError:
            return 1
    return 1


# --- The matchers themselves, on both platforms' output ------------------
#
# The suite above can only ever observe this platform. These drive the two
# matchers directly with the line shapes pytest emits on each, which is the
# only way a Linux-only CI can hold the Windows behaviour.

WINDOWS_EXEMPT = (
    "SKIPPED [1] tests\\unit\\test_dependency_direction.py:148: "
    "composition root may wire concrete adapters"
)
POSIX_EXEMPT = (
    "SKIPPED [1] tests/unit/test_dependency_direction.py:148: "
    "composition root may wire concrete adapters"
)


def test_an_exempt_skip_is_recognized_with_either_separator():
    """The gate must not fire on a legitimate exemption on Windows.

    Before the fix this returned the line as unaccounted-for, which is the
    tracked 389-vs-388 discrepancy: the check failed rather than skipped.
    """
    assert _unaccounted_for([POSIX_EXEMPT]) == []
    assert _unaccounted_for([WINDOWS_EXEMPT]) == []


def test_a_genuinely_unaccounted_skip_is_still_caught_with_either_separator():
    """Normalizing must not have turned the gate into a rubber stamp."""
    posix = "SKIPPED [1] tests/unit/test_something_new.py:12: because"
    windows = "SKIPPED [1] tests\\unit\\test_something_new.py:12: because"
    assert _unaccounted_for([posix]) == [posix]
    assert _unaccounted_for([windows]) == [windows]


def test_the_fork_bomb_canary_fires_with_either_separator():
    """The guard that had silently stopped working on Windows.

    `SELF` is written with `/`, so a Windows line naming this very file
    could never match it -- the canary was inert on exactly the platform
    where nobody would notice until the fork bomb ran.
    """
    posix = f"SKIPPED [1] {SELF}:60: nested run"
    windows = "SKIPPED [1] tests\\unit\\test_expected_skips.py:60: nested run"
    assert _re_entries([posix]) == [posix]
    assert _re_entries([windows]) == [windows]


def test_the_canary_does_not_fire_on_an_unrelated_file():
    assert _re_entries([POSIX_EXEMPT, WINDOWS_EXEMPT]) == []
