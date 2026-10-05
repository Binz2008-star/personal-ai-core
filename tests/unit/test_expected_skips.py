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
  - The server-gated PostgreSQL suites (ADR-016 conformance and composition).
    These are unverified on any engine this repository does not ship a server
    for, which is a different and weaker claim than "passing".

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

# Skips outside the tokenizer harness are individually accounted for: the two
# layering-check exemptions, plus the 49 collected legs of the Postgres
# conformance module and the 6 legs of the server-composition integration
# file, all when no server is advertised. 2 + 49 + 6 = 57.
#
# 36 -> 57 is the destructive-entry gate. It added 21 server-gated legs to the
# Postgres conformance module -- the foreign-`users` regression, the occupied
# database, the view named `users`, the partial schema, the two OPERATE
# properties, the identity-mismatch refusal, the search-path pin (including a
# schema name that breaks naive quoting), and the seven drop-approval legs
# (missing approval, each of the four fields bound, a verdict that changed
# under the approval, an undeclared target, a differently-spelled target, no
# target advertised, and the configured denylist). All of them need a real
# server, so all of them skip here for the same accounted reason as the other
# 36.
#
# 57 is MEASURED from this tree, not projected. The 21 was counted off a run
# with no server advertised, which is the only condition under which this
# number is the number. An earlier estimate of 52 for this change was wrong
# by 5 and would have left this gate red.
#
# This number belongs to the tree it was measured in, and the tree matters.
# While this gate and the ADR-017 feedback work shared one working tree the
# same assertion read 66, because ADR-017 contributed 9 further server-gated
# legs to this same module. Those legs are not in this tree. When that work
# lands alongside this one, the budget must be re-MEASURED against the merged
# tree, not carried over and not raised -- 66 is the number that tree earns,
# 57 is the number this one earns, and neither is a licence for the other.
#
# The 56 driver-free legs of `tests/unit/test_postgres_safety.py` cost this
# budget nothing: they classify a `SchemaSnapshot` in pure Python, so they RUN
# in CI rather than skipping, and the gate's arithmetic is the reason the
# safety rules are split that way in the first place.
MAX_NON_HARNESS_SKIPS = 57

# Skips that say the MACHINE cannot do something, not that a test was turned
# off. Windows refuses to create a symlink without Developer Mode or an
# elevated shell, and the sandbox tests that need one skip with this reason.
# Measured on the owner's rig 2026-10-01: 11 such skips made this audit fail
# there while CI (which can create symlinks) was green. They are allowed from
# any file and kept out of the structural count -- but only where symlinks are
# genuinely unavailable: on Linux, where CI and every server run, the same
# skip is still a failure (test_environment_skips_never_happen_on_linux).
ENVIRONMENT_SKIP_REASONS = ("cannot create a symlink here",)

pytestmark = pytest.mark.skipif(
    os.environ.get(NESTED_MARKER) == "1",
    reason="nested run started by the skip audit itself; not re-entered",
)


# How long the child run may take. It is the whole suite: about 170 seconds on
# Linux and, at about 2850 tests, past 300 on the Windows runner -- where a
# limit of 300 failed this audit on a run in which every other test passed
# (#233, 2026-10-05). The limit is there to stop a hang, not to time the suite.
CHILD_RUN_TIMEOUT_SECONDS = 1800


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
        timeout=CHILD_RUN_TIMEOUT_SECONDS,
    )
    return [
        line.strip()
        for line in completed.stdout.splitlines()
        if line.strip().startswith("SKIPPED")
    ]


@pytest.fixture(scope="module")
def skips() -> list[str]:
    return _skip_report()


def _environmental(line: str) -> bool:
    return any(reason in line for reason in ENVIRONMENT_SKIP_REASONS)


def _unaccounted_for(lines: list[str]) -> list[str]:
    return [
        line
        for line in lines
        if not any(source in _normalize(line) for source in ALLOWED_SKIP_SOURCES)
        and not _environmental(line)
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
        line for line in skips
        if "test_token_estimator_validation.py" not in line and not _environmental(line)
    ]
    total = sum(_count(line) for line in structural)
    assert total <= MAX_NON_HARNESS_SKIPS, (
        f"expected at most {MAX_NON_HARNESS_SKIPS} structural skips, found "
        f"{total}: {structural}"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="the allowance below exists for Windows")
def test_environment_skips_never_happen_on_linux(skips):
    """The symlink allowance is for machines that cannot create one. On Linux
    they always can, so a skip there means the tests stopped running."""
    assert not [line for line in skips if _environmental(line)]


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
