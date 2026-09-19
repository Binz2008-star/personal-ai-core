# Component Extraction Matrix

**Status: Phase 0 — Core Source Audit: sufficiently verified for architecture drafting.**

Not a claim that every source repository has been audited. Deferred sources are listed in
§6 and may be added later as supplemental rows without blocking the architecture.

Every row below is labelled:

| Label | Meaning |
|---|---|
| **VERIFIED SOURCE FACT** | Read from the source at the recorded SHA in this audit |
| **ARCHITECTURAL DECISION** | A choice made in response to the evidence |
| **DESIGN TO BUILD** | No adequate source exists; must be engineered |
| **DEFERRED / UNVERIFIED** | Not inspected, or inspected only structurally |

A filename, a README claim, or a line count is **not** evidence. Several claims inherited
from the original master plan did not survive contact with the source; they are recorded
in §5 so they are not silently reintroduced.

---

## 1. Repository lineage

**VERIFIED SOURCE FACT.**

| Repository | SHA audited | Lineage |
|---|---|---|
| `local-llm-rig` | (see repo `HANDOFF.md`) | independent — model runtime & measurement |
| `unified-llm-local` | `21a36b0` | **Second Brain lineage — current/superset snapshot** |
| `second-brain-kb` | `c65dbce` | Second Brain lineage — historical snapshot |
| `code-it` | `c72aa0a` | Second Brain lineage — UI variant |
| `rag-engine` | `16f6279` | independent — evaluation & analysis |
| `Rico-...-UAE` | `origin/main @ 215c3169` | independent — product |
| `Robin-Content-Engine-v2` | `6ee2249` | independent — video content pipeline |

`second-brain-kb`, `unified-llm-local` and `code-it` are **one project lineage, not three
independent systems.** Evidence: both brain repos carry the byte-identical README title
`Second Brain v4 — Autonomous Self-Evolving Code Knowledge Base (Code It)`; several files
are hash-identical (`Mcp-All.json`, `HEIMDALL-SETUP.md`); `unified-llm-local` is a strict
superset (adds `Makefile`, `context_builder.py`, `HARDENING-PLAN.md`, ~24 more Python
files) and is newer (2026-09-07 vs 2026-09-04). `code-it` is the React dashboard named in
that README; the brain repos' own `ai-dashboard/` is packaged as
`code-it-intelligence-console`.

**ARCHITECTURAL DECISION.** `unified-llm-local @ 21a36b0` is the single extraction source
for this lineage. The other two are reference material. There is no three-way retrieval
`MERGE`.

---

## 2. Extraction matrix

### 2.1 `unified-llm-local` @ `21a36b0`

| Component | Source path | Size | Tests | Coupling | Maturity | Class | Destination |
|---|---|---|---|---|---|---|---|
| Tool security / policy / audit | `tool_security.py` | 526 | ~962 lines | 0 project refs | **production-worthy** | **ADAPT** | `agent/tools/`, `agent/policy/` |
| Rollback | `rollback.py` | 458 | ~796 lines | 0 | **production-worthy** | **ADAPT** | `agent/recovery/` |
| Protected-path policy | `protected_path_policy.py` | 319 | 368 lines | 0 | strong | **ADAPT** | `agent/policy/` |
| Path containment | `path_security.py` | 239 | shared | 1 | strong | **ADAPT** | `agent/policy/` |
| Hybrid retrieval (RRF) | `chunker_v4.py` L460–532 (SQL) | ~70 | **0** | 0 | implemented, unproven | **ADAPT** | `knowledge/retrieval/` |
| pgvector + HNSW schema | `chunker_v4.py` L400–458 | ~60 | 0 | 0 | implemented | **ADAPT** | `persistence/migrations/` |
| Context token budget | `context_builder.py` | 220 | 1 file | 0 | partial | **ADAPT** | `context/budget/` |
| Chunker | `chunker_v4.py` | 536 | **0** | code-oriented | prototype for our use | **REWRITE** | `knowledge/chunking/` |
| Embedding access | `api.py`, `evolve.py`, `scripts/e2e_test.py` | scattered | 1 file | 0 | not abstracted | **REWRITE** | `knowledge/embeddings/` |
| Agent orchestration | `brain_agent_v4.py` | 1260 | 0 | high | reference only | **REWRITE** | architectural reference |
| Merge gate / merge lock | `merge_gate.py`, `merge_lock.py` | 930 | 409 lines | git workflow | project-specific | **DROP** | — |
| Reranking | — | — | — | — | **does not exist** | **BUILD** | `knowledge/reranking/` |

### 2.2 `rag-engine` @ `16f6279`

