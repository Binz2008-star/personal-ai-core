# PAC (local) provider for OpenCode

PAC is the model and reasoning provider. OpenCode remains the filesystem, terminal, and MCP tool executor. Plain `pac` is a chat interface and does not provide this integration.

The installed OpenCode version audited for this adapter is **2.0.22**. Its native OpenAI-compatible provider supports the required tool loop: it sends messages and tool definitions to PAC, receives assistant content or native calls, executes calls itself, and sends `role=tool` results back through PAC. PAC adds its identity/profile and checks the complete request budget before calling the configured Boss model through the existing Ollama provider.

## Local endpoints and production paths

Use the existing GPU-backed Ollama service at **`http://127.0.0.1:11434`**. PAC's HTTP adapter listens separately at **`http://127.0.0.1:8765/v1`**. OpenCode connects to PAC on port 8765; PAC connects to Ollama on port 11434. The adapter does not switch to a CPU instance, select a different model, or increase the model's context window.

Run the provider from the repository with the existing virtual environment:

```powershell
& .\.venv\Scripts\python.exe -m personal_ai_core.integrations.opencode
```

Settings continue to use `PAC_BOSS_MODEL`, `PAC_BOSS_CONTEXT_WINDOW`, `PAC_OLLAMA_HOST`, and the existing PAC sampling configuration. Keep `PAC_BOSS_CONTEXT_WINDOW` at **8192** for this integration. The live adapter also checks the actual loaded Ollama context after generation and withholds a response if Ollama did not honor the configured window.

Profile path resolution, in order:

1. Explicit `--profile PATH`.
2. `PAC_PROFILE`.
3. `profile.md` beside an explicit integration `--database PATH`.
4. `profile.md` beside the path named by `PAC_DATABASE`.
5. `~/.personal-ai-core/profile.md`.

PAC also reads `projects.md` beside the selected profile. The integration database resolves from explicit `--database`, then `PAC_OPENCODE_DATABASE`, then `~/.personal-ai-core/opencode.db`. `PAC_DATABASE` informs the profile location; it does not redirect the integration store into PAC's ordinary database.

The integration store is a **separate session mapping store**. It saves stable client-to-PAC session IDs, profile fingerprints, timestamps, and allowed numerical/status metadata. OpenCode owns and persists the full conversation, including native tool history. The adapter does not save a second full transcript in PAC's ordinary conversation store. `core.db` and databases belonging to another application are refused as integration stores.

## Register the native OpenCode provider

Merge the following provider entry into OpenCode's existing configuration. Preserve unrelated providers, MCP servers, permissions, and other settings. This example is the native **v2** schema: `providers`, `package`, and `settings`.

```json
{
  "providers": {
    "pac": {
      "name": "PAC (local)",
      "package": "@opencode/ai/providers/openai-compatible",
      "settings": {
        "baseURL": "http://127.0.0.1:8765/v1",
        "timeout": 300000
      },
      "models": {
        "pac-local": {
          "name": "PAC (local)",
          "modelID": "pac-local",
          "capabilities": {
            "tools": true,
            "input": ["text"],
            "output": ["text"]
          },
          "compatibility": {
            "maxTokensField": "max_tokens",
            "requireFinishReason": true
          },
          "limit": {
            "context": 8192,
            "output": 1024
          }
        }
      }
    }
  }
}
```

The output limit matches the adapter's funded reply reserve. The provider does not advertise image/audio support. Unsupported nontext request content produces an explicit error.

OpenCode 2.0.22 also normalizes legacy `provider` entries using `npm: "@ai-sdk/openai-compatible"` to its bundled native implementation. Do not configure OpenCode to point directly at Ollama: model calls must pass through PAC.

## Selective delivery with execution permissions unchanged

The full active OpenCode configuration was measured at approximately **26,703 tokens** for an 8192-token PAC request. Its `available_skills` section alone accounted for approximately **10,028 tokens**. This request is refused before a model call. Repairing GPU access does not make that request fit the fixed context window.

The current requirement is to keep **all OpenCode execution permission rules unchanged**. Permission-based agent drafts were removed without installation. No user configuration or execution permission rules were changed when these repository artifacts were created.

