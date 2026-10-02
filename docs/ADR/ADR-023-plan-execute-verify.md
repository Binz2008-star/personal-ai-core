# ADR-023 — Planning, execution/test and verification in the agent loop

**Status:** PROPOSED / DRAFT — NOT IMPLEMENTED

| Stage | State |
|---|---|
| Proposed | yes: this document, 2026-10-02 |
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

These are proposals. None is implemented. Each is stated as a rule the loop would
enforce, with what it would read.

### 2.1 No action, no completion

- **Rule:** if the task requires an action or change and no tool call has executed, a
  final answer is not accepted as completion.
- **What happens instead:** the model is told, as a protocol message, that the task
  required action and none was taken. That turn counts against the budget like any
  failed step.
- **Open:** how the loop knows a task requires action (Q1). It must not depend on the
  benchmark's task files, which the production loop never sees.

### 2.2 No claim without evidence

- **Rule:** when the answer claims that something was changed, committed, tested or
  verified, the claim is checked against the audit log before the answer is accepted.
  For example:
  - "created X" needs an executed `write_file` to X;
  - "committed" needs an executed commit;
  - "tests pass" needs an executed test command after the last change, and that command
    must have succeeded.
- **For a project change that has tests**, the loop expects this sequence:
  1. modify;
  2. run the test;
  3. inspect the result;
  4. repair if needed;
  5. verify the final state.
- **An unsupported claim** is returned to the model with what the log shows. It is
  neither shown to the user as fact nor silently removed.
- **Open:** how claims are recognized without a judge model (Q2). The verifier already
  names grounding as the layer it does not have (`agent/verifier.py`, layer 2); this
  would be its mechanical form, limited to claims about tool actions.

### 2.3 Environment context

At the start of the loop, the model receives authoritative facts about where it is
working, gathered by code, not guessed by the model:
- operating system and shell (and what that shell does with quotes);
- the project's language or runtime as detected from the workspace;
- whether the workspace is a git repository, and its state;
- the supported test command, if one can be determined;
- the command tools and what each accepts.

**Open:** the source of the "supported test command" (Q3), and how much context this
costs on an 8192-token window.

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
| Accept this ADR (PROPOSED → ACCEPTED) | the owner, after answering §6 |
| Implement any control | a separate, explicit authorization per control, after acceptance |
| Measure an implementation on the rig | the owner's go, as for every rig run |
| Adopt a control in `pac --agent` | the owner, on the measured result |

Each control would be its own PR: small, behind tests, and changing one thing.

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

## 6. Open questions for the owner

1. **Q1, which tasks require action.** Options:
   - the user's wording (a classifier the loop runs, itself measured);
   - the model stating its intent first (a plan step the loop then holds it to);
   - an explicit mode chosen by the caller.

   The draft proposes the plan step, because it also serves Planning. It needs the
   owner's choice.
2. **Q2, recognizing claims without a judge model.**
   - The draft proposes a closed list of action claims mapped to audit-log evidence,
     in English and Arabic, accepting that unlisted phrasings are missed.
   - Alternatively, a structured answer field where the model lists what it did.
3. **Q3, the test command.**
   - Detected from the workspace (pytest config, `tests/`, etc.), declared by the
     caller, or both.
   - What happens when none is found.
4. **Order.**
   - The draft proposes three PRs: 2.3 (environment) first, because it changes no
     acceptance rule; then 2.1; then 2.2.
   - Each would be measured separately or together, at the owner's choice.
5. **Budget.** Rejected answers would count against the failure budget
   (`max_failures=3`). Should the budget change with these controls, or stay as the
   system's real setting?
6. **Arabic.** 31 of 37 Arabic no-action answers were not in Arabic. The language guard
   does not run in the agent loop today. Is that in scope for this ADR or kept
   separate?
