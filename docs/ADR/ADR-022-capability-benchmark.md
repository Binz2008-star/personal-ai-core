# ADR-022 — A capability benchmark: does the system do the task?

**Status:** ACCEPTED · direction approved by the owner 2026-10-02 ("put item 10 first") ·
§8 decided 2026-10-02 (D1 24 tasks, D2 5 runs, D3 containment as in §3.5) · units 1-3 built (the v0 set: 24 tasks)

- Serves the owner's order of 2026-10-02: measurement first, then targeted improvement
  against measured gaps. LoRA or fine-tuning is considered only if this baseline shows a
  recurring weakness that prompting, RAG, tools, memory, architecture or model choice
  cannot fix.
- Builds on ADR-013 (the harness: mechanical checks, no judge model) and ADR-020 (weights
  by digest, runs repeated, comparisons per case under a calibrated rule).
- Does not replace `contract_v1` or `refusal_v2`. Those measure whether the system keeps
  its rules. This measures whether it can do the work.

## 1. The problem

Everything measured so far is compliance: the language of the reply, secrets, refusals,
declines. Nothing measures whether the system:

- edits a file correctly;
- makes a failing test pass;
- runs a git command it was asked for;
- finds the answer that sits in its own knowledge base.

Without that, every planned improvement is a guess:

- semantic retrieval;
- a planning step;
- a "run the tests" step;
- long-term memory;
- a stronger model.

So is the owner's final question: is there a weakness that only training can fix?

What we have seen so far is anecdotal:

- asked to count JSON files, the agent searched file contents and answered "none" (fixed
  by `find_files`, #129);
- a retrieval test returned an Arabic passage for an English query, because the embedder
  is a hashing embedder, not a semantic one.

## 2. What is ruled out

- **A judge model as the primary measure.** It inherits the failures it should catch
  (ADR-013), and it is not reproducible. Every task has a mechanical check.
- **Public benchmarks as the baseline.** SWE-bench and similar test a different stack:
  other tools and other repositories, mostly English. Ours must exercise this system's
  tools, its sandbox, its knowledge base and Arabic. Public-benchmark *style* tasks are
  welcome.
- **One run per task.** The model samples. ADR-020 showed a single run can swing from 2
  to 9 of 15 on identical weights.

## 3. Proposed design

### 3.1 Two tracks, one harness

- **Agent track.** A task runs through `build_agent`, the same composition `pac --agent`
  uses, in a fresh temporary workspace built from a fixture. When the agent stops, the
  checks inspect the workspace and the agent's answer.
- **Knowledge track.** A question runs through the grounded conversation path, the one
  `pac --documents` uses, over a fixture corpus ingested into a fresh in-memory store.
  The checks inspect the answer and the citations.

Every task exists in English and in Arabic. The workspace, corpus and expected outcome
are the same; the language of the instruction differs. The fixtures are the same
because a capability gap and a language gap must be separable.

### 3.2 Categories, as the owner listed them

| Category | Example task | Mechanical check |
|---|---|---|
| File operations | rename a file; create one with given content | file exists / does not; content equals or contains |
| Code generation | add a function to a module | a provided test file passes when run |
| Code modification | change a function's behaviour | the provided tests pass; the untouched tests still pass |
| Debugging | a failing test with one bug planted | the test passes; nothing else in the diff |
| Test execution | "run the tests and tell me how many fail" | the answer contains the true count |
| Verification | "fix it and confirm the tests pass" | the tests pass, and the transcript shows a test run after the last edit |
| Git | commit a change with a given message; report the last commit | `git log` shows it; the answer names the right hash |
| Multi-step | find the config, change a value, run the check script | the final state, plus each intermediate fact |
| Tool selection | count files of a type; find a string | the right answer, and the tool actually used (from the audit log) |
| Failure / recovery | the first obvious approach fails (a missing directory, a wrong path) | the end state, after a failed step is recorded |
| Knowledge QA | answer from the corpus; decline when the corpus does not hold it | the expected fact is present; the cited document is the right one; a decline when it should decline |
| Arabic / English | each task above in both | the same checks; the reply-language check where the answer is free text |

### 3.3 What a task file states

A task names:

- its fixture (files, an optional git history, a test file), or a corpus;
- the instruction, in both languages;
- its checks.

The check types reuse ADR-013's style:

- `file_exists`, `file_absent`, `file_contains`, `file_equals`;
- `command_passes`: runs the fixture's own test or check script after the agent stops,
  outside the agent's control;
- `git_log_contains`;
- `answer_contains`, `answer_number`;
- `cites`, `declines`;
- `tool_used` and `tool_not_used`, read from the audit log;
- `tested_after_last_edit`.

There is no free-text grading.

### 3.4 What is recorded per run

Everything ADR-020 records (commit, weights digest, provider, settings, role), and per
task:

- **Outcome:** success or failure, and each check's verdict with its detail.
- **Tools:** the tools called, in order, with each policy decision and verifier result,
  from the audit log; the number of steps and of failed steps; protocol errors.
- **Stop:** how the run stopped (answered, budget, protocol). The answer is kept
  verbatim; secrets are redacted as everywhere.
- **Latency:** wall time per task and per model call.
- **Tokens:** prompt and generated tokens where the runtime reports them.
- **Sampling:** what was actually sent to the model. The agent loop sends only
  `num_predict`, not the Boss model's sampling settings, and the record says so rather
  than assuming.

### 3.5 Approvals during a benchmark: containment, not a sandbox

The agent asks the owner before a HIGH or CRITICAL tool call. A benchmark cannot ask, so
`app/bench/policy.py` answers under a fixed policy and records every answer with its
reason:

- **LOW and MEDIUM tools** (read, list, search, find, write) never reach it: `RiskPolicy`
  allows them inside the workspace sandbox, as always.
- **Approved:** `delete_file` (the sandbox confines the path) and `run_command` (its own
  allowlist: inspection commands, read-only git, pytest, ruff, mypy).
- **`shell`, only within the benchmark command policy:**
  - `git` with a local subcommand (status, log, diff, show, rev-parse, ls-files, add,
    commit, mv, rm, restore), no option before the subcommand, and none of `--exec`,
    `--upload-pack`, `--receive-pack`, `--output`, `--ext-diff`, `--git-dir`,
    `--work-tree`, `--template`;
  - `pytest ...`, `python -m pytest|ruff|mypy ...` (the checks `run_command` already
    allows by name; added after the smoke run, which found `python -m mypy` refused
    while `mypy` was allowed), `python FILE.py ...`;
  - no shell metacharacters (`| ; & $ \` ! { } ( ) [ ] < > % ^ * ?`, newline), no
    absolute, `~` or `..` path, a command named by name; on Windows no single quote, which
    cmd does not read as a quote.

  So no unrestricted shell command is exposed to the benchmark: push, pull, fetch, clone,
  remote, config, reset, checkout, `python -c`, `python -m pip` and every other program are
  refused.
- **Denied:** `web_search`, `fetch_url`, and any tool not named above.
- A task the agent could only finish with something denied counts as a failure; the run
  record shows the denial and its reason.

**The limit, stated plainly.** This is benchmark containment, not a security sandbox.
There is no OS-level process isolation yet. A Python file or a test the agent writes and
then runs executes with the user's network access and file permissions; the policy
stops the benchmark from handing the agent the network or the rest of the disk through
its tools, not code the agent wrote from reaching them. Results and reports must not
describe the benchmark environment as sandboxed or network-isolated. The real sandbox is
roadmap item 8 and is not part of this ADR.

### 3.6 Runs and reading

- Each task runs **5 times** per side by default (D2), and a task's score is its success
  rate.
- A baseline is the current system on the rig. Later comparisons use the ADR-020 rule
  style: per task, never one averaged number. Their thresholds are derived the same way
  (amendment 1) before any comparison is read.
- The first baseline is read for **gaps**: which categories fail, in which language, at
  which step. That reading is what the next improvement is chosen from.

### 3.7 Safety

- Fixtures live in the repository and are small.
- Each run copies its fixture into a new temporary directory, the agent's workspace.
- The workspace sandbox (ADR-004 and #151) and the command allowlist apply unchanged.
- The tools give the agent no path to the owner's files, the owner's `core.db`, or the
  network. Code the agent writes and runs is not isolated (§3.5).

## 4. What this does not give

- **It measures this system on these tasks.** A good score is not general competence.
- **It cannot grade quality beyond its checks.** A correct but clumsy patch passes.
- **On the rig it is slow.** At 60–100 s per model call, a multi-step task takes minutes.
  The size of v0 is set by that (D1).

## 5. How it would be verified

- **Unit tests:** every check type on hand-built workspaces and answers, and the
  approvals policy.
- **End-to-end:** the runner with a scripted fake model that solves one task and fails
  another, so a broken check cannot pass.
- **Fixtures:** each one is validated by solving it with a scripted, correct "agent"
  (every check passes) and by an empty run (every check fails). A task whose checks
  cannot tell the two apart is not admitted.

## 6. Invariants

- No judge model decides a result.
- Result files are immutable. The Boss model and `pac` behaviour are unchanged.
- No network tool during a benchmark (§3.5 states what that does not cover). No schema,
  Neon or pgvector change.

## 7. Documentation

`ARCHITECTURE.md` §8 gains the TARGET line; `evals/README.md` gains the benchmark's run
instructions when it is built.

## 8. Decisions the owner is asked for

- **D1.** The size of v0. Proposed: **24 tasks**, 12 agent and 12 knowledge, each in
  English and Arabic, so 48 task-runs per pass. At 5 runs that is several hours on the
  rig. A smaller first cut (12 tasks) would get a baseline sooner.
- **D2.** Runs per task: 5 (proposed), or 3 for v0 to get a first reading faster.
- **D3.** The approvals policy in §3.5: approve inside the workspace, deny the network.

**Decided 2026-10-02 by the owner:**

- **D1:** 24 tasks, each in English and Arabic.
- **D2:** 5 runs per task: 120 runs per language, 240 in all.
- **D3:** approve automatically only what is scoped to the temporary benchmark workspace,
  and block network tools. Local git and Python/pytest only through the defined command
  policy; no unrestricted shell. Documented as containment, not a sandbox (§3.5); the
  full sandbox stays with roadmap item 8.
- **Scope:** v0 establishes a reproducible capability baseline and stays small; it is not
  to grow into a large evaluation framework yet.
- **Recorded per run:** success or failure; each check's verdict; tools used, in order;
  commands executed; verification result; stop reason; latency; tokens; the sampling
  settings actually sent; the final workspace and git state.
- **Citation is reported, not required (2026-10-02, after the smoke run).** A knowledge
  task succeeds on the fact, or on a correct decline. The `cites` check is
  `informational`: judged, recorded, and reported as its own rate, but not part of
  success. The grounding prompt never asks the model to name its source, so requiring
  it would have scored a missing prompt sentence as a retrieval failure.
- **After the baseline:** stop and report before implementing the next capability
  improvement. No LoRA or fine-tuning work until the benchmark shows that
  architecture, tool and model changes are insufficient.

## 9. Proposed units, if authorized

1. The task format, the check types and the approvals policy, with unit tests. No model.
2. The runner (`python -m personal_ai_core.app.bench`) for both tracks, with the
   end-to-end test on a scripted fake model.
3. The v0 task set and fixtures, each validated by a scripted solve and an empty run.
4. The baseline on the rig, committed as results, and a gap reading.

Each unit is its own PR.