The native Promise plugin [`tools/opencode_pac_select_tools.mjs`](../tools/opencode_pac_select_tools.mjs) uses OpenCode's `session.http.request` hook to select advertised tool definitions for PAC requests without rewriting permission rules. Its event exposes the actual lowered HTTP `Request`, plus the session, agent, model, and request-kind identifiers. The plugin reads a clone of that request and changes only its `tools` array and audit headers. It scopes its hook to provider `pac`, so unrelated providers remain unaffected. Complete native declarations, including schema projections and `strict` fields, are retained exactly. OpenCode keeps its original tool registry, request definitions, and executor; PAC rejects calls whose names were not advertised to it.

MCP selection has a further constraint: native Code Mode exposes permitted MCP tools through one `execute` definition, while the Code Mode catalog, MCP server guidance, and skill descriptions are already assembled in the request's system instructions. Removing direct schemas does not remove that system text. The installed interface has no exposed per-request Code Mode namespace-selection setting. Preserve the native system text; do not invent abbreviated instructions or strip unrelated sections to make the budget fit.

Measure the unchanged system instructions together with the proposed native schemas, PAC identity, complete transcript, and existing reserves. If that request still exceeds **8192**, stop and report the component breakdown. Do not change execution permissions, raise context, shrink the profile floor, switch model, or call a chat-only fallback to make acceptance pass.

### Native advertised-tool profiles

The selector defaults to `repo`. A plugin option can explicitly select another deterministic profile. If no option is supplied, an existing agent named `pac-repo`, `pac-github`, `pac-context7`, or `pac-memory` selects the corresponding profile. The plugin does not create agents or change their permissions.

| Profile | Advertised definitions retained |
| --- | --- |
| `repo` | Existing native `shell`, `read`, `glob`, `grep` definitions. |
| `github` | Existing `execute` plus direct definitions beginning with `github_`, if any. |
| `context7` | Existing `execute` plus direct definitions beginning with `context7_`, if any. |
| `memory` | Existing `execute` plus direct definitions beginning with `memory_`, if any. |

The installed native Code Mode wrapper still contains its complete permitted runtime catalog. Retaining `execute` does **not** isolate that runtime to one MCP server. Server-group profile names describe direct schema selection, not an execution boundary. PAC must not claim that hidden or unavailable direct tools were used. Explicit work requiring another advertised group needs the appropriate profile, while every actual execution remains subject to unchanged OpenCode governance.

For a reviewable configuration addition, append a native plugin entry to the existing `plugins` array instead of replacing it:

```json
{
  "plugins": [
    {
      "package": "file:///C:/Users/loyal/personal-ai-core/tools/opencode-pac-native",
      "options": {"profile": "repo"}
    }
  ]
}
```

Configured local plugins must be directories in OpenCode 2.0.22. The small `opencode-pac-native` package supplies its `index.js` entry point; a configured `.mjs` file is rejected before activation. The experiment used a temporary configuration overlay, preserving all existing plugin entries, MCP configuration, and execution permissions. It did not install a permanent selector into the user's configuration.

An optional `serverID` selects the direct-definition prefix when a configured MCP server has a different ID. OpenCode preserves letters, digits, `_`, and `-` in these names. A forced named `tool_choice` keeps its entire declaration even outside the profile; a missing declaration is refused. Explicit mentions of GitHub, Context7, or memory in the current user exchange include their native executor when available. Selection also applies to compaction requests, which OpenCode can issue before its first primary turn. Transcripts and generation options remain intact.

The native HTTP hook adds `X-PAC-Request-Kind` and a content-free `X-PAC-Tool-Selection` header. It reports exact incoming/selected names, every omitted name with reason `upstream_selection`, and schema estimates using PAC's script-aware costs. PAC independently checks the selected schema cost against its production estimator. A disagreement is recorded as a sanitized warning with received-only metadata; it never changes the authoritative budget.

