"""A malformed reply from the model server is a sentence and a recorded failure.

The RC bug hunt found replies that escaped the provider as something other
than a ProviderError: a body that is not UTF-8 (UnicodeDecodeError), a body
cut short (http.client.IncompleteRead), an answer that is not HTTP at all
(http.client.BadStatusLine), valid JSON that is not an object (`[1, 2]`,
`null`, `"hi"`), and a `message.content` that is not text (`null`, `5`). Each
ended in a traceback, and the event trail stopped at GENERATION_REQUESTED: no
GENERATION_FAILED. With the language guard off, a null content reached the
store and the turn was blamed on the database (NOT NULL constraint failed).

Each one is now `ProviderError(kind="invalid_response")` -- or, for an answer
that is not HTTP, the kind a refused connection gets -- so the existing path
records GENERATION_FAILED and says a sentence, exit 1.
"""
from __future__ import annotations

import http.client
import io
import json
import sqlite3
import urllib.request
from typing import Any, Mapping

import pytest

from personal_ai_core.app.cli import main
from personal_ai_core.core.domain import Message, Role
from personal_ai_core.core.errors import ProviderError
from personal_ai_core.runtime.llamacpp import provider as llamacpp
from personal_ai_core.runtime.ollama import provider as ollama


class _Body(io.BytesIO):
    """What `urlopen` returns: a readable body that is its own context manager."""

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


class _CutBody(_Body):
    def read(self, *args):  # type: ignore[override]
        raise http.client.IncompleteRead(b'{"message": {"con', 40)


def _answering(body: bytes):
    def urlopen(request, timeout=None):
        return _Body(body)
    return urlopen


def _truncated(request, timeout=None):
    return _CutBody()


def _not_http(request, timeout=None):
    raise http.client.BadStatusLine("SSH-2.0-OpenSSH_9.6")


def _json(value: Any):
    def transport(url: str, payload: Mapping[str, Any], timeout: int):
        return value
    return transport


def _reply(content: Any):
    return _json({"model": "m", "message": {"role": "assistant", "content": content}})


def _user(text: str) -> Message:
    return Message(session_id="s1", role=Role.USER, content=text)


# The real transport, through urlopen.
BODIES = {
    "not-utf8": _answering(b'{"message": {"content": "\xff\xfe"}}'),
    "truncated": _truncated,
}
# Valid JSON of the wrong shape, through a fake transport.
REPLIES = {
    "list": _json([1, 2]),
    "null": _json(None),
    "string": _json("hi"),
    "content-null": _reply(None),
    "content-int": _reply(5),
}


def _pac(tmp_path, transport, *, guard: bool = True, agent: bool = False):
    argv = ["--database", str(tmp_path / "core.db")]
    line = "hello"
    if agent:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        argv += ["--agent", "--workspace", str(workspace)]
        line = "[action_required=false] hello"
    out = io.StringIO()
    code = main(argv, transport=transport, stdin=iter([line]), stdout=out,
                env={} if guard else {"PAC_LANGUAGE_GUARD": "0"})
    return code, out.getvalue()


def _rows(tmp_path, sql):
    connection = sqlite3.connect(tmp_path / "core.db")
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def _failed(tmp_path):
    return [json.loads(p) for (p,) in _rows(
        tmp_path, "select payload from events where type = 'generation.failed' order by seq")]


def _assert_a_recorded_invalid_response(tmp_path, code, output):
    assert code == 1
    assert "Traceback" not in output
    assert "could not be reached" in output  # the sentence for this kind of failure
    assert "NOT NULL" not in output and "pac: " not in output  # not blamed on the store
    [failed] = _failed(tmp_path)
    assert failed["error_type"] == "ProviderError"
    assert failed["error_kind"] == "invalid_response"
    # Nothing from the model was stored as a reply.
    assert _rows(tmp_path, "select count(*) from messages where role = 'assistant'") == [(0,)]


# --- through pac ---------------------------------------------------------------------


@pytest.mark.parametrize("guard", [True, False], ids=["guard-on", "guard-off"])
@pytest.mark.parametrize("urlopen", BODIES.values(), ids=BODIES.keys())
def test_an_unreadable_body_is_a_sentence_and_a_recorded_failure(
        tmp_path, monkeypatch, urlopen, guard):
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    code, output = _pac(tmp_path, ollama.http_transport, guard=guard)
    _assert_a_recorded_invalid_response(tmp_path, code, output)


