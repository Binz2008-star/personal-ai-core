"""Running the benchmark (ADR-022 unit 2): every task, every language, N times.

    python -m personal_ai_core.app.bench --runs 5
    python -m personal_ai_core.app.bench --report evals/results/bench/bench-<stamp>.jsonl

A run is one task in one language. Agent tasks go through `build_agent`, the
composition `pac --agent` uses, in a fresh copy of the fixture, under the
benchmark's containment policy. Knowledge tasks go through the grounded path
`pac --documents` uses, over a fresh in-memory index of the task's corpus.
The checks judge each run when it stops; no model judges anything.

The result file is JSON Lines, written as the runs finish, so a run that is
interrupted on the rig keeps what it measured and `--resume` continues it:

    {"kind": "header", ...}       what the numbers are bound to
    {"kind": "run", ...}          one per run, in the order they ran
    {"kind": "end", ...}          what the model server had loaded, at the end

Before the first run every task is proved (a reference solve passes, an empty
run fails); one that is not admitted stops the benchmark before it starts.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO

from ...conversation.factory import (
    build_agent,
    build_grounded_in_memory_service,
    describe_loaded,
    http_transport,
)
from ...core.config import DEFAULT_BOSS_MODEL, Settings
from ...core.errors import ProviderError
from ..cli import _ingest
from ..evaluate import PASS, _git_commit, _machine
from .checks import EDITING_TOOLS, RunEvidence, ToolCall, judge, tested_after_last_edit
from .policy import BenchmarkConfirm, describe
from .tasks import LANGUAGES, Task, load, materialize, prove

HARNESS = "ADR-022 capability benchmark v0"
DEFAULT_TASKS = Path("evals/bench")
DEFAULT_OUT = Path("evals/results/bench")
CLIP_CHARS = 2_000
OUTPUT_CLIP_CHARS = 500
# Not part of a workspace's final state: caches the checks or tests leave.
IGNORED_PARTS = frozenset({".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"})

Transport = Callable[[str, Mapping[str, Any], int], Mapping[str, Any]]


class CallLog:
    """A transport that records each model call: time, tokens, options sent.

    The agent loop sends only `num_predict`, not the Boss model's sampling
    settings; what is recorded is what the request carried, not an assumption.
    """

    def __init__(self, inner: Transport) -> None:
        self._inner = inner
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, payload: Mapping[str, Any], timeout: int) -> Mapping[str, Any]:
        started = time.perf_counter()
        call: dict[str, Any] = {"options_sent": dict(payload.get("options") or {})}
        try:
            raw = self._inner(url, payload, timeout)
        except Exception as exc:
            call.update(seconds=round(time.perf_counter() - started, 3), error=str(exc))
            self.calls.append(call)
            raise
        call.update(
            seconds=round(time.perf_counter() - started, 3),
            prompt_tokens=raw.get("prompt_eval_count"),
            completion_tokens=raw.get("eval_count"),
            done_reason=raw.get("done_reason"),
        )
        self.calls.append(call)
        return raw

    def take(self) -> list[dict[str, Any]]:
        calls, self.calls = self.calls, []
        return calls


def _clip(value: Any, limit: int = CLIP_CHARS) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"... [{len(value) - limit} more chars]"
    if isinstance(value, Mapping):
        return {k: _clip(v, limit) for k, v in value.items()}
    return value


def _final_state(workspace: Path) -> dict[str, Any]:
    files = {}
    for path in sorted(workspace.rglob("*")):
        relative = path.relative_to(workspace)
        if path.is_file() and not IGNORED_PARTS.intersection(relative.parts):
            files[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    state: dict[str, Any] = {"files": files}
    if (workspace / ".git").exists():
        def git(*args: str) -> str:
            done = subprocess.run(["git", *args], cwd=workspace, capture_output=True,
                                  text=True, timeout=60)
            return done.stdout.strip() if done.returncode == 0 else f"[git failed: {done.stderr.strip()}]"
        state["git_status"] = git("status", "--porcelain")
        state["git_log"] = git("log", "-n", "10", "--format=%h %s").splitlines()
    return state


def _signals(record: Mapping[str, Any], evidence: RunEvidence) -> list[str]:
    """Mechanical labels for why a run went the way it did. Descriptive: they
    are what the gap reading counts, not a verdict."""
    signals = []
    if record.get("error"):
        signals.append("provider_error")
    if record["stop"] == "budget":
        signals.append("no_answer")
    if record.get("protocol_errors"):
        signals.append("protocol_errors")
    steps = record.get("steps", [])
    if any(not s["executed"] and s["decision"] in ("deny", "ask") for s in steps):
        signals.append("tool_refused")
    if any(s["executed"] and not s["ok"] for s in steps):
        signals.append("failed_steps")
    edited = any(c.executed and c.tool in EDITING_TOOLS for c in evidence.calls)
    if edited and tested_after_last_edit(evidence)[0] != PASS:
        signals.append("not_tested_after_edit")
    if not record["success"]:
        signals.extend(f"check:{c['check']}" for c in record["checks"] if c["verdict"] != PASS)
    return signals


def run_agent_task(task: Task, language: str, settings: Settings, log: CallLog,
                   workspace: Path) -> dict[str, Any]:
    workspace.mkdir(parents=True)
    materialize(task, workspace)
    confirm = BenchmarkConfirm()
    record: dict[str, Any] = {}
    started = time.perf_counter()
    answer = None
    steps: tuple = ()
    try:
        agent = build_agent(settings, workspace=workspace, transport=log, confirm=confirm)
        outcome = agent.loop.run(task.instruction[language], session_id="bench")
        answer, steps = outcome.answer, outcome.steps
        record["stop"] = "answered" if outcome.finished else "budget"
        record["stopped_reason"] = outcome.stopped_reason
        record["protocol_errors"] = outcome.protocol_errors
    except ProviderError as exc:
        record["stop"], record["error"] = "error", str(exc)
    record["seconds"] = round(time.perf_counter() - started, 3)

    calls, step_records = [], []
    for step in steps:
        r = step.record
        ok = bool(r.executed and r.result is not None and r.result.ok and step.verified)
        calls.append(ToolCall(tool=r.request.tool, arguments=r.request.arguments,
                              decision=r.decision.decision.value, executed=r.executed, ok=ok))
        step_records.append({
            "tool": r.request.tool,
            "arguments": _clip(dict(r.request.arguments)),
            "decision": r.decision.decision.value,
            "reason": r.decision.reason,
            "executed": r.executed,
            "ok": ok,
            "verified": step.verified,
            "failed_checks": list(step.failed_checks),
            "error": r.result.error if r.result is not None else None,
            "output": _clip(r.result.output, OUTPUT_CLIP_CHARS) if r.result is not None else "",
        })
    record["answer"] = answer
    record["steps"] = step_records
    record["commands"] = [s["arguments"].get("command") for s in step_records
                          if s["tool"] in ("run_command", "shell")]
    record["approvals"] = [dataclasses.asdict(a) for a in confirm.answers]
    record["final_state"] = _final_state(workspace)
    evidence = RunEvidence(workspace=workspace, answer=answer, calls=tuple(calls))
    record["verification"] = dict(zip(("verdict", "detail"), tested_after_last_edit(evidence)))
    success, results = judge(evidence, task.checks)
    record["success"], record["checks"] = success, results
    record["signals"] = _signals(record, evidence)
    return record


def run_knowledge_task(task: Task, language: str, settings: Settings, log: CallLog,
                       workspace: Path) -> dict[str, Any]:
    assert task.corpus is not None
    workspace.mkdir(parents=True)
    record: dict[str, Any] = {"steps": []}
    started = time.perf_counter()
    answer = None
    try:
        grounded = build_grounded_in_memory_service(settings, transport=log)
        files = sorted(p for p in task.corpus.iterdir() if p.is_file())
        _ingest(grounded.ingestion, files, io.StringIO())
        service = grounded.service
        session = service.start_session(service.create_user().id)
        answer = service.send(session_id=session.id, content=task.instruction[language]).content
        record["stop"] = "answered"
        record["events"] = [e.type.value for e in grounded.events.list_for_session(session.id)]
    except ProviderError as exc:
        record["stop"], record["error"] = "error", str(exc)
    record["seconds"] = round(time.perf_counter() - started, 3)
    record["answer"] = answer
    evidence = RunEvidence(workspace=workspace, answer=answer)
    success, results = judge(evidence, task.checks)
    record["success"], record["checks"] = success, results
    record["signals"] = _signals(record, evidence)
    return record


def _task_digest(task: Task) -> str:
    """The task and its fixture or corpus: a resumed run must measure the same tasks."""
    digest = hashlib.sha256(json.dumps(
        {"instruction": task.instruction, "checks": task.checks, "git": task.git,
         "git_commits": task.git_commits},
        sort_keys=True, ensure_ascii=False).encode("utf-8"))
    for root in (task.fixture, task.corpus):
        if root is None:
            continue
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()[:16]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m personal_ai_core.app.bench",
                                     description="ADR-022 capability benchmark")
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--runs", type=int, default=5, help="runs per task and language (D2: 5)")
    parser.add_argument("--only", nargs="+", metavar="TASK_ID")
    parser.add_argument("--languages", nargs="+", choices=LANGUAGES, default=list(LANGUAGES))
    parser.add_argument("--num-ctx", type=int, help="the context Ollama was started with")
    parser.add_argument("--resume", type=Path, help="continue an interrupted result file")
    parser.add_argument("--report", type=Path, help="print the summary of a result file")
    parser.add_argument("--show-policy", action="store_true",
                        help="print the containment policy and exit")
    return parser


def _read(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: Transport | None = None,
    stdout: TextIO | None = None,
    env: Mapping[str, str] | None = None,
    now: Callable[[], datetime] | None = None,
    commit: str | None = None,
    probe: Callable[..., Mapping[str, Any]] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    out = stdout if stdout is not None else sys.stdout
    if args.show_policy:
        print(describe(), file=out)
        return 0
    if args.report is not None:
        print(report(_read(args.report)), file=out)
        return 0
    if args.runs < 1:
        print("--runs must be at least 1", file=out)
        return 2

    settings = Settings.from_env(dict(env) if env is not None else None)
    # As in the contract harness: a run against another model is not evidence
    # about this system, and is not recorded under the Boss model's name.
    if settings.boss_model != DEFAULT_BOSS_MODEL:
        print(f"refusing to run: PAC_BOSS_MODEL is {settings.boss_model!r}, not the Boss "
              f"model {DEFAULT_BOSS_MODEL!r}", file=out)
        return 2
    # No profile: the owner's profile.md would make the result depend on it.
    settings = dataclasses.replace(settings, profile="")

    tasks = load(args.tasks)
    if args.only:
        unknown = set(args.only) - {t.id for t in tasks}
        if unknown:
            print(f"no such task: {', '.join(sorted(unknown))}", file=out)
            return 2
        tasks = [t for t in tasks if t.id in set(args.only)]
    with tempfile.TemporaryDirectory(prefix="pac-bench-proof-", ignore_cleanup_errors=True) as scratch:
        refused = {t.id: p for t in tasks if (p := prove(t, Path(scratch)))}
    if refused:
        for task_id, problems in refused.items():
            print(f"not admitted: {task_id}: {'; '.join(problems)}", file=out)
        return 2

    header = {
        "kind": "header",
        "harness": HARNESS,
        "commit": commit if commit is not None else _git_commit(),
        "model": settings.boss_model,
        "role": "boss",
        "machine": _machine(),
        "tasks_dir": args.tasks.as_posix(),
        "tasks": {t.id: {"track": t.track, "category": t.category, "digest": _task_digest(t)}
                  for t in tasks},
        "runs": args.runs,
        "languages": args.languages,
        "num_ctx_measured_by_owner": args.num_ctx,
        "num_ctx_sent_by_core": False,
        "profile": "none (deliberately empty)",
        "judge_model": "none (ADR-013)",
        "language_guard": settings.language_guard,
        "sampling": "recorded per model call as sent (options_sent); the agent loop "
                    "sends only num_predict",
        "policy": describe(),
        "environment": "benchmark containment, not a sandbox: code the agent runs is not "
                       "isolated from the network (ADR-022 §3.5)",
    }

    done: set[tuple[str, str, int]] = set()
    if args.resume is not None:
        previous = _read(args.resume)
        old = previous[0] if previous and previous[0].get("kind") == "header" else {}
        for key in ("commit", "model", "tasks", "runs", "languages"):
            if old.get(key) != header[key]:
                print(f"refusing to resume: {key} differs from the file's header", file=out)
                return 2
        done = {(r["task"], r["language"], r["run"]) for r in previous if r.get("kind") == "run"}
        path = args.resume
        header = old
    else:
        stamp = (now or (lambda: datetime.now(timezone.utc)))()
        header["started_at"] = stamp.isoformat()
        args.out.mkdir(parents=True, exist_ok=True)
        path = args.out / f"bench-{stamp.strftime('%Y%m%dT%H%M%SZ')}.jsonl"

    log = CallLog(transport or http_transport)
    plan = [(t, lang, run) for run in range(1, args.runs + 1) for t in tasks
            for lang in args.languages]
    with path.open("a", encoding="utf-8") as sink, \
            tempfile.TemporaryDirectory(prefix="pac-bench-", ignore_cleanup_errors=True) as tmp:
        if args.resume is None:
            sink.write(json.dumps(header, ensure_ascii=False) + "\n")
        for index, (task, language, run) in enumerate(plan, 1):
            if (task.id, language, run) in done:
                continue
            workspace = Path(tmp) / f"{task.id}-{language}-{run}"
            runner = run_agent_task if task.track == "agent" else run_knowledge_task
            record = runner(task, language, settings, log, workspace)
            calls = log.take()
            record = {"kind": "run", "task": task.id, "track": task.track,
                      "category": task.category, "language": language, "run": run,
                      **record, "model_calls": calls,
                      "prompt_tokens": sum(c.get("prompt_tokens") or 0 for c in calls),
                      "completion_tokens": sum(c.get("completion_tokens") or 0 for c in calls)}
            sink.write(json.dumps(record, ensure_ascii=False) + "\n")
            sink.flush()
            verdict = "PASS" if record["success"] else "FAIL"
            print(f"[{index}/{len(plan)}] {verdict} {task.id} {language} run {run} "
                  f"({record['seconds']:.0f}s)", file=out)
        loaded = describe_loaded("ollama", ollama_host=settings.ollama_host, llamacpp_host="",
                                 model=settings.boss_model, probe=probe,
                                 live=transport is None)
        measured = loaded.get("context_length")
        mismatch = (args.num_ctx is not None and isinstance(measured, int)
                    and measured != args.num_ctx)
        sink.write(json.dumps({"kind": "end", "ollama_loaded": loaded,
                               "weights": loaded.get("weights"),
                               "context_mismatch": mismatch}, ensure_ascii=False) + "\n")

    print(report(_read(path)), file=out)
    print(f"results: {path}", file=out)
    if mismatch:
        print(f"WARNING: --num-ctx says {args.num_ctx} but Ollama has the model loaded at "
              f"{measured}; the file is marked context_mismatch.", file=out)
        return 3
    return 0


def _rate(rows: Sequence[Mapping[str, Any]]) -> str:
    passed = sum(1 for r in rows if r["success"])
    return f"{passed}/{len(rows)} ({100 * passed / len(rows):.0f}%)" if rows else "-"


def report(lines: Sequence[Mapping[str, Any]]) -> str:
    """Success rates by category, language and task, and the failure signals.

    Rates are reported with their counts: 5 runs per task is a small sample,
    and a percentage without its denominator hides that.
    """
    runs = [r for r in lines if r.get("kind") == "run"]
    if not runs:
        return "no runs"
    out = [f"overall: {_rate(runs)}"]
    by: dict[str, dict[str, list]] = {"language": defaultdict(list), "track": defaultdict(list),
                                      "category": defaultdict(list), "task": defaultdict(list)}
    for r in runs:
        by["language"][r["language"]].append(r)
        by["track"][r["track"]].append(r)
        by["category"][r["category"]].append(r)
        by["task"][r["task"]].append(r)
    for name in ("track", "language", "category"):
        out.append(f"by {name}:")
        out.extend(f"  {key:20} {_rate(rows)}" for key, rows in sorted(by[name].items()))
    out.append("by task (en | ar):")
    for task_id, rows in sorted(by["task"].items()):
        en = [r for r in rows if r["language"] == "en"]
        ar = [r for r in rows if r["language"] == "ar"]
        out.append(f"  {task_id:30} {_rate(en):>12} | {_rate(ar)}")
    failures = [r for r in runs if not r["success"]]
    counts = Counter(s for r in failures for s in r.get("signals", []))
    out.append(f"signals in the {len(failures)} failed run(s):")
    out.extend(f"  {signal:28} {count}" for signal, count in counts.most_common())
    seconds = sorted(r["seconds"] for r in runs)
    out.append(f"seconds per run: median {seconds[len(seconds) // 2]:.0f}, max {seconds[-1]:.0f}")
    return "\n".join(out)
