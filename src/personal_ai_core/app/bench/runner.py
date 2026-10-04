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
import os
import shlex
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
from ...core.agent import AgentTaskContract
from ...core.config import DEFAULT_BOSS_MODEL, Settings
from ...core.errors import ProviderError
from ..cli import _ingest
from ..evaluate import PASS, _git_commit, _machine
from .checks import (
    CHECKS,
    EDITING_TOOLS,
    SCORER,
    RunEvidence,
    ToolCall,
    judge,
    tested_after_last_edit,
)
from .policy import BenchmarkConfirm, describe
from .tasks import ANSWER_CHECKS, LANGUAGES, Task, load, materialize, prove

HARNESS = "ADR-022 capability benchmark v0"
DEFAULT_TASKS = Path("evals/bench")
DEFAULT_OUT = Path("evals/results/bench")
CLIP_CHARS = 2_000
OUTPUT_CLIP_CHARS = 2_000
# A refused reply is kept whole up to here: the loop asks for at most 1024
# tokens, which is about this many characters of English.
REFUSED_REPLY_CLIP_CHARS = 4_000
# Not part of a workspace's final state: caches the checks or tests leave.
# Environment variables that change how the tasks' own commands behave, recorded
# in the header by name and value (paths, never secrets). PYTEST_DEBUG_TEMPROOT:
# the rig needs it because its default pytest temp folder is access-denied
# (2026-10-02); without it every `tmp_path` test errors there.
RECORDED_ENVIRONMENT = ("PYTEST_DEBUG_TEMPROOT", "PYTHONPATH", "PYTHONHASHSEED", "PYTHONUTF8")
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
        tools = payload.get("tools")
        if tools is not None:
            # ADR-025: how many tools the request declared natively.
            call["tools_sent"] = len(tools)
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
        if tools is not None:
            message = raw.get("message")
            returned = message.get("tool_calls") if isinstance(message, Mapping) else None
            call["tool_calls_returned"] = len(returned) if isinstance(returned, list) else 0
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
    if record.get("action_rejections"):
        signals.append("action_rejected")
    steps = record.get("steps", [])
    if any(not s["executed"] and s["decision"] in ("deny", "ask") for s in steps):
        signals.append("tool_refused")
    if any(s["executed"] and not s["ok"] for s in steps):
        signals.append("failed_steps")
    edited = any(c.executed and c.tool in EDITING_TOOLS for c in evidence.calls)
    if edited and tested_after_last_edit(evidence)[0] != PASS:
        signals.append("not_tested_after_edit")
    if record.get("track") == "agent" and record["stop"] == "answered" and not any(
            s["executed"] for s in steps):
        signals.append("answered_without_acting")
    if not record["success"]:
        signals.extend(f"check:{c['check']}" for c in record["checks"]
                       if c["verdict"] != PASS and not c.get("informational"))
    return signals


def agent_test_command(task: Task) -> str | None:
    """The task's supported test command, as the agent runs it (ADR-023 §2.3).

    Taken from the task's own `command_passes` check, so the task files and
    their digests are unchanged: `python -m pytest ARGS` becomes `pytest ARGS`,
    the form `run_command` accepts (`python` is not an allowed executable,
    `pytest` is). None for a task with no such check."""
    for check in task.checks:
        argv = list(check.get("argv") or ()) if check.get("type") == "command_passes" else []
        if argv[:3] == ["python", "-m", "pytest"]:
            return shlex.join(["pytest", *argv[3:]])
        if argv[:1] == ["pytest"]:
            return shlex.join(argv)
    return None


def _contract(task: Task, language: str) -> AgentTaskContract:
    """The contract the task file states (ADR-023 §8.3), in the run's language."""
    if task.action_required is None:
        raise ValueError(f"{task.id}: an agent task states action_required")
    return AgentTaskContract(task_text=task.instruction[language],
                             action_required=task.action_required,
                             test_command=agent_test_command(task))


