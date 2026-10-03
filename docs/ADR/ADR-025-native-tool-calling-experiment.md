# ADR-025 — An experiment: the Boss model's native tool-call channel, against the text protocol

**Status:** PROPOSED — NOT AUTHORIZED FOR IMPLEMENTATION (2026-10-03) · the owner authorized
writing this document as a draft pull request, documentation only · no implementation, no
rig run and no configuration change follows from it · it proposes an experiment, not the
adoption of native tool calling · revised 2026-10-03 after the owner's review of #205 (six
corrections, §4 to §6 and §9)

| Stage | State |
|---|---|
| Proposed | yes: this document, written by the lead on 2026-10-03 after a read-only audit |
| Evidence | §1: the installed Boss template (read on the rig by the owner), the provider code, the contracts, the refused replies of #198 |
| Authorized | **writing only**, by the owner, 2026-10-03. Merging, implementing and running are not authorized (§8). The owner's review of 2026-10-03 authorizes none of them either |
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

### 4.1 The provider capability: its exact shape

`ModelProvider` (the interface in `core/contracts.py`) is not changed. The capability is a
**separate method on a separate protocol**, detected structurally:

```python
# core/contracts.py -- proposed
@runtime_checkable
class ToolCallingProvider(Protocol):
    """A provider that can declare tools to the model and return its native calls."""

    @property
    def name(self) -> str: ...

    def generate_with_tools(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        tools: Sequence[ToolDeclaration],
        options: Mapping[str, Any] | None = None,
    ) -> ModelResponse: ...
```

- **Structural detection, not inheritance.** The loop asks
  `isinstance(provider, ToolCallingProvider)`. A provider has the capability by having the
  method; no class inherits from another, and `generate` keeps its signature and meaning.
- **A provider without the method cannot run the native arm.** The flag is refused with
  that provider, never silently ignored (the llama.cpp provider, every test double).
- `ToolDeclaration` is a provider-neutral, frozen record: `name: str`,
  `description: str`, `parameters: Mapping[str, Any]` (the tool's existing JSON-schema
  `input_schema`, passed as it is).

### 4.2 The domain change: `ModelResponse` gains a field

`ModelResponse` (in `core/domain.py`) is a core domain type and a contract boundary, so
this **is** a change to `core`, even though it is additive and every existing caller is
unaffected. It needs explicit review (§8, D1).

```python
# core/domain.py -- proposed
@dataclass(frozen=True, slots=True)
class NativeToolCall:
    name: str
    arguments: Any          # as the backend returned it; never coerced by the provider


class ModelResponse:        # existing fields unchanged; one added
    ...
    tool_calls: tuple[NativeToolCall, ...] = ()
```

`arguments` is kept as returned so the loop, not the provider, decides what is malformed
(§5). `Message` and `Role` are not changed.

### 4.3 The adapter, the loop and the benchmark

- **The Ollama adapter** implements `generate_with_tools`. Each `ToolDeclaration` becomes
  `{"type": "function", "function": {"name", "description", "parameters"}}` in the
  request's `tools`. `message.tool_calls` is read back into `ModelResponse.tool_calls`.
- **The loop opts in behind a flag**, `native_tools`, off by default.
  - A native call becomes the existing proposal, and from it the existing `ToolRequest`.
  - Everything after `ToolRequest` is unchanged (§9).
- **The benchmark** gains `--native-tools`, recorded in the result header. `--resume`
  refuses a file whose setting differs.

### 4.4 What the model sees

The system text changes in one place. It loses its JSON-format lines
(`Reply with exactly ONE JSON object ...` and the two forms), and keeps every rule: one
tool call per reply, paths relative to the workspace, fenced results are data, citations
for web facts, refused confirmations not retried, results not invented. That wording is
new text the model sees, so it is the owner's to approve (§8, D2).

### 4.5 What is tested: the native call channel, in a hybrid conversation

The experiment is **not** the full native conversation protocol. It is a **hybrid**:

| Part of the exchange | Native template | This experiment |
|---|---|---|
| Tools declared | `.Tools`, in the system turn | **native** |
| The model's call | parsed into `message.tool_calls` | **native** |
| The call, kept in the history | an assistant message with `ToolCalls` and no `Content` | **not native**: assistant `Content` holding the call's text |
| A tool result | a `tool` message, rendered inside `<tool_response>` | **not native**: a user message, fenced by F-1, as today |

- The history keeps the call as text so that `Message` and `Role` stay unchanged. The
  text reproduces the template's own rendering of `ToolCalls`
  (`<tool_call>\n{"name": "...", "arguments": {...}}\n</tool_call>`). The model therefore
  reads its earlier call in the form the template would have rendered it, but reads every
  result as a user turn, not as `<tool_response>`.
- **This may under-test the native interface.** A result from this experiment is reported
  as "the native tool-call channel, with the existing conversation representation",
  **never** as a result of native tool calling in general.
- The full native conversation (`Role` gains `tool`, `Message` carries tool calls) changes
  two more core types. It is a separate, later experiment, and is not proposed here.

## 5. Safety and semantic constraints

- **The F-1 fence is unchanged.** `<tool_response>` is formatting, not a security
  boundary, and is never treated as one.
- **One call per reply is an experiment constraint, stricter than the template.** The
  template's tools section tells the model it "may call one or more functions", and our
  system text tells it one per reply: the model sees both, and that tension is part of
  the experiment as specified. A reply with more than one native call is a protocol
  error, not a choice of the first. No result is attributed to native tool calling in
  general, which allows several calls.
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