OpenCode 2.0.22 has native lazy Code Mode discovery. It keeps namespace inventory and a selection of tool signatures inline, with an approximately 2000-token catalog budget. Other selected-group tools remain available through synchronous `search(...)` **inside `execute`**. PAC preserves the delivered system instructions, tool definitions, call IDs, and result roles; it does not rewrite MCP or silently drop those fields.

The catalog budget is not a hard bound on the entire request: namespace descriptions, pinned entries, reachable MCP instructions, profile, transcript, and results still count. A selected native delivery profile can still overflow 8192. PAC then reports `context_length_exceeded` and makes no model call. Report the component breakdown and stop; no automatic truncation or context increase is performed.

**Status 2026-10-07:** the native-schema selector documented above is **rejected for this branch and is not staged with the integration**. `tools/opencode_pac_select_tools.mjs` and its directory wrapper `tools/opencode-pac-native`, together with `tests/unit/test_opencode_native_selection.py`, stay in the working tree only as the record of the measured experiment and are excluded from the staged set. Neither plugin is registered anywhere (`plugins` is absent from the active configuration), which is why the audit's selection-manifest count is 0 and every `request_kind` value seen is the adapter's own default label. Header duty moved to the supported observer below, and tool-schema reduction is still unsolved, so the measured STOP in the table above still stands.

## Native request-kind observer

`tools/opencode-pac-observer` is a directory plugin (`index.js` plus a `package.json` with `"type": "module"`) that registers through OpenCode's supported `session.hook("http.request", ...)` scoped to `providerID: "pac"`.

- It rebuilds `event.request` from a copy of the lowered request with exactly one header changed: `X-PAC-Request-Kind`.
- The value must be in the whitelist `primary`, `title`, `summary`, `compaction`, `auxiliary`, `generate`, which is PAC's `SUPPORTED_REQUEST_KINDS` (`integrations/opencode/store.py`). OpenCode 2.0.22's own `SessionRequestKind` emits only `primary`, `compaction`, `title`, `generate`, so no native kind can reach the adapter's `invalid_request_kind` rejection (`http.py:199`).
- Request body, advertised `tools`, `system`, permissions, method and URL stay byte-identical, nothing is logged, and the registration returns a disposer.
- Any other kind value, and any non-`pac` provider, leaves the request object untouched, so an unrecognized value is never exposed upstream.

The observer deliberately sends no selection header. The adapter therefore keeps its content-free default `selection_visibility`, the audit's selection-manifest count stays 0, and no advertised-tool reduction happens at delivery time.

`tests/unit/test_opencode_observer.py` imports the real plugin under `node` and checks the accepted kinds, rejected providers, malformed kinds, header preservation, body identity, silent operation and disposal.

## Native compaction hook

`tools/opencode-pac-compaction` registers OpenCode's supported `session.hook("compaction", ...)` scoped to `providerID: "pac"`.

- For `pac` it sets `event.tools` to an empty set and replaces `event.system` with the fixed `COMPACTION_SYSTEM` instruction, which tells the model to summarize without calling tools.
- `event.messages`, `event.model` and `event.options` are untouched and the plugin supplies no summary of its own: OpenCode appends its own checkpoint template after the hook and keeps native compaction intact.
- For every other provider the hook does nothing.

`tests/unit/test_opencode_compaction_hook.py` runs the real plugin under `node` for `pac` and a non-pac provider and asserts transcript identity, model, options and the resulting tool-name set.

## Run-scoped plugin registration

Neither plugin is written into the user's configuration. OpenCode merges its configuration sources, and `OPENCODE_CONFIG_CONTENT` is the runtime-override layer, so both plugins can be registered for a single command:

```powershell
$env:OPENCODE_CONFIG_CONTENT = Get-Content -Raw tools\opencode-pac-plugin-overlay.json
opencode run --standalone --agent pac --model pac/pac-local --format json --title "PAC acceptance" "<prompt>"
```

The acceptance runner accepts the same file as `--config-overlay tools\opencode-pac-plugin-overlay.json` and exports it as `OPENCODE_CONFIG_CONTENT` for that subprocess only. The active configuration contains no `plugins` key, so the overlay only adds these two entries; providers, MCP servers, permissions and skills are left exactly as configured.

## Acceptance runner

