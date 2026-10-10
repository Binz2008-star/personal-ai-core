# ADDENDUM — raw-evidence answers to checkpoint review points (2026-10-10)

Reply to the reviewer section "Latest owner-reported live comparisons" in
`ai-core-checkpoint-2026-10-10.md`. This document cites the raw records now packaged
beside it. Everything below is read directly from the JSONL, not from the summary.

## Reviewer point: attribute WinError 4551 correctly (agent step vs post-run check)

Finding: `OSError [WinError 4551] An Application Control policy has blocked this file`
appears ONLY inside agent tool steps, never in a post-run `command_passes` check.

Every occurrence (file / task / language / run / step / tool / command):
- commands-baseline: verify-off-by-one en run2 step2 run_command `pytest -f test_calc.py`
- commands-baseline: verify-off-by-one en run3 step1 run_command `pytest -k test_calc -v --lf --no-capture`
- commands-baseline: verify-off-by-one en run3 step3 run_command `pytest -k test_calc -v --lf --no-capture`
- commands-baseline: verify-off-by-one ar run3 step1 run_command `pytest -f calc.py --collect-only`
- commands-guidance: verify-off-by-one en run5 step2 run_command `pytest test_calc.py`
- completion-on: verify-off-by-one en run2 step1/step3 run_command `pytest -q -p no:cacheprovider test_calc.py`
- completion-on: verify-off-by-one en run3 step1/step3 run_command `pytest -q -p no:cacheprovider test_calc.py`
- completion-on: verify-off-by-one en run4 step1 run_command `pytest -q test_calc.py --tb=short --ff`
- completion-on: verify-off-by-one en run5 step1 run_command `pytest -q -p no:cacheprovider test_calc.py`
- completion-on: verify-off-by-one en run5 step2/step3 shell `pytest …` (reported as `exit code 4551`)

Conclusion: the blocked executable is the bare `pytest` the agent's `run_command`/`shell`
invoked (the venv `Scripts\pytest.exe` console-script shim). It is NOT the benchmark's
`command_passes` post-run check, which uses `sys.executable -m pytest`. Do not attribute
this to the model or either PR, and do not weaken the allowlist or add a Python fallback.

## Reviewer point: separate a missing executable from an allowlist denial (`cat`)

Finding, same named command, two different mechanisms:
- run_command `cat test_calc.py` -> error `command not installed: cat` (allowlisted name,
  executable absent on Windows). completion-on verify-off-by-one en run1 step1.
- shell `cat test_calc.py` (en run1 step2) and `cat test_calc.py calc.py` (ar run1 step1)
  -> refused by benchmark policy: `not confirmed by the user; nothing ran`.

So `cat` was never an allowlist denial under run_command; it is a missing Windows binary.
The shell arm did deny it under the benchmark containment policy.

## Binding metadata (all three bench files agree)

| File | Commit | verify_completion | model | num_ctx sent/measured | weights digest | manifest |
|---|---|---|---|---|---|---|
| bench-...111502Z (baseline) | a43c6f4 | False | :7b | 8192/8192 | sha256:212345411ab8…f7691c | sha256:103482475c9b…c2219 |
| bench-...111742Z (guidance) | ab448b5 | False | :7b | 8192/8192 | sha256:212345411ab8…f7691c | sha256:103482475c9b…c2219 |
| bench-...112826Z (completion) | ab448b5 | True | :7b | 8192/8192 | sha256:212345411ab8…f7691c | sha256:103482475c9b…c2219 |

`context_mismatch=false`, `weights.verified=true`, scorer `bench-checks-v2` on all three.
Task digests are identical across phases (git-last-commit-file `99f9e4dc2ec05b4f`,
verify-off-by-one `9e4d394b968de4ba`).

## Notes for the reviewer

- Exact-read observer metadata (per B record): `configured_context=8192`,
  `loaded.context_length=8192`, `model_details.quantization_level=Q4_K_M`,
  `family=qwen2`, `parameter_size=7.6B`, no probe error.
- `A` guidance hint text observed verbatim in refusal reasons: "…run_command has no shell.
  Use one command per tool call, without pipes, chaining or redirection, and read the
  returned output directly."
- `B` candidate refusals are `kind=exact_read_required`; no rejected answer was a byte-exact
  copy (checked each), so no false refusals were found.
- The approximately 71-second guidance retry loops are `verify-off-by-one en/ar run 2` in
  commands-guidance (long invented-flag retry chains), not the git task.
