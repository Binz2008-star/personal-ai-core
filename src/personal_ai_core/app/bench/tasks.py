"""The benchmark's task files: loading, validating, and proving each task (ADR-022 §3.3, §5).

A task file is JSON:

    {
      "id": "debug-off-by-one",
      "track": "agent",                     # or "knowledge"
      "category": "debugging",              # one of CATEGORIES
      "fixture": "fixtures/off_by_one",     # agent: copied into the workspace
      "git": true,                          # optional: the fixture is committed first
      "corpus": "corpora/policies",         # knowledge: the documents to ingest
      "instruction": {"en": "...", "ar": "..."},
      "checks": [{"type": "command_passes", "argv": ["python", "-m", "pytest", "-q"]}],
      "reference": {                        # a correct solve, used only to validate
        "writes": {"calc.py": "..."},
        "deletes": [],
        "commands": [["git", "add", "-A"]],
        "answer": {"en": "...", "ar": "..."},
        "calls": [{"tool": "run_command", "arguments": {"command": "pytest -q"}}]
      }
    }

Paths are relative to the task file's directory. A task is admitted only when
the reference solve passes every check and an empty run fails the task
(`prove`): a task whose checks cannot tell the two apart measures nothing.
"""
from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..evaluate import PASS
from .checks import CHECKS, RunEvidence, ToolCall, interpreter, judge

