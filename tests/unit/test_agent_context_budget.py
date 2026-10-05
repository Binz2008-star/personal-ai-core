"""N2: an agent request is measured before it is sent.

The chat path measures every turn against the window (P0-1/P0-2); the agent
loop did not. Its messages grew by a model reply and a tool result per step,
each result up to 20,000 characters -- about 10,400 estimated tokens of code,
12,400 of Arabic -- so one full file read already overran an 8192 window by the
Core's own estimate. The server then truncated the prompt silently, dropping
the oldest non-system message first: the task.

Now every request is allocated with the same kind of policy and the same
estimator a chat turn uses. A request that does not fit is not sent and the
run stops on the record. A tool result is cut to the room left (owner
decision 1), and one with under MIN_RESULT_TOKENS of room stops the run
instead of showing a stub (owner decision 2). The task and every system
message are never dropped.
"""
from __future__ import annotations

from typing import Any

import pytest

from personal_ai_core.agent.executor import ToolExecutor
from personal_ai_core.agent.loop import FITTED_MARKER, MIN_RESULT_TOKENS, AgentLoop
from personal_ai_core.agent.policy import RiskPolicy
from personal_ai_core.agent.recovery import Checkpoints
from personal_ai_core.agent.sandbox import Workspace
from personal_ai_core.agent.tools import MAX_OUTPUT_CHARS, default_tools
from personal_ai_core.context import ReserveBasedBudgetPolicy, ScriptAwareTokenEstimator
from personal_ai_core.core.domain import EventType, Message, ModelResponse, Role
from personal_ai_core.identity import DefaultIdentityComposer
from personal_ai_core.persistence.in_memory import InMemoryEventRepository

ESTIMATOR = ScriptAwareTokenEstimator()
CODE = ("def total(items):\n    return sum(item.price for item in items)\n" * 400)[:20_000]
ARABIC = ("الإجازة السنوية تنتقل إلى العام التالي بحد أقصى خمسة أيام. " * 400)[:20_000]
TASK = "Summarise big.py"


class Recorder:
    """A provider that scripts each reply and keeps every request it was sent."""

    name = "recorder"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def generate(self, *, model, messages, options=None):
        self.calls.append({"messages": list(messages), "options": dict(options or {})})
        return ModelResponse(text=self.replies.pop(0), model=model)


def read(path: str) -> str:
    return '{"tool": "read_file", "arguments": {"path": "%s"}}' % path


def build(tmp_path, provider, *, window=8192, files=None, events=None, policy=None):
    root = tmp_path / "ws"
    root.mkdir(exist_ok=True)
    for name, text in (files or {}).items():
        (root / name).write_text(text, encoding="utf-8")
    workspace = Workspace(root)
    checkpoints = Checkpoints(workspace)
    identity = DefaultIdentityComposer()
    policy = policy or ReserveBasedBudgetPolicy(identity_reserve=identity.tokens(ESTIMATOR))
    loop = AgentLoop(
        provider=provider, model="boss",
        executor=ToolExecutor(default_tools(workspace, checkpoints), RiskPolicy()),
        context_window=window, budget_policy=policy, estimator=ESTIMATOR,
        checkpoints=checkpoints, identity=identity, events=events,
        session_exists=(lambda s: True) if events is not None else None,
    )
    return loop, policy


class _Window:
    name, provider = "boss", "recorder"

    def __init__(self, context_window: int) -> None:
        self.context_window = context_window


def fits(call: dict[str, Any], policy, window: int) -> bool:
    """The invariant, computed independently of the loop: everything but the
    identity message (funded by its reserve), plus the reserves, within the window."""
    measured = call["messages"][1:]
    history = sum(ESTIMATOR.estimate(m.content) for m in measured)
    return not policy.allocate(model=_Window(window), history_tokens=history).overcommitted


# --- the invariant ---------------------------------------------------------------


def test_every_request_fits_the_window_it_is_sent_with(tmp_path):
    provider = Recorder(read("big.py"), read("big.py"), read("big.py"), '{"answer": "done"}')
    loop, policy = build(tmp_path, provider, files={"big.py": CODE})
    loop.run(TASK, session_id="s1")
    assert len(provider.calls) >= 2
    assert all(fits(call, policy, 8192) for call in provider.calls)
    assert all(call["options"]["num_ctx"] == 8192 for call in provider.calls)


def test_the_task_and_every_system_message_are_in_every_request(tmp_path):
    provider = Recorder(read("big.py"), read("big.py"), '{"answer": "done"}')
    loop, _ = build(tmp_path, provider, files={"big.py": CODE})
    loop.run(TASK, session_id="s1")
    first = provider.calls[0]["messages"]
    systems = [m.content for m in first if m.role is Role.SYSTEM]
    for call in provider.calls:
        contents = [m.content for m in call["messages"]]
        assert TASK in contents
        assert all(text in contents for text in systems)


# --- a result too big for the room left is cut, and says so ------------------------