@pytest.mark.parametrize("guard", [True, False], ids=["guard-on", "guard-off"])
@pytest.mark.parametrize("transport", REPLIES.values(), ids=REPLIES.keys())
def test_a_reply_of_the_wrong_shape_is_a_sentence_and_a_recorded_failure(
        tmp_path, transport, guard):
    code, output = _pac(tmp_path, transport, guard=guard)
    _assert_a_recorded_invalid_response(tmp_path, code, output)


@pytest.mark.parametrize("transport", REPLIES.values(), ids=REPLIES.keys())
def test_the_agent_says_a_sentence_for_a_reply_of_the_wrong_shape(tmp_path, transport):
    code, output = _pac(tmp_path, transport, agent=True)
    assert code == 1
    assert "Traceback" not in output and "could not be reached" in output


def test_an_answer_that_is_not_http_is_a_sentence_from_pac(tmp_path, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", _not_http)
    code, output = _pac(tmp_path, ollama.http_transport)
    assert code == 1 and "Traceback" not in output
    assert "nothing answered at" in output
    [failed] = _failed(tmp_path)
    assert failed["error_kind"] == "unreachable"


# --- the transports and providers ---------------------------------------------------


@pytest.mark.parametrize("transport", [ollama.http_transport, llamacpp.http_transport],
                         ids=["ollama", "llamacpp"])
def test_an_answer_that_is_not_http_is_classified_as_nothing_answering(monkeypatch, transport):
    """BadStatusLine is an HTTPException, not an OSError: it escaped before.
    Nothing usable answered, as with a refused connection, so it gets that
    kind -- and its one retry."""
    monkeypatch.setattr(urllib.request, "urlopen", _not_http)
    with pytest.raises(ProviderError) as caught:
        transport("http://x.invalid/api/chat", {}, 5)
    assert (caught.value.kind, caught.value.status) == ("unreachable", None)


@pytest.mark.parametrize("transport", [ollama.http_transport, llamacpp.http_transport],
                         ids=["ollama", "llamacpp"])
@pytest.mark.parametrize("urlopen", BODIES.values(), ids=BODIES.keys())
def test_an_unreadable_body_is_an_invalid_response(monkeypatch, transport, urlopen):
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(ProviderError) as caught:
        transport("http://x.invalid/api/chat", {}, 5)
    assert caught.value.kind == "invalid_response"


def _llama_reply(content: Any):
    return _json({"model": "m", "choices": [{"message": {"content": content}}]})


LLAMA_REPLIES = {
    "list": _json([1, 2]),
    "null": _json(None),
    "string": _json("hi"),
    "content-null": _llama_reply(None),
    "content-int": _llama_reply(5),
}


@pytest.mark.parametrize("transport", LLAMA_REPLIES.values(), ids=LLAMA_REPLIES.keys())
def test_llamacpp_refuses_a_reply_of_the_wrong_shape(transport):
    provider = llamacpp.LlamaCppProvider("http://x.invalid", transport=transport,
                                         sleep=lambda s: None)
    with pytest.raises(ProviderError) as caught:
        provider.generate(model="m", messages=[_user("hi")])
    assert caught.value.kind == "invalid_response"


@pytest.mark.parametrize("content", [None, 5], ids=["content-null", "content-int"])
def test_ollama_tool_calling_refuses_content_that_is_not_text(content):
    provider = ollama.OllamaProvider("http://x.invalid", transport=_reply(content),
                                     sleep=lambda s: None)
    with pytest.raises(ProviderError) as caught:
        provider.generate_with_tools(model="m", messages=[_user("hi")], tools=())
    assert caught.value.kind == "invalid_response"


def test_a_malformed_reply_is_not_retried():
    calls = []

    def transport(url: str, payload: Mapping[str, Any], timeout: int) -> Any:
        calls.append(url)
        return [1, 2]

    provider = ollama.OllamaProvider("http://x.invalid", transport=transport,
                                     sleep=lambda s: None)
    with pytest.raises(ProviderError):
        provider.generate(model="m", messages=[_user("hi")])
    assert len(calls) == 1
