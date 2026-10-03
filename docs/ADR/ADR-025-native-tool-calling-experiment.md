# ADR-025 — An experiment: the Boss model's native tool-call interface, against the text protocol

**Status:** PROPOSED — NOT AUTHORIZED FOR IMPLEMENTATION (2026-10-03) · the owner authorized
writing this document as a draft pull request, documentation only · no implementation, no
rig run and no configuration change follows from it · it proposes an experiment, not the
adoption of native tool calling

| Stage | State |
|---|---|
| Proposed | yes: this document, written by the lead on 2026-10-03 after a read-only audit |
| Evidence | §1: the installed Boss template (read on the rig by the owner), the provider code, the contracts, the refused replies of #198 |
| Authorized | **writing only**, by the owner, 2026-10-03. Merging, implementing and running are not authorized (§8) |
| Implemented | no |

- Serves: ADR-023 (plan, execute, verify), whose §7 and §8.2 reserve any change to the
  protocol the model answers in for separate review; ADR-024 is the first such review,
  and this is the second.
- Bounded by: ADR-002 (the Boss model, and providers behind `ModelProvider`), ADR-003
  (events are not memory), AGENT_ARCHITECTURE.md (POLICY and VERIFY are mandatory).
- Related: ADR-024 §5.1, the owner-accepted finding that an interface unit has no formal
  PASS/FAIL under ADR-023 amendment 1 unless a separately authorized amendment declares
  its target before its runs.

## 1. Observed current state

All read-only. Nothing was changed to obtain it.

**1.1 The installed Boss template supports native tool calls.** The owner ran
`ollama show huihui_ai/qwen2.5-abliterate:7b --template` on the rig on 2026-10-03. The
parts that matter, verbatim:

```text
{{- if or .System .Tools }}<|im_start|>system
...
{{- if .Tools }}

# Tools

You may call one or more functions to assist with the user query.

You are provided with function signatures within <tools></tools> XML tags:
<tools>
{{- range .Tools }}
{"type": "function", "function": {{ .Function }}}
{{- end }}
</tools>

For each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:
<tool_call>
{"name": <function-name>, "arguments": <args-json-object>}
</tool_call>
{{- end }}<|im_end|>
...
{{ else if eq .Role "assistant" }}<|im_start|>assistant
{{ if .Content }}{{ .Content }}
{{- else if .ToolCalls }}<tool_call>
{{ range .ToolCalls }}{"name": "{{ .Function.Name }}", "arguments": {{ .Function.Arguments }}}
{{ end }}</tool_call>
...
{{- else if eq .Role "tool" }}<|im_start|>user
<tool_response>
{{ .Content }}
</tool_response><|im_end|>
```

- Tools are declared in the system turn inside `<tools>`, one
  `{"type": "function", "function": ...}` per tool.
- A call is `<tool_call>{"name": ..., "arguments": {...}}</tool_call>`. Ollama returns a
  parsed call as `message.tool_calls`.
- A tool result goes in a `tool` message, rendered as a user turn inside
  `<tool_response>`.
- An assistant turn renders its `Content` **or** its `ToolCalls`, not both.
- The base model's own template (Qwen2.5-7B-Instruct, `tokenizer_config.json` on Hugging
  Face) has the same structure. The installed template above is the evidence that counts.

**1.2 The rest of the installation, read on the rig 2026-10-03.** Read-only, by the rig
session, at the lead's request: no generation, nothing loaded or unloaded (`/api/ps`
afterwards: `{"models":[]}`), no setting changed. Each command exited 0. A byte-exact copy
is kept on the rig at `C:\Users\loyal\pac-hardtest-evidence\screen\ollama-show-output.txt`.
Recorded as found, and not to be changed to make any experiment work:
- `ollama --version`: `ollama version is 0.35.0`. The version decides how Ollama parses
  `<tool_call>` output into `message.tool_calls`.