@pytest.mark.parametrize("text", [CODE, ARABIC], ids=["code", "arabic"])
def test_a_full_size_result_is_cut_to_fit_and_says_so(tmp_path, text):
    provider = Recorder(read("big.txt"), '{"answer": "done"}')
    loop, policy = build(tmp_path, provider, files={"big.txt": text})
    outcome = loop.run(TASK, session_id="s1")
    assert outcome.finished
    shown: Message = provider.calls[1]["messages"][-1]
    assert FITTED_MARKER.strip() in shown.content
    assert len(shown.content) < len(text)
    assert fits(provider.calls[1], policy, 8192)


def test_a_result_that_fits_is_shown_exactly_as_before(tmp_path):
    provider = Recorder(read("small.md"), '{"answer": "done"}')
    loop, _ = build(tmp_path, provider, files={"small.md": "five days carry over\n"})
    loop.run(TASK, session_id="s1")
    shown = provider.calls[1]["messages"][-1].content
    assert "five days carry over" in shown
    assert FITTED_MARKER.strip() not in shown and "[output truncated" not in shown


def test_the_reply_limit_sent_is_the_policys_reserve(tmp_path):
    provider = Recorder('{"answer": "done"}')
    loop, _ = build(tmp_path, provider, policy=ReserveBasedBudgetPolicy(generation_reserve=512))
    loop.run(TASK, session_id="s1")
    assert provider.calls[0]["options"]["num_predict"] == 512


# --- when it cannot fit, the run stops on the record ---------------------------------


def _opening_tokens(tmp_path) -> int:
    probe = Recorder('{"answer": "x"}')
    loop, _ = build(tmp_path / "probe", probe)
    loop.run(TASK, session_id="s1")
    return sum(ESTIMATOR.estimate(m.content) for m in probe.calls[0]["messages"][1:])


def test_an_opening_that_does_not_fit_sends_nothing_and_says_why(tmp_path):
    events = InMemoryEventRepository()
    provider = Recorder('{"answer": "never"}')
    loop, _ = build(tmp_path, provider, window=2048, events=events)
    outcome = loop.run("ب" * 8000, session_id="s1")
    assert provider.calls == []
    assert not outcome.finished
    assert "the next request needs about" in (outcome.stopped_reason or "")
    [finish] = [e for e in events.list_for_session("s1") if e.type is EventType.AGENT_FINISHED]
    assert finish.payload["finished"] is False
    assert "ب" not in str(finish.payload)  # numbers, never the text


def test_too_little_room_for_a_result_stops_instead_of_showing_a_stub(tmp_path):
    (tmp_path / "probe").mkdir()
    identity_reserve = DefaultIdentityComposer().tokens(ESTIMATOR)
    reserves = 1024 + 256 + identity_reserve
    # Room for the opening and the model's tool call, then under the minimum.
    window = _opening_tokens(tmp_path) + reserves + MIN_RESULT_TOKENS // 2 + 40
    events = InMemoryEventRepository()
    provider = Recorder(read("big.py"), '{"answer": "never"}')
    loop, _ = build(tmp_path, provider, window=window, files={"big.py": CODE}, events=events)
    outcome = loop.run(TASK, session_id="s1")
    assert len(provider.calls) == 1
    assert not outcome.finished
    assert "result does not fit" in (outcome.stopped_reason or "")
    [finish] = [e for e in events.list_for_session("s1") if e.type is EventType.AGENT_FINISHED]
    assert "result does not fit" in finish.payload["stopped_reason"]


def test_a_stop_after_a_change_names_the_file_for_the_undo(tmp_path):
    """Room for a small write and its result, then not for a file read: the
    run stops, and the change it made is on the outcome for pac's undo."""
    (tmp_path / "probe").mkdir()
    reserves = 1024 + 256 + DefaultIdentityComposer().tokens(ESTIMATOR)
    window = _opening_tokens(tmp_path) + reserves + 300
    provider = Recorder(
        '{"tool": "write_file", "arguments": {"path": "out.md", "content": "x"}}',
        read("big.py"), '{"answer": "never"}',
    )
    loop, policy = build(tmp_path, provider, window=window, files={"big.py": CODE})
    outcome = loop.run(TASK, session_id="s1")
    assert len(provider.calls) == 2
    assert all(fits(call, policy, window) for call in provider.calls)
    assert not outcome.finished and "result does not fit" in (outcome.stopped_reason or "")
    assert outcome.touched_files == ("out.md",)


# --- search_text has the same output cap as every other tool --------------------------


def test_search_text_output_is_capped_like_every_other_tool(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "min.js").write_text("var needle=" + "x" * 30_000 + "\n", encoding="utf-8")
    workspace = Workspace(root)
    from personal_ai_core.core.agent import ToolRequest

    executor = ToolExecutor(default_tools(workspace), RiskPolicy())
    result = executor.execute(ToolRequest("search_text", {"text": "needle"})).result
    assert result is not None and result.ok
    assert len(result.output) <= MAX_OUTPUT_CHARS and result.truncated