def run_agent_task(task: Task, language: str, settings: Settings, log: CallLog,
                   workspace: Path, *, environment_context: bool = False,
                   lenient_protocol: bool = False,
                   native_tools: bool = False,
                   verify_completion: bool = False) -> dict[str, Any]:
    workspace.mkdir(parents=True)
    materialize(task, workspace)
    confirm = BenchmarkConfirm()
    # The contract the run was held to, so a reader of the file need not guess
    # whether the gate was engaged: baseline runs carry neither key ("no contract").
    record: dict[str, Any] = {"track": "agent", "action_required": task.action_required}
    started = time.perf_counter()
    answer = None
    steps: tuple = ()
    try:
        agent = build_agent(settings, workspace=workspace, transport=log, confirm=confirm,
                            environment_context=environment_context,
                            lenient_protocol=lenient_protocol,
                            native_tools=native_tools,
                            verify_completion=verify_completion)
        outcome = agent.loop.run(_contract(task, language), session_id="bench")
        answer, steps = outcome.answer, outcome.steps
        record["stop"] = "answered" if outcome.finished else "budget"
        record["stopped_reason"] = outcome.stopped_reason
        record["protocol_errors"] = outcome.protocol_errors
        record["action_rejections"] = outcome.action_rejections
        # What the model wrote each time the loop refused a reply (handoff, Next
        # 6b2): the text that explains a protocol error or a rejected answer.
        # Recorded only; nothing below reads it, so scoring is unchanged.
        record["refused_replies"] = [
            {"call": r.call, "kind": r.kind, "error": r.error,
             "text": _clip(r.text, REFUSED_REPLY_CLIP_CHARS)}
            for r in outcome.refused_replies
        ]
        # ADR-024 unit A: every reply read leniently, by call and rule, so each
        # can be audited; recorded only when the unit is on.
        if lenient_protocol:
            record["lenient_parses"] = [{"call": p.call, "rules": list(p.rules)}
                                        for p in outcome.lenient_parses]
        # ADR-023 unit 3: answers refused for want of a passing test run;
        # recorded only when the check is on.
        if verify_completion:
            record["verification_rejections"] = outcome.verification_rejections
        if outcome.environment is not None:
            record["environment"] = dict(outcome.environment)
    except ProviderError as exc:
        record["stop"], record["error"] = "error", str(exc)
    record["seconds"] = round(time.perf_counter() - started, 3)

    # The policy's answers, in the order the ASK steps asked them.
    answers = iter(confirm.answers)
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
        if r.decision.decision.value == "ask":
            # Asked of the benchmark policy, not of a person: its verdict and
            # reason belong on the step, beside the executor's own wording.
            answer_ = next(answers, None)
            step_records[-1]["approval"] = (
                {"by": "benchmark policy", "approved": answer_.approved, "reason": answer_.reason}
                if answer_ is not None else {"by": "benchmark policy", "approved": None,
                                             "reason": "no answer recorded"})
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


