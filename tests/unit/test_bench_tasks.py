"""Task files: the format is validated, and every shipped task is proved (ADR-022 §5)."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from personal_ai_core.app.bench.tasks import TaskError, load, materialize, parse, prove

BENCH = Path(__file__).resolve().parents[2] / "evals" / "bench"


def _shipped(task_id: str) -> dict:
    return json.loads((BENCH / f"{task_id}.json").read_text(encoding="utf-8"))


def test_the_shipped_tasks_load():
    tasks = load(BENCH)
    assert [t.id for t in tasks] == sorted(t.id for t in tasks)
    assert {t.track for t in tasks} == {"agent", "knowledge"}


@pytest.mark.parametrize("task", load(BENCH), ids=lambda t: t.id)
def test_every_shipped_task_is_proved(task, tmp_path):
    """Its reference solve passes, and an empty run fails, in both languages."""
    assert prove(task, tmp_path) == []


@pytest.mark.parametrize("mutate, reason", [
    (lambda d: d.update(id="Bad Id"), "id must be"),
    (lambda d: d.update(track="web"), "track"),
    (lambda d: d.update(category="vibes"), "unknown category"),
    (lambda d: d["instruction"].pop("ar"), "no ar instruction"),
    (lambda d: d["instruction"].update(ar="fix calc.py"), "no Arabic"),
    (lambda d: d.update(checks=[]), "without checks"),
    (lambda d: d["checks"].append({"type": "looks_good"}), "unknown check"),
    (lambda d: d["checks"].append({"type": "file_exists"}), "file_exists"),
    (lambda d: d["checks"].append({"type": "file_exists", "path": "a", "x": 1}), "file_exists"),
    (lambda d: d.update(fixture="fixtures/missing"), "no fixture"),
    (lambda d: d.update(fixture="../../src"), "leaves the task directory"),
    (lambda d: d.pop("reference"), "no reference"),
])
def test_a_malformed_agent_task_is_refused_with_its_reason(mutate, reason):
    data = copy.deepcopy(_shipped("verify-off-by-one"))
    mutate(data)
    with pytest.raises(TaskError, match=reason):
        parse(data, BENCH)


def test_a_knowledge_task_may_only_use_answer_checks():
    data = copy.deepcopy(_shipped("kb-leave-carryover"))
    data["checks"].append({"type": "file_exists", "path": "a"})
    with pytest.raises(TaskError, match="no workspace"):
        parse(data, BENCH)


def test_a_knowledge_task_needs_documents():
    data = copy.deepcopy(_shipped("kb-leave-carryover"))
    data["corpus"] = "corpora/missing"
    with pytest.raises(TaskError, match="no documents"):
        parse(data, BENCH)


def test_duplicate_ids_are_refused(tmp_path):
    import shutil

    shutil.copytree(BENCH / "corpora", tmp_path / "corpora")
    for name in ("a.json", "b.json"):
        (tmp_path / name).write_text(json.dumps(_shipped("kb-leave-carryover")), encoding="utf-8")
    with pytest.raises(TaskError, match="duplicate id"):
        load(tmp_path)


def test_a_task_whose_checks_an_empty_run_passes_is_not_admitted(tmp_path):
    data = copy.deepcopy(_shipped("verify-off-by-one"))
    # Only "the tests are unchanged": true of the untouched fixture.
    data["checks"] = [c for c in data["checks"] if c["type"] == "file_equals"]
    problems = prove(parse(data, BENCH), tmp_path)
    assert any("empty run passes" in p for p in problems)


def test_a_task_whose_reference_fails_is_not_admitted(tmp_path):
    data = copy.deepcopy(_shipped("verify-off-by-one"))
    data["reference"]["writes"] = {}
    problems = prove(parse(data, BENCH), tmp_path)
    assert any("reference solve fails" in p for p in problems)


def test_a_git_fixture_starts_committed_with_a_fixed_identity(tmp_path):
    import subprocess

    data = copy.deepcopy(_shipped("verify-off-by-one"))
    data["git"] = True
    materialize(parse(data, BENCH), tmp_path)
    log = subprocess.run(["git", "log", "--format=%an %s %H"], cwd=tmp_path,
                         capture_output=True, text=True, check=True).stdout.split()
    assert log[:2] == ["bench", "initial"]
    # Outside the first repository, so the second is its own.
    other = tmp_path.parent / (tmp_path.name + "-2")
    other.mkdir()
    materialize(parse(data, BENCH), other)
    log2 = subprocess.run(["git", "log", "--format=%H"], cwd=other, capture_output=True,
                          text=True, check=True).stdout.split()
    assert log2[0] == log[2], "the same fixture must give the same commit on every run"
