# Phase 2 — Knowledge Contract

**Status: types and contracts only. No implementation exists behind any protocol here.**

The Phase 2 vertical slice is:

```text
Document → Chunk → EmbeddingProvider → Index → Hybrid Retrieval (RRF)
        → Provenance → Context Budget
```

This step settles the contract. The in-memory implementation comes next; pgvector, Neon
and the Second Brain retrieval come later as adapters **behind** these protocols.

## The governing rule

> Do not rebuild what exists — but do not let what exists own the Core architecture.

`unified-llm-local @ 21a36b0765d89390eed63da094977e4bb8e5b4c2` holds a working hybrid
retrieval with real RRF, pgvector/HNSW and a tsvector lexical arm, and Neon holds real
indexed data. Those are assets to adapt. They are **not** the contract, and the contract is
written first precisely so that none of them can become it by being there already.

Nothing in `core/` names Postgres, pgvector, HNSW, tsvector, Neon, SQL or a vector
operator. `tests/test_dependency_direction.py` enforces that in code, with an adversarial
test proving the check is not vacuous.

## Types — `core/knowledge.py`

| Type | Contract it serves |
|---|---|
| `Document` | identity of a source, independent of its content |
| `DocumentVersion` | the exact revision a citation resolves against |
| `Chunk` | the retrievable unit, locatable back to its source |
| `Embedding` | a vector **plus** the model that produced it |
| `Candidate` | one chunk as proposed by one path, with that path's rank |
| `CandidateList` | one path's ordered output |
| `FusedCandidate` | post-fusion, with per-path contributions retained |
| `RetrievalProvenance` | why this chunk is here and where it came from |
| `RetrievalResult` | a chunk bound to its provenance |
| `RetrievalQuery` | what was asked, including language |
| `RetrievalMethod` | which path — semantic, lexical, fused |

### Decisions worth stating

**`Document` holds no content.** A document's identity must survive its text changing, or a
citation made last week cannot be resolved today. Content belongs to `DocumentVersion`,
identified by `content_hash`, which also makes re-ingestion idempotent.

**`Document != Chunk`.** Separate identities, separate lifetimes. A chunk is only meaningful
relative to the version it was cut from.

**The back-reference is deterministic.** `(version_id, start, end)` locates an exact
character span, so a citation can be re-read and checked rather than trusted. The audited
source carried provenance only to file level.

**Language lives on the chunk, not only the document.** A document is frequently mixed;
treating its declared language as authoritative is how Arabic content inside an English
document becomes unreachable (ADR-006). `RetrievalQuery.language` is explicit so a retriever
cannot quietly ignore it.

**An embedding carries its model.** Comparing vectors from two models is meaningless but
numerically silent. The type makes the mismatch detectable.

**Fusion consumes ranks, not scores.** Cosine similarity and a lexical score share no scale.

**Tie-breaking is part of the contract.** Equal fused scores order by ascending `chunk_id`.
Arbitrary, but *stable* — the same inputs must always produce the same order, or the ranking
is untestable.

**Provenance is a type, not a metadata dictionary.** It answers, without consulting anything
else: where did this originate, which version produced it, which exact text, which retrieval
path found it, what ranking selected it, and against which index build.

## Types — `core/context.py`

| Type | Contract it serves |
|---|---|
| `ContextBudget` | how many tokens evidence may occupy, and where that number came from |
| `ExclusionReason` | why a result did not make it in |
| `ExcludedResult` | a dropped result with its reason and cost |
| `BudgetedContext` | what was selected, what was excluded, and whether it fits |

**A budget is not a model's context window.** The Boss model's 8192 is a model property
recorded in `ModelRegistry`. A budget is what this system chooses to spend on retrieved
evidence after identity, history, tool observations and the generation reserve take their
share. ADR-005 records the audited source hard-coding 24000 for a 32K model; a budget that
silently exceeds what fits fails as truncation, the quietest failure there is. A budget is
therefore constructed explicitly and records its `source`.

**Exclusions are carried, not discarded.** A context that drops evidence without saying
which or why is indistinguishable from one that is working.

## Protocols — `core/contracts.py`

| Protocol | Responsibility |
|---|---|
| `EmbeddingProvider` | text → vectors; states `model_id` and `dimensions` |
| `Chunker` | content → chunks; must be deterministic |
| `VectorIndex` | semantic candidates; reports `index_version` |
| `LexicalIndex` | lexical candidates; takes `language` explicitly |
| `RankFusion` | ranked lists → one ranking; deterministic |
| `Retriever` | hybrid retrieval returning results with provenance |
| `TokenEstimator` | token cost; must be correct for Arabic and English |
| `ContextAssembler` | fit results to a budget, recording exclusions |

`VectorIndex` and `LexicalIndex` are separate because they are genuinely different
capabilities with different failure modes — and because the audited lexical arm was
hard-coded to English. Making `language` an explicit parameter forces an implementation to
decide rather than default silently.

`RankFusion` is where RRF will live. The audited RRF is classified `implemented, unproven`
with **zero** dedicated tests, so it is characterized before it is adapted, never assumed
correct.

## Not built in this step

In-memory index · real embedding provider · real chunker · RRF implementation · retriever ·
token estimator · context assembler · pgvector · Neon · HNSW · migrations · any import from
`unified-llm-local`.

## Next step

The in-memory implementation behind these protocols, with characterization tests written
before any legacy behaviour is adapted.