| Component | Source path | Size | Class | Destination |
|---|---|---|---|---|
| Drift detection | `analysis/drift_detector.py` | 249 | **ADAPT** | `learning/analysis/` |
| Shadow evaluation | `evaluation/shadow_evaluator.py` | 248 | **ADAPT** | `learning/evaluation/` |
| Evaluation entrypoint | `evaluation/eval_main.py` | 144 | **ADAPT** | `learning/evaluation/` |
| Eval gate | `evaluation/eval_gate.py` | 81 | **ADAPT** | `learning/promotion/` |
| Metrics | `evaluation/metrics.py` | 57 | **ADAPT** | `learning/evaluation/` |
| Refusal evaluation | `evaluation/refusal.py` | 81 | **MERGE** with `local-llm-rig` probe | `evaluation/capability/refusal/` |
| Shadow report | `evaluation/shadow_report.py` | 86 | ADAPT | `learning/evaluation/` |
| Event store | `events/store.py` | 52 | **REWRITE** | `learning/events/` |
| Event schema | `events/schema.py` | 32 | **REWRITE** | `learning/events/` |
| Event replay | `events/replay.py` | **4** | **DROP** | — |
| Dataset builder | `training/dataset_builder.py` | 45 | **REWRITE** | `learning/dataset/` |
| Intent classifier training | `training/train_intent.py` | 37 | **DROP** for LLM training | see §5.2 |
| Training registry | `training/registry.py` | 53 | REWRITE | `learning/training/` |

`rag-engine` carries **22 test files** — the only source with substantive test coverage of
its evaluation path.

### 2.3 `Rico` @ `origin/main 215c3169`

Domain coupling measured by counting job/CV/career/UAE/Telegram/Jotform/Paddle terms.

| Component | Source path | Size | Domain refs | Tests | Class | Destination |
|---|---|---|---|---|---|---|
| Memory interface | `src/rico_memory.py` | 343 | **1** | 17 files | **ADAPT** | `memory/` |
| Feedback lifecycle | `src/feedback_loop.py` | 337 | 2 | 1 file | **ADAPT** | `learning/feedback/` |
| Runtime/provider abstraction | `src/rico_openai_runtime.py` | 1298 | 4 | 13 files | **ADAPT** | `runtime/` |
| Tool registry | `src/rico_tool_registry.py` | 256 | 4 | **0** | **ADAPT** + characterization required | `agent/tools/` |
| Quality checks | `src/rico_quality.py` | 95 | 5 | **0** | **ADAPT** (concepts) | `agent/verifier/` |
| Identity contracts | `src/rico_identity.py` | 237 | **54** | 9 files | **ADAPT/REWRITE** | `identity/` |
| NLU | `src/rico_nlu.py` | 131 | 10 | **0** | **REWRITE** | `agent/planner/` |
| Safety | `src/rico_safety.py` | 208 | 23 | 4 files | **REWRITE** | `agent/policy/` |
| Agent orchestration | `src/rico_agent.py` | 296 | 24 | 33 files | **REWRITE/MERGE** | `agent/` |

**`rico_memory.py` — cleanest component in any source.** Interface is domain-neutral
(`save_profile`, `append_chat_message`, `add_memory`, `search_memories`,
`summarize_recent_memory`, `set_context`); the single domain reference is a `job_id`
parameter name. **Classified ADAPT, not KEEP**, because persistence is file-backed
(`Path`-based) and carries none of the Core memory contract: no provenance, no confidence,
no versioning, no status, no promotion.

**`rico_identity.py` — split component.** The `RICO_IDENTITY` prompt content is entirely
job-hunt/UAE specific and is **excluded**. The surrounding structure is generic and wanted:
`IDENTITY_INTEGRITY_RULE`, `UNTRUSTED_METADATA_RULE`, `SAFETY_CONSTRAINTS_RULE`,
`EVIDENCE_CONTRACT`, `get_grounding_contract()`, `get_language_rule(user_lang)`.

### 2.4 `Robin-Content-Engine-v2` @ `6ee2249`

**VERIFIED SOURCE FACT.** Robin is a video content pipeline. `quality_gate.py` (496 lines)
carries 67 video/media references and uses `cv2.VideoCapture`, `ffmpeg`, `_mean_luma`,
`_ffmpeg_decode_error_count`. It validates **video files**, not model output.
`production_runner.py` (1477) and `publishing.py` (554) are video-domain. `ops_actions.py`
(296) has **no tests**.

| Pattern | Source | Class | Destination |
|---|---|---|---|
| Structured gate result (`QualityGateConfig`/`QualityCheck`/`QualityGateResult`, frozen dataclasses, pass/fail + reasons) | `quality_gate.py` | **REWRITE** (concept) | `agent/verifier/` |
| Bounded automation (`allowed()` → `record()` → `summary()`) | `upload_budget.py` (82) | **REWRITE** (concept) | `agent/policy/` |
| Everything video-specific | — | **DROP** | — |

### 2.5 `local-llm-rig`

