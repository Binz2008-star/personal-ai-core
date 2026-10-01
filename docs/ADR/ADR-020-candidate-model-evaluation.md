# ADR-020 — Evaluating a candidate model

**Status:** PROPOSED · writing authorized by the owner 2026-10-01 ("موافق على ADR-020") ·
§8 decided 2026-10-01 ("موافق على الأربعة": D1 authorized, D2 and D3 as written, D4 required) ·
units in §9 being built

- Settles: `ARCHITECTURE.md` OD-8, "how a candidate model is named and admitted".
- Serves:
  - ADR-002: replacing the Boss model is "a configuration change plus an evaluation gate";
  - ADR-002 owner note (2026-10-01): a candidate must not refuse more;
  - ADR-013: the harness.
- Inputs: the read-only review of 2026-10-01, whose six points are §3.1 to §3.6.

## 1. The problem

ADR-002 allows a candidate to replace the Boss model through an evaluation gate, but no
gate exists. Today:

- **A candidate cannot be evaluated under its own name.** The harness refuses any
  `PAC_BOSS_MODEL` other than the Boss model's.
- **Results do not name the weights.** The Ollama path records a tag, which can be
  re-pointed to different weights. The llama.cpp path records a file name, which can be
  reused.
- **llama.cpp runs are mislabelled.** The registry hard-codes `provider="ollama"`, so they
  are recorded as Ollama runs.
- **Nothing defines a regression.** Identical configurations have scored 18 to 22 of 22, so
  one run against one run proves nothing.

The first candidate in view is a fine-tune of the Boss model (LoRA). Fine-tuning is not
decided by this ADR, but no fine-tune can be judged until this gate exists.

## 2. What is ruled out

- **Judging by one run.** The variance above is larger than most effects we care about.
- **A judge model.** ADR-013 keeps verdicts mechanical. A candidate judged by a model
  inherits that model's failures.
- **Letting evaluation change what `pac` runs.** Evaluating a candidate and adopting it
  are separate acts with separate authority (§3.6).

## 3. Proposed design

### 3.1 Weights are identified by digest

Every evaluation run records the **digest of the weights** that produced it, alongside the
name. A name or file name alone is not trusted.

- **Ollama:** the model's digest as Ollama reports it (`/api/show` or `/api/tags`). For a
  blob path, the `sha256-…` blob name *is* the content digest.
- **llama.cpp:** the SHA-256 of the GGUF file named by `/props` `model_path`, computed once
  per file and cached by path, size and modification time.
- **Unconfirmed digest:** if the digest cannot be confirmed, the run is recorded as
  `weights_unverified`. The comparison in §3.5 refuses it.

### 3.2 The provider is recorded from the adapter that ran

`ModelSpec.provider` stops being a constant. The header and `GENERATION_REQUESTED` record
the name of the provider object that served the turn (`ollama` or `llamacpp`). The
registry's hard-coded `"ollama"` is removed.

### 3.3 A regression is defined per case, over runs

- **Runs:** baseline and candidate each have at least **5 contract runs** and **3 refusal
  runs**, under identical settings (§3.4).
- **Contract set:** a case **regresses** if its failure count across runs rises by at least
  2 compared with the baseline. One extra failure in five is within the observed noise. A
  case failing in every candidate run regresses whatever the baseline was.
- **Refusal set:** as `evals/README.md` already says, a case counts as refused if any run
  refuses it. **Any refusal the baseline did not have is a regression** (ADR-002 owner
  note), weighted like a contract regression.
- **Gate result:** the gate reports per-case deltas, never a single averaged number.
  - It **passes** only with zero regressions in either set.
  - Improvements are reported but cannot buy back a regression.

### 3.4 The guard and every setting are held equal

- The language guard (ADR-019) and the contract's script check measure the same property,
  so a guarded run partly passes by construction. Baseline and candidate therefore run
  with the **same guard setting**.
- The comparison also includes **one unguarded pair**: 5 runs each with
  `--no-language-guard`. This shows what the weights do on their own.
- Sampling, identity variant, `num_ctx`, runtime and hardware condition (CPU or GPU share)
  are part of the comparison's key. A key mismatch refuses the comparison (§3.5).

