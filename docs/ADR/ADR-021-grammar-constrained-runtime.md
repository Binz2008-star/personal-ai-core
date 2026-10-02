# ADR-021 — Forbidding foreign scripts during decoding (llama.cpp grammar)

**Status:** PROPOSED / DEFERRED · direction requested by the owner 2026-10-02 · §7
decided 2026-10-02 (D1 Phases A-C only, D2 GPU-only in production, D3 grammar only under
the guard's expected script) · deferred until the capability baseline (ADR-022) exists ·
nothing built · Phase D is a separate decision

- Serves:
  - ADR-012 amendment 1, the language rule;
  - ADR-019, the guard that catches a foreign script after the fact.
- Depends on:
  - ADR-020: a runtime change is measured before it is adopted;
  - ADR-002: the Boss model is unchanged.

## 1. The problem, as measured

The guard (ADR-019) catches a reply in the wrong script and regenerates once. On
`ground-decline-ar` the retry fails too often.

| Run | Commit | Result |
|---|---|---|
| #143 | 6565dd3 | 0 of 5 Chinese deliveries |
| Unit 4 (#148) | 5a88b0c | 5 of 10 |
| Acceptance (#154) | 29abb4e | 2 of 15 on one side, 9 of 15 on the other, identical weights |

This is the noisiest case in the set. A second retry would be one more sample from the
same distribution: the failure is in decoding, so the fix belongs in decoding.

llama.cpp accepts a GBNF grammar that makes a forbidden token impossible to sample.
`runtime/llamacpp/grammar.py` has one, `NO_FOREIGN_SCRIPT`: no CJK, Kana, Hangul,
fullwidth or Cyrillic. The evidence so far is thin:

| Grammar | Runs | Result | Conditions |
|---|---|---|---|
| none | 1 | 20 / 1 / 1 | CPU, 5ac65b2 |
| no-foreign-script | 1 | 22 / 0 / 0 | CPU, 5ac65b2 |

One run each, on the CPU, before the instrument and the gate were calibrated. That is a
reason to measure, not a result.

## 2. What is ruled out

- **A grammar in Ollama.** Ollama exposes JSON-schema `format`, not GBNF, and no logit
  bias. Constrained decoding means llama.cpp.
- **More retries.** Each retry is one more sample from the same distribution, at the cost
  of one more generation.
- **Disabling Smart App Control** to run the CUDA build. It is irreversible, and the
  owner's standing constraint keeps it on.
- **An unconditional grammar in `pac`.** `NO_FOREIGN_SCRIPT` would also stop a reply the
  user asked for in Chinese or Russian. It must follow the same exemptions as the guard
  (ADR-019 §3.4): applied only when the expected script is Arabic or Latin and the turn is
  not a translation request.

## 3. Phases

Each phase is gated on the one before. No phase changes what `pac` runs until §3.4.

### 3.1 Phase A: feasibility on the rig (no code)

Can `llama-server` use the GPU on the rig with Smart App Control on? Two things to try:

1. **The Vulkan build** of llama.cpp: Ollama already runs on Vulkan there.
2. **The CPU build**, which is known to run, measured at the same `num_ctx`.

Record which one runs, tokens per second, and seconds per `contract_v1` run, next to
Ollama's figures (about 75 s per run, 85% GPU). If only the CPU build runs and it is
several times slower, `pac` would get slower for every turn. That is a product cost, and
the owner weighs it at D1.

### 3.2 Phase B: one comparison tool flag (code, small)

`app.compare` refuses two sides that differ in any settings field. For a runtime
question the field under test must differ, and nothing else may.

`--vary FIELD` (`grammar` or `runtime`) allows exactly that one field to differ. Every
other field is still required to match. The report names the varied field and both
values. It is the same gate and the same rule; only one key field is released, by name.

### 3.3 Phase C: the measurement (rig)

Both sides on llama.cpp, same commit, same weights (the same Ollama blob, already
verified to give the same digest), guard on:

- baseline: `--grammar none`;
- candidate: `--grammar no-foreign-script`;
- 15 contract + 9 refusal runs per side (ADR-020 amendment 1).

Read with `app.compare --vary grammar`:

- **Gate:** the grammar must not regress any case. It is a constraint on output, so a
  regression would show it breaking something it should not touch, such as a code
  block, a quoted name or a refusal answer.
- **What it is for**, reported per case: the Chinese and Cyrillic failures, above all
  `ground-decline-ar`, and how often the guard still fires.

### 3.4 Phase D: adoption (code, and the owner's decision)

Only on a passed Phase C and the owner's D1. `pac` would then run on llama.cpp:

- the grammar is applied per turn, under the guard's exemptions;
- the guard stays as a second line;
- the adapter, the digest and the provider label already exist (ADR-020 units 1-2).

What does not exist yet, and would be its own units:

- who starts and stops `llama-server` for `pac`;
- how `pac` finds the Ollama blob for the Boss model's weights;
- what happens when the server is not running.

## 4. What this does not give

- **It removes scripts, not languages.** Persian in reply to Arabic passes, as with the
  guard (ADR-019 §4).
- **It does not make an answer right.** A decline forced into Arabic can still be a wrong
  decline. The scorer's `declines` check still judges that.
- **Unknown on this rig:** speed. Phase A decides whether the cost is acceptable.

## 5. Invariants

- The Boss model and its weights are unchanged (ADR-002).
- No schema, migration, Neon or pgvector change.
- Smart App Control stays on. No driver or system setting changes without the owner.
- Result files stay immutable.

## 6. Documentation

`ARCHITECTURE.md` §8 gains this design's TARGET lines. Its CURRENT lines change only
when a phase lands.

## 7. Decisions the owner is asked for

- **D1.** Authorize Phases A to C: feasibility, the `--vary` flag, the measurement.
  Phase D is a separate decision, taken on their results.
- **D2.** The acceptable speed cost if only the CPU build runs: a ceiling in seconds per
  reply, or "GPU only".
- **D3.** Whether the grammar, once adopted, applies only when the guard's expected script
  is Arabic or Latin and the turn is not a translation request (proposed), or always.

**Decided 2026-10-02 by the owner:**

- **D1:** Phases A to C are approved, and only those. Phase D is a separate decision,
  taken after the Phase C results are reviewed.
- **D2:** Production stays GPU-only. Phase A may measure the CPU build to establish the
  performance gap. CPU never becomes the production fallback if the Vulkan/GPU path is
  not viable.
- **D3:** The grammar applies only when the guard expects Arabic or English. It does not
  apply when the user's explicit request needs another language, translation and
  Chinese included. It follows the guard's existing exemptions, and only those.
- **Phase C stays controlled.** Same model, same prompts and test set, same hardware and
  runtime conditions, same calibrated gate. The grammar is the only intended variable.
- **Deferred.** The owner put measurement first (ADR-022, the capability benchmark).
  Phases A to C are revisited once that baseline exists. Nothing here reaches production
  without its validation and the owner's review.

## 8. Proposed units, if authorized

1. Phase A on the rig, reported as a note (no PR).
2. `app.compare --vary FIELD`, with tests.
3. Phase C on the rig, committed as results.
4. Phase D, its own ADR amendment and units, only on D1 after Phase C.
