"""N1: the model server is told the window the Core budgets against.

Before this, every turn was measured against `PAC_BOSS_CONTEXT_WINDOW` (8192 by
default) and the request carried no `num_ctx`, so Ollama ran whatever window it
had loaded -- its own default, `OLLAMA_CONTEXT_LENGTH`, a Modelfile, or a value
an earlier client chose. A prompt between that window and the Core's was
truncated by the server with no error: the oldest non-system messages dropped,
which on the agent path is the task itself.

Every assertion here is on the payload the transport received, because that is
the only place the claim can be checked; the allocation alone would re-check
accounting that was already true.
"""
from __future__ import annotations

import io
from typing import Any, Mapping

import pytest

from personal_ai_core.app.cli import main
from personal_ai_core.conversation.factory import (
    build_agent,
    build_grounded_in_memory_service,
    build_in_memory_service,
    build_persistent_service,
)
from personal_ai_core.core.config import DEFAULT_BOSS_CONTEXT_WINDOW, Settings
from personal_ai_core.core.domain import EventType
from personal_ai_core.core.knowledge import Document

QUESTION = "ما الفرق بين الذاكرة قصيرة المدى والذاكرة طويلة المدى؟"
CHINESE = "。提供的信息中没有提到您的笔记本电脑的序列号。"
ARABIC = "الذاكرة قصيرة المدى تحتفظ بالمعلومات لفترة قصيرة، والطويلة لفترات أطول."


class Transport:
    """Scripts each reply and keeps each payload."""

    def __init__(self, *replies: str | Mapping[str, Any]) -> None:
        self.replies = list(replies) or ["ok"]
        self.payloads: list[Mapping[str, Any]] = []

    def __call__(self, url: str, payload: Mapping[str, Any], timeout: int):
        self.payloads.append(dict(payload))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        message = dict(reply) if isinstance(reply, Mapping) else {"content": reply}
        return {"model": payload["model"], "message": message, "done_reason": "stop"}

    def windows(self) -> list[Any]:
        return [p.get("options", {}).get("num_ctx") for p in self.payloads]


def _settings(window: int | None = None) -> Settings:
    env = {} if window is None else {"PAC_BOSS_CONTEXT_WINDOW": str(window)}
    return Settings.from_env(env)


def _assembled(events, session_id: str):
    return [e for e in events.list_for_session(session_id)
            if e.type is EventType.CONTEXT_ASSEMBLED]


def _requested(events, session_id: str):
    return [e for e in events.list_for_session(session_id)
            if e.type is EventType.GENERATION_REQUESTED]


# --- chat: the window sent is the window the turn was budgeted against --------


def test_the_default_persistent_path_sends_the_window_it_budgeted_against(tmp_path):
    transport = Transport()
    slice_ = build_persistent_service(_settings(), database=tmp_path / "core.db",
                                      transport=transport)
    try:
        service = slice_.service
        session = service.start_session(service.create_user().id)
        service.send(session_id=session.id, content="hello")
        [assembled] = _assembled(slice_.events, session.id)
    finally:
        slice_.close()
    assert transport.windows() == [assembled.payload["context_window"]]
    assert transport.windows() == [DEFAULT_BOSS_CONTEXT_WINDOW]


@pytest.mark.parametrize("durable", [False, True], ids=["in-memory", "sqlite"])
def test_the_grounded_path_sends_the_window_it_budgeted_against(tmp_path, durable):
    transport = Transport()
    if durable:
        slice_ = build_persistent_service(_settings(), database=tmp_path / "core.db",
                                          transport=transport, grounded=True)
        service, events, ingestion = slice_.service, slice_.events, slice_.ingestion
    else:
        grounded = build_grounded_in_memory_service(_settings(), transport=transport)
        service, events, ingestion = grounded.service, grounded.events, grounded.ingestion
    assert ingestion is not None
    ingestion.ingest(Document(source_uri="file:///notes.md"),
                     "Annual leave carries over up to five days.")
    session = service.start_session(service.create_user().id)
    service.send(session_id=session.id, content="How many leave days carry over?")
    [assembled] = _assembled(events, session.id)
    assert assembled.payload["used"] >= 1  # the turn really was grounded
    assert transport.windows() == [assembled.payload["context_window"]]
    if durable:
        slice_.close()  # type: ignore[possibly-undefined]


def test_a_configured_window_is_the_one_sent_not_the_default():
    """A literal 8192 would pass every test above; this one it fails."""
    transport = Transport()
    service, events = build_in_memory_service(_settings(4096), transport=transport)
    session = service.start_session(service.create_user().id)
    service.send(session_id=session.id, content="hello")
    [assembled] = _assembled(events, session.id)
    assert assembled.payload["context_window"] == 4096
    assert transport.windows() == [4096]


def test_the_language_guard_retry_sends_the_same_window():
    transport = Transport(CHINESE, ARABIC)
    service, events = build_in_memory_service(_settings(), transport=transport)
    session = service.start_session(service.create_user().id)
    reply = service.send(session_id=session.id, content=QUESTION)
    assert reply.content == ARABIC  # the retry happened
    assert transport.windows() == [DEFAULT_BOSS_CONTEXT_WINDOW] * 2


