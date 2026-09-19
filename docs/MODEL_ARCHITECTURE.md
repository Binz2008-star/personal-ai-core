# Model Architecture

**Status: Phase 0 — design.**

---

## 1. The Boss model

**ARCHITECTURAL DECISION.**

```text
Personal AI Core Boss model:  huihui_ai/qwen2.5-abliterate:7b
```

Operationally primary, architecturally replaceable.

**This must not be confused with `local-llm-rig`'s benchmark result.** That project's
measurements found `qwen2.5:7b` scored equally on refusal (0/15) and produced longer
answers. That is a *measurement about a benchmark*, recorded in `local-llm-rig`. It is
**not** a Core model decision and does **not** replace the Boss model. The two are kept
distinct deliberately. See `ADR/ADR-002`.

## 2. Layering

```text
        Personal AI Core
               ↓
        ModelRegistry          which models exist, which is active
               ↓
        ModelProvider          interface
               ↓
        OllamaProvider         the only implementation for now
               ↓
             Ollama
               ↓
   huihui_ai/qwen2.5-abliterate:7b
```

**ARCHITECTURAL DECISION.** No `import ollama` outside `runtime/ollama/`. No model name
literal in business logic. This is a direct response to a verified defect: in
`unified-llm-local`, embedding access is hard-wired to an Ollama HTTP endpoint and
duplicated across `api.py`, `evolve.py` and `scripts/e2e_test.py`.

## 3. Registry record

```yaml
id: model-001
provider: ollama
name: huihui_ai/qwen2.5-abliterate:7b
role: boss
status: active
context_window: 8192
hardware_profile: gtx1060-6gb
evidence: local-llm-rig/results/...
```

`context_window` is registry data, consumed by the context budget. See §5.

## 4. Hardware profile

Declared, not hard-coded in logic:

```yaml
cpu: { model: Intel Core i7-8700, cores: 6, threads: 12, avx2: true }
ram: { gb: 32 }
gpu: { model: GTX 1060, vram_gb: 6, architecture: Pascal }
```

**VERIFIED SOURCE FACT.** `local-llm-rig` records that placement measurement on this card
is currently unreliable (family-prefix model matching; `OLLAMA_KV_CACHE_TYPE` unset during
the run) and is being re-measured. The Core therefore treats VRAM placement as **unknown**
rather than importing a figure. Throughput measurements from that repo are valid.

## 5. Context budget is model-derived

**ARCHITECTURAL DECISION.** The budget comes from the active model's registry entry.

**VERIFIED SOURCE FACT.** The audited `context_builder.py` hard-codes
`DEFAULT_TOKEN_BUDGET = 24000`, sized for "qwen2.5:7b 32K", and estimates with
`CHARS_PER_TOKEN = 4`. Both are unsafe here: the Boss model runs at an 8192 context, so the
inherited budget is roughly triple what fits, and a 4-chars-per-token heuristic is
English-centric and wrong for Arabic. The contract is adapted; the constants are not.
See `ADR/ADR-005`.

## 6. Replacement procedure

```text
candidate → benchmark → golden set → regression → compare → promote | reject
```

No model is promoted because it "feels better". Swapping the Boss model must require
changing registry configuration only — if it requires a code change, the abstraction has
failed and that is a bug.

## 7. Weight immutability

Weights are immutable during normal operation. Changes are offline, batched, evaluated,
versioned and rollbackable. See `LEARNING_ARCHITECTURE.md`.

## 8. Extraction plan

| From | Take | Class |
|---|---|---|
| `Rico` `src/rico_openai_runtime.py` (1298, 4 domain refs, 13 tests) | provider abstraction and fallback shape | **ADAPT** — cloud-provider assumptions excluded |
| `local-llm-rig` `scripts/bench.py` | benchmarking, behind `ModelBenchmarkRunner` | **ADAPT** |
| `local-llm-rig` `scripts/refusal-probe.py` + `probes/false-refusal.json` | capability probing | **ADAPT** / **KEEP** (data) |
| `local-llm-rig` `results/*` | evidence, never truth | **KEEP** |
| `unified-llm-local` embedding access | hard-wired, duplicated | **REWRITE** behind `EmbeddingProvider` |
| `local-llm-rig` `modelfiles/` | stays in that repo | — |