| Component | Class | Destination |
|---|---|---|
| `scripts/bench.py` | **ADAPT** behind a `ModelBenchmarkRunner` abstraction | `evaluation/performance/` |
| `scripts/refusal-probe.py` | **ADAPT** | `evaluation/capability/refusal/` |
| `probes/false-refusal.json` | **KEEP** as data | `evaluation/capability/refusal/` |
| `results/*` | **KEEP** as evidence, never as truth | `evaluation/evidence/` |
| `docs/HARDWARE_NOTES.md`, `MODEL_SELECTION.md`, `TROUBLESHOOTING.md` | KEEP | `docs/infrastructure/` |
| `modelfiles/` | **stays in `local-llm-rig`** | — |

---

## 3. Excluded — product/business logic

**ARCHITECTURAL DECISION.** These never enter the Core. If wanted later they arrive as
project connectors (see `PROJECT_INTEGRATION.md`).

UAE/job-search logic · CV/job matching and scoring · job boards · application workflows ·
LinkedIn/Indeed integrations · Telegram · Jotform · Paddle/billing · SaaS/cloud
assumptions · the `RICO_IDENTITY` text · all video/ffmpeg/OpenCV code · `merge_gate.py` /
`merge_lock.py` git workflow.

---

## 4. What must be built from scratch

**DESIGN TO BUILD.** No source provides an adequate implementation.

| Subsystem | Why no source serves |
|---|---|
| Event model | Rico has **no event module**; `rag-engine` events are 52+32+4 lines |
| Experience model | no source |
| Feedback normalization | Rico's loop is product-shaped |
| Memory candidate generation | no source |
| Memory promotion / rejection | no source; Rico writes memory directly |
| Provenance, confidence, versioning, supersession | absent from every memory implementation |
| Generic verifier | Rico has none; Robin's is video-specific |
| Adapter / LoRA training pipeline | `rag-engine` training is sklearn (see §5.2) |
| Reranking | absent from every source |
| Multilingual retrieval strategy | sources are English-hardcoded (see §5.4) |
| End-to-end RAG evaluation | RRF, chunker and tsvector have **zero** tests |

**The Core backbone is newly engineered. Existing repositories supply edge components and
patterns.**

---

## 5. Master-plan claims that did not survive the audit

Recorded so they are not silently reintroduced.

**5.1 "Three brain systems to MERGE."** One lineage, three snapshots. See §1.

**5.2 "`rag-engine` has a mature training lifecycle."** `training/train_intent.py` is 37
lines: a `TfidfVectorizer` + `LogisticRegression` sklearn pipeline for **intent
classification**. It is not LLM or LoRA training. Adapter architecture must be designed
independently. `rag-engine` remains valuable for evaluation, shadow evaluation, eval gating
and drift.

**5.3 "Robin supplies the verifier."** Robin's quality gate validates video files. The
generic verifier is a **DESIGN TO BUILD** item.

**5.4 "RAG is the strength of `unified-llm-local`."** Partly inverted. The RAG path is
genuinely implemented — hybrid search is called at `brain_agent_v4.py:249` with a vector
fallback — but is **almost entirely untested** (RRF 0 tests, chunker 0, tsvector 0). Its
~3,900-line test suite is concentrated on security (1,732), rollback (796), merge lock
(409) and evidence (407). **Its strongest verified asset is the tool security/policy/audit
layer**, which happens to fill the verifier/policy gap left by Rico and Robin.

**5.5 Implementation presence ≠ production readiness.** RAG here is implemented and
unproven. Both facts are recorded.

---

## 6. Deferred / unverified sources

**DEFERRED.** Not audited. May be added as supplemental rows later; none blocks the
architecture.

| Source | Status | Expected contribution |
|---|---|---|
| `rico-hunt-ai` | not cloned | likely historical snapshot |
| `rico-hunt-ai-d23beb87` | not cloned | likely historical snapshot |
| `rico-hunt-ai-bd873eb7` | not cloned | likely historical snapshot |
| `clean-rtl-assistant` | not cloned | Arabic/RTL concepts |
| `Send-Offer-to-Client-via-Gmail-Inbox` | not cloned | idempotency, audit, service boundaries |

That the Rico variants are duplicates is a **hypothesis**, supported by the Second Brain
lineage precedent but **not verified**.

Partially verified: `second-brain-kb` and `code-it` were inspected structurally only;
`unified-llm-local`'s retrieval, security, context and test layers were read, its
`reindex_v4.py`, `memory.py`, `evolve.py` and `mcp_server_v4.py` were not.

---

## 7. Extraction rules

**ARCHITECTURAL DECISION.**

- **KEEP** — only when implementation *and* tests justify it.
- **ADAPT** — implementation useful; contracts, storage or runtime assumptions differ.
- **REWRITE** — only the architectural idea transfers.
- **DROP** — domain-specific, trivial, or misleading.

No component is integrated without characterization tests taken against its recorded SHA
first. See `ENGINEERING_PLAYBOOK.md`.
