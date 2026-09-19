# Phase 2, step 3 — In-memory implementations behind the contracts

Status: **implemented and tested.** No Neon, no pgvector, no HNSW, no network, no runtime
dependency, and no import from any legacy repository.

The contracts were settled in `PHASE_2_CONTRACT.md` and the legacy retrieval behaviour was
pinned in `RRF_CHARACTERIZATION.md` before a line of this was written. This document records
what was built, what was deliberately inherited from the audited system, what was
deliberately refused, and — the part that matters most — what these implementations honestly
**do not** do.

## What was built

| Module | Class | Contract |
|---|---|---|
| `knowledge/chunking.py` | `FixedSizeChunker` | `Chunker` |
| `knowledge/embedding.py` | `HashingEmbeddingProvider` | `EmbeddingProvider` |
| `knowledge/vector_index.py` | `InMemoryVectorIndex` | `VectorIndex` |
| `knowledge/lexical_index.py` | `InMemoryLexicalIndex` | `LexicalIndex` |
| `knowledge/fusion.py` | `ReciprocalRankFusion` | `RankFusion` |
| `knowledge/retrieval.py` | `HybridRetriever` | `Retriever` |
| `knowledge/ingestion.py` | `IngestionService` | composition of the above |
| `knowledge/catalog.py` | `InMemoryChunkCatalog` | chunk lookup for provenance |
| `knowledge/text.py`, `knowledge/language.py` | — | shared, language-neutral policy |
| `context/token_estimator.py` | `ScriptAwareTokenEstimator` | `TokenEstimator` |
| `context/assembler.py` | `GreedyContextAssembler` | `ContextAssembler` |

Two new layers, both permitted to import `core` and nothing else, enforced by
`tests/unit/test_dependency_direction.py`.

## Inherited from the audited system, on purpose

| Behaviour | Why it was kept |
|---|---|
| `k = 60` in RRF | The constant from the original RRF paper and the value that ran in production. Changing it changes every ranking for no reason anyone could point at. |
| Rank-based fusion | Scores from a vector arm and a lexical arm share no scale. Combining ranks is the reason RRF exists. |
| A missing candidate contributes zero | Absence is absence: no penalty, no imputed worst rank. |

Each is pinned twice — once as a unit test, once as a direct comparison against the legacy
transcription in `tests/characterization/test_core_fusion_vs_legacy.py`.

## Deliberately refused

| Legacy behaviour | Why it was not inherited | What replaced it |
|---|---|---|
| `candidate_k := GREATEST(match_count * 8, 50)` | Couples retrieval depth to how many rows a caller wants displayed. Asking for fewer results silently searched a shallower pool and could return a different top result. | `HybridRetriever(candidate_depth=…)`, a fixed floor independent of `limit`, never below `limit` so a caller is not truncated by a hidden setting. |
| `to_tsvector('english', …)` | English stemming and stop words applied to every language, silently. Arabic went through an English stemmer and came out unmatched, with no error (ADR-006). | BM25 over Unicode tokenization. No per-language asset exists to be wrong. |
| Undefined tie ordering | Equal fused scores fell to the store's row order, which is not a guarantee. A ranking that can differ between two identical runs cannot be tested — which is how it stayed unproven. | Ties break on ascending `chunk_id`, via `FusedCandidate.sort_key`. Arbitrary but fixed. |
| Filters that return nothing | **Verified defect:** in the audited `search_brain()`, passing a `language` or `chunk_type` filter always returned an empty list — indistinguishable from "there is no Arabic content". | One shared policy in `knowledge/language.py`, used identically by both arms, with the legacy symptom pinned as a regression test. |
| `CHARS_PER_TOKEN = 4` | Roughly right for English, wrong by about half for Arabic. Under-estimating overflows the context window and the overflow is resolved by silent truncation (ADR-005). | Per-script costs, biased pessimistic. Arabic is estimated at roughly twice English for the same character count. |

## What these implementations honestly do not do

Stated plainly here so nothing downstream is built on a misreading.

