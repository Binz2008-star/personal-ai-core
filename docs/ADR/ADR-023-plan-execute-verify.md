# ADR-023 — Planning, execution/test and verification in the agent loop

**Status:** PROPOSED / DRAFT — NOT IMPLEMENTED — NOT ACCEPTED

| Stage | State |
|---|---|
| Proposed | yes: this document, 2026-10-02 |
| Design questions | decided by the owner, 2026-10-02 (§6) |
| Accepted | **no** |
| Authorized | **drafting only.** The owner authorized writing this ADR, not implementing it |
| Implemented | **no.** No control below exists in the code |
| Verified | **no.** Nothing has been measured against these controls |

- Serves the owner's order after the capability baseline: Planning, then Execution/Test,
  then Verification, built against measured gaps.
- Evidence: ADR-022, the first baseline (#165) and its scorer correction (#166).
- Builds on:
  - AGENT_ARCHITECTURE.md §1 (the loop) and §5 (VERIFY);
  - BehavioralContract rule 2 (no external effect without confirmation);
  - the contract's rule against reporting results that were not seen.

## 1. The evidence

### 1.1 The run

- 24 tasks x 2 languages x 5 runs: **240 of 240 attempts completed** on the rig at
  `a609f69`.
- Conditions:
  - Boss model `huihui_ai/qwen2.5-abliterate:7b`, weights digest verified;
  - 8192 context;
  - GPU share 0.85;
  - benchmark containment policy (ADR-022 §3.5).
- Files:
  - raw: `evals/results/bench/bench-20261002T081707Z.jsonl` (immutable);
  - derived: `...rescored-bench-checks-v2.jsonl`.

| | Raw (`bench-checks-v1`) | After the scorer correction (`v2`) |
|---|---|---|
| Overall | 106/240 = 44% | **123/240 = 51%** |
| Knowledge track | 97/120 | **114/120 = 95%** |
| Agent track | 9/120 | **9/120 = 8%** |

The difference between the two columns is a scorer correction, not a change to the
system (ADR-022 §10).

### 1.2 The agent track: 111 of 120 attempts failed

The failures fall into four groups. Each failed attempt is in exactly one, classified
mechanically from the run record:

| Class | Definition (from the record) | Attempts | en | ar |
|---|---|---|---|---|
| **1. Answers without executing** | stopped with an answer; no tool call executed | **52** | 15 | 37 |
| **2. Executes without adequate verification** | at least one tool call executed; a check still failed | **43** | 27 | 16 |
| **3. Wrong or unknown environment commands** | no tool call executed; the attempt ended on refused commands | **14** | 8 | 6 |
| Protocol only | no tool call executed or refused; replies were not valid protocol JSON | 2 | 1 | 1 |

What the records show inside each class. These are observations from this benchmark,
not claims beyond it.

**Class 1: answers without executing (52).**
- Some answers describe a plan: "I will add the function to text_utils.py."
- In 17 of the 52, the answer claims an action that never happened, for example:
  - "The file config/settings.json has been created…";
  - "The changes have been committed and pushed to the repository."

  Counted by completion phrases in the answer ("has been", "committed", "pushed",
  "created", "added", "updated", "fixed", "تم").
- 31 of the 37 Arabic answers in this class contain no Arabic script.
- The loop accepts any well-formed `{"answer": …}` as the end of the task. The only
  check on an answer is the secret check.

**Class 2: executes without adequate verification (43).**
- In 27, a file was changed and no test ran after the last change.
- 17 ended on the failure budget (3 failed actions).
- The checks that failed most often:
  - the fixture's own test command, 24;
  - a required file content, 7;
  - a stale path recreated, 6;
  - an answer fact, 14.
- Observed shapes:
  - writing the moved file's old path (`docs/CHANGELOG.md`);
  - editing the test file the task said to leave alone;
  - using `git diff HEAD` to answer what the last commit changed;
  - a changed file reported as done with the tests never run.

**Class 3: wrong or unknown environment commands (14).** Across all 120 agent attempts
the policy refused 73 shell commands:

| Refused command | Count |
|---|---|
| `python -m unittest …`, mostly with `*` patterns; the fixtures use pytest | 34 |
| `ls`, `cat`, `type`, `dir`, `nano` | 10 |
| `python -m debug…` modules that do not exist or are interactive | 9 |
| `dotnet test` / `mstest` in a Python project | 7 |
| single-quoted arguments on Windows `cmd` | 6 |
| `git push` | 2 |
| other | 5 |

`run_command` also rejected `python` 6 times and `dotnet` 5 times. Nothing in the
loop's prompt tells the model the operating system, the shell, the project's language,
or how its tests run.

**The 9 successes:**
- all in English;
- spread over file creation (3), a commit (3), counting files (2) and changing a
  function (1);
- no success in debugging, verification, code generation, multi-step, recovery or
  test execution.

### 1.3 The knowledge track

- 114/120.
- The six failures are all Arabic:
  - four declines of the gym question, three that switched mid-reply into Chinese or
    Korean and one mostly in English;
  - one cross-document answer that drifted into Chinese and never named the gateway;
  - one parental-leave answer that said the documents did not state the figure, then
    supplied one ("8 weeks").
- The citation rate is 5/80. It is informational, because the grounding prompt does not
  ask for citations.

**This benchmark gives no reason to change the knowledge architecture.**

### 1.4 What the evidence does and does not say

- **It says** the current planning/execution/verification loop fails on these tasks:
  - it produces final responses with no observable execution;
  - it executes without verifying the result;
  - it chooses commands for an environment it has not been told about.
- **It does not say** the model is incapable. Every class above is something the loop
  accepts, permits or leaves unstated. Whether the model succeeds once the loop stops
  accepting these outcomes is what a future measurement would show.
- No LoRA or training proposal is made, and none is a prerequisite.

## 2. Proposed controls

These are proposals. None is implemented. The order below is the order of the loop and
the proposed order of implementation (§6, decision 4):

1. environment context;
2. action enforcement;
3. execution;
4. verification.

Environment context is information the model works with. It is never evidence that
anything succeeded.

### 2.1 Environment context

At the start of the loop, code (not the model) gathers authoritative facts about where
the model is working and states them to it:

- operating system and shell, and what that shell does with quotes and wildcards;
- the project's language or runtime, as found in the workspace;
- whether the workspace is a git repository, and its state;
- the supported test command, resolved in this order (§6, decision 3):
  1. the task or repository contract;
  2. an explicit command from the caller;
  3. verified repository configuration (for example a pytest configuration that is
     present and parses);
  4. constrained discovery, only as a fallback (for example a `tests/` directory with
     pytest-style files);
- the command tools and what each accepts.

When a supported test command is known, the model does not choose another. A test run
that counts as verification (§2.3) is a run of that command.

### 2.2 Action enforcement: no action, no completion

The authority for whether a task requires action is the **task contract**, never the
model (§6, decision 1):

    task contract -> action_required -> agent plan -> execution -> verification

- **The task contract** states `action_required` and, where it applies, the evidence
  that completes the task. The model may produce a plan, but its own statement that a
  task does or does not need action decides nothing.
- **When `action_required` is true** and no tool call has executed, a final answer is
  not accepted as completion. The model receives a protocol message saying so. That
  turn consumes one failure (§2.4).
- **In the benchmark,** each task file is the contract.

### 2.3 Verification: no observable evidence, no accepted completion claim

Completion claims are **structured**, not read from free text (§6, decision 2). The
final answer carries a list of claims. The loop accepts a claim only with observable
evidence from a tool receipt or from the workspace:

| Claim | Evidence required |
|---|---|
| `file_created` | an executed write to that path, and the file exists afterwards |
| `file_modified` | an executed write or edit to that path, and its content differs from the checkpoint taken before the run |
| `file_deleted` | an executed delete of that path, and the path is absent |
| `tests_passed` | an executed run of the supported test command (§2.1), after the last file change, with exit status 0 |
| `committed` | an executed `git commit` with exit status 0, and a new commit in `git log` |
| `pushed` | an executed push with exit status 0. Pushing is not permitted in the benchmark, so this claim cannot be accepted there |

- **Rule:** no observable evidence means no accepted completion claim.
- **An unsupported claim** is returned to the model with what the evidence shows, and
  consumes one failure (§2.4). It is not shown to the user as fact.
- **Free-text wording is not the mechanism.** A phrase list may later be used only to
  report an answer that asserts a completion without a structured claim. It never
  accepts anything.
- **For a project change that has a supported test command,** completion needs the full
  sequence:
  1. modify;
  2. run the test;
  3. inspect the result;
  4. repair if needed;
  5. `tests_passed` on the final state.

### 2.4 The failure budget

The budget stays at **3 failures** (and 12 actions). It is not tuned from this
benchmark (§6, decision 5). This is what consumes it.

**Today, in the code (`agent/recovery.py`, `agent/loop.py`), one failure is:**

- a tool call the policy denied or that was not confirmed;
- a tool call with invalid arguments;
- an executed tool call whose result failed, or that the verifier did not pass;
- a reply that is not valid protocol JSON.

**Proposed additions,** one failure each:

- a final answer rejected under §2.2 (action required, none taken);
- a completion claim rejected under §2.3 (no observable evidence).

**Not a failure:** a successful step, the final accepted answer, and the environment
context of §2.1.

**Counting:**

- Failures accumulate over the run and do not reset after a success.
- Every tool call and every rejected answer also counts as one action.

**After the third failure:**

- The loop stops. No further model call or tool call is made.
- The outcome has no answer (`answer` is `None`) and records the stop reason, "3 failed
  actions reached the limit of 3".
- The run is not a completion, whatever it claimed earlier.
- Files changed before the stop are left as they are. The checkpoints taken before each
  change are returned to the caller, who decides whether to roll back. Nothing is rolled
  back automatically.
- In the benchmark, such a run is a failure, with the `no_answer` signal.

## 3. Non-goals

- No implementation in this ADR. No change to the agent runtime, the loop, the tools,
  the policy or the prompts.
- No change to:
  - the benchmark, its 24 tasks, its checks or expected answers;
  - the 240 recorded attempts or any result file;
  - the Boss model;
  - the knowledge path.
- No prompt change made only to raise the score. A control enters the loop because it
  makes the loop correct for any task; the benchmark only measures whether it did.
- No LoRA, fine-tuning or training.
- No Neon, pgvector, migration, schema or production database change.
- Output-language policy is not part of this ADR (§6, decision 6). The language guard
  does not run in the agent loop today, and 31 of the 37 Arabic no-action answers
  contained no Arabic script. That is recorded as a separate finding and a dependency
  for whoever decides the agent loop's language behaviour. It is not a control here.
- Not addressed here, each kept separate:
  - the fixture fingerprint discrepancy;
  - Remote Control's worktree mode;
  - the ADR-021 `ground-decline-ar` observations;
  - the 50 test sessions in `core.db`;
  - the NVIDIA/Vulkan status.

## 4. Authorization boundary

| Action | Needs |
|---|---|
| Edit this draft | already authorized |
| Accept this ADR (PROPOSED → ACCEPTED) | the owner; the design questions are decided (§6), the items in §7 remain |
| Implement any control | a separate, explicit authorization per control, after acceptance |
| Measure an implementation on the rig | the owner's go, as for every rig run |
| Adopt a control in `pac --agent` | the owner, on the measured result |

Each control would be its own PR, in the order of §2: small, behind tests, and changing
one thing.

## 5. How a future implementation would be evaluated

**Same instrument.** The same 24 tasks and 240 attempts:
- same checks and scorer (`bench-checks-v2`, or a later version applied to both sides
  by rescore);
- same Boss model and weights digest;
- same rig conditions;
- the baseline is the rescored file in §1.1.

**Compared, per side, by language and by category:**

| Measure | Definition, from the run records |
|---|---|
| Action rate | share of agent attempts with at least one executed tool call |
| Evidence-backed completion rate | share of completions whose claims are all supported by the audit log |
| Verification rate | share of attempts that changed a file and ran a successful test after the last change, where the task has tests |
| Task success | the existing per-task success (all deciding checks pass) |
| Regression rate | tasks, including knowledge tasks, whose success rate falls against the baseline |
| False rejection rate | answers the new controls rejected although the task's checks would have passed. Each rejection is recorded so this is countable |

- **No target score is set.** A number to reach invites tuning toward these 24 tasks.
  The reading is per task and per class, as ADR-020's comparisons are.
- A control that raises success while raising false rejection or regressions is not
  read as an improvement.
- Before any comparison, the rule that decides "better" is fixed and written down,
  following ADR-020 amendment 1, and applied to the 5-run samples with their variance
  stated.

## 6. Decisions on the design questions (owner, 2026-10-02)

These decide the design. They do not accept the ADR or authorize implementation.

1. **What requires action.**
   - The task contract is the authority: `action_required`, then plan, execution,
     verification.
   - The model's plan is not the authority.
2. **Claim verification.**
   - Structured claims tied to observable evidence and tool receipts: `file_created`,
     `file_modified`, `tests_passed`, `committed`, `pushed`, as in §2.3.
   - Not a closed list of natural-language words.
   - No observable evidence means no accepted completion claim.
3. **The test command.** In order of precedence:
   1. the task or repository contract;
   2. an explicit command from the caller;
   3. verified repository configuration;
   4. constrained discovery as a fallback.

   The model does not invent a test command when one is defined.
4. **Order.**
   1. Environment context.
   2. Action enforcement.
   3. Execution.
   4. Verification.

   Environment context informs; it proves nothing.
5. **Failure budget.**
   - It stays at 3 and is not tuned from this benchmark.
   - §2.4 defines what consumes it and what happens after the third failure.
6. **Language guard.**
   - Out of this ADR.
   - Recorded as a separate finding and dependency only (§3).

## 7. Questions that remain open

These follow from the decisions and are left for acceptance or implementation:

1. **Where the contract comes from outside the benchmark.**
   - In `pac --agent` a person types a request; there is no task file.
   - Who sets `action_required` there: the caller, a command-line flag, or a default?
   - What applies when it is not set?
2. **How the model states structured claims.**
   - A field in the final protocol object (for example `{"answer": …, "claims": […]}`)
     changes the protocol the model is told about, which is a prompt change.
   - It would be its own reviewed change, made for correctness and not for the score.
3. **`file_modified` for a file the run created.** Created then edited: is that
   `file_created` only, or both?