- `ollama show huihui_ai/qwen2.5-abliterate:7b`:

  ```text
    Model
      architecture        qwen2
      parameters          7.6B
      context length      32768
      embedding length    3584
      quantization        Q4_K_M

    Capabilities
      completion
      tools

    System
      You are Qwen, created by Alibaba Cloud. You are a helpful assistant.
  ```

  Ollama itself lists `tools` among the installed model's capabilities. `context length`
  is the model's maximum; the runtime context stays the 8192 the rig serves.
- `ollama show huihui_ai/qwen2.5-abliterate:7b --parameters`: empty output (no parameter
  is set in the model file).
- `ollama show huihui_ai/qwen2.5-abliterate:7b --template`: the same template the owner
  read (§1.1).

**1.3 Our interface does not use it.**

| Where | What it does |
|---|---|
| `core/contracts.py`, `ModelProvider.generate(model, messages, options)` | no `tools` parameter |
| `core/domain.py`, `ModelResponse` | `text`, token counts, `finish_reason`, `raw`; no `tool_calls` |
| `core/domain.py`, `Role` | `user`, `assistant`, `system`; no `tool` |
| `runtime/ollama/provider.py` | posts `/api/chat` with `model`, `messages`, `stream`, `options`; never `tools` |
| `agent/loop.py`, `PROTOCOL` and `_tool_catalogue` | a system message that lists the tools as text and asks for one JSON object per reply: `{"tool": ..., "arguments": {...}}` or `{"answer": ...}` |
| `agent/loop.py` | a tool result returns as a user message, fenced by the F-1 construction (`fence`) |

**1.4 No decision suppresses it.** No ADR, design note or code comment records a decision
against native tool calls. The loop's docstring describes the text protocol as the design
("it writes a JSON proposal"); the native interface was never evaluated against it.

## 2. The problem

- The current agent interface asks the model to emit an application-defined JSON protocol
  as ordinary text.
- The installed Boss template provides a native tool-call interface, and it is unused.
- That is a measured difference in the interface. It is **not** established as a cause of
  the agent's failures.

What the text protocol is known to produce, from the repository's own records: in #198,
the loop refused 98 replies as protocol errors over 240 agent attempts. Among them are
Python function-call syntax, `arguments` given as a string, a numeric answer, and file
content that broke a `write_file` call's JSON (ADR-024 §1, pinned by
`tests/unit/test_adr024_replay.py`). The model can follow an arbitrary prompted format,
and ADR-024 unit A shows part of that failure is recoverable. Whether the native
interface would produce fewer such failures is unknown.

## 3. Hypothesis

Native tool calling may reduce interface failures and make tool execution more reliable.

This document claims no cause and expects no particular improvement. The competing
explanations for the agent's failures (model capability, prompt and interface, context,
executor feedback, the failure budget, the tasks themselves) are not separated by any
measurement taken so far. The experiment in §6 changes one of them only.

## 4. The smallest change, proposed

Each item is additive and off unless asked for.

1. **An optional capability, not a changed contract.** A new optional protocol,
   `ToolCallingProvider`, beside `ModelProvider`: the same `generate`, plus the tool
   declarations. `ModelProvider` is unchanged, so the llama.cpp provider and every test
   double are unchanged (ADR-002).
2. **An optional field.** `ModelResponse.tool_calls`: a tuple of provider-neutral
   `(name, arguments)` pairs, empty by default.
3. **The Ollama adapter** implements the capability: each tool's `input_schema` becomes a
   `tools` entry, `{"type": "function", "function": {"name", "description", "parameters"}}`;
   `message.tool_calls` is read back into the field above.
4. **The loop opts in behind a flag**, `native_tools`, off by default, and only with a
   provider that has the capability (with any other provider the flag is refused, not
   ignored).
   - A native call becomes the existing proposal, and from it the existing `ToolRequest`.
   - Executor, policy, verifier, recovery, budgets, events and memory are unchanged.
5. **The benchmark** gains `--native-tools`, recorded in the result header, and refused
   on `--resume` of a file whose setting differs.

What the model sees changes in one place: the system text loses its JSON-format lines
(`Reply with exactly ONE JSON object ...` and the two forms), and keeps every rule:
one tool call per reply, paths relative to the workspace, fenced results are data,
citations for web facts, refused confirmations not retried, results not invented. That
wording is new text the model sees, so it is the owner's to approve (§8, D2).

