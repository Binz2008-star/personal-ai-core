# Engineering Playbook

**Status: Phase 0 — active.**

---

## 1. Method

**Brownfield Component Extraction & Controlled Integration.**

We are not building an AI from scratch, and we are not merging repositories. We take
proven parts from existing systems, pin their behaviour, isolate them behind contracts, and
integrate them one at a time.

Legacy code is not waste to be deleted. It is a **source asset** to be characterized,
extracted and judged on evidence.

## 2. The chain

No shortcuts. Every component entering the Core passes through all of it.

```text
DISCOVER → CLASSIFY → FREEZE → CHARACTERIZE → EXTRACT
        → ADAPT → UNIT → CONTRACT → INTEGRATION → EVALUATE → PROMOTE
```

| Stage | Means |
|---|---|
| DISCOVER | read the implementation, not the filename or the README |
| CLASSIFY | KEEP / ADAPT / MERGE / REWRITE / DROP, with evidence |
| FREEZE | record the exact source SHA |
| CHARACTERIZE | tests describing what it *does* today |
| EXTRACT | copy into the Core's tree |
| ADAPT | reshape to the Core contract, invert dependencies |
| TEST | unit → contract → integration |
| EVALUATE | against the golden set |
| PROMOTE | only on evidence |

## 3. Techniques

**Characterization testing.** Pin current behaviour before changing it.

**Strangler migration.** Never `OLD → DELETE → NEW`. Extract, test, adapt, and let the old
implementation fall away once the new one is proven.

**Anti-corruption layer.** The Core never imports a project's types. Adapters translate.
Deleting a connector must leave the Core working.

**Dependency inversion.** `from core.memory import MemoryStore`, never
`from rico_memory import RicoMemory`.

**Contract-first.** Interface → schema → tests → implementation → integration. Not code
first and hope.

**Vertical slices.** One thin path working end to end beats six half-built layers.

## 4. Evidence rules

Learned the expensive way on `local-llm-rig`, where eight measurement bugs each produced a
clean, plausible summary table and every one was caught only by reading raw output.

- **Read the raw data, not the summary.** A tidy table is not evidence.
- **A filename is not an implementation.** `events/replay.py` was 4 lines.
- **A line count is not maturity.** `train_intent.py` is 37 lines and is not LLM training.
- **Implementation is not proof.** The audited RRF works and has zero tests.
- **A shorter rewrite is not automatically better.** Find out what it removed.
- **Say what a number does not prove.** Scope every claim to what was tested.
- **Verify against the source of truth**, not a convenient substitute.
- **"Placed" is not "committed" is not "pushed."** Only the last one counts.

## 5. Labelling

Every document distinguishes:

**VERIFIED SOURCE FACT** · **ARCHITECTURAL DECISION** · **DESIGN TO BUILD** ·
**DEFERRED / UNVERIFIED**

An audit hypothesis is never silently promoted to a fact. Where the original master plan's
assumptions failed, the failure is recorded rather than quietly corrected — see
`COMPONENT_EXTRACTION_MATRIX.md` §5.

## 6. Git governance

**One writer at a time.** Before editing, say what you are taking. After pushing, say what
landed and at which SHA.

**No manufactured commits.** No empty commits, no commits to look busy.

**Source provenance.** A commit extracting code names the source repository, path and SHA.

Every completed task reports:

```text
Agent · Task · Source · Destination · Files changed · Tests
Findings · Commit SHA · Acceptance status · Dependencies · Remaining risks
```

`"done"` without a SHA is not a report.

## 7. Definition of done

A component is integrated only when all of:

```text
[ ] Source identified, SHA recorded
[ ] Dependencies identified
[ ] Existing tests identified
[ ] Characterization tests added and passing
[ ] Core contract defined
[ ] Adapter implemented
[ ] Unit / contract / integration tests pass
[ ] Regression suite passes
[ ] Documentation updated
[ ] Diff reviewed
[ ] Commit created, SHA recorded
[ ] Acceptance completed
```

## 8. Phases

```text
Phase 0  audit and freeze evidence                  ← current
Phase 1  core + runtime + identity + conversation + memory foundation
Phase 2  knowledge / RAG
Phase 3  events + feedback + learning
Phase 4  agent + tools + policy + verifier
Phase 5  controlled training / adapters
Phase 6  project connectors
```

Phase 6 does not begin before the Core is stable. Phase 5 does not begin before memory,
knowledge, events, feedback, evaluation and golden sets exist — training without them
injects noise that cannot be measured.

## 9. Rollback

Every promotion has a rollback point. Model versions, adapters, memory promotions and
schema migrations are all reversible. A change that cannot be undone does not ship.