### 3.5 The instrument is frozen during a comparison

- A comparison is valid only if baseline and candidate share:
  - the **cases file version**;
  - the **scorer version**;
  - the settings key in §3.4.
- The comparison tool refuses mixed inputs and names the field that differs.
- **No change to the instrument during a comparison.** A change to a case file or the
  scorer starts a new comparison: the baseline is re-run (or rescored, where `--rescore`
  suffices) under the new instrument before any candidate is judged by it. This is the
  `ARCHITECTURE.md` target: "a change to the instrument is never approved by the result it
  produces".

### 3.6 Evaluating is not adopting

- **Evaluating:** the harness gains `--candidate NAME`. It evaluates that model under its
  own name and digest, and the result header marks it `role: candidate`. The guard that
  refuses other model names stays for ordinary runs, so a mistyped
  `PAC_BOSS_MODEL` still cannot produce a result filed under the Boss model.
- **Pac is untouched:** nothing in this ADR changes what `pac` runs.
- **Adopting:** adoption is a separate owner decision under ADR-002. It is made on a
  passed gate (§3.3), and recorded by changing `DEFAULT_BOSS_MODEL` in `core/config.py`
  and ADR-002 in one PR, citing the comparison's result files.

### 3.7 The comparison tool

A new read-only command, `python -m personal_ai_core.app.compare BASELINE_DIR CANDIDATE_DIR`:

- It reads committed result files only and writes one comparison report.
- It calls no model.
- It checks §3.5 first, then computes §3.3.

## 4. What this does not give

- **Quality.** The gate measures the contract and refusals, not whether answers are good.
  The refusal baseline already showed passing replies that did not answer well, for
  example `refusal-euthanasia-ar`. A quality instrument is separate work.
- **Generalisation.** 22 contract cases and 14 refusal cases are a small instrument. A
  passed gate means "no regression on this instrument", nothing wider.
- **Thresholds from statistics.** "At least 2 more failures in 5 runs" is a rule of thumb
  sized to the observed 18-to-22 spread, not a significance test. §8 D2 lets the owner
  change it.

## 5. How it would be verified

- **Unit tests:**
  - digest capture for both runtimes, using recorded `/api/show` and `/props` responses
    and a small file for the hash;
  - the provider recorded from the adapter;
  - `--candidate` naming and the `role` field;
  - the comparison tool's refusals (cases version, scorer version, settings key,
    `weights_unverified`);
  - the per-case regression arithmetic on hand-built result files.
- **First real use:** compare the Boss model against itself. Baseline and "candidate" are
  two separate sets of runs of the same weights. The gate must **pass**, and the size of
  the deltas measures the instrument's own noise. Run this before any real candidate.

## 6. Invariants

- The Boss model and what `pac` runs are unchanged by anything here (ADR-002, invariant 3).
- No schema, migration, Neon or pgvector change.
- Result files stay immutable. The comparison report is a new file.

## 7. Documentation

`ARCHITECTURE.md` is updated in the same change as this ADR:

- OD-8 points here;
- §8 gains this design's TARGET lines.

The CURRENT lines change only when units are built.

## 8. Decisions the owner is asked for

- **D1.** Authorize the design (units in §9), send it back, or reject it.
- **D2.** The regression rule: at least 2 more failures in 5 runs per contract case, and any
  new refusal. Keep it, or set another.
- **D3.** Run counts: 5 contract and 3 refusal runs per side, plus one unguarded pair of 5.
  Keep them, or set others.
- **D4.** Whether a passed gate is required before adoption (proposed), or advisory. This is
  `ARCHITECTURE.md` OD-3, for models only.

**Decided 2026-10-01.** The owner approved all four as proposed: the design is
authorized, the regression rule and run counts stand as written, and a passed gate is
required before a candidate is adopted.

## 9. Units

1. Weights digest and provider label (§3.1, §3.2): recorded in headers and events, with
   tests. No behaviour change.
2. `--candidate` (§3.6), with tests.
3. The comparison tool (§3.3 to §3.5, §3.7), with tests.
4. The self-comparison on the rig (§5), committed as results.

Each unit is its own PR.
