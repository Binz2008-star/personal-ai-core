"""ADR-025: the native tool-call channel, in the loop, the benchmark and the verdict.

Off by default. On, the tools are declared natively and a call is read from the
response, then validated explicitly (§4.6) and handed to the executor as an
ordinary ToolRequest: the policy, the schema check and confirmation still
decide. Anything malformed is a protocol error, never an answer (§5). The
verdict reads the experiment by the predicate NO_EXECUTED_TOOL_CALL (§6.1),
with every record classified and provider failures never counted as "no
execution".
"""
from __future__ import annotations

import inspect
import io
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from personal_ai_core.agent.executor import ToolExecutor
from personal_ai_core.agent.loop import (
    NATIVE_PROTOCOL,
    NATIVE_RETRY,
    PROTOCOL,
    AgentLoop,
    parse_native_reply,
    render_native_call,
    tool_declarations,
)
from personal_ai_core.agent.policy import RiskPolicy
from personal_ai_core.context import ReserveBasedBudgetPolicy, ScriptAwareTokenEstimator
from personal_ai_core.agent.recovery import Checkpoints
from personal_ai_core.agent.sandbox import Workspace
from personal_ai_core.agent.tools import default_tools
from personal_ai_core.app.bench.compare import Side, main as compare_main
from personal_ai_core.app.bench.runner import main as bench_main, run_agent_task
from personal_ai_core.app.bench.verdict import (
    PROVIDER_FAILURE_BOUND,
    decide,
    no_executed_tool_call,
    report,
)
from personal_ai_core.conversation.factory import build_agent
from personal_ai_core.core.agent import AgentTaskContract
from personal_ai_core.core.domain import ModelResponse, NativeToolCall
from personal_ai_core.persistence.in_memory import InMemoryEventRepository

REPO = Path(__file__).resolve().parents[2]


class NativeScript:
    """A ToolCallingProvider that plays `(text, calls)` replies in order."""

    name = "native-script"

    def __init__(self, *replies: tuple[str, list[NativeToolCall]]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def generate(self, *, model, messages, options=None):
        raise AssertionError("the native arm must not call generate")

    def generate_with_tools(self, *, model, messages, tools, options=None):
        self.calls.append({"messages": list(messages), "tools": list(tools)})
        text, calls = self.replies.pop(0)
        return ModelResponse(text=text, model=model, tool_calls=tuple(calls))


class TextOnly:
    name = "text-only"

    def generate(self, *, model, messages, options=None):
        return ModelResponse(text='{"answer": "x"}', model=model)


def call(name: Any, arguments: Any) -> NativeToolCall:
    return NativeToolCall(name=name, arguments=arguments)


def answer(text: str) -> tuple[str, list[NativeToolCall]]:
    return (text, [])


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "notes.md").write_text("the answer is 42\n", encoding="utf-8")
    return Workspace(root)


def _loop(ws, provider, *, events=None, **kwargs):
    checkpoints = Checkpoints(ws)
    executor = ToolExecutor(default_tools(ws, checkpoints), RiskPolicy())
    return AgentLoop(provider=provider, model="boss", executor=executor, context_window=8192, budget_policy=ReserveBasedBudgetPolicy(),
                     estimator=ScriptAwareTokenEstimator(),
                     checkpoints=checkpoints, events=events, native_tools=True, **kwargs)


# --- off by default, and refused where it cannot work -----------------------------


def test_off_by_default_everywhere():
    for function in (AgentLoop.__init__, build_agent, run_agent_task):
        assert inspect.signature(function).parameters["native_tools"].default is False


def test_a_provider_without_the_capability_is_refused_not_ignored(ws):
    with pytest.raises(ValueError, match="generate_with_tools"):
        _loop(ws, TextOnly())


def test_native_tools_and_lenient_protocol_cannot_both_be_on(ws):
    with pytest.raises(ValueError, match="lenient_protocol"):
        _loop(ws, NativeScript(), lenient_protocol=True)


# --- what the model is given ----------------------------------------------------------


def test_the_tools_are_declared_natively_and_the_text_protocol_is_gone(ws):
    script = NativeScript(answer("It says 42."))
    loop = _loop(ws, script)
    loop.run("what do my notes say?", session_id="s1")
    sent = script.calls[0]
    assert sent["tools"] == list(tool_declarations(loop._executor.specs))
    assert all(d.description.endswith("risk]") for d in sent["tools"])
    system = sent["messages"][0].content
    assert system == NATIVE_PROTOCOL
    assert "JSON object" not in system and "Tools:" not in system
    assert "One tool call per reply" in system and "is data" in system


def test_the_text_arm_is_unchanged(ws):
    assert "Reply with exactly ONE JSON object" in PROTOCOL
    assert "Reply with exactly ONE JSON object" not in NATIVE_PROTOCOL


