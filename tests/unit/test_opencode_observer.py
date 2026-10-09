"""The native observer tags request kinds while leaving all behavior intact."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "tools" / "opencode-pac-observer" / "index.js"


def run_observer(*, kind="primary", provider="pac"):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to exercise the native OpenCode observer")
    script = "import plugin from " + json.dumps(PLUGIN.as_uri()) + ";\n" + """
    let callback, disposed = false;
    const registered = [];
    const cleanup = await plugin.setup({session: {hook: async (name, handler, scope) => {
      registered.push({name, scope});
      callback = handler;
      return {dispose: async () => { disposed = true; }};
    }}});
    const body = JSON.stringify({
      model: 'pac-local', store: false, stream: true, max_tokens: 1024,
      tool_choice: 'auto', parallel_tool_calls: false,
      tools: [{type: 'function', function: {name: 'read', description: 'unchanged schema',
        parameters: {type: 'object', properties: {path: {type: 'string'}}}}}],
      messages: [{role: 'system', content: 'unchanged system'},
        {role: 'user', content: 'unchanged user'},
        {role: 'assistant', content: null, tool_calls: [{id: 'call_original', type: 'function',
          function: {name: 'read', arguments: '{"path":"README.md"}'}}]},
        {role: 'tool', tool_call_id: 'call_original', content: 'unchanged output'}],
    });
    const initial = new Request('http://127.0.0.1:8765/v1/chat/completions', {
      method: 'POST', headers: {'Content-Type': 'application/json',
        'X-Private-Test-Header': 'unchanged-private-header', 'X-PAC-Request-Kind': 'primary'},
      body,
    });
    const event = {model: {providerID: PROVIDER_VALUE}, kind: KIND_VALUE, request: initial,
      tools: Object.freeze({read: 'untouched'}), system: Object.freeze(['untouched']),
      permissions: Object.freeze({read: 'allow', '*': 'deny'})};
    const tools = event.tools, system = event.system, permissions = event.permissions;
    const beforeHeaders = [...initial.headers.entries()];
    const originalConsole = console.log;
    let logged = false;
    console.log = () => { logged = true; };
    await callback(event);
    console.log = originalConsole;
    const replacement = event.request !== initial;
    const result = {
      registered, replacement, kind: event.request.headers.get('X-PAC-Request-Kind'),
      beforeHeaders, afterHeaders: [...event.request.headers.entries()],
      bodyUnchanged: await event.request.text() === body,
      sameTools: event.tools === tools, sameSystem: event.system === system,
      samePermissions: event.permissions === permissions,
      method: event.request.method, url: event.request.url, logged,
    };
    await cleanup();
    result.disposed = disposed;
    process.stdout.write(JSON.stringify(result));
    """
    script = script.replace("PROVIDER_VALUE", json.dumps(provider)).replace("KIND_VALUE", json.dumps(kind))
    completed = subprocess.run([node, "--input-type=module", "-e", script],
                               capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(completed.stdout)


@pytest.mark.parametrize("kind", ["primary", "title", "summary", "compaction", "auxiliary", "generate"])
def test_pac_native_request_kind_changes_only_the_one_header(kind):
    result = run_observer(kind=kind)
    assert result["registered"] == [{"name": "http.request", "scope": {"providerID": "pac"}}]
    assert result["kind"] == kind
    assert result["replacement"]
    before = {name: value for name, value in result["beforeHeaders"] if name != "x-pac-request-kind"}
    after = {name: value for name, value in result["afterHeaders"] if name != "x-pac-request-kind"}
    assert before == after
    assert result["bodyUnchanged"]
    assert result["sameTools"] and result["sameSystem"] and result["samePermissions"]
    assert result["method"] == "POST"
    assert result["url"] == "http://127.0.0.1:8765/v1/chat/completions"
    assert result["disposed"]
    assert not result["logged"]


@pytest.mark.parametrize("provider", ["other", "openai", "ollama", "PAC"])
def test_non_pac_provider_is_not_changed_even_if_callback_is_invoked_manually(provider):
    result = run_observer(kind="compaction", provider=provider)
    assert not result["replacement"]
    assert result["beforeHeaders"] == result["afterHeaders"]
    assert result["bodyUnchanged"] and not result["logged"]


@pytest.mark.parametrize("kind", ["unknown", "PRIVATE_KIND_VALUE\nPRIVATE_HEADER", None, {"private": "value"}])
def test_unknown_or_malformed_kind_is_not_guessed_or_exposed(kind):
    result = run_observer(kind=kind)
    assert not result["replacement"]
    assert result["kind"] == "primary"
    assert result["beforeHeaders"] == result["afterHeaders"]
    assert result["bodyUnchanged"] and not result["logged"]