def test_the_request_event_records_the_window_sent():
    transport = Transport()
    service, events = build_in_memory_service(_settings(4096), transport=transport)
    session = service.start_session(service.create_user().id)
    service.send(session_id=session.id, content="hello")
    [requested] = _requested(events, session.id)
    assert requested.payload["num_ctx"] == 4096 == transport.windows()[0]


# --- a caller cannot name another window ---------------------------------------


def test_a_caller_naming_another_window_is_refused_before_anything_is_stored():
    transport = Transport()
    service, events = build_in_memory_service(_settings(), transport=transport)
    session = service.start_session(service.create_user().id)
    before = list(events.list_for_session(session.id))
    with pytest.raises(ValueError, match="num_ctx 2048 differs"):
        service.send(session_id=session.id, content="hello", options={"num_ctx": 2048})
    assert transport.payloads == []
    assert list(service.history(session.id)) == []
    assert list(events.list_for_session(session.id)) == before


def test_a_caller_naming_the_same_window_is_accepted():
    transport = Transport()
    service, _ = build_in_memory_service(_settings(), transport=transport)
    session = service.start_session(service.create_user().id)
    service.send(session_id=session.id, content="hello",
                 options={"num_ctx": DEFAULT_BOSS_CONTEXT_WINDOW})
    assert transport.windows() == [DEFAULT_BOSS_CONTEXT_WINDOW]


# --- the agent: every model call, on both protocols ----------------------------


def _agent(tmp_path, transport, **kwargs):
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    (workspace / "notes.md").write_text("five days carry over\n", encoding="utf-8")
    return build_agent(_settings(4096), workspace=workspace, transport=transport, **kwargs)


def test_every_agent_call_sends_the_window_on_the_text_protocol(tmp_path):
    transport = Transport(
        '{"tool": "read_file", "arguments": {"path": "notes.md"}}',
        '{"tool": "list_directory", "arguments": {}}',
        '{"answer": "five days"}',
    )
    outcome = _agent(tmp_path, transport).loop.run("how many days?", session_id="s1")
    assert outcome.finished
    assert transport.windows() == [4096, 4096, 4096]


def test_every_agent_call_sends_the_window_on_the_native_protocol(tmp_path):
    call = {"content": "", "tool_calls": [
        {"function": {"name": "read_file", "arguments": {"path": "notes.md"}}}]}
    transport = Transport(call, "five days")
    outcome = _agent(tmp_path, transport, native_tools=True).loop.run(
        "how many days?", session_id="s1")
    assert outcome.finished
    assert [len(p.get("tools", [])) > 0 for p in transport.payloads] == [True, True]
    assert transport.windows() == [4096, 4096]


@pytest.mark.parametrize("window", [0, -1, True, 8192.0, "8192"])
def test_the_agent_refuses_a_window_that_is_not_a_positive_whole_number(window):
    from personal_ai_core.agent import RiskPolicy, ToolExecutor
    from personal_ai_core.agent.loop import AgentLoop
    from personal_ai_core.context import ReserveBasedBudgetPolicy, ScriptAwareTokenEstimator

    with pytest.raises(ValueError, match="context_window"):
        AgentLoop(provider=Transport(), model="boss",  # type: ignore[arg-type]
                  executor=ToolExecutor([], RiskPolicy()), context_window=window,
                  budget_policy=ReserveBasedBudgetPolicy(),
                  estimator=ScriptAwareTokenEstimator())


# --- every builder, and the program itself -------------------------------------


@pytest.mark.parametrize("builder", ["in_memory", "grounded", "persistent"])
def test_every_conversation_builder_sends_the_configured_window(tmp_path, builder):
    transport = Transport()
    if builder == "in_memory":
        service, _ = build_in_memory_service(_settings(4096), transport=transport)
    elif builder == "grounded":
        service = build_grounded_in_memory_service(_settings(4096), transport=transport).service
    else:
        slice_ = build_persistent_service(_settings(4096), database=tmp_path / "core.db",
                                          transport=transport)
        service = slice_.service
    session = service.start_session(service.create_user().id)
    service.send(session_id=session.id, content="hello")
    if builder == "persistent":
        slice_.close()  # type: ignore[possibly-undefined]
    assert transport.windows() == [4096]


def test_pac_sends_the_configured_window_in_chat_and_agent(tmp_path):
    chat = Transport()
    out = io.StringIO()
    code = main(["--database", str(tmp_path / "chat.db")], transport=chat,
                stdin=iter(["hello"]), stdout=out, env={"PAC_BOSS_CONTEXT_WINDOW": "4096"})
    assert code == 0, out.getvalue()
    assert chat.windows() == [4096]

    workspace = tmp_path / "ws"
    workspace.mkdir()
    agent = Transport('{"answer": "done"}')
    code = main(["--database", str(tmp_path / "agent.db"), "--agent", "--workspace",
                 str(workspace)], transport=agent,
                stdin=iter(["[action_required=false] say done"]), stdout=out,
                env={"PAC_BOSS_CONTEXT_WINDOW": "4096"})
    assert code == 0, out.getvalue()
    assert agent.windows() == [4096]
