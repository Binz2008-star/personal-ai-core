# Engineering Playbook

**Status: the method is active. Phase status lives in [`PROJECT_STATE.md`](../PROJECT_STATE.md), not here.**

This header used to read "Phase 0 — active" long after Phase 4 was accepted. A status
line in a method document goes stale without anything checking it, so this one points
to the file that is checked.

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
| CLASSIFY | KEEP / ADAPT / MERGE / REWRITE / DROP / BUILD, with evidence — vocabulary defined in `COMPONENT_EXTRACTION_MATRIX.md` §7 |
| FREEZE | record the exact source SHA |
| CHARACTERIZE | tests describing what it *does* today |
| EXTRACT | copy into the Core's tree |
| ADAPT | reshape to the Core contract, invert dependencies |
| TEST | unit → contract → integration |
| EVALUATE | against the golden set |
| PROMOTE | only on evidence |

A **BUILD** item skips DISCOVER, FREEZE and CHARACTERIZE: no source exists, so there is
nothing to read, pin or pin down. It enters at contract definition and carries a different
risk profile — no prior behaviour constrains it, and none validates it.

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

**ARCHITECTURAL DECISION (recorded 2026-09-23, owner-approved): the executed phases are
authoritative, and the numbering below the plan is not.** The Phase 0 plan and what was
built diverged from Phase 3 onward. Two documents kept using "Phase 3" and "Phase 4" for
different scopes. This section records the reordering as a decision, so that a phase number
has one meaning.

The plan, as written in Phase 0:

```text
Phase 0  audit and freeze evidence
Phase 1  core + runtime + identity + conversation + memory foundation
Phase 2  knowledge / RAG
Phase 3  events + feedback + learning
Phase 4  agent + tools + policy + verifier
Phase 5  controlled training / adapters
Phase 6  project connectors
```

What was executed. Status here is copied from `PROJECT_STATE.md`, which is authoritative:

| Phase | Executed scope | Status |
|---|---|---|
| 0 | audit, evidence freeze, extraction matrix | completed |
| 1 | core, runtime, conversation slice; identity added last (#39); memory foundation came from Phase 3 | components complete, **not accepted** |
| 2 | knowledge and context: retrieval, fusion, budgeting | accepted |
| 3 | memory domain, promotion gate, write path | accepted |
| 4 | memory-aware context: session-scoped recall, shared budget | accepted |
| 5 | cross-session memory | **not authorized**, design not started |

The practical difference: memory was built as Phases 3 and 4, before events, feedback
and learning. The plan's Phases 3 to 6 are therefore **not started**, and they no longer
carry numbers:

- events + feedback + learning. Events are recorded; feedback and learning have no code.
- agent + tools + policy + verifier
- controlled training / adapters
- project connectors

Each will get a phase number when it is authorised, not before. The reason memory came
first was not written down when the order changed. This records that the order changed; it
does not invent a justification.

Two gates survive the renumbering, stated by content because their numbers no longer
point anywhere: project connectors do not begin before the Core is stable, and training
does not begin before memory, knowledge, events, feedback, evaluation and golden sets
exist. Training without those injects noise that cannot be measured.

## 9. Rollback

Every promotion has a rollback point. Model versions, adapters, memory promotions and
schema migrations are all reversible. A change that cannot be undone does not ship.
