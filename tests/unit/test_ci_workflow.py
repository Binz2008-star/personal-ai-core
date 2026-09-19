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
