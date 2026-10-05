"""The environment's settings are checked when they are read.

Gap analysis P1-4 and P1-7. `PAC_REQUEST_TIMEOUT_SECONDS=abc` was a
ValueError traceback from deep in startup, and `0` or `-5` seconds was
accepted and meant whatever the HTTP library made of it. A setting outside
its range is now refused with a sentence naming the variable, its range and
what was given; `pac` prints it and exits 2, as it does for a bad flag.
"""
from __future__ import annotations

import io

import pytest

from personal_ai_core.app.cli import main
from personal_ai_core.core.config import (
    CONTEXT_WINDOW_RANGE,
    REQUEST_TIMEOUT_RANGE,
    Settings,
)
from personal_ai_core.core.errors import ConfigError


@pytest.mark.parametrize(("name", "raw"), [
    ("PAC_REQUEST_TIMEOUT_SECONDS", "abc"),
    ("PAC_REQUEST_TIMEOUT_SECONDS", "0"),
    ("PAC_REQUEST_TIMEOUT_SECONDS", "-5"),
    ("PAC_REQUEST_TIMEOUT_SECONDS", "1.5"),
    ("PAC_REQUEST_TIMEOUT_SECONDS", "3601"),
    ("PAC_REQUEST_TIMEOUT_SECONDS", ""),
    ("PAC_BOSS_CONTEXT_WINDOW", "8k"),
    ("PAC_BOSS_CONTEXT_WINDOW", "100"),
    ("PAC_BOSS_CONTEXT_WINDOW", "99999999"),
    ("PAC_OLLAMA_HOST", "localhost:11434"),
    ("PAC_OLLAMA_HOST", "ftp://host"),
    ("PAC_OLLAMA_HOST", "http://"),
])
def test_a_setting_out_of_range_is_refused_by_name(name, raw):
    with pytest.raises(ConfigError) as caught:
        Settings.from_env({name: raw})
    message = str(caught.value)
    assert message.startswith(name) and repr(raw) in message


@pytest.mark.parametrize(("name", "raw", "field", "value"), [
    ("PAC_REQUEST_TIMEOUT_SECONDS", "1", "request_timeout_seconds", 1),
    ("PAC_REQUEST_TIMEOUT_SECONDS", " 300 ", "request_timeout_seconds", 300),
    ("PAC_REQUEST_TIMEOUT_SECONDS", "3600", "request_timeout_seconds", 3600),
    ("PAC_BOSS_CONTEXT_WINDOW", "1024", "boss_context_window", 1024),
    ("PAC_BOSS_CONTEXT_WINDOW", "32768", "boss_context_window", 32768),
    ("PAC_OLLAMA_HOST", "http://192.168.1.20:11434", "ollama_host", "http://192.168.1.20:11434"),
    ("PAC_OLLAMA_HOST", "https://ollama.example", "ollama_host", "https://ollama.example"),
])
def test_a_setting_in_range_is_read_as_given(name, raw, field, value):
    assert getattr(Settings.from_env({name: raw}), field) == value


def test_the_ranges_hold_the_defaults():
    defaults = Settings.from_env({})
    assert CONTEXT_WINDOW_RANGE[0] <= defaults.boss_context_window <= CONTEXT_WINDOW_RANGE[1]
    assert REQUEST_TIMEOUT_RANGE[0] <= defaults.request_timeout_seconds <= REQUEST_TIMEOUT_RANGE[1]


def test_pac_says_what_is_wrong_and_exits_2_without_starting(tmp_path):
    out = io.StringIO()
    called: list[str] = []

    def transport(url, payload, timeout):
        called.append(url)
        return {"model": payload["model"], "message": {"content": "ok"}}

    code = main(["--database", str(tmp_path / "core.db")], transport=transport,
                stdin=iter(["hello"]), stdout=out,
                env={"PAC_REQUEST_TIMEOUT_SECONDS": "abc"})
    output = out.getvalue()
    assert code == 2
    assert output.startswith("pac: PAC_REQUEST_TIMEOUT_SECONDS must be a whole number")
    assert "Traceback" not in output and "session:" not in output
    assert called == [] and not (tmp_path / "core.db").exists()
