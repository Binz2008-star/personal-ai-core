# Learning Architecture

**Status: Phase 0 — design. Largely DESIGN TO BUILD.**

---

## 1. The rule that shapes everything

**ARCHITECTURAL DECISION.** A normal conversation never modifies model weights.

Learning is offline, batched, evaluated, versioned and rollbackable. A conversation
produces *evidence*; evidence produces *candidates*; only an evaluation gate promotes a
candidate.

## 2. Pipeline

```text
              CONVERSATION
                    │
                    ▼
                  EVENT                  immutable, append-only
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
      FEEDBACK            OBSERVATION
          │                   │
          └─────────┬─────────┘
                    ▼
               EXPERIENCE               normalized outcome
                    │
      ┌─────────────┼─────────────┐
      ▼             ▼             ▼
   MEMORY      KNOWLEDGE      TRAINING
  CANDIDATE       GAP          EXAMPLE
      │             │             │
      └─────────────┼─────────────┘
                    ▼
                EVALUATION
                    │
           ┌────────┴────────┐
           ▼                 ▼
        PROMOTE           REJECT
```

Three distinct kinds of learning, deliberately kept separate:

- **Memory learning** — the system learns *you*. Cheap, fast, reversible.
- **Knowledge learning** — the system learns *information*. Ingest and re-index.
- **Model learning** — the system learns *behaviour*. Expensive, gated, last resort.

Most improvement should come from the first two. Reaching for weights first is the
expensive way to fix a retrieval problem.

## 3. Events

**DESIGN TO BUILD.**

**VERIFIED SOURCE FACT.** No adequate source exists. Rico has no event module.
`rag-engine` has `events/store.py` (52 lines), `events/schema.py` (32) and
`events/replay.py` (**4 lines** — a one-function wrapper, classified `DROP`).

Events are append-only and never mutated. Minimum shape: `id`, `session_id`,
`message_id`, `type`, `payload`, `occurred_at`, `actor`. Deleting or editing an event is
not a supported operation; corrections are new events.

## 4. Feedback

Structured signals, not free text:

```text
GOOD · BAD · WRONG · WRONG_SOURCE · TOO_SLOW · FALSE_REFUSAL
CORRECTION · REMEMBER_THIS · FORGET_THIS · PREFERENCE · KNOWLEDGE_GAP
```

The first six are adapted from `rag-engine`'s verified label set; the rest are added for
personal use. Each maps to a defined downstream effect — a feedback label with no effect
is a bug.

## 5. Knowledge-gap detection

**ADAPT** from `rag-engine/analysis/drift_detector.py` (249 lines — substantive, verified).

```text
repeated query → low-evidence answers → gap recorded
      → research / ingest → re-index → evaluate
```

This is how the system gets smarter without touching weights, and it is why gap detection
is built before any training pipeline.

## 6. Training

**DESIGN TO BUILD.**

**VERIFIED SOURCE FACT.** `rag-engine/training/train_intent.py` is 37 lines of
`TfidfVectorizer` + `LogisticRegression` — an sklearn **intent classifier**, not LLM or
LoRA training. The original plan treated it as a training lifecycle to reuse. It is not.
The adapter pipeline is designed from scratch.

The sklearn classifier may still be useful for **intent routing**, which is a different
job and a separate decision.

```text
experience → curated dataset → quality gate → training candidate
   → adapter → evaluation vs golden set → promote | reject → rollback point
```

Preconditions before any training work begins: memory, knowledge, events, feedback,
evaluation and golden sets must all exist. Training on top of missing layers injects noise
and cannot be evaluated.

## 7. Evaluation gates

**ADAPT** from `rag-engine`, the only source with substantive evaluation tests (22 files):
`evaluation/eval_gate.py` (81), `shadow_evaluator.py` (248), `eval_main.py` (144),
`metrics.py` (57).

A candidate is promoted only if it passes the golden set, shows no regression, and was
evaluated through **the same retrieval and prompting path production uses**.

## 8. Registry

```text
models · model_versions · datasets · training_examples · training_runs
evaluations · evaluation_cases · evaluation_runs · promotions
```

Every promoted version is rollbackable. Every rejection keeps its evaluation record.

## 9. Extraction plan

| From | Take | Class |
|---|---|---|
| `rag-engine` `analysis/drift_detector.py` | gap detection | **ADAPT** |
| `rag-engine` `evaluation/*` | gates, shadow eval, metrics | **ADAPT** |
| `rag-engine` `events/*` | schema ideas only | **REWRITE** |
| `rag-engine` `events/replay.py` | — | **DROP** (4 lines) |
| `rag-engine` `training/train_intent.py` | not LLM training | **DROP** for this purpose |
| `Rico` `src/feedback_loop.py` (337, 2 domain refs) | collect → analyse → learn → persist → schedule lifecycle | **ADAPT** (1 test file only) |
| — | events, experience, candidates, promotion, adapters | **BUILD** |