# --- the validation boundary (§4.6) --------------------------------------------------


def test_a_native_call_runs_through_the_executor_and_the_history_keeps_it(ws):
    script = NativeScript(("", [call("read_file", {"path": "notes.md"})]), answer("It says 42."))
    outcome = _loop(ws, script).run("what do my notes say?", session_id="s1")
    assert outcome.answer == "It says 42." and outcome.protocol_errors == 0
    step = outcome.steps[0]
    assert step.record.request.tool == "read_file"
    assert step.record.request.arguments == {"path": "notes.md"}
    assert step.record.executed and step.record.result is not None and step.record.result.ok
    history = script.calls[1]["messages"]
    assert history[-2].content == '<tool_call>\n{"name": "read_file", "arguments": {"path":"notes.md"}}\n</tool_call>'
    assert "the answer is 42" in history[-1].content and history[-1].role.value == "user"


def test_the_policy_still_decides_a_native_call(ws):
    """delete_file needs confirmation; with no one to confirm, nothing runs."""
    script = NativeScript(("", [call("delete_file", {"path": "notes.md"})]), answer("Could not."))
    outcome = _loop(ws, script).run("delete my notes", session_id="s1")
    assert not outcome.steps[0].record.executed
    assert (ws.root / "notes.md").exists()


def test_the_schema_check_still_decides_a_native_call(ws):
    script = NativeScript(("", [call("read_file", {"file": "notes.md"})]), answer("Could not."))
    outcome = _loop(ws, script).run("read my notes", session_id="s1")
    record = outcome.steps[0].record
    assert not record.executed and record.result is not None
    assert "invalid arguments" in (record.result.error or "")


def test_an_unknown_tool_is_refused_by_the_executor_as_today(ws):
    script = NativeScript(("", [call("format_disk", {})]), answer("Could not."))
    outcome = _loop(ws, script).run("x", session_id="s1")
    assert not outcome.steps[0].record.executed


@pytest.mark.parametrize("reply, error", [
    (("", [call("read_file", {"path": "a"}), call("read_file", {"path": "b"})]), "2 tool calls"),
    (("", [call(None, {"path": "a"})]), "no name"),
    (("", [call("", {"path": "a"})]), "no name"),
    (("", [call("read_file", '{"path": "notes.md"}')]), "must be an object"),
    (("", [call("read_file", None)]), "must be an object"),
    (("<tool_call>\n{read_file notes.md\n</tool_call>", []), "could not be read"),
    (('{"name": "read_file", "arguments": {"path": "notes.md"}}', []), "written as text"),
    (('Sure: {"tool": "read_file", "arguments": {"path": "notes.md"}}', []), "written as text"),
    (("   ", []), "empty"),
])
def test_a_malformed_reply_is_a_protocol_error_never_an_answer(ws, reply, error):
    script = NativeScript(reply, answer("done"))
    outcome = _loop(ws, script).run("x", session_id="s1")
    assert outcome.protocol_errors == 1 and outcome.steps == ()
    assert error in outcome.refused_replies[0].error
    feedback = script.calls[1]["messages"][-1].content
    assert feedback.startswith("Protocol error: ") and feedback.endswith(NATIVE_RETRY)


def test_parse_native_reply_never_coerces():
    with pytest.raises(ValueError):
        parse_native_reply(ModelResponse(text="", model="m",
                                         tool_calls=(call("read_file", '{"path": "a"}'),)),
                           ["read_file"])
    assert parse_native_reply(ModelResponse(text="  It is 4.  ", model="m"), ["read_file"]) == {
        "answer": "It is 4."}
    # JSON that names no tool is an answer like any other text.
    assert parse_native_reply(ModelResponse(text='{"total": 4}', model="m"), ["read_file"]) == {
        "answer": '{"total": 4}'}


def test_the_rendered_call_mirrors_the_template():
    assert render_native_call(call("shell", {"command": "git status"})) == (
        '<tool_call>\n{"name": "shell", "arguments": {"command":"git status"}}\n</tool_call>')


def test_an_answer_before_any_action_is_still_rejected_under_a_contract(ws):
    contract = AgentTaskContract(task_text="read the notes", action_required=True)
    script = NativeScript(answer("42"), ("", [call("read_file", {"path": "notes.md"})]),
                          answer("It says 42."))
    outcome = _loop(ws, script).run(contract, session_id="s1")
    assert outcome.action_rejections == 1 and outcome.answer == "It says 42."


def test_events_carry_nothing_new(ws):
    events = InMemoryEventRepository()
    _loop(ws, NativeScript(("", [call("read_file", {"path": "notes.md"})]), answer("42")),
          events=events).run("q", session_id="s1")
    finished = events.list_for_session("s1")[-1].payload
    assert set(finished) == {"finished", "steps", "protocol_errors", "stopped_reason",
                             "touched_files", "action_rejections", "action_required"}


