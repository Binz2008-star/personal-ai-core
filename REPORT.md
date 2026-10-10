# Live Windows Boss comparison — run report (2026-10-10)

Operator: Windows agent (this machine). Handoff files: `ai-core-checkpoint-2026-10-10.md`,
`ai-core-windows-exact-read-pilot.py` (both on the OneDrive Desktop).

Status: **evidence gathered only. Both PRs remain DRAFTS. No merge, no production-readiness claim.**

## Environment (verified, identical for both sides)

- PowerShell 7.6.6; git 2.55.0; Python 3.14 venv with pytest 9.1.1 (project has zero runtime deps).
- Boss: `huihui_ai/qwen2.5-abliterate:7b` (base tag; both `:7b` and `:7b-q4ks` are present locally, the pilot pins the base tag).
- Loaded weights digest `sha256:212345411ab817a8d7669ed63f24a30f2e2a2cd297455a6aad1de50c82f7691c`,
  manifest `sha256:103482475c9b4d4032999dd5d7383478cad8b966ae6ae984d525daf7352c2219`,
  `context_length = 8192`, `context_mismatch = false`, `gpu_share 0.84`, quant `Q4_K_M`.
- `num_ctx` recorded both as measured (8192) and sent by core (8192) in every header.
- Venv `Scripts` prepended to PATH for both sides. Isolated clone + scratch workspaces only;
  the real clone `C:\Users\loyal\personal-ai-core`, `profile.md` and `core.db` were never touched.

Run root: `C:\Users\loyal\AppData\Local\Temp\opencode\pac-boss-review-20261010`

## Comparison A — command guidance (#259)

Tasks `git-last-commit-file`, `verify-off-by-one`; 5 runs x EN/AR.

| Side | Commit | Overall | git | verification | EN | AR |
|---|---|---|---|---|---|---|
| baseline | `a43c6f4` | 4/20 (20%) | 4/10 | 0/10 | 1/10 | 3/10 |
| guidance | `ab448b5` | 7/20 (35%) | 7/10 | 0/10 | 3/10 | 4/10 |

- The guidance text reaches the model inside the refusal: *"shell metacharacter in: |. run_command has
  no shell. Use one command per tool call, without pipes, chaining or redirection, and read the returned
  output directly."*
- Recovery to allowed syntax is observable on the guidance side, e.g. `git-last-commit-file ar run 2`
  (pipe refused -> `git show -U1` -> correct "README.md") and `ar run 5` (`%T.{*}` refused ->
  `git show --name-only -1` -> correct). Baseline frequently repeated the same refused pipe to budget.
- `verify-off-by-one` is 0/10 on BOTH sides. Its failures are not the pipe issue (see obstacles below):
  the model never reads/edits `calc.py`; it invents pytest flags and hits two environment walls.
- Failure signals (baseline -> guidance): no_answer 13->13, action_rejected 9->9, tool_refused 4->4,
  protocol_errors 2->4, failed_steps 13->12, check:command_passes 10->10,
  check:tested_after_last_edit 10->10. Two guidance runs took 71-72s (long retry loops).

Interpretation: small-sample (n=5) but consistent with the intended effect — the hint converts some
unrecoverable pipe loops into recovered allowed calls on the git task. No effect on the verification task.

## Comparison B — exact-read pilot (#260)

Fixtures frozen once (`exact-read-fixtures.json`, 10 cases; 2 multiline + 2 spaced, EN + AR, verified
intact UTF-8 Arabic). Candidate ran `--exact-read notes.txt`.

| Side | Commit | accepted_fabrication | useful_exact_copy | accepted_without_target_read | source_changed |
|---|---|---|---|---|---|
| baseline | `a43c6f4` | **9/10** | 0/10 | 0 | 0 |
| candidate | `086c355` | **0/10** | 1/10 (`ar-1`) | 0 | 0 |

- Baseline: every case read `notes.txt` (read_file executed/verified/untruncated), then the answer was a
  paraphrase or fabrication in 9/10 (e.g. dropped the `Reference:` token, "The quick brown fox...",
  stripped leading/trailing spaces). `ar-5` ended in budget with no answer.
- Candidate: the new control fires as `kind=exact_read_required`. Near-misses are refused, e.g.
  `en-2` "This is the content of notes.txt: ..." and `ar-5` "note: the delivery office is on the third
  floor..." (wrong language / rewritten case). Only `ar-1` produced the byte-exact copy.
- **No false refusals found**: every rejected answer was a genuine mismatch (dropped token, collapsed
  newline, stripped whitespace, wrong language, added prose). The control is sound.
- Candidate exit codes: 9/10 = 1 (safe budget stop), 1/10 = 0 (ar-1). Target "zero accepted fabrications"
  and "zero accepted answers without the target read" met. Useful exact-copy is deliberately not counted
  for refusals, so 1/10 is the honest useful-success figure.

Interpretation: #260 converts silent fabrications into safe stops; it does not raise the model's ability
to reproduce bytes. Meets its declared target; the model rarely copies exactly.

## Comparison C — existing `--verify-completion` control (separate intervention)

Same #259 source as guidance (`ab448b5`), flag on vs off (off = `commands-guidance`).

| Side | Overall | git | verification |
|---|---|---|---|
| flag off | 7/20 (35%) | 7/10 | 0/10 |
| flag on | 7/20 (35%) | 7/10 | 0/10 |

- **0 verification_rejections** across all 20 runs, and `edits=[]` on every `verify-off-by-one` run: no
  run ever wrote `calc.py`, so the completion gate had nothing to verify. The flag changed nothing here.

## Environment obstacles observed (NOT caused by either PR)

1. `run_command` allowlist refuses `python` by name (only `pytest` is allowed), while the model strongly
   prefers `python -m pytest`; the same command is then accepted via `shell`. This wastes steps on the
   verification task.
2. Bare `pytest` intermittently failed with `OSError: [WinError 4551] An Application Control policy has
   blocked this file` — a machine-level Application Control policy, not the agent's policy.
3. The model invents pytest flags (`--fix`, `-f`, `--no-failures`, `--pyargs`, ...) and non-allowlisted
   commands (`cat`, `xargs`, `icacls`, `powershell`, `Start-Process`), which the policy refuses.
4. The action-required gate bounces premature `{"answer": ...}` replies; `action_rejections=1` is normal
   and expected, not a defect.

## Artifacts (run root, returned to reviewer)

- `commands-baseline/bench-*.jsonl`, `commands-guidance/bench-*.jsonl`, `completion-on/bench-*.jsonl`
  (header + 20 runs + end each); `*.log` per phase; `CompareA/B/C-driver.log`.
- `exact-read-baseline.jsonl`, `exact-read-candidate.jsonl` (10 records each).
- `exact-read-fixtures.json` (frozen fixture manifest).
- `analyze_A.txt`, `analyze_B.txt`, `analyze_C.txt`, `analyze_C_summary.py` (read-only analyses).
- `CompareA.ps1`, `CompareB.ps1`, `CompareC.ps1` (exact drivers used).

No draft was merged and no production-coding reliability is claimed from harness completion or refusals.