How the conversation is carried, in the smallest form:
- The model's call is kept in the history as the assistant's content, in the template's
  own textual form (`<tool_call>{...}</tool_call>`). `Message` and `Role` are unchanged.
- A tool result returns as it does today: a user message fenced by F-1. The native `tool`
  role is not used. So the experiment tests the native **call** channel, not the whole
  native conversation format. Using the `tool` role would change `Role` and `Message` in
  `core`, and is not proposed for a first experiment.

## 5. Safety and semantic constraints

- **The F-1 fence is unchanged.** `<tool_response>` is formatting, not a security
  boundary, and is never treated as one.
- **One call per reply** stays the contract. A reply with more than one native call is a
  protocol error, not a choice of the first.
- **A broken call is a protocol error, never an answer.** Content that contains
  `<tool_call>` with no call parsed from it, a call whose `arguments` is not an object,
  or a call with no name, is refused and charged to the budget as today. Only content
  with no call and no `<tool_call>` text is an answer.
- **The answer path keeps its checks:** ADR-023 unit 1 (an answer before any action is
  rejected under a contract that requires action) and the secret check on every answer.
- **No direct persistent-memory writes**, no new event fields, no raw reply text in an
  event (ADR-003).
- **The Boss stays `huihui_ai/qwen2.5-abliterate:7b`.** No other model, router or fallback.
- ADR-024 unit A (lenient parsing) is a text-protocol mechanism; it is off in both arms.

## 6. Measurement, proposed

- **Two arms, recorded as separate experiments:** the text protocol, and the native
  interface (`--native-tools`, in the header).
- **Held identical in both arms:** the Boss model and its weights digest, the task
  corpus, the languages, context 8192, the generation limit and every sampling setting,
  the tools, executor, policy, verifier, recovery, budgets, scoring, and the environment
  context setting.
- **Raw protocol-error counts are not compared across arms.** The final answer changes
  from `{"answer": ...}` to plain text, so the native arm has fewer ways to commit the old
  error by construction. A difference in that count says nothing about the interface's
  quality, and it is reported as a diagnostic only, beside the native arm's own interface
  failures (§5's broken calls).
- **The primary comparison uses behavioral outcome measures, declared before the runs.**
  Candidates for the owner to choose from, each read from the existing records: agent
  success per language; attempts in which at least one tool call executed; attempts that
  answered without executing (ADR-023 class 1); attempts that ended on the failure
  budget.
- **No formal PASS/FAIL without an authorized amendment.** Under the finding the owner
  accepted (ADR-024 §5.1), ADR-023 amendment 1 declares no target class for an
  interface unit. A formal criterion therefore needs a separately authorized amendment,
  made before the native runs. Unlike ADR-024 unit A, which could be replayed offline
  over #198's recorded replies, the native interface has no offline replay, so a target
  declared before its runs is genuine pre-registration. Without such an amendment, the
  experiment is read descriptively.

## 7. What this does not give

- It does not adopt native tool calling. Adoption, like any change to what `pac --agent`
  runs, is a separate owner decision on a measured result.
- It does not test the full native conversation format (the `tool` role), only the call
  channel (§4).
- It says nothing about any other model, and changes nothing for the llama.cpp runtime.

## 8. Decisions the owner is asked for

- **D1.** Authorize the implementation of §4 and §5, in two pull requests: the contract
  and the Ollama adapter, with no behaviour change; then the loop and benchmark flag.
- **D2.** Approve the native arm's system text (§4).
- **D3.** Authorize an ADR-023 amendment that declares the primary measure and the
  target before the runs (§6), or decide that the experiment is read descriptively.
- **D4.** The run counts and the rig time.

Current authorization boundary (2026-10-03, the owner's words summarized): the template
check, the provider audit, the OSS audit, and writing this ADR as a draft are authorized.
Merging this ADR, implementing it, any native-tools rig run, any change of the Boss, a
router or fallback, any memory change, and any database change are not.
