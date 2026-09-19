# Testing Strategy

**Status: Phase 0 — design.**

---

## 1. Why this document is strict

Two verified findings set the tone.

**VERIFIED SOURCE FACT.** In `unified-llm-local` @ `21a36b0`, the RAG path is implemented
— hybrid search is genuinely called at `brain_agent_v4.py:249` — and is almost entirely
**unproven**: RRF has **0** test files, the chunker **0**, tsvector **0**. Its ~3,900-line
test suite is concentrated on security (1,732), rollback (796), merge lock (409) and
evidence (407).

**VERIFIED SOURCE FACT.** In Rico, three extraction candidates have **no tests at all**:
`rico_nlu.py`, `rico_quality.py`, `rico_tool_registry.py`.

The lesson carried into this repository: **implementation presence is not maturity, and a
clean summary is not evidence.** Test coverage is recorded per component in the extraction
matrix and is a promotion criterion, not a nice-to-have.

## 2. Characterization before extraction

**ARCHITECTURAL DECISION.** No component is extracted before its current behaviour is
pinned.

```text
source @ recorded SHA → characterization tests → freeze → extract → adapt → verify
```

Characterization tests describe what the code *does*, not what it *should* do. They live in
`tests/extraction/<source>/` and cite the source SHA. They are the safety net that makes a
refactor provable rather than hopeful.

## 3. Levels

| Level | Scope | Gate |
|---|---|---|
| **L1 Unit** | pure functions, contracts | every push |
| **L2 Contract** | adapters satisfy Core protocols | every push |
| **L3 Integration** | subsystems with real storage | every push |
| **L4 Regression** | golden set, no behaviour loss | every push |
| **L5 Evaluation** | retrieval, memory, agent, Arabic, coding quality | before promotion |
| **L6 End-to-end** | full vertical slice | before release |

## 4. Required coverage per subsystem

**Retrieval** — this is where the sources are weakest, so it is specified hardest:
RRF ranking correctness on known inputs · vector recall · lexical recall ·
**Arabic lexical retrieval** · hybrid beating either arm alone · deduplication ·
provenance survival from document → chunk → result → context · empty and adversarial
queries.

**Embeddings** — provider abstraction honoured, dimensionality asserted, batching,
failure and timeout handling, no direct HTTP call outside the provider.

**Chunking** — deterministic, boundary-safe, metadata preserved, non-code documents,
Arabic text not corrupted.

**Context** — budget never exceeded, budget derived from the active model, compression
preserves evidence, tokenizer-backed counting for both Arabic and English.

**Memory** — `Event != Memory` enforced (a conversation turn cannot write persistent
memory), promotion rules, confidence thresholds, supersession, provenance completeness.

**Agent and tool security** — policy gate cannot be bypassed, path containment holds
against traversal, risk levels enforced, audit record written for every call, timeouts,
idempotency, rollback restores state.

**Model** — registry swap changes no code path, context window respected, promotion gate
blocks a regressing candidate.

## 5. Multilingual is a first-class test axis

**ARCHITECTURAL DECISION.** Arabic is not an afterthought suite.

**VERIFIED SOURCE FACT.** The audited lexical search hard-codes
`to_tsvector('english', …)` and `websearch_to_tsquery('english', …)`; the audited context
builder estimates tokens at `CHARS_PER_TOKEN = 4`. Both silently degrade for Arabic —
they do not error, they return worse results. Only a test catches that.

Every retrieval, memory, context and evaluation suite carries Arabic cases alongside
English. A mixed Arabic/English query is a required case, not an edge case.

## 6. Golden regression set

`tests/evaluation/golden/` — fixed cases covering general reasoning, coding, Arabic,
memory recall, retrieval grounding, tool use, refusal behaviour, long conversation and
context budgeting.

Any change to model, runtime, retrieval or memory must pass it. Evaluation must run through
**the same retrieval and prompting path as production** — a principle taken from
`rag-engine`, which had previously fixed exactly that split. A separate evaluation pipeline
proves nothing about the runtime.

## 7. Evidence handling

Raw run output is committed unmodified as evidence and never edited to match a conclusion.
Re-scoring happens in a separate derived file that names its source. A results file states
the machine it came from — every number here is hardware-bound.

## 8. CI

Unit, contract and regression run on every push. Evaluation runs before promotion. Any bug
found in a pure function is pinned by a regression test in the same change that fixes it.