**`HashingEmbeddingProvider` is not a semantic model.** It is signed feature hashing over
character n-grams and word tokens. Two texts that say the same thing in different words score
near zero against each other. It exists so the contract is proven implementable, so tests are
deterministic and offline, and so a real model later is a substitution rather than a
redesign. Because `Embedding` carries `model_id`, vectors from it can never be silently
compared with a real model's. **The "semantic" arm is not yet semantic**, and no test in this
repository claims otherwise.

**`ScriptAwareTokenEstimator` is a heuristic, not a tokenizer.** This project has no runtime
dependencies, so there is no vocabulary available. Its `model_id` begins with `heuristic:` so
a budget traced back to it cannot be mistaken for a real count. Its error is deliberately
one-sided — over-estimating wastes budget, under-estimating truncates silently.

**Neither index scales.** Both are exact O(n) scans. That is a deliberate trade: being exact
makes them a reference an approximate index can be checked against later.

**No stemming, in any language.** "running" and "run" are different terms. Raising recall
needs per-language morphology, which must arrive as a tested per-language component — not as
one language's defaults applied to all. Arabic alef/yeh/teh-marbuta folding is recorded as a
deliberate deferral in `knowledge/text.py` for the same reason.

**Scripts written without spaces** (Chinese, Japanese, Thai) tokenize as one long run. A real
gap, needing a segmenter rather than a regex, and written down rather than discovered later.

## Test layering

```
tests/
├── unit/              per-component behaviour; never skips
├── characterization/  legacy behaviour, and Core-vs-legacy comparison; never skips
└── integration/       the stack composed; no infrastructure required
```

The dependency runs one way only: the cross-check imports the Core, the Core imports nothing
from `tests/`. That is enforced by
`test_no_core_module_imports_anything_from_the_test_tree`.

## Evidence

- 297 passed, 2 skipped.
- 17 targeted mutations of the new behaviour, **17 killed, 0 survived** — including
  `k=60 → 10`, dropping either tie-break, the legacy always-empty language filter, the flat
  four-chars-per-token estimate, reinstating the `* 8` depth formula, replacing `blake2b`
  with the salted builtin `hash()`, and making ingestion accumulate instead of replace.

## Wiring — the slice is vertical (audit finding 4)

The layers above were reachable from nothing: `knowledge` and `context` were imported by no
application code. They are now consumed by the conversation path.

```
User -> Session -> Message
     -> measure history -> allocate budget from the ACTIVE model
     -> retrieve -> fuse -> provenance -> fit to budget
     -> [ephemeral system message] -> ModelProvider -> Response -> Event
```

**The dependency direction did not move to achieve it.** `service.py` and `grounding.py`
import `core` only; `factory.py`, the composition root the layering test exempts by name, is
the single module that names `knowledge`, `context`, `persistence` and `runtime` together.

| Decision | Why |
|---|---|
| Grounding is **optional** | With no `context_builder` the turn behaves exactly as before. Adding retrieval must not alter a path that already worked, and `build_in_memory_service` is unchanged. |
| The grounding message is **never persisted** | It is derived from the index at one moment and rebuilt next turn. Persisting it would make history un-reproducible and charge the budget for the same evidence on every later turn. |
| **No evidence means no message** | An empty evidence block invites an answer that claims to have consulted sources it never received. |
| Retrieval failure **propagates** | An ungrounded answer that the caller believes is grounded is worse than a failed turn: the first is invisible. A `RETRIEVAL_FAILED` event is recorded and the model is never called. |
| The budget is **derived per turn** | `ReserveBasedBudgetPolicy` takes the active `ModelSpecLike` and the measured history, so the budget follows the Boss model and shrinks as the conversation grows (ADR-005). `ContextAllocation` records every share, not just the answer. |
| `CONTEXT_ASSEMBLED` carries the **exclusions** | The exclusions are what explain a bad answer. An event recording only what was kept cannot tell "never retrieved" from "retrieved and dropped". |

`Event != Memory` is untouched: retrieval reads an index and promotes nothing.

## Not built

Real embedding model · persistent index · pgvector or Neon adapter · migrations ·
approximate nearest-neighbour search · per-language stemming · identity foundation ·
memory foundation (both still outstanding from Phase 1, per `PHASE_1_RECONCILIATION.md`).

Audit findings 1, 2, 3 and 6 remain open and are deliberately untouched: they are naming and
documentation corrections, not behaviour.