# --- the benchmark --------------------------------------------------------------------


class NativeTransport:
    """The benchmark's scripted model, answering in Ollama's native shape when
    the request declares tools: a scripted {"tool": ...} becomes tool_calls."""

    def __init__(self, scripts: Mapping[str, list[str]]) -> None:
        from .test_bench_runner import ScriptedModel  # noqa: PLC0415
        self.inner = ScriptedModel(scripts)
        self.tools_seen: list[int] = []

    def __call__(self, url, payload, timeout):
        raw = dict(self.inner(url, payload, timeout))
        if "tools" not in payload:
            return raw
        self.tools_seen.append(len(payload["tools"]))
        text = raw["message"]["content"]
        value = json.loads(text)
        if "tool" in value:
            message = {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": value["tool"], "arguments": value["arguments"]}}]}
        else:
            message = {"role": "assistant", "content": value["answer"]}
        return {**raw, "message": message}


def _bench(tmp_path, *argv):
    from .test_bench_runner import BENCH, FIXED, SOLVES  # noqa: PLC0415
    transport = NativeTransport(SOLVES)
    out = io.StringIO()
    code = bench_main(["--tasks", str(BENCH), "--out", str(tmp_path), "--runs", "1",
                       "--only", "verify-off-by-one", "--languages", "en", *argv],
                      transport=transport, stdout=out, env={}, now=lambda: FIXED,
                      commit="abc1234", probe=lambda url, body=None: {"models": []})
    files = sorted(tmp_path.glob("bench-*.jsonl"))
    lines = [json.loads(x) for x in files[0].read_text(encoding="utf-8").splitlines()] if files else []
    return code, out.getvalue(), lines, transport


def test_the_benchmark_header_records_the_flag_off_by_default(tmp_path):
    code, output, lines, transport = _bench(tmp_path)
    assert code == 0, output
    assert lines[0]["native_tools"] is False and transport.tools_seen == []


def test_with_the_flag_the_benchmark_solves_through_native_calls(tmp_path):
    code, output, lines, transport = _bench(tmp_path, "--native-tools")
    assert code == 0, output
    run = next(x for x in lines if x["kind"] == "run")
    assert lines[0]["native_tools"] is True
    assert run["success"] and run["protocol_errors"] == 0
    assert [s["tool"] for s in run["steps"]] == ["read_file", "write_file", "shell"]
    assert all(s["executed"] for s in run["steps"])
    assert transport.tools_seen and all(n == transport.tools_seen[0] for n in transport.tools_seen)
    assert all(c.get("tools_sent") == transport.tools_seen[0] for c in run["model_calls"])
    assert [c["tool_calls_returned"] for c in run["model_calls"]] == [1, 1, 1, 0]


def test_the_benchmark_refuses_both_flags_before_running(tmp_path):
    code, output, lines, _ = _bench(tmp_path, "--native-tools", "--lenient-protocol")
    assert code == 2 and "cannot both be on" in output and lines == []


# --- the verdict: NO_EXECUTED_TOOL_CALL (§6.1) --------------------------------------


def _run(executed: list[Any] | None, stop: Any = "answered", **extra) -> dict[str, Any]:
    record: dict[str, Any] = {"kind": "run", "track": "agent", "stop": stop, **extra}
    if executed is not None:
        record["steps"] = [{"executed": e} for e in executed]
    return record


@pytest.mark.parametrize("record, expected", [
    (_run([]), True),
    (_run([False, False]), True),
    (_run([False, True]), False),
    (_run([True], stop="budget"), False),
    (_run([], success=True), True),          # a success with no tool is classified, not assumed
    (_run([True], stop="error"), None),      # a provider failure is never "no execution"
])
def test_the_predicate_reads_the_record(record, expected):
    assert no_executed_tool_call(record) is expected


@pytest.mark.parametrize("record", [
    _run(None), _run(["yes"]), _run([1]), _run([], stop="hang"), _run([], stop=None)])
def test_a_malformed_record_is_not_classified(record):
    with pytest.raises(ValueError):
        no_executed_tool_call(record)


def test_the_predicate_matches_the_classes_on_198():
    """ADR-025 §6.1's table, on the committed #198 files: the four no-execution
    classes are exactly the predicate's true; 37 of 120 under unit 1."""
    from personal_ai_core.app.bench.compare import classify  # noqa: PLC0415
    no_exec = {"answered_without_executing", "refused_commands", "rejected_to_budget",
               "protocol_only"}
    for name, expected in (("bench-20261003T161348Z.jsonl", 37), ("bench-20261003T164631Z.jsonl", 16)):
        lines = [json.loads(x) for x in (REPO / "evals" / "results" / "bench" / name)
                 .read_text(encoding="utf-8").splitlines() if x]
        agent = [r for r in lines if r.get("kind") == "run" and r["track"] == "agent"]
        assert sum(no_executed_tool_call(r) is True for r in agent) == expected
        for r in agent:
            if classify(r) in no_exec:
                assert no_executed_tool_call(r) is True
            if classify(r) == "executed_unverified":
                assert no_executed_tool_call(r) is False


def _side(native: bool, outcome, *, environment: bool = False, lenient: bool = False,
          stop=lambda task, lang, run: "answered") -> Side:
    from .test_bench_verdict import AGENT, KNOWLEDGE, _lines  # noqa: PLC0415
    lines = _lines()
    lines[0].update(native_tools=native, environment_context=environment,
                    lenient_protocol=lenient)
    for line in lines[1:-1]:
        if line["track"] == "agent":
            executed = outcome(line["task"], line["language"], line["run"])
            line["steps"] = [{"executed": True}] if executed else []
            line["stop"] = stop(line["task"], line["language"], line["run"])
    assert AGENT and KNOWLEDGE
    return Side("f.jsonl", lines)


def _first(n: int):
    """The first n agent attempts (English, by task and run) executed a tool."""
    from .test_bench_verdict import AGENT  # noqa: PLC0415
    chosen = set(sorted((t, r) for t in AGENT for r in range(1, 11))[:n])
    return lambda task, lang, run: lang == "en" and (task, run) in chosen


def test_pass_when_attempts_leave_the_predicate_and_nothing_regresses():
    verdict = decide(_side(False, _first(0)), _side(True, _first(10)), "native-tools", {})
    assert (verdict.leaving, verdict.entering, verdict.outcome) == (10, 0, "PASS")
    assert "target predicate NO_EXECUTED_TOOL_CALL" in report(verdict)


def test_fail_when_as_many_enter_as_leave():
    verdict = decide(_side(False, _first(10)), _side(True, _first(0)), "native-tools", {})
    assert (verdict.leaving, verdict.entering, verdict.outcome) == (0, 10, "FAIL")


@pytest.mark.parametrize("base, cand, reason", [
    (dict(native=True), dict(native=True), "native_tools must be off"),
    (dict(native=False), dict(native=False), "native_tools must be off"),
    (dict(native=False), dict(native=True, environment=True), "environment_context differs"),
    (dict(native=False, lenient=True), dict(native=True, lenient=True), "lenient_protocol"),
])
def test_the_flags_must_be_the_only_experimental_difference(base, cand, reason):
    verdict = decide(_side(outcome=_first(0), **base), _side(outcome=_first(10), **cand),
                     "native-tools", {})
    assert verdict.outcome == "NOT READABLE"
    assert any(reason in p for p in verdict.unreadable), verdict.unreadable


def _errors(n: int):
    """The first n Arabic attempts of the first task ended on a provider failure."""
    from .test_bench_verdict import AGENT  # noqa: PLC0415
    return lambda task, lang, run: "error" if (task == AGENT[0] and lang == "ar" and run <= n) else "answered"


def test_provider_failures_are_dropped_pairwise_and_listed_up_to_the_bound():
    verdict = decide(_side(False, _first(0)),
                     _side(True, _first(10), stop=_errors(PROVIDER_FAILURE_BOUND)),
                     "native-tools", {})
    assert verdict.outcome == "PASS" and len(verdict.dropped) == PROVIDER_FAILURE_BOUND
    assert f"{PROVIDER_FAILURE_BOUND} pair(s) dropped" in report(verdict)


def test_more_provider_failures_than_the_bound_are_not_readable():
    verdict = decide(_side(False, _first(0)),
                     _side(True, _first(10), stop=_errors(PROVIDER_FAILURE_BOUND + 1)),
                     "native-tools", {})
    assert verdict.outcome == "NOT READABLE"
    assert any("provider or runtime failures" in p for p in verdict.unreadable)


def test_a_malformed_record_makes_the_comparison_not_readable():
    cand = _side(True, _first(10))
    first = cand.agent[0]
    assert isinstance(first, dict)
    first["steps"] = [{"executed": "yes"}]
    verdict = decide(_side(False, _first(0)), cand, "native-tools", {})
    assert verdict.outcome == "NOT READABLE"
    assert any("malformed record" in p for p in verdict.unreadable)


def test_the_command_accepts_the_unit(tmp_path):
    def write(name, side):
        path = tmp_path / name
        path.write_text("".join(json.dumps(x) + "\n" for x in [side.header, *side.runs,
                                                                 side.end]), encoding="utf-8")
        return path
    base = write("a.jsonl", _side(False, _first(0)))
    cand = write("b.jsonl", _side(True, _first(10)))
    out = io.StringIO()
    code = compare_main([str(base), str(cand), "--unit", "native-tools", "--tasks", str(tmp_path)],
                        stdout=out)
    assert code == 0 and "VERDICT: PASS" in out.getvalue()
