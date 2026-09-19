"""OllamaProvider tests — fake transport, no live model, GPU or network."""
import pytest

from personal_ai_core.core.domain import Message, Role
from personal_ai_core.core.errors import ProviderError
from personal_ai_core.runtime.ollama.provider import OllamaProvider


def _messages():
    return [Message(session_id="s", role=Role.USER, content="hello")]


def test_generate_maps_an_ollama_response_to_the_core_type():
    captured = {}

    def transport(url, payload, timeout):
        captured["url"] = url
        captured["payload"] = payload
        return {
            "model": "huihui_ai/qwen2.5-abliterate:7b",
            "message": {"role": "assistant", "content": "hi there"},
            "prompt_eval_count": 11,
            "eval_count": 3,
            "done_reason": "stop",
        }

    provider = OllamaProvider("http://127.0.0.1:11434", transport=transport)
    response = provider.generate(
        model="huihui_ai/qwen2.5-abliterate:7b", messages=_messages()
    )

    assert response.text == "hi there"
    assert response.model == "huihui_ai/qwen2.5-abliterate:7b"
    assert response.prompt_tokens == 11
    assert response.completion_tokens == 3
    assert response.finish_reason == "stop"
    assert captured["url"] == "http://127.0.0.1:11434/api/chat"
    assert captured["payload"]["stream"] is False
    assert captured["payload"]["messages"] == [{"role": "user", "content": "hello"}]


def test_provider_name_is_stable():
    assert OllamaProvider("http://x", transport=lambda u, p, t: {}).name == "ollama"


def test_host_trailing_slash_is_normalised():
    captured = {}

    def transport(url, payload, timeout):
        captured["url"] = url
        return {"message": {"content": "x"}}

    OllamaProvider("http://h:11434/", transport=transport).generate(
        model="m", messages=_messages()
    )
    assert captured["url"] == "http://h:11434/api/chat"


def test_options_are_forwarded_only_when_given():
    seen = []
    provider = OllamaProvider(
        "http://x", transport=lambda u, p, t: seen.append(p) or {"message": {"content": "x"}}
    )
    provider.generate(model="m", messages=_messages())
    assert "options" not in seen[0]
    provider.generate(model="m", messages=_messages(), options={"temperature": 0.2})
    assert seen[1]["options"] == {"temperature": 0.2}


def test_malformed_response_raises_provider_error():
    provider = OllamaProvider("http://x", transport=lambda u, p, t: {"unexpected": True})
    with pytest.raises(ProviderError, match="missing message.content"):
        provider.generate(model="m", messages=_messages())


def test_empty_messages_rejected():
    provider = OllamaProvider("http://x", transport=lambda u, p, t: {})
    with pytest.raises(ProviderError, match="at least one message"):
        provider.generate(model="m", messages=[])


def test_transport_failure_surfaces_as_provider_error():
    def failing(url, payload, timeout):
        raise ProviderError("connection refused")

    provider = OllamaProvider("http://x", transport=failing)
    with pytest.raises(ProviderError):
        provider.generate(model="m", messages=_messages())
