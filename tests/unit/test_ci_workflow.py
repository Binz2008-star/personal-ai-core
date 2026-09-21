"""The CI workflow's claims about this repository are checked here.

A workflow that names test files by path makes a claim about the repository
layout, and nothing was checking it. When the test tree was reorganised into
`unit/`, `characterization/` and `integration/`, two workflow steps kept
pointing at paths that no longer existed. Every one of those steps had been
passing by running a subset of the suite; afterwards they failed with
"file or directory not found", and the local suite could not show it because
the local suite does not read the workflow.

This is the same shape as the other gaps found in this phase: a claim written
down in one place, with nothing anywhere that would notice when it stopped
being true.

Parsed with a regex rather than a YAML library on purpose -- this project has
no dependencies, and a test that needs one installed to guard CI is a test
that silently stops running.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "tests.yml"

# Any token that looks like a path into the test tree.
_TEST_PATH = re.compile(r"(?<![\w/.])tests/[\w/.\-]*")


@pytest.fixture(scope="module")
def workflow_text() -> str:
    if not WORKFLOW.exists():
        pytest.fail(f"the CI workflow is missing: {WORKFLOW.relative_to(REPO)}")
    return WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def referenced_paths(workflow_text: str) -> list[str]:
    found = sorted({m.rstrip(".") for m in _TEST_PATH.findall(workflow_text)})
    assert found, "the workflow references no test paths at all"
    return found


def test_every_test_path_in_the_workflow_exists(referenced_paths):
    """The failure this file exists to prevent."""
    missing = [p for p in referenced_paths if not (REPO / p).exists()]
    assert not missing, (
        f"the CI workflow runs paths that do not exist: {missing}. "
        "CI fails with 'file or directory not found' while the local suite "
        "passes, because the local suite never reads the workflow."
    )


def test_all_three_test_layers_are_run(referenced_paths):
    """A layer nobody runs is a layer nobody maintains."""
    for layer in ("tests/unit", "tests/characterization", "tests/integration"):
        assert layer in referenced_paths, f"{layer} is not run by CI"


def test_the_architectural_guards_are_run_by_name(referenced_paths):
    """These two are what stop the architecture eroding quietly.

    They are inside `tests/unit` and would run anyway; naming them makes a
    violation legible in the checks list instead of buried in the full run.
    """
    for guard in (
        "tests/unit/test_dependency_direction.py",
        "tests/unit/test_event_not_memory.py",
    ):
        assert guard in referenced_paths, f"{guard} is not named as its own step"


def test_the_workflow_runs_the_whole_suite_not_only_selections(referenced_paths):
    """Named selections are for legibility. They are not the safety net.

    Without a bare `tests/` step, a new test directory would be created,
    never referenced, and never run.
    """
    assert "tests/" in referenced_paths or "tests" in referenced_paths


def test_no_test_directory_is_left_unrun(referenced_paths, workflow_text):
    """Catches the next reorganisation, not just the last one."""
    on_disk = {
        f"tests/{d.name}"
        for d in (REPO / "tests").iterdir()
        if d.is_dir() and d.name != "__pycache__"
    }
    unrun = sorted(d for d in on_disk if d not in referenced_paths)
    assert not unrun, f"test directories exist that CI never names: {unrun}"


# --- The static-analysis gate ---------------------------------------------
#
# Same shape as everything above: the gate is only a gate while it runs, and
# nothing would notice if the job were deleted or quietly emptied. Regex
# again, for the reason this module's docstring gives.

def test_the_static_analysis_gate_exists(workflow_text):
    """Lint and types are checked, not merely checkable."""
    assert re.search(r"^  static:$", workflow_text, re.MULTILINE), (
        "the `static` job is gone. Ruff and pyright findings then accumulate "
        "unread again, which is the state that let 13 of them build up."
    )
    assert re.search(r"^\s*run: ruff check", workflow_text, re.MULTILINE)
    assert re.search(r"^\s*run: pyright\b", workflow_text, re.MULTILINE)


def test_the_static_gate_is_its_own_job_not_a_step_on_suite(workflow_text):
    """Steps are sequential: as steps on `suite`, a red suite skips them.

    A gate that stops running on exactly the commits most likely to need it
    is worse than none, because the checks list still shows green for it.
    """
    static_at = workflow_text.index("\n  static:")
    suite_at = workflow_text.index("\n  suite:")
    assert static_at > suite_at
    # Nothing between the two job keys may be indented as a step of `suite`
    # *after* `static` begins -- i.e. `static` really opens a new job block.
    assert re.match(r"\n  static:\n    runs-on:", workflow_text[static_at:])


def test_the_static_tools_are_pinned(workflow_text):
    """An unpinned linter makes every upstream release a possible red build.

    A gate that fails for reasons unrelated to the change is one people learn
    to override, so the versions move by deliberate PR or not at all.
    """
    install = re.search(r"run: pip install ([^\n]*ruff[^\n]*)", workflow_text)
    assert install, "the static job installs no tools"
    for tool in ("ruff", "pyright", "pytest"):
        assert re.search(rf"\b{tool}==\d", install.group(1)), (
            f"{tool} is not pinned in the static job: {install.group(1)!r}"
        )


def test_the_type_checker_can_resolve_the_test_dependencies(workflow_text):
    """pytest is installed for pyright, and it is easy to drop as 'unused'.

    It is not unused: type-checking `tests/` means resolving what `tests/`
    imports. Without it pyright reported 33 unresolved-import errors that
    said nothing about the code -- and a gate full of noise is one whose real
    findings get skimmed past.
    """
    static = workflow_text[workflow_text.index("\n  static:"):]
    assert re.search(r"pip install[^\n]*\bpytest==", static), (
        "the static job no longer installs pytest, so pyright cannot resolve "
        "what the test tree imports"
    )


# --- Full history for the merge-ledger check ------------------------------

def test_the_suite_job_checks_out_full_history(workflow_text):
    """`fetch-depth: 0`, without which the merge-ledger check has no input.

    The default is a depth-1 clone, where `git log --merges` returns nothing.
    `test_merge_ledger.py` fails loudly in that case rather than comparing
    against an empty set, so removing this would turn one test red rather than
    silent -- but it would still remove a working check for no reason, and the
    reason it is here would not be obvious from the diff that removed it.
    """
    suite = workflow_text[workflow_text.index("\n  suite:"):]
    static_at = suite.find("\n  static:")
    if static_at != -1:
        suite = suite[:static_at]
    assert re.search(r"fetch-depth:\s*0", suite), (
        "the suite job no longer checks out full history, so "
        "tests/unit/test_merge_ledger.py cannot read `git log --merges`"
    )
