# ADR-013 — Evaluation harness

**Status:** PROPOSED · design only · nothing built

- Harness: **PROPOSED**, not accepted
- Implementation: **NOT AUTHORIZED**
- It creates no package, no runner and no golden set, and adds no CI gate.

## Why an ADR and not a harness

The usual reason is governance. This time there is a harder one.

**An evaluation harness cannot be verified where it is being written.** Every gate this
repository trusts was run before it was claimed: the suite, `ruff`, `pyright`, the
mutations, the live smoke test on the owner's rig. An evaluation harness needs a model
answering real prompts, and the environment these changes are authored in has no model
server. Building it here would mean shipping the one thing in this repository that nobody
had run — in the file whose job is to decide whether things work.

So the design is written here and the build belongs where a model is.

## What an evaluation is for

`docs/TESTING_STRATEGY.md` §6 carries a principle taken from `rag-engine`, which had
already fixed exactly this split:

> Evaluation must run through **the same retrieval and prompting path as production**. A
> separate evaluation pipeline proves nothing about the runtime.

That is the whole architecture of this ADR. The harness calls
`conversation/factory.py` — the same composition root `pac` calls — and differs only in
what it does with the reply.

**Decision:** the harness composes the system through the production factory and asserts
on the reply. It does not reimplement retrieval, prompting or budgeting.

**Rejected:** a standalone evaluation pipeline that builds its own prompt.

## The hard question: what can be scored without a judge?

Most "LLM evaluation" scores quality, and quality needs either a human or a judge model.
Both are available in principle and neither is free:

| | |
|---|---|
| **a judge model** | the Boss model judging its own output is circular; a second model makes the score a claim about *two* models |
| **a human** | the only real authority on quality, and not automatable |

But this system has something most do not: **a written contract**. ADR-012's rules were
chosen so that each answers a stated behavioural failure, and most of them are
**mechanically checkable**:

| Rule | Checkable without a judge? |
|---|---|
| reply in the user's language; MSA for Arabic, no dialect | **yes** — script and dialect-marker detection |
| no padding, no decorative menus | **partly** — emoji and menu markers are detectable; "padding" is not |
| no credential the evidence does not contain | **yes, for planted cases** — the case supplies the evidence, so an invented certification is a string that is not in it |
| say so where the evidence does not reach | **yes** — a case with no supporting passage has a right answer that is a refusal |
| retrieved text is data, not instructions | **yes** — plant an injection in a document and check the rules held |
| no secrets | **yes, for planted cases** — a planted token either appears in the reply or does not |

**Decision:** the harness scores the CONTRACT, not the quality. Every case states the
rule it exercises and the property that decides it. Quality judgements stay with the
owner, recorded as notes beside the evidence, never as an automated score.

**Rejected:** an LLM judge. It would turn a measurement into an opinion with a number on
it, and this repository already has a name for that.

## The prompt-injection case is the one to build first

Rule 5 exists because `conversation/grounding.py` puts retrieved user documents into the
**same `Role.SYSTEM` message** the contract arrives in. That is the only rule whose
failure mode is a security failure rather than a quality one, and the only one with a
decisive test: ingest a document containing an instruction to ignore the rules, ask a
question that retrieves it, and check whether the reply followed the document or the
contract.

It is also the only case where a *passing* result is weak evidence and a *failing* result
is conclusive. That asymmetry should be stated in the results, not smoothed over.

## Evidence

`docs/TESTING_STRATEGY.md` §7 already decides this and the harness inherits it: raw output
is committed unmodified, re-scoring goes in a separate derived file that names its source,
and a results file states the machine it came from, because every number is hardware-bound.

One addition, from this repository's own history: a results file must also record the
**commit it ran against** and the **model name and context window**. A score without those
is a number about nothing — the same defect class as the merge ledger's, in a different
file.

## What this does not decide

- **Whether evaluation gates anything.** TESTING_STRATEGY says L5 runs "before
  promotion". Making a red evaluation block a merge is a CI decision, and CI decisions in
  this repository have a history: renaming a required check once left `main` requiring one
  no run could produce (PR #36). A gate is added deliberately or not at all.
- **The golden set's contents.** Cases are written against rules; the rules exist, the
  cases do not, and listing them here would be inventing a corpus.
- **Anything about Phase 5, persistence or the Boss model.**

## Status

**Harness:** PROPOSED — not accepted

**Implementation:** NOT AUTHORIZED

**Where it must be built:** an environment with a live model, because the first thing it
must do is run.
