"""N1 Part B: pac checks the server runs the window the Core budgets against.

Part A makes every request name the window (`num_ctx`). That is the
mechanism, not the proof: Ollama caps `num_ctx` at the model's trained context
without a word, and a server may not honour it at all. So:

- before the first turn, a model loaded at another window is announced: the
  first reply reloads it, which takes as long as a cold start;
- once, after the first reply has loaded the model, `/api/ps` is asked what
  window it runs. Smaller than the Core's: the session stops, with the fix,
  before another turn is budgeted for room the server does not have. Unknown:
  pac says it could not confirm, and carries on -- the request still named it.
"""
from __future__ import annotations

import io
from typing import Any, Mapping

from personal_ai_core.app.cli import main
from personal_ai_core.core.config import DEFAULT_BOSS_CONTEXT_WINDOW, Settings

BOSS = Settings().boss_model
RELOAD = "the first reply reloads it at"
UNCONFIRMED = "window not confirmed"


class Transport:
    def __init__(self, *replies: str) -> None:
        self.replies = list(replies) or ["ok"]
        self.calls = 0

    def __call__(self, url: str, payload: Mapping[str, Any], timeout: int):
        self.calls += 1
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return {"model": payload["model"], "message": {"content": reply}}


def probe_reporting(*windows: Any):
    """Each /api/ps answers with the next window: an int, None for a server
    that does not report one, "unloaded" for the model absent, or an exception."""
    queue = list(windows)
    asked: list[str] = []

    def probe(url: str, body: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        if not url.endswith("/api/ps"):
            return {"modelfile": "FROM /models/blobs/sha256-" + "a" * 64}
        asked.append(url)
        window = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(window, BaseException):
            raise window
        if window == "unloaded":
            return {"models": []}
        entry: dict[str, Any] = {"name": BOSS, "model": BOSS, "size": 10, "size_vram": 10}
        if window is not None:
            entry["context_length"] = window
        return {"models": [entry]}

    probe.asked = asked  # type: ignore[attr-defined]
    return probe


def chat(tmp_path, transport, probe, *lines: str):
    out = io.StringIO()
    code = main(["--database", str(tmp_path / "core.db")], transport=transport,
                stdin=iter(lines or ["hello"]), stdout=out, env={}, probe=probe)
    return code, out.getvalue()


# --- before the first turn ------------------------------------------------------------


def test_a_model_loaded_at_another_window_is_announced_as_a_reload(tmp_path):
    code, output = chat(tmp_path, Transport(), probe_reporting(4096, DEFAULT_BOSS_CONTEXT_WINDOW))
    assert code == 0
    assert f"loaded with a 4096-token window: {RELOAD} {DEFAULT_BOSS_CONTEXT_WINDOW}" in output
    assert output.index(RELOAD) < output.index("core> ok")


def test_a_model_loaded_at_the_window_is_not_announced(tmp_path):
    code, output = chat(tmp_path, Transport(), probe_reporting(DEFAULT_BOSS_CONTEXT_WINDOW))
    assert code == 0 and RELOAD not in output and UNCONFIRMED not in output


def test_an_unloaded_model_keeps_its_own_announcement(tmp_path):
    _, output = chat(tmp_path, Transport(),
                     probe_reporting("unloaded", DEFAULT_BOSS_CONTEXT_WINDOW))
    assert "not loaded yet: the first reply loads it" in output and RELOAD not in output


# --- once, after the first reply --------------------------------------------------------


def test_a_smaller_window_after_the_first_reply_stops_the_session_with_the_fix(tmp_path):
    transport = Transport("first", "second")
    code, output = chat(tmp_path, transport, probe_reporting(4096, 4096), "one", "two")
    assert code == 2
    assert "core> first" in output and "core> second" not in output
    assert transport.calls == 1  # the second turn was never sent
    assert "with a 4096-token window" in output
    assert "set PAC_BOSS_CONTEXT_WINDOW=4096" in output


def test_a_model_the_first_reply_reloaded_at_the_window_passes(tmp_path):
    transport = Transport("first", "second")
    code, output = chat(tmp_path, transport,
                        probe_reporting(4096, DEFAULT_BOSS_CONTEXT_WINDOW), "one", "two")
    assert code == 0 and transport.calls == 2
    assert "core> second" in output and UNCONFIRMED not in output


def test_a_larger_window_is_not_a_mismatch(tmp_path):
    code, _ = chat(tmp_path, Transport(), probe_reporting(32768))
    assert code == 0


def test_a_server_that_reports_no_window_is_said_once_and_the_session_goes_on(tmp_path):
    transport = Transport("first", "second", "third")
    probe = probe_reporting(None)
    code, output = chat(tmp_path, transport, probe, "one", "two", "three")
    assert code == 0 and transport.calls == 3
    assert output.count(UNCONFIRMED) == 1
    # The banner, then one check -- not one per turn.
    assert len(probe.asked) == 2  # type: ignore[attr-defined]


def test_a_probe_that_fails_after_the_reply_is_not_taken_for_a_mismatch(tmp_path):
    code, output = chat(tmp_path, Transport(),
                        probe_reporting(DEFAULT_BOSS_CONTEXT_WINDOW, OSError("refused")))
    assert code == 0 and UNCONFIRMED in output


def test_a_test_transport_with_no_probe_asks_nothing_and_says_nothing(tmp_path):
    out = io.StringIO()
    code = main(["--database", str(tmp_path / "core.db")], transport=Transport(),
                stdin=iter(["hello"]), stdout=out, env={})
    assert code == 0 and UNCONFIRMED not in out.getvalue()


# --- the agent: after the first task, before the second ----------------------------------


def test_the_agent_stops_before_a_second_task_on_a_smaller_window(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    transport = Transport('{"answer": "first done"}', '{"answer": "second done"}')
    out = io.StringIO()
    code = main(["--database", str(tmp_path / "core.db"), "--agent", "--workspace",
                 str(workspace)], transport=transport,
                stdin=iter(["[action_required=false] one", "[action_required=false] two"]),
                stdout=out, env={}, probe=probe_reporting(4096, 4096))
    output = out.getvalue()
    assert code == 2
    assert "core> first done" in output and "second done" not in output
    assert transport.calls == 1
    assert "set PAC_BOSS_CONTEXT_WINDOW=4096" in output


def test_the_agent_checks_after_its_only_task_too(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    out = io.StringIO()
    code = main(["--database", str(tmp_path / "core.db"), "--agent", "--workspace",
                 str(workspace)], transport=Transport('{"answer": "done"}'),
                stdin=iter(["[action_required=false] one"]), stdout=out, env={},
                probe=probe_reporting(4096, 4096))
    assert code == 2 and "core> done" in out.getvalue()