def _task_digest(task: Task, *, with_contract: bool = True) -> str:
    """The task and its fixture or corpus: a resumed run must measure the same tasks.

    The contract (`action_required`) is part of the task a run measured, so it is
    in the digest: a file written before the contract existed, or under another
    one, is not resumed (ADR-023 §8.3). A knowledge task has no contract and keeps
    the digest it always had. `with_contract=False` is the digest from before the
    field existed; `rescore` accepts it, because the contract changes no check.
    """
    parts: dict[str, Any] = {"instruction": task.instruction, "checks": task.checks,
                             "git": task.git, "git_commits": task.git_commits}
    if with_contract and task.action_required is not None:
        parts["action_required"] = task.action_required
    digest = hashlib.sha256(json.dumps(
        parts, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    for root in (task.fixture, task.corpus):
        if root is None:
            continue
        # Ordered by the relative path as text: Path ordering ignores letter case
        # on Windows only, which put check_settings.py before README.md there and
        # gave file-create-settings another digest than on Linux.
        files = sorted((p.relative_to(root).as_posix(), p) for p in root.rglob("*") if p.is_file())
        for relative, path in files:
            digest.update(relative.encode("utf-8"))
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
    parser.add_argument("--environment-context", action="store_true",
                        help="give each agent run the environment facts the program read "
                             "(ADR-023 §2.1, unit 2). Off by default, so a run is comparable "
                             "with the baseline and with unit 1 alone")
    parser.add_argument("--lenient-protocol", action="store_true",
                        help="read three reply shapes the strict protocol refuses "
                             "(ADR-024 unit A). Off by default, so a run is comparable "
                             "with one made without it at the same commit")
    parser.add_argument("--native-tools", action="store_true",
                        help="declare the tools through the model's native tool interface "
                             "(ADR-025). Off by default, so a run is comparable with one "
                             "made without it at the same commit")
    parser.add_argument("--verify-completion", action="store_true",
                        help="accept an agent answer only after the task's test command "
                             "has passed since the last change (ADR-023 unit 3, §2.3). Off "
                             "by default, so a run is comparable with one made without it "
                             "at the same commit")
    parser.add_argument("--resume", type=Path, help="continue an interrupted result file")
    parser.add_argument("--report", type=Path, help="print the summary of a result file")
    parser.add_argument("--rescore", type=Path,
                        help="re-judge the answer checks of a result file with the current "
                             "checks, into a new file beside it (never overwrites)")
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
    if args.rescore is not None:
        return rescore(args.rescore, args.tasks, out)
    if args.runs < 1:
        print("--runs must be at least 1", file=out)
        return 2

    if args.native_tools and args.lenient_protocol:
        # Lenient parsing reads the text protocol; the native arm has none (ADR-025 §5).
        print("refusing to run: --native-tools and --lenient-protocol cannot both be on",
              file=out)
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
        "environment_context": args.environment_context,
        "lenient_protocol": args.lenient_protocol,
        "native_tools": args.native_tools,
        "verify_completion": args.verify_completion,
        "test_command": "each agent task's command_passes check, run as pytest (agent_test_command); "
                        "the loop reads it only under verify_completion",
        "contract": "each agent task file states action_required and the runner passes an "
                    "AgentTaskContract built from it (ADR-023 §8.3); a file without this key "
                    "ran without a contract",
        "refused_replies": "each agent run lists the replies the loop refused, with their "
                           f"text (clipped at {REFUSED_REPLY_CLIP_CHARS} characters); not scored",
        "judge_model": "none (ADR-013)",
        "scorer": SCORER,
        "language_guard": settings.language_guard,
        "sampling": "recorded per model call as sent (options_sent); the agent loop "
                    "sends only num_predict",
        "policy": describe(),
        "environment": "benchmark containment, not a sandbox: code the agent runs is not "
                       "isolated from the network (ADR-022 §3.5)",
        "environment_variables": {name: os.environ[name] for name in RECORDED_ENVIRONMENT
                                  if name in os.environ},
    }

    done: set[tuple[str, str, int]] = set()
    if args.resume is not None:
        previous = _read(args.resume)
        old = previous[0] if previous and previous[0].get("kind") == "header" else {}
        flags = ("environment_context", "lenient_protocol", "native_tools", "verify_completion")
        for key in ("commit", "model", "tasks", "runs", "languages", *flags):
            # A file from before a flag existed ran without it.
            flag = key in flags
            if (bool(old.get(key)) if flag else old.get(key)) != header[key]:
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
            if task.track == "agent":
                record = run_agent_task(task, language, settings, log, workspace,
                                        environment_context=args.environment_context,
                                        lenient_protocol=args.lenient_protocol,
                                        native_tools=args.native_tools,
                                        verify_completion=args.verify_completion)
            else:
                record = run_knowledge_task(task, language, settings, log, workspace)
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


def rescore(path: Path, tasks_dir: Path, out: TextIO) -> int:
    """Re-judge a result file's answer checks with the current checks.

    Only checks that read the answer alone are re-judged: the answer is in the
    record, the workspace is gone. Workspace checks keep their recorded verdict.
    The result goes to a new file beside the source, which is never modified,
    and whose header names the source and the scorer applied. A task whose
    answer checks are re-judged must not have changed since the run (its digest
    must match): otherwise its checks would not be the ones the run was judged by.
    """
    lines = _read(path)
    header = lines[0] if lines and lines[0].get("kind") == "header" else None
    if header is None:
        print(f"refusing to rescore: {path} has no header", file=out)
        return 2
    before = header.get("scorer", "bench-checks-v1")
    if before == SCORER:
        print(f"nothing to do: {path} was scored with {SCORER}", file=out)
        return 2
    target = path.with_name(f"{path.stem}.rescored-{SCORER}.jsonl")
    if target.exists():
        print(f"refusing to rescore: {target} exists; results are never overwritten", file=out)
        return 2
    tasks = {t.id: t for t in load(tasks_dir)}
    for task_id, recorded in header.get("tasks", {}).items():
        task = tasks.get(task_id)
        if task is None:
            print(f"refusing to rescore: task {task_id} is missing", file=out)
            return 2
        # Only a task whose answer checks are re-judged must be the same task;
        # the others keep their recorded verdicts and are not re-read.
        # A file from before the contract recorded the digest without it; the
        # contract changes no check, so that form still proves the task is the one.
        rejudged = any(c["type"] in ANSWER_CHECKS for c in task.checks)
        if rejudged and recorded.get("digest") not in (
                _task_digest(task), _task_digest(task, with_contract=False)):
            print(f"refusing to rescore: task {task_id} changed since the run", file=out)
            return 2
    rescored = [{**header, "scorer": SCORER, "rescored_from": path.name, "rescored_with": SCORER,
                 "scorer_before": before}]
    changed = 0
    for line in lines[1:]:
        if line.get("kind") != "run":
            rescored.append(line)
            continue
        task = tasks[line["task"]]
        if len(task.checks) != len(line["checks"]):
            print(f"refusing to rescore: {line['task']} has another number of checks", file=out)
            return 2
        evidence = RunEvidence(workspace=Path("."), answer=line.get("answer"))
        checks = []
        for spec, old in zip(task.checks, line["checks"]):
            if spec["type"] not in ANSWER_CHECKS:
                checks.append(old)
                continue
            params = {k: v for k, v in spec.items() if k not in ("type", "informational")}
            verdict, detail = CHECKS[spec["type"]](evidence, **params)
            new = {**old, "verdict": verdict, "detail": detail}
            changed += new["verdict"] != old["verdict"]
            checks.append(new)
        success = all(c["verdict"] == PASS for c in checks if not c.get("informational"))
        signals = [s for s in line.get("signals", []) if not s.startswith("check:")]
        if not success:
            signals += [f"check:{c['check']}" for c in checks
                        if c["verdict"] != PASS and not c.get("informational")]
        rescored.append({**line, "checks": checks, "success": success, "signals": signals})
    target.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rescored),
                      encoding="utf-8")
    print(report(rescored), file=out)
    print(f"{changed} check verdict(s) changed ({before} -> {SCORER})", file=out)
    print(f"rescored: {target}", file=out)
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
    secondary: dict[str, list[bool]] = defaultdict(list)
    for r in runs:
        for c in r.get("checks", []):
            if c.get("informational"):
                secondary[c["check"]].append(c["verdict"] == PASS)
    if secondary:
        out.append("informational checks (not part of success):")
        for name, verdicts in sorted(secondary.items()):
            out.append(f"  {name:20} {sum(verdicts)}/{len(verdicts)} "
                       f"({100 * sum(verdicts) / len(verdicts):.0f}%)")
    failures = [r for r in runs if not r["success"]]
    counts = Counter(s for r in failures for s in r.get("signals", []))
    out.append(f"signals in the {len(failures)} failed run(s):")
    out.extend(f"  {signal:28} {count}" for signal, count in counts.most_common())
    seconds = sorted(r["seconds"] for r in runs)
    out.append(f"seconds per run: median {seconds[len(seconds) // 2]:.0f}, max {seconds[-1]:.0f}")
    return "\n".join(out)
