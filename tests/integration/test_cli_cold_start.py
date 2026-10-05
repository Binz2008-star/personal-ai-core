"""pac says the first reply will load the model, when it will.

Gap analysis P1-6. After Ollama unloads the model, the first reply loads it
again -- a minute or more on the owner's machine -- and pac printed its
banner and then nothing, which looks exactly like a hang. Ollama reports what
it has loaded (`/api/ps`); when the Boss is not there, the banner says the
wait is coming.
"""
from __future__ import annotations

import io
from typing import Any, Mapping

from personal_ai_core.app.cli import main
from personal_ai_core.core.config import Settings

BOSS = Settings().boss_model
NOTICE = "not loaded yet: the first reply loads it"


def _transport(url: str, payload: Mapping[str, Any], timeout: int):
    return {"model": payload["model"], "message": {"content": "ok"}}


def _probe_reporting(*models: str, calls: list[str] | None = None):
    def probe(url: str, body: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        if calls is not None:
            calls.append(url)
        if url.endswith("/api/ps"):
            return {"models": [{"name": m, "model": m, "size": 10, "size_vram": 10,
                                "context_length": 8192} for m in models]}
        return {"modelfile": "FROM /models/blobs/sha256-" + "a" * 64}
    return probe


def _run(tmp_path, probe, argv=()):
    out = io.StringIO()
    code = main(["--database", str(tmp_path / "core.db"), *argv], transport=_transport,
                stdin=iter(["hello"]), stdout=out, env={}, probe=probe)
    return code, out.getvalue()


def test_the_wait_is_announced_when_the_model_is_not_loaded(tmp_path):
    calls: list[str] = []
    code, output = _run(tmp_path, _probe_reporting("another-model:7b", calls=calls))
    assert code == 0
    assert NOTICE in output
    # Before the first reply, right under the model it is about.
    lines = output.splitlines()
    assert lines[lines.index(f"model:   {BOSS}") + 1].strip().startswith("not loaded yet")
    assert output.index(NOTICE) < output.index("core> ok")
    assert calls[0] == f"{Settings().ollama_host.rstrip('/')}/api/ps"


def test_nothing_is_said_when_the_model_is_loaded(tmp_path):
    code, output = _run(tmp_path, _probe_reporting(BOSS))
    assert code == 0 and NOTICE not in output


def test_a_server_that_cannot_be_asked_is_not_guessed_about(tmp_path):
    def refusing(url: str, body: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        raise OSError("connection refused")

    code, output = _run(tmp_path, refusing)
    assert code == 0 and NOTICE not in output and "core> ok" in output


def test_the_agent_banner_says_it_too(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    out = io.StringIO()
    main(["--database", str(tmp_path / "core.db"), "--agent", "--workspace", str(workspace)],
         transport=_transport, stdin=iter([]), stdout=out, env={},
         probe=_probe_reporting())
    assert NOTICE in out.getvalue()


def test_a_test_transport_without_a_probe_asks_no_server(tmp_path):
    # Every other CLI test: a transport is injected and no probe, so nothing
    # reaches the network and the banner is as it was.
    out = io.StringIO()
    code = main(["--database", str(tmp_path / "core.db")], transport=_transport,
                stdin=iter(["hello"]), stdout=out, env={})
    assert code == 0 and NOTICE not in out.getvalue()