`tools/opencode_provider_acceptance.py` is the executable acceptance record for this integration. `--phase json` drives the adapter directly (native tool call, real Git execution, exact tool-result replay, grounded continuation). `--phase opencode` runs the installed OpenCode binary as the sole executor and classifies its JSONL output.

- Scenarios: `repository`, `identity-english`, `identity-arabic`, `temporary-edit`, `github`, `context7`, `memory`, `context-pressure`.
- Evidence is written to a temporary directory outside the repository, and a before/after repository snapshot (head, index, status, per-file digests) proves the run modified nothing.
- Audit records are consumed from their starting byte offset and paired admission to completion by correlation ID; refusal, mismatch and generation-error counts are reported, and a `request_kind` label counts only under `--audit-request-kind-trusted`.
- Reports contain counts, digests and conditions only: no prompt, profile, path, argument or transcript text is persisted.

`tests/unit/test_opencode_acceptance_runner.py` pins the classifier against claimed-but-unexecuted tools, echo strings, failed tools, ungrounded answers, unpaired audit rows, compaction ordering and file-name leakage.

## Protocol behavior

OpenCode's installed native provider sends `stream: true`, function `tools`, and `stream_options.include_usage`. It can also send `store: false`; the adapter explicitly accepts that OpenAI transport option. `store: true` is refused. The option does not change the separate PAC session mapping behavior.

The adapter currently waits for the existing nonstreaming Ollama generation to complete, validates the result, and then emits standard OpenAI SSE chunks. This is **completed-result SSE**, not live Boss-model token streaming. Chunks preserve assistant text and protocol-valid `tool_calls`, end with the applicable finish reason, and terminate with `data: [DONE]`. Provider and validation errors remain ordinary JSON error responses before streaming headers are sent.

Assistant call arguments arrive from OpenCode as JSON object strings and are sent to Ollama as objects. Follow-up `role=tool` messages retain their `tool_call_id`; the adapter supplies the native Ollama `tool_name`. Calls execute only in OpenCode. `tool_choice` and `parallel_tool_calls` constraints are checked explicitly, and a violation is refused instead of replaced with a text answer.

The adapter scans text and structured argument values for secrets. It redacts historical content before generation and withholds newly generated secret-bearing tool arguments rather than changing the command that OpenCode would execute. HTTP logs contain route/status information, not transcript or tool-result text.

Session mapping uses `X-OpenCode-Session-ID`, which native OpenCode supplies. A client without that header can reuse `X-PAC-Session-ID`. The native selector supplies `X-PAC-Request-Kind` to distinguish primary and auxiliary requests.

## Integration audit

The CLI supplies a dedicated JSONL callback, defaulting to `opencode-audit.jsonl` beside the separate integration database. `--audit-log` selects a different destination. This callback never writes to the session store or `core.db`.

An admission event contains the measured budget and selection metadata before generation. Its outcome is `admitted`, `refused_context_budget`, `refused_required_tool_unavailable`, or `refused_protocol`. Admitted requests then get a completion event with the same correlation ID and outcome `generated_text`, `generated_tool_calls`, `provider_error`, or `protocol_error`. Completion records contain only fixed metadata and optional numeric counts. Refused requests have only the admission event.

Both events are immutable append-only records. Neither contains prompt/profile/schema content, arguments, results, filesystem paths, or raw errors. Code Mode paths are logical catalogue identifiers such as `tools.github.get_file_contents`, never local filesystem paths. Suspect identifiers are withheld. Standalone message and catalogue estimates are diagnostic; the complete canonical-array budget remains authoritative.

Audit failure prints a fixed sanitized warning and continues by default. Explicit `--audit-required` test/debug mode refuses before generation if admission cannot be written, or withholds the response if completion cannot be written.

## Measured selection result: STOP

The installed OpenCode acceptance command reached PAC through the native selector. OpenCode attempted compaction before its first primary turn. The unchanged system text remained too large, so PAC refused before Ollama or any tool execution.

