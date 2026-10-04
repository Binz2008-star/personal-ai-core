# ADR-025 — An experiment: the Boss model's native tool-call channel, against the text protocol

**Status:** ACCEPTED as an experiment by the owner (2026-10-03, §11) · the gate, the
implementation and the measurement are authorized · adopting native tool calls as the
default is **not** decided, and waits for the measurement · it proposes an experiment, not
the adoption of native tool calling · revised 2026-10-03 after the owner's review of #205 (six
corrections, §4 to §6 and §9), and again after its second review (the R1 predicate, the
R0 wording, the validation boundary: §4.6, §6)

| Stage | State |
|---|---|
| Proposed | yes: this document, written by the lead on 2026-10-03 after a read-only audit |
| Evidence | §1: the installed Boss template (read on the rig by the owner), the provider code, the contracts, the refused replies of #198 |
| Authorized | by the owner, 2026-10-03 (§11): the gate `NO_EXECUTED_TOOL_CALL` with ADR-023 amendment 2, the implementation behind an off-by-default flag, and the measurement on the rig. Adoption as the default is not decided |
| Implemented | PR 1, the core types and the Ollama adapter (#206); PR 2, the loop, `--native-tools` and the verdict predicate (#207). Off by default. Not yet measured |

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

### 4.6 The validation boundary

A native call reaches a tool only through the boundary that a text-protocol call crosses
today. Nothing is skipped and nothing new can execute:

```text
provider ──> NativeToolCall ──> explicit validation ──> ToolRequest ──> policy ──> executor ──> verifier
           (raw, uncoerced)       (the loop, §5)        (existing)     (existing)  (existing)  (existing)
```

- **The provider** reports what the backend returned, in `NativeToolCall`. It neither
  coerces `arguments` nor drops a call.
- **The loop validates explicitly**, before any `ToolRequest` exists: exactly one call;
  `name` a non-empty string; `arguments` a JSON object (a mapping), with no coercion, so a
  JSON *string* is refused, not parsed; and no `<tool_call>` text left in the content
  without a parsed call. Anything else is a protocol error (§5), charged to the budget as
  today.
- **Only a validated call becomes a `ToolRequest`**, built the same way a text-protocol
  proposal is. From there the path is the existing one: `executor.execute` applies the
  policy (`allow`, `deny`, `ask`), validates the arguments against the tool's schema
  (`validate_arguments`), handles confirmation, and runs the tool; the verifier checks the
  result. An unknown tool name passes the loop's check (it is a string) and is refused by
  the executor, exactly as today.
- **The loop never runs a tool, never builds an `AuditRecord`, and never calls the
  executor any other way.** A native call cannot bypass `ToolRequest`, the policy or the
  schema check.
- Text that accompanies a valid call is not an answer. It is kept in the history before
  the call's text (§4.5), as the model wrote it.

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
- **What differs, and what is held identical.** `native_tools` is the only experimental
  condition flag that differs. The native arm necessarily changes the model-facing tool
  declaration and call channel (§4.1, §4.3) and the corresponding system-text protocol
  instructions (§4.4). All other conditions are held identical: the model and its weights
  digest, the task corpus, the languages, the context (8192), the generation and sampling
  settings, the tools, executor, policy, verifier, recovery, budgets, scorer, and the
  environment-context setting.
- **Raw protocol-error counts are not compared across arms.** The final answer changes
  from `{"answer": ...}` to plain text, so the native arm has fewer ways to commit the old
  error by construction. A difference in that count says nothing about the interface's
  quality, and it is reported as a diagnostic only, beside the native arm's own interface
  failures (§5's broken calls).

### 6.1 The primary measure, proposed for pre-registration

Proposed by the lead, to be fixed by the owner before any implementation or run is
authorized (§8, D3). It has the form of ADR-023 amendment 1: a pre-declared target tested
by R1, success guarded by R2.

**The target is a named behavioral predicate, not a union of classifier labels:**

```text
NO_EXECUTED_TOOL_CALL(attempt)  :=  executed_tool_calls(attempt) == 0
executed_tool_calls(attempt)    :=  the number of entries in the attempt's run record
                                    `steps` whose `executed` is `true`
```

- **Its source** is the immutable JSONL run record the benchmark writes (`kind: "run"`,
  `track: "agent"`), field `steps[*].executed`, which the benchmark copies from the
  executor's `AuditRecord.executed`. Nothing is judged by hand and nothing is inferred
  from a label.
- **What `executed: true` means** (`agent/executor.py`, `_decide_and_run`): the tool
  exists, the policy did not deny, the arguments passed `validate_arguments` against the
  tool's schema, any confirmation was given, and the tool's `run` was called. The tool's
  own result does not matter: a tool that ran and reported an error counts as executed.
  An unknown tool, a policy denial, invalid arguments or a refused confirmation leave
  `executed: false`. A protocol error or a rejected answer creates no step at all.
- **It is computed identically in both arms**, because in the native arm a call reaches
  the executor only as a `ToolRequest` (§4.6) and produces the same `steps` entries.

**Every attempt is either classifiable or handled explicitly. None is classified as "no
execution" by default:**

| Record | Classifiable | Treatment |
|---|---|---|
| An agent run with `stop` "answered" or "budget", `steps` present, every `executed` a boolean | yes | the predicate, from `steps` |
| `stop` "error": a provider or runtime failure | no | excluded from R1. Its (task, language, run) pair is dropped from the sign test on both sides, and every dropped pair is listed and counted |
| An expected (task, language, run) with no record, a file without its end record, a record without `steps`, or a non-boolean `executed` | no | the comparison is NOT READABLE (R0), as for any incomplete or malformed file |

Proposed with it, for the owner's decision (D3): if either arm has more than 5 provider or
runtime failures (about 2% of 240 agent attempts), the comparison is NOT READABLE,
because dropping pairs at that scale could bias the paired test. Once fixed, this bound
is a pre-registered readability rule: it is never adjusted after any result is seen.

**The ADR-023 classes stay diagnostic.** By construction of `compare.classify`, they
relate to the predicate as follows. The relation is reported, never used as the gate, so
a later change to the classifier cannot change the gate:

| ADR-023 class (`compare.classify`) | `NO_EXECUTED_TOOL_CALL` |
|---|---|
| `answered_without_executing`, `refused_commands`, `rejected_to_budget`, `protocol_only` | true, by construction |
| `executed_unverified` | false, by construction |
| `success` | read from `steps`: either value is possible (a task whose checks pass without a tool) |
| `provider_error` | not classifiable (above) |

Checked on #198's two files: no provider failures, every `executed` a boolean, and the
table holds; every success there executed at least one call.

**R1 with the predicate:**
- **leaving** = the predicate true in the baseline arm and false in the native arm;
- **entering** = the reverse;
- R1 passes when leaving outnumbers entering by a one-sided exact sign test at 5%, over
  classifiable pairs.

For scale, not as a prediction: in #198's unit 1 run, 37 of 120 agent attempts had no
executed tool call, and none had a provider failure. This is a descriptive baseline read
before any native run. It is not a gate result, and the rule is never applied to #198
retroactively.

**The rest of the rule:**
- **R2, unchanged:** no track × language group's failures rise by 16 or more, at 10 runs
  per side. Agent success is guarded, not the target.
- **R0:** as in ADR-023 amendment 1, with the conditions of §6 (only the `native_tools`
  flag differs as an experimental condition) and the table above.
- **R3, R4 and R5 unchanged.**
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
2. A target may be a named predicate over the run record (such as
   `NO_EXECUTED_TOOL_CALL`, §6.1), defined with its classification of every record,
   including provider failures and incomplete records.
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
  Ollama adapter, with no behaviour change; then the loop, with the validation boundary
  of §4.6, and the benchmark flag. If D3 authorizes a formal gate, the verdict code
  (`bench.compare --unit`, #203), which today accepts a single class as a target, also
  learns the predicate of §6.1 and its classification table.
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

## 10. The governance sequence

Recorded after the second review (2026-10-03), which approved this draft's methodology for
the next governance step and authorized nothing else. Each step waits for the one before:

1. This ADR defines the proposed experiment and its predicate (done, as a draft).
2. A governance decision: is `NO_EXECUTED_TOOL_CALL` an admissible primary gate?
3. If it is, ADR-023 amendment 2 formally records the rule, before any native run.
   ADR-023 amendment 1 is not changed by this ADR, and amendment 2 is not written
   merely because this ADR proposes it.
4. D4, the run count, under the accepted rule.
5. The implementation is authorized (D1, D2).
6. Only the approved native capability is implemented.
7. The experiment runs.
8. The pre-registered verdict is applied mechanically.

| Decision | State after the second review |
|---|---|
| D1, the implementation shape | ready for an authorization decision |
| D2, the system text | ready for an authorization decision |
| D3, amendment 2 and the formal measure | required before any formal gate |
| D4, the run count | deferred until D3 |
| A change to ADR-023 | not yet |
| Implementation, a rig run | not authorized |
| Merging this ADR | not yet, if merging would imply authorizing implementation |

## 11. Decided (2026-10-03)

After the second review approved this design for the governance step, the lead asked for explicit approval of three points: (1) the measure `NO_EXECUTED_TOOL_CALL` as the formal gate, with ADR-023 amendment 2; (2) the implementation behind a flag that is off by default, in two pull requests; (3) the measurement on the rig, both interfaces, the same model, 10 runs per side. The owner
answered, in these words: "موافق على النقاط جميعها" ("I approve all the points").

So, as of this decision:
- **Step 2 and D3.** `NO_EXECUTED_TOOL_CALL` (§6.1) is the formal primary gate, with its
  classification table and the bound of 5 provider failures per arm, fixed before any
  implementation or run. ADR-023 amendment 2 records the rule that admits it.
- **D1.** Implement §4 to §5 in two pull requests: first the core types and the Ollama
  adapter, with no behaviour change; then the loop with the validation boundary of §4.6,
  the benchmark flag, and the predicate in the verdict code.
- **D2.** The native arm's system text is the one in the loop's pull request, written to
  §4.4: the JSON-format lines removed, every rule kept.
- **D4.** 10 runs per side, as ADR-023 amendment 1 D1 fixes for a formal reading: both
  tracks, both languages, the text arm and the native arm at the same commit.
- Unchanged: §9's invariants, the validation boundary of §4.6, and no default changes
  without the measurement.

Superseded by §11. The authorization boundary as it stood before this decision
(2026-10-03, the owner's words summarized): the template
check, the provider audit, the OSS audit, and writing this ADR as a draft are authorized.
Merging this ADR, implementing it, any native-tools rig run, any change of the Boss, a
router or fallback, any memory change, and any database change are not.
