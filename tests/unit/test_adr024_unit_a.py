"""ADR-024 unit A: three reply shapes the strict protocol refuses, read when asked.

Off by default. On, the strict reading runs first and wins; a repair is tried
only when it refuses, and the repaired reply must still pass the strict parser.
Nothing the model sees changes: when no repair applies, the strict error is
raised word for word.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from personal_ai_core.agent.executor import ToolExecutor
from personal_ai_core.agent.loop import (
    AgentLoop,
    parse_reply,
    parse_reply_lenient,
    single_field_tools,
)
from personal_ai_core.agent.policy import RiskPolicy
from personal_ai_core.agent.recovery import Checkpoints
from personal_ai_core.agent.sandbox import Workspace
from personal_ai_core.agent.tools import Shell, default_tools
from personal_ai_core.agent.web import FetchUrl, WebSearch
from personal_ai_core.app.bench.runner import run_agent_task
from personal_ai_core.core.domain import ModelResponse
from personal_ai_core.conversation.factory import build_agent
from personal_ai_core.persistence.in_memory import InMemoryEventRepository

REPO = Path(__file__).resolve().parents[2]


class Script:
    name = "script"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def generate(self, *, model, messages, options=None):
        self.calls.append(list(messages))
        return ModelResponse(text=self.replies.pop(0), model=model)


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "notes.md").write_text("the answer is 42\n", encoding="utf-8")
    return Workspace(root)


def _loop(ws, script, *, lenient, events=None):
    checkpoints = Checkpoints(ws)
    executor = ToolExecutor(default_tools(ws, checkpoints), RiskPolicy())
    return AgentLoop(provider=script, model="boss", executor=executor, context_window=8192,
                     checkpoints=checkpoints, events=events, lenient_protocol=lenient)


SINGLE = {"read_file": "path", "run_command": "command", "delete_file": "path"}


def _strict_error(text: str) -> str:
    with pytest.raises(ValueError) as caught:
        parse_reply(text)
    return str(caught.value)


# --- the parser ---------------------------------------------------------------


@pytest.mark.parametrize("reply, expected, rules", [
    ('{"answer": 4}', {"answer": "4"}, ("numeric_answer",)),
    ('{"answer": 2.5}', {"answer": "2.5"}, ("numeric_answer",)),
    ('{"tool": "run_command", "arguments": "git status"}',
     {"tool": "run_command", "arguments": {"command": "git status"}}, ("string_arguments",)),
    ("{'tool': 'read_file', 'arguments': {'path': 'notes.md'}}",
     {"tool": "read_file", "arguments": {"path": "notes.md"}}, ("python_literal",)),
    ("{'tool': 'read_file', 'arguments': 'notes.md'}",
     {"tool": "read_file", "arguments": {"path": "notes.md"}}, ("python_literal", "string_arguments")),
])
def test_each_unambiguous_shape_is_read_and_named(reply, expected, rules):
    assert parse_reply_lenient(reply, SINGLE) == (expected, rules)


def test_a_reply_the_strict_parser_accepts_is_returned_untouched_and_unnamed():
    reply = '{"tool": "read_file", "arguments": {"path": "a"}}'
    assert parse_reply_lenient(reply, SINGLE) == (parse_reply(reply), ())


@pytest.mark.parametrize("reply", [
    '{"answer": true}',                                       # a bool is not a number here
    '{"answer": NaN}',                                        # not a finite number
    '{"answer": -Infinity}',
    "{'answer': b'4'}",                                       # bytes: no JSON text
    '{"tool": "write_file", "arguments": "VERSION"}',         # two required fields: ambiguous
    'write_file(VERSION, "1.5.0")',                           # function-call syntax: not read
    '{"tool": "write_file", "arguments": {"path": "a", "content": "{"x": 1}"}}',  # broken content
    "{'tool': __import__('os').getcwd()}",                    # literal_eval runs no code
    "I will read the file first.",
])
def test_anything_else_raises_the_strict_error_word_for_word(reply):
    with pytest.raises(ValueError) as caught:
        parse_reply_lenient(reply, SINGLE)
    assert str(caught.value) == _strict_error(reply)


def _bench_specs(root: Path) -> dict:
    """The tools build_agent offers: the workspace tools, the shell, the web."""
    sandbox = Workspace(root)
    tools = [*default_tools(sandbox, Checkpoints(sandbox)), Shell(sandbox),
             WebSearch(None), FetchUrl(None)]
    return {t.spec.name: t.spec for t in tools}


def test_single_field_tools_are_derived_from_the_schemas_not_listed(tmp_path):
    """Exactly one REQUIRED field, of type string; optional fields keep their
    defaults. write_file (path and content) is the one tool left out."""
    assert single_field_tools(_bench_specs(tmp_path)) == {
        "read_file": "path", "delete_file": "path", "run_command": "command",
        "shell": "command", "web_search": "query", "fetch_url": "url",
        "find_files": "pattern", "search_text": "text"}


# --- the loop -----------------------------------------------------------------


def test_off_by_default_a_numeric_answer_is_still_a_protocol_error(ws):
    """The default itself, not an explicit False: the loop built without the
    argument, and the factory's and the benchmark's defaults."""
    checkpoints = Checkpoints(ws)
    loop = AgentLoop(provider=Script('{"answer": 42}', '{"answer": "42"}'), model="boss",
                     executor=ToolExecutor(default_tools(ws, checkpoints), RiskPolicy()),
                     context_window=8192, checkpoints=checkpoints)
    outcome = loop.run("what is the answer?", session_id="s1")
    assert outcome.protocol_errors == 1 and outcome.lenient_parses == ()
    for function in (build_agent, run_agent_task):
        assert inspect.signature(function).parameters["lenient_protocol"].default is False


def test_on_a_numeric_answer_finishes_and_is_recorded(ws):
    outcome = _loop(ws, Script('{"answer": 42}'), lenient=True).run(
        "what is the answer?", session_id="s1")
    assert outcome.answer == "42" and outcome.protocol_errors == 0
    assert [(p.call, p.rules) for p in outcome.lenient_parses] == [(1, ("numeric_answer",))]


def test_on_a_string_argument_runs_the_tool_it_names(ws):
    outcome = _loop(ws, Script('{"tool": "read_file", "arguments": "notes.md"}',
                               '{"answer": "It says 42."}'), lenient=True).run(
        "what do my notes say?", session_id="s1")
    assert outcome.finished and outcome.protocol_errors == 0
    assert outcome.steps[0].record.request.arguments == {"path": "notes.md"}
    assert outcome.steps[0].record.executed
    assert [(p.call, p.rules) for p in outcome.lenient_parses] == [(1, ("string_arguments",))]


def test_on_an_unrepairable_reply_the_model_is_told_exactly_what_it_is_told_today(ws):
    reply = 'write_file(VERSION, "1.5.0")'
    script = Script(reply, '{"answer": "done"}')
    _loop(ws, script, lenient=True).run("bump the version", session_id="s1")
    feedback = script.calls[1][-1].content
    assert feedback == f"Protocol error: {_strict_error(reply)}. Reply with one JSON object."


def test_on_events_carry_nothing_new(ws):
    events = InMemoryEventRepository()
    _loop(ws, Script('{"answer": 42}'), lenient=True, events=events).run("q", session_id="s1")
    finished = events.list_for_session("s1")[-1].payload
    assert set(finished) == {"finished", "steps", "protocol_errors", "stopped_reason",
                             "touched_files", "action_rejections", "action_required"}


# --- the recorded replies of #198, through the real parser --------------------


def _protocol_error_texts():
    for name in ("bench-20261003T161348Z.jsonl", "bench-20261003T164631Z.jsonl"):
        for line in (REPO / "evals" / "results" / "bench" / name).read_text(
                encoding="utf-8").splitlines():
            run = json.loads(line) if line else {}
            if run.get("kind") == "run" and run.get("track") == "agent":
                for refused in run["refused_replies"]:
                    if refused["kind"] == "protocol_error":
                        yield refused["text"]


def test_the_real_parser_recovers_what_adr_024_s_replay_predicted(tmp_path):
    """17 of 98, by the same three rules (ADR-024 §3), now through the code."""
    single = single_field_tools(_bench_specs(tmp_path))
    texts = list(_protocol_error_texts())
    assert len(texts) == 98
    counted: dict[str, int] = {}
    for text in texts:
        try:
            _, rules = parse_reply_lenient(text, single)
        except ValueError:
            continue
        for rule in rules:
            counted[rule] = counted.get(rule, 0) + 1
    assert counted == {"numeric_answer": 5, "string_arguments": 8, "python_literal": 4}


def _agent_runs(name: str) -> list[dict]:
    path = REPO / "evals" / "results" / "bench" / name
    runs = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]
    return [r for r in runs if r.get("kind") == "run" and r["track"] == "agent"]


def test_unit_a_touches_too_few_attempts_for_the_deciding_rule_to_see_it_alone(tmp_path):
    """Planning (ADR-024 §5), read from #198 after its text was seen: the
    attempts holding at least one reply unit A reads. Even if every one of
    them had passed, agent success moves by at most 6 of 120 (5 points) under
    unit 1, where ADR-023 amendment 1 is powered for 20 (D2)."""
    single = single_field_tools(_bench_specs(tmp_path))

    def readable(text: str) -> bool:
        try:
            return bool(parse_reply_lenient(text, single)[1])
        except ValueError:
            return False

    for name, touched, failed in (("bench-20261003T161348Z.jsonl", 6, 5),
                                  ("bench-20261003T164631Z.jsonl", 9, 7)):
        runs = _agent_runs(name)
        assert len(runs) == 120
        hit = [r for r in runs if any(x["kind"] == "protocol_error" and readable(x["text"])
                                      for x in r["refused_replies"])]
        assert (len(hit), sum(not r["success"] for r in hit)) == (touched, failed)


def test_the_rule_adr_024_quotes_is_the_accepted_rule_word_for_word():
    """§5.1's finding rests on R1's text: it must be the amendment's, not a paraphrase."""
    def flat(name: str) -> str:
        return " ".join((REPO / "docs" / "ADR" / name).read_text(encoding="utf-8").split())

    r1 = ("The control's target class is declared before the runs: unit 1 → class 1, "
          "unit 2 → class 3, unit 3 → class 2.")
    assert r1 in flat("ADR-023-plan-execute-verify.md")
    assert f'"{r1}"' in flat("ADR-024-reply-protocol.md")