| Component | Before selection | After selection |
| --- | ---: | ---: |
| System canonical tokens | 15,742 | 15,742 |
| Non-system canonical increment | 1,370 | 1,370 |
| PAC identity/profile reserve | 1,321 | 1,321 |
| Native tool schemas | 6,934 | 2,318 |
| Generation reserve | 1,024 | 1,024 |
| Overhead / guard | 256 / 56 | 256 / 56 |
| **Authoritative total** | **26,703** | **22,087** |

Code Mode contributes a separate diagnostic estimate of 4,059 tokens within the system text; it is not added again to this table. The automatically supplied skill catalogue accounts for 10,028 canonical transcript tokens and is the largest component. PAC's base identity costs 498 tokens, the profile adds 823, and no projects file was present.

Selected native tools: `glob`, `grep`, `read`, `shell`. Omitted by the native upstream selector: `edit`, `question`, `skill`, `subagent`, `webfetch`, `websearch`, `write`, `execute`. Each omission was audited explicitly. Native schema savings: **4,616** tokens. Removing the `skill` definition did not remove its independently injected catalogue. Execution permissions and all MCP server configuration were preserved.

**Result B: still above 8192.** Acceptance stopped. No OpenCode tools executed and no native tool round-trip PASS is claimed. No system text or selected schemas were truncated; the Boss model, profile floor, context, and core persistence stayed unchanged.

## Real acceptance test

In OpenCode, select **PAC (local)** with an existing agent whose execution permissions remain unchanged. Start a fresh session in this repository and ask:

> Inspect this repository. Run git status and git rev-parse HEAD, then explain the architecture. Do not modify anything.

The equivalent installed CLI invocation from the repository, using the existing agent and unchanged execution permissions, is:

```powershell
opencode run --standalone --model pac/pac-local --format json --title "PAC acceptance" "Inspect this repository. Run git status and git rev-parse HEAD, then explain the architecture. Do not modify anything."
```

`--standalone` uses a private OpenCode server with this process's configuration instead of an existing background service. `--title` avoids an automatic title-generation request during this test.

PASS requires visible OpenCode executions of both Git commands and repository reads, actual tool results returned through PAC, an answer grounded in those results, stable session mapping, active profile behavior, and no repository modifications. A textual answer without tool execution is **FAIL**. A reduced JSON-provider proof by itself is also insufficient to prove OpenCode acceptance.

The full active configuration currently fails the 8192 budget, so that configuration has not passed this acceptance test. The native selector must first fit the unchanged system instructions plus retained schemas. A budget refusal, missing server, permission denial, or unexecuted call is an unmet acceptance condition. If unchanged system instructions still overflow after native schema selection, report their measured breakdown and stop acceptance work. No permission-based preset or chat-only fallback is installed.

The runnable form of this checklist is `tools/opencode_provider_acceptance.py --phase opencode --scenario repository` (optionally with `--config-overlay tools\opencode-pac-plugin-overlay.json` and `--audit` pointed at the active JSONL). It enforces every condition above — visible tool execution, grounded answer, stable session mapping, unchanged repository — and exits non-zero on any unmet one. It requires a live PAC adapter and Ollama endpoint, so it is an online step and is not part of the offline validation sweep.

## Audited upstream interface

- [OpenCode 2.0.22 native provider route](https://github.com/anomalyco/opencode/blob/v2.0.22/packages/ai/src/protocols/openai-compatible-chat.ts).
- [Native v2 provider configuration](https://github.com/anomalyco/opencode/blob/v2.0.22/packages/schema/src/config/provider.ts) and [agent configuration](https://github.com/anomalyco/opencode/blob/v2.0.22/packages/schema/src/config/agent.ts).
- [Permission-filtered tool snapshots](https://github.com/anomalyco/opencode/blob/v2.0.22/packages/core/src/tool.ts), [lazy Code Mode catalog](https://github.com/anomalyco/opencode/blob/v2.0.22/packages/core/src/codemode/catalog.ts), and [MCP permission naming/execution](https://github.com/anomalyco/opencode/blob/v2.0.22/packages/core/src/tool/mcp.ts).
- [Ollama 0.35.0 native message and call linkage](https://github.com/ollama/ollama/blob/v0.35.0/api/types.go).
