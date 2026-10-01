"""The llama.cpp adapter and its grammar (experiment, evaluation runs only)."""
from __future__ import annotations

import re

import pytest

from personal_ai_core.conversation.language_guard import script_of
from personal_ai_core.core.domain import Message, Role
from personal_ai_core.core.errors import ProviderError
from personal_ai_core.runtime.llamacpp import GRAMMARS, NO_FOREIGN_SCRIPT, LlamaCppProvider


class Recorder:
    def __init__(self, response=None):
        self.calls: list[tuple[str, dict]] = []
        self.response = response or {
            "model": "boss.gguf",
            "choices": [{"message": {"content": "مرحبا"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        }

    def __call__(self, url, payload, timeout):
        self.calls.append((url, payload))
        return self.response


def msg(text):
    return Message(session_id="s", role=Role.USER, content=text, language="ar")


def test_a_reply_is_read_from_the_openai_shaped_response():
    transport = Recorder()
    out = LlamaCppProvider("http://h:8080/", transport=transport).generate(
        model="boss", messages=[msg("سؤال")]
    )
    assert out.text == "مرحبا" and out.prompt_tokens == 12 and out.completion_tokens == 3
    assert out.finish_reason == "stop"
    assert transport.calls[0][0] == "http://h:8080/v1/chat/completions"


def test_core_options_are_translated_and_unknown_ones_dropped():
    transport = Recorder()
    LlamaCppProvider("http://h", transport=transport).generate(
        model="boss", messages=[msg("x")],
        options={"num_predict": 256, "temperature": 0.7, "top_k": 20, "num_ctx": 8192},
    )
    payload = transport.calls[0][1]
    assert payload["max_tokens"] == 256 and "num_predict" not in payload
    assert payload["temperature"] == 0.7 and payload["top_k"] == 20
    assert "num_ctx" not in payload  # a server setting, not a request field


def test_the_grammar_is_sent_only_when_one_is_set():
    plain, constrained = Recorder(), Recorder()
    LlamaCppProvider("http://h", transport=plain).generate(model="b", messages=[msg("x")])
    LlamaCppProvider("http://h", transport=constrained, grammar=NO_FOREIGN_SCRIPT).generate(
        model="b", messages=[msg("x")]
    )
    assert "grammar" not in plain.calls[0][1]
    assert constrained.calls[0][1]["grammar"] == NO_FOREIGN_SCRIPT


def test_a_malformed_response_is_a_provider_error():
    with pytest.raises(ProviderError):
        LlamaCppProvider("http://h", transport=Recorder({"error": "x"})).generate(
            model="b", messages=[msg("x")]
        )


def _forbidden(grammar: str) -> list[tuple[int, int]]:
    body = re.search(r"\[\^(.*)\]\*", grammar).group(1)  # type: ignore[union-attr]
    return [(int(a, 16), int(b, 16)) for a, b in re.findall(r"\\u(\w{4})-\\u(\w{4})", body)]


def _blocked(ch: str) -> bool:
    return any(lo <= ord(ch) <= hi for lo, hi in _forbidden(NO_FOREIGN_SCRIPT))


@pytest.mark.parametrize("ch", ["中", "。", "，", "あ", "カ", "한", "я", "Ж"])
def test_the_grammar_forbids_every_script_the_rig_leaked(ch):
    assert _blocked(ch)


@pytest.mark.parametrize("ch", ["ب", "،", "؟", "a", "Z", "é", "1", ".", "\n", "[", "]"])
def test_the_grammar_allows_arabic_latin_and_punctuation(ch):
    assert not _blocked(ch)


def test_the_grammar_agrees_with_the_language_guard_on_foreign_letters():
    """Every letter the guard counts as han, kana, hangul or cyrillic is one the
    grammar forbids, so the two cannot disagree about what a leak is."""
    for code in range(0x0400, 0xFFFF):
        ch = chr(code)
        if script_of(ch) in {"han", "kana", "hangul", "cyrillic"}:
            assert _blocked(ch), hex(code)


def test_grammars_are_named():
    assert GRAMMARS["none"] is None and GRAMMARS["no-foreign-script"] == NO_FOREIGN_SCRIPT
