# ADR-007 — The adapter/training pipeline is built, not extracted

**Status:** Accepted · Phase 0

## Context

The original master plan treated `rag-engine` as the source of a training lifecycle, citing
`training/dataset_builder.py`, `training/train_intent.py`, `training/registry.py` and
`workers/trainer.py`.

**VERIFIED SOURCE FACT.** `train_intent.py` @ `16f6279` is 37 lines:

```python
Pipeline([
    ("tfidf", TfidfVectorizer(ngram_range=(1, 2))),
    ("clf", LogisticRegression(max_iter=500)),
])
```

That is a scikit-learn **intent classifier**, not LLM or LoRA training.
`dataset_builder.py` (45 lines) builds datasets for that classifier, coupled to an
`event_type`/`intent`/`failure_type` schema. `registry.py` is 53 lines.

## Decision

The LLM adapter/training pipeline is designed and built independently. `rag-engine` is
**not** described as an LLM training source anywhere in this repository.

The sklearn classifier may be useful for **intent routing** — a different job, a separate
decision, not a substitute.

`rag-engine` remains a genuine source for evaluation, shadow evaluation, eval gating,
metrics and drift detection, where its implementations are substantive and tested.

## Consequences

Phase 5 is larger than the plan assumed. It stays last, and starts only once memory,
knowledge, events, feedback, evaluation and golden sets exist — training without them
injects noise that cannot be measured.