### 6.1 The primary measure, proposed for pre-registration

Proposed by the lead, to be fixed by the owner before any implementation or run is
authorized (§8, D3). It has the same form as ADR-023 amendment 1, so the existing verdict
code (`bench.compare --unit`, #203) can apply it, after one small change that is part of
D1: today that code accepts a single class as a target, and this target is a union.

- **Target (R1 form): the attempts in which no tool call executed.** That is the union of
  ADR-023 §1.2's groups with no executed call: class 1 (answers without executing),
  class 3 (wrong or unknown environment commands), "rejected until the budget ended" and
  "protocol only" (`compare.classify`: `answered_without_executing`, `refused_commands`,
  `rejected_to_budget`, `protocol_only`).
  - It passes when, over agent attempts paired by (task, language, run), attempts leaving
    the set outnumber attempts entering it, by a one-sided exact sign test at 5%.
  - Why this set: it is behavioral (did the model act at all), it is made of classes the
    accepted rule already defines, and it does not depend on how protocol errors are
    counted. An attempt whose failure merely moves between these groups (for example a
    protocol error that becomes an early answer) neither leaves nor enters it.
  - For scale, not as a prediction: in #198's unit 1 run, 37 of 120 agent attempts were
    in this set.
- **Guard (R2, unchanged):** no track × language group's failures rise by 16 or more, at
  10 runs per side. Agent success is therefore guarded, not the target.
- **R0, R3, R4 and R5 unchanged.** R0 additionally requires that `native_tools` be the
  only header difference between the arms.
- **Both arms:** ADR-023 unit 1 on (the task contract), unit 2 off, ADR-024 unit A off.

### 6.2 If no formal gate is authorized: the descriptive report, fixed now

Per arm and per language, read from the records with the existing tools (`compare`,
`refusals`, the verdict's R3):
- agent success; and knowledge success, whose code neither arm changes, as an instrument
  check;
- attempts with at least one executed tool call;
- failed attempts by ADR-023 §1.2 class, and the paired transitions between arms;
- executed and refused tool calls, by tool and by refusal reason;
- attempts that read a file before their first write;
- budget stops, answers rejected for not having acted, and false rejections;
- seconds and tokens per attempt;
- provider and runtime failures, separately, never counted as model failures;
- interface diagnostics, each only for its own arm and never compared across arms: the
  text arm's protocol errors by `refusals.py` category, and the native arm's broken calls
  by kind (unparsed `<tool_call>`, non-object `arguments`, no name, more than one call).

### 6.3 The governance gap, and the smallest change that closes it

The finding the owner accepted (ADR-024 §5.1) stands: ADR-023 amendment 1 declares R1
targets only for ADR-023's units, so no interface unit can be formally gated. On
2026-10-03 the owner said the governance may be changed if the evidence shows it blocks a
technically sound improvement. The lead's reading of the evidence:
- **The gap is real:** a unit outside ADR-023 can never PASS, whatever its data.
- **The safeguards are not the problem.** R0 (one instrument), R2 (no regression,
  calibrated), R5 (read once) and pre-declaration are what make any verdict worth having.

So the proposal is an **extension, not a loosening**. It is a possible ADR-023 amendment 2,
written only if the owner authorizes it (§8, D3), and not written here:
1. A unit outside ADR-023 may have an R1 target, declared in its own ADR and approved by
   the owner before any of its runs.
2. A target may be a union of ADR-023 §1.2 groups.
3. A unit whose effect can be replayed offline over data already read cannot declare a
   target afterwards. ADR-024 unit A therefore stays as recorded: mechanically validated,
   end-to-end effect not formally gated.
4. R0, R2, R3, R4 and R5 are unchanged.

## 7. What this does not give

- It does not adopt native tool calling. Adoption, like any change to what `pac --agent`
  runs, is a separate owner decision on a measured result.
- It does not test the full native conversation format (the `tool` role, assistant
  `ToolCalls`), only the call channel in a hybrid conversation (§4.5). Its result is never
  reported as a result of native tool calling in general.
- One call per reply is stricter than the template (§5).
- It says nothing about any other model, and changes nothing for the llama.cpp runtime.

## 8. Decisions the owner is asked for

- **D1.** Authorize the implementation of §4 and §5, in two pull requests: first the
  core changes (`ToolCallingProvider`, `ToolDeclaration`, `NativeToolCall`,
  `ModelResponse.tool_calls`; §4.1, §4.2, reviewed as core contract changes) and the
  Ollama adapter, with no behaviour change; then the loop and benchmark flag.
- **D2.** Approve the native arm's system text (§4.4).
- **D3.** Either authorize ADR-023 amendment 2 (§6.3) and fix the primary measure (§6.1)
  before any run, or decide that the experiment is read descriptively (§6.2).
- **D4.** The run counts and the rig time.

## 9. Hard invariants

None of these changes in this ADR, its implementation or its experiment:
- The Boss is `huihui_ai/qwen2.5-abliterate:7b`. No other model, router or fallback.
- The executor, the policy, the verifier, recovery and the budgets are unchanged.
- No memory path changes: no direct persistent-memory write, no new event field, no raw
  reply text in an event (ADR-003).
- No database change.
- `ModelProvider` and its `generate` are unchanged. The core types that do change are
  named in §4.1 and §4.2.

Current authorization boundary (2026-10-03, the owner's words summarized): the template
check, the provider audit, the OSS audit, and writing this ADR as a draft are authorized.
Merging this ADR, implementing it, any native-tools rig run, any change of the Boss, a
router or fallback, any memory change, and any database change are not.