CATEGORIES = frozenset({
    "file_operations", "code_generation", "code_modification", "debugging",
    "test_execution", "verification", "git", "multi_step", "tool_selection",
    "recovery", "knowledge_qa",
})
TRACKS = frozenset({"agent", "knowledge"})
LANGUAGES = ("en", "ar")
# Checks that read only the answer: the only ones a knowledge task can use.
ANSWER_CHECKS = frozenset({"answer_contains", "answer_number", "cites", "declines"})
_ARABIC = re.compile(r"[؀-ۿ]")
_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# Fixed identity and dates, so a fixture's history is the same on every run.
GIT_ENV = {
    "GIT_AUTHOR_NAME": "bench", "GIT_AUTHOR_EMAIL": "bench@example.invalid",
    "GIT_COMMITTER_NAME": "bench", "GIT_COMMITTER_EMAIL": "bench@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


class TaskError(ValueError):
    """A task file that cannot be admitted. The message names the task and the reason."""


@dataclass(frozen=True)
class Task:
    id: str
    track: str
    category: str
    instruction: Mapping[str, str]
    checks: Sequence[Mapping[str, Any]]
    reference: Mapping[str, Any]
    base: Path
    fixture: Path | None = None
    corpus: Path | None = None
    git: bool = False


def _fail(task_id: str, reason: str) -> TaskError:
    return TaskError(f"{task_id}: {reason}")


def _inside(base: Path, relative: str, task_id: str) -> Path:
    path = (base / relative).resolve()
    if not path.is_relative_to(base.resolve()):
        raise _fail(task_id, f"path leaves the task directory: {relative}")
    return path


def _check_spec(task_id: str, spec: Mapping[str, Any], track: str) -> None:
    kind = str(spec.get("type"))
    if kind not in CHECKS:
        raise _fail(task_id, f"unknown check type {kind!r}")
    if track == "knowledge" and kind not in ANSWER_CHECKS:
        raise _fail(task_id, f"a knowledge task has no workspace; {kind} cannot apply")
    params = {k: v for k, v in spec.items() if k != "type"}
    try:
        inspect.signature(CHECKS[kind]).bind(None, **params)
    except TypeError as exc:
        raise _fail(task_id, f"check {kind}: {exc}") from None


def parse(data: Mapping[str, Any], base: Path) -> Task:
    """One task from its JSON, validated. Nothing is run."""
    task_id = str(data.get("id", "?"))
    if not _ID.match(task_id):
        raise _fail(task_id, "id must be lowercase letters, digits and dashes")
    track, category = data.get("track"), data.get("category")
    if track not in TRACKS:
        raise _fail(task_id, f"track must be one of {sorted(TRACKS)}")
    if category not in CATEGORIES:
        raise _fail(task_id, f"unknown category {category!r}")
    instruction = data.get("instruction") or {}
    for language in LANGUAGES:
        if not str(instruction.get(language, "")).strip():
            raise _fail(task_id, f"no {language} instruction")
    if not _ARABIC.search(instruction["ar"]):
        raise _fail(task_id, "the ar instruction has no Arabic in it")
    checks = data.get("checks") or []
    if not checks:
        raise _fail(task_id, "a task without checks cannot fail")
    for spec in checks:
        _check_spec(task_id, spec, track)

    fixture = corpus = None
    if track == "agent":
        if "fixture" not in data:
            raise _fail(task_id, "an agent task needs a fixture")
        fixture = _inside(base, data["fixture"], task_id)
        if not fixture.is_dir():
            raise _fail(task_id, f"no fixture directory {data['fixture']}")
    else:
        if "corpus" not in data:
            raise _fail(task_id, "a knowledge task needs a corpus")
        corpus = _inside(base, data["corpus"], task_id)
        if not corpus.is_dir() or not any(corpus.iterdir()):
            raise _fail(task_id, f"no documents in {data['corpus']}")
    reference = data.get("reference")
    if not isinstance(reference, Mapping):
        raise _fail(task_id, "no reference solve; a task is admitted only on one")
    return Task(id=task_id, track=track, category=category, instruction=dict(instruction),
                checks=list(checks), reference=reference, base=base, fixture=fixture,
                corpus=corpus, git=bool(data.get("git", False)))


def load(directory: Path) -> list[Task]:
    """Every `*.json` task under `directory`, sorted by id; ids must be unique."""
    tasks = [parse(json.loads(p.read_text(encoding="utf-8")), p.parent)
             for p in sorted(directory.glob("*.json"))]
    seen: set[str] = set()
    for task in tasks:
        if task.id in seen:
            raise _fail(task.id, "duplicate id")
        seen.add(task.id)
    return sorted(tasks, key=lambda t: t.id)


def _git(workspace: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True,
                   env={**os.environ, **GIT_ENV}, timeout=60)


def materialize(task: Task, workspace: Path) -> None:
    """Build the task's starting workspace in an empty directory."""
    if task.fixture is None:
        return
    shutil.copytree(task.fixture, workspace, dirs_exist_ok=True)
    if task.git:
        _git(workspace, "init", "-q", "-b", "main")
        # In the repository's own config, not only the environment: the agent's
        # `shell` strips variables whose names look secret, and GIT_AUTHOR_*
        # matches "AUTH". A commit the agent makes needs an identity too.
        _git(workspace, "config", "user.name", GIT_ENV["GIT_AUTHOR_NAME"])
        _git(workspace, "config", "user.email", GIT_ENV["GIT_AUTHOR_EMAIL"])
        _git(workspace, "add", "-A")
        _git(workspace, "commit", "-q", "-m", "initial")


def _apply_reference(task: Task, workspace: Path, language: str) -> RunEvidence:
    ref = task.reference
    for relative, content in (ref.get("writes") or {}).items():
        target = _inside(workspace, relative, task.id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    for relative in ref.get("deletes") or []:
        _inside(workspace, relative, task.id).unlink()
    for argv in ref.get("commands") or []:
        if argv and argv[0] == "git":
            _git(workspace, *argv[1:])
        else:
            subprocess.run(interpreter(argv), cwd=workspace, check=True, capture_output=True,
                           timeout=120)
    answer = (ref.get("answer") or {}).get(language)
    calls = tuple(ToolCall(tool=c["tool"], arguments=c.get("arguments", {}), decision="allow",
                           executed=True, ok=c.get("ok", True))
                  for c in ref.get("calls") or [])
    return RunEvidence(workspace=workspace, answer=answer, calls=calls)


def prove(task: Task, scratch: Path) -> list[str]:
    """Problems that keep the task out of the set; empty when it is admitted.

    In each language: the reference solve must pass every check, and an empty
    run (fixture untouched, no answer, no tool calls) must fail the task.
    """
    problems: list[str] = []
    for language in LANGUAGES:
        for kind in ("reference", "empty"):
            workspace = scratch / f"{task.id}-{language}-{kind}"
            workspace.mkdir(parents=True)
            materialize(task, workspace)
            if kind == "reference":
                evidence = _apply_reference(task, workspace, language)
                ok, results = judge(evidence, task.checks)
                if not ok:
                    failed = [f"{r['check']}: {r['detail']}" for r in results
                              if r["verdict"] != PASS]
                    problems.append(f"{language}: the reference solve fails {failed}")
            else:
                ok, _ = judge(RunEvidence(workspace=workspace, answer=None), task.checks)
                if ok:
                    problems.append(f"{language}: an empty run passes; the checks measure nothing")
    return problems
