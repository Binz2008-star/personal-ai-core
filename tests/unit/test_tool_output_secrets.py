"""N7: a secret-shaped value in a tool's output is withheld before the model reads it.

The verifier already failed a step whose output looked secret -- but only to
charge the budget: `_describe` handed the output to the model whole. A key in a
file a command printed, or a token on a fetched page, went into the model's
context, from where the model could write it into a file (`write_file` runs
without asking) or put it in a URL. Now the model is shown the output with each
value replaced by the same marker the chat path and the evidence redactor use,
and the step still fails verification as before. A check that cannot run
withholds the whole output (ADR-018 §3.8).

Every secret here is obviously fake.
"""
from __future__ import annotations

import pytest

from personal_ai_core.agent.executor import ToolExecutor
from personal_ai_core.agent.loop import AgentLoop, fence
from personal_ai_core.agent.policy import RiskPolicy
from personal_ai_core.agent.recovery import Checkpoints
from personal_ai_core.agent.sandbox import Workspace
from personal_ai_core.agent.tools import default_tools
from personal_ai_core.agent.verifier import WITHHELD_MARKER, SecretShapeRedactor
from personal_ai_core.agent.web import FetchUrl
from personal_ai_core.core.domain import ModelResponse
from personal_ai_core.core.redaction import RedactionError

GITHUB = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
AWS = "AKIA" + "ABCDEFGHIJKLMNOP"


class Script:
    name = "script"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def generate(self, *, model, messages, options=None):
        self.calls.append(list(messages))
        return ModelResponse(text=self.replies.pop(0), model=model)


def loop(tmp_path, script, *, extra_tools=(), files=None):
    root = tmp_path / "ws"
    root.mkdir(exist_ok=True)
    for name, text in (files or {}).items():
        (root / name).write_text(text, encoding="utf-8")
    workspace = Workspace(root)
    checkpoints = Checkpoints(workspace)
    executor = ToolExecutor([*default_tools(workspace, checkpoints), *extra_tools],
                            RiskPolicy(), confirm=lambda request, spec: True)
    return AgentLoop(provider=script, model="boss", executor=executor,
                     checkpoints=checkpoints)


def shown_after(script) -> str:
    """What the model was shown of the first tool's result."""
    return script.calls[1][-1].content


def test_a_token_in_a_file_the_agent_reads_never_reaches_the_model(tmp_path):
    script = Script('{"tool": "read_file", "arguments": {"path": "deploy.md"}}',
                    '{"answer": "done"}')
    agent = loop(tmp_path, script,
                 files={"deploy.md": f"Deploy with the CI token\nGITHUB_TOKEN={GITHUB}\nthen tag.\n"})
    outcome = agent.run("summarise deploy.md", session_id="s1")
    shown = shown_after(script)
    assert GITHUB not in shown
    assert WITHHELD_MARKER in shown
    assert "Deploy with the CI token" in shown and "then tag." in shown
    # The step still fails its check, as before: the change is what the model sees.
    assert not outcome.steps[0].verified
    assert any("no secret in output" in check for check in outcome.steps[0].failed_checks)


def test_a_key_a_command_prints_is_withheld_too(tmp_path):
    script = Script('{"tool": "run_command", "arguments": {"command": "cat settings.ini"}}',
                    '{"answer": "done"}')
    agent = loop(tmp_path, script, files={"settings.ini": f"[aws]\nkey = {AWS}\nregion = me\n"})
    agent.run("what region?", session_id="s1")
    shown = shown_after(script)
    assert AWS not in shown and WITHHELD_MARKER in shown and "region = me" in shown


def test_a_token_on_a_fetched_page_is_withheld_too(tmp_path):
    page = f"<html><body><p>Example key: {GITHUB}</p><p>Docs end.</p></body></html>"

    def fetch(url, timeout):
        return url, "text/html", page.encode("utf-8")

    script = Script('{"tool": "fetch_url", "arguments": {"url": "https://example.com/"}}',
                    '{"answer": "done"}')
    agent = loop(tmp_path, script, extra_tools=[FetchUrl(fetch)])
    agent.run("read the page", session_id="s1")
    shown = shown_after(script)
    assert GITHUB not in shown and "Docs end." in shown


def test_a_clean_output_is_shown_exactly_as_before(tmp_path):
    script = Script('{"tool": "read_file", "arguments": {"path": "notes.md"}}',
                    '{"answer": "done"}')
    agent = loop(tmp_path, script, files={"notes.md": "the answer is 42\n"})
    agent.run("read notes", session_id="s1")
    assert shown_after(script) == fence("read_file -> ok", "the answer is 42\n")


def test_when_the_check_cannot_run_the_whole_output_is_withheld(tmp_path, monkeypatch):
    def broken(self, text):
        raise RedactionError(RedactionError.INTERNAL)

    monkeypatch.setattr(SecretShapeRedactor, "redact", broken)
    script = Script('{"tool": "read_file", "arguments": {"path": "notes.md"}}',
                    '{"answer": "done"}')
    agent = loop(tmp_path, script, files={"notes.md": "the answer is 42\n"})
    agent.run("read notes", session_id="s1")
    shown = shown_after(script)
    assert "the answer is 42" not in shown
    assert "could not be checked for secrets" in shown


def test_the_tools_own_result_is_unchanged(tmp_path):
    """Only the model's view changes: the record the executor keeps is the
    tool's result as it was, so nothing about what ran is lost or rewritten."""
    script = Script('{"tool": "read_file", "arguments": {"path": "deploy.md"}}',
                    '{"answer": "done"}')
    agent = loop(tmp_path, script, files={"deploy.md": f"token {GITHUB}\n"})
    outcome = agent.run("read", session_id="s1")
    result = outcome.steps[0].record.result
    assert result is not None and GITHUB in result.output


def test_a_token_in_a_tools_error_is_withheld_too(tmp_path):
    """The executor turns a tool's exception into its error text, and that
    text is shown to the model in the step's status line."""
    from personal_ai_core.core.agent import RiskLevel, ToolSpec

    class Leaky:
        spec = ToolSpec(name="leaky", description="fails loudly", risk_level=RiskLevel.LOW,
                        input_schema={"type": "object", "properties": {},
                                      "additionalProperties": False},
                        timeout_seconds=1, idempotent=True)

        def run(self, arguments):
            raise RuntimeError(f"auth failed for token {GITHUB}")

    script = Script('{"tool": "leaky", "arguments": {}}', '{"answer": "done"}')
    agent = loop(tmp_path, script, extra_tools=[Leaky()])
    agent.run("try it", session_id="s1")
    shown = shown_after(script)
    assert GITHUB not in shown and "auth failed" in shown


@pytest.mark.parametrize("text", ["", "plain text, nothing secret"])
def test_text_with_nothing_to_withhold_is_returned_as_is(text):
    from personal_ai_core.agent.loop import _withheld

    assert _withheld(text) == text
