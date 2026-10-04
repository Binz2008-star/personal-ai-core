"""The verdict on one comparison, by ADR-023 amendment 1 (accepted 2026-10-03, #191).

    python -m personal_ai_core.app.bench.compare BASELINE CANDIDATE --unit 1

The amendment fixed the rule that decides "better" before any comparison it
governs was run. It said that, once approved, the rule becomes a mode of
`bench.compare` that prints the verdict; this is that mode. It applies the
rule as written and holds no threshold of its own making: each number below is
the amendment's, and a test re-derives the ones it computed.

R0 readable. Read only when both sides have the same weights digest
   (recorded), scorer, model, runs and languages; the same num_ctx, stated and
   confirmed by what the server had loaded, with no context_mismatch; task
   digests that are identical or differ only by the contract (ADR-023 §8.3);
   no hang -- a hung run is stopped, never continued by hand, so its file has
   no end record or misses attempts: every task x language x run is present
   exactly once; and the run count D1 fixed, 10 per side, which puts 120
   attempts in each track x language group. Stricter than the amendment in one
   place: a digest change it would accept as a documented fix (ADR-022 §10)
   cannot be verified here, so it reads NOT READABLE and is left to be read by
   hand. Never looser.
R1 target. The class each control targets, declared before its runs: unit 1
   class 1, unit 2 class 3, unit 3 class 2. Over agent attempts paired by
   (task, language, run), attempts leaving the class must outnumber attempts
   entering it by a one-sided exact sign test at 5% (D3): P(X >= leaving) <=
   0.05, X binomial over the attempts that moved, p = 1/2. A control the
   amendment declares no class for -- ADR-024's units A and B among them, as
   the owner accepted on 2026-10-03 -- has no R1 and so no verdict. Only a
   separately authorized amendment, made before its runs, could give it one.
R2 no regression. For each of the four groups track x language (D1 (b)), a
   rise in failures of 16 or more is a regression (family-wise 8.73% at the
   worst base rate, 87.3% power for 50% -> 70%, D2). Both tracks and both
   languages count; an improvement never buys back a regression.
R3 costs, reported and not gating: budget stops, answer rejections per agent
   attempt, protocol errors, and false rejections -- rejected answers the
   task's own checks would have passed, read by `refusals.py` (#195).
R4 PASS when R0, R1 and R2 hold; NOT READABLE when R0 fails, and then nothing
   else is computed; FAIL otherwise. A PASS is evidence for adoption, not
   adoption (ADR-023 §4), and no default changes until false rejections are
   counted (D4): the report counts them.
R5 a comparison is read once, and a FAIL is not re-run until it passes. This
   cannot enforce that; it prints it.
"""
from __future__ import annotations

import tempfile
from collections import Counter
from dataclasses import dataclass, field
from math import comb
from pathlib import Path
from typing import Any, Mapping

from .compare import LABELS, Side, classify
from .refusals import read as read_refusals
from .runner import _task_digest
from .tasks import Task

RUNS_PER_SIDE = 10              # D1 (b)
ATTEMPTS_PER_GROUP = 120        # 12 tasks x 10 runs, per track x language
REGRESSION_DELTA = 16           # D1 (b) at D2's 20 points; a test re-derives it
FAMILY_TARGET = 0.10            # ADR-020 amendment 1, which the amendment follows
SIGN_TEST_ALPHA = 0.05          # D3
GROUPS = (("agent", "en"), ("agent", "ar"), ("knowledge", "en"), ("knowledge", "ar"))
# R1: the class each control targets, declared in the amendment before any of
# its runs. ADR-024's units have none (owner-accepted finding, 2026-10-03).
TARGETS = {"1": "answered_without_executing", "2": "refused_commands",
           "3": "executed_unverified"}
# ADR-023 amendment 2 (accepted by the owner 2026-10-03): a unit outside ADR-023
# may declare its target in its own ADR before its runs, as a named predicate
# over the run record. ADR-025 §6.1 is the first: NO_EXECUTED_TOOL_CALL.
NO_EXECUTED_TOOL_CALL = "NO_EXECUTED_TOOL_CALL"
PREDICATE_TARGETS = {"native-tools": NO_EXECUTED_TOOL_CALL}
PROVIDER_FAILURE_BOUND = 5      # ADR-025 §6.1: more in either arm -> NOT READABLE
CLASSIFIABLE_STOPS = ("answered", "budget")
UNITS = sorted([*TARGETS, *PREDICATE_TARGETS])


def sign_test(leaving: int, entering: int) -> float:
    """One-sided exact sign test: P(X >= leaving), X ~ Binomial(leaving + entering, 1/2)."""
    n = leaving + entering
    return sum(comb(n, k) for k in range(leaving, n + 1)) / 2 ** n


def no_executed_tool_call(run: Mapping[str, Any]) -> bool | None:
    """ADR-025 §6.1: `executed_tool_calls == 0`, from the run record alone.

    True or False for a classifiable attempt (stop "answered" or "budget",
    `steps` a list, every `executed` a boolean); None for a provider or
    runtime failure (stop "error"), which is never counted as "no execution".
    Raises ValueError for a malformed record, which makes a comparison not
    readable.
    """
    stop = run.get("stop")
    if stop == "error":
        return None
    if stop not in CLASSIFIABLE_STOPS:
        raise ValueError(f"stop is {stop!r}")
    steps = run.get("steps")
    if not isinstance(steps, list):
        raise ValueError("the record has no steps")
    executed = [step.get("executed") if isinstance(step, Mapping) else None for step in steps]
    if any(not isinstance(value, bool) for value in executed):
        raise ValueError("a step's executed is not a boolean")
    return not any(executed)


def _predicate_problems(side: Side) -> list[str]:
    problems: list[str] = []
    failures = 0
    for run in side.agent:
        try:
            failures += no_executed_tool_call(run) is None
        except ValueError as exc:
            problems.append(f"{run.get('task')} {run.get('language')} run {run.get('run')}: "
                            f"malformed record ({exc})")
    if failures > PROVIDER_FAILURE_BOUND:
        problems.append(f"{failures} provider or runtime failures; more than "
                        f"{PROVIDER_FAILURE_BOUND} make the comparison not readable (ADR-025 §6.1)")
    return problems


def _native_unreadable(base: Side, cand: Side) -> list[str]:
    """R0's conditions for the native-tools unit (ADR-025 §6): `native_tools`
    is the only experimental flag that differs, off then on."""
    problems = []
    if bool(base.header.get("native_tools")) or not bool(cand.header.get("native_tools")):
        problems.append("native_tools must be off in the baseline and on in the candidate")
    if bool(base.header.get("environment_context")) != bool(cand.header.get("environment_context")):
        problems.append("environment_context differs")
    if bool(base.header.get("lenient_protocol")) or bool(cand.header.get("lenient_protocol")):
        problems.append("lenient_protocol must be off in both arms")
    for label, side in (("baseline", base), ("candidate", cand)):
        problems.extend(f"{label}: {p}" for p in _predicate_problems(side))
    return problems


def _verify_unreadable(base: Side, cand: Side) -> list[str]:
    """R0's flag conditions for unit 3 (ADR-023 §2.3): `verify_completion` is
    the only experimental flag that differs, off then on."""
    problems = []
    if bool(base.header.get("verify_completion")) or not bool(cand.header.get("verify_completion")):
        problems.append("verify_completion must be off in the baseline and on in the candidate")
    for flag in ("environment_context", "lenient_protocol", "native_tools"):
        if bool(base.header.get(flag)) != bool(cand.header.get(flag)):
            problems.append(f"{flag} differs")
    return problems


def _key(run: Mapping[str, Any]) -> tuple[str, str, int]:
    return run["task"], run["language"], run["run"]


def _side_problems(side: Side) -> list[str]:
    problems = []
    runs = side.header.get("runs")
    if runs != RUNS_PER_SIDE:
        problems.append(f"{runs} runs; the rule was fixed for {RUNS_PER_SIDE} per side (D1)")
    stated = side.header.get("num_ctx_measured_by_owner")
    loaded = (side.end.get("ollama_loaded") or {}).get("context_length")
    if not side.end:
        problems.append("no end record: the run did not finish (a hang is stopped, "
                        "never continued by hand)")
    elif stated is None or side.end.get("context_mismatch") is not False or loaded != stated:
        problems.append(f"num_ctx not confirmed: stated {stated}, the server had {loaded}, "
                        f"context_mismatch {side.end.get('context_mismatch')}")
    tasks, languages = side.header.get("tasks") or {}, side.header.get("languages") or []
    expected = {(t, lang, r) for t in tasks for lang in languages
                for r in range(1, (runs if isinstance(runs, int) else 0) + 1)}
    seen = Counter(_key(r) for r in side.runs)
    missing = len(expected - set(seen))
    extra = sum(n for k, n in seen.items() if k not in expected) + sum(
        n - 1 for n in seen.values() if n > 1)
    if missing or extra:
        problems.append(f"attempts missing {missing}, extra or repeated {extra}: "
                        "not every task x language x run exactly once")
    sizes = Counter((r["track"], r["language"]) for r in side.runs)
    for group in GROUPS:
        if sizes[group] != ATTEMPTS_PER_GROUP:
            problems.append(f"{group[0]} {group[1]} has {sizes[group]} attempts; R2's "
                            f"threshold was derived for {ATTEMPTS_PER_GROUP}")
    return problems


def unreadable(base: Side, cand: Side, tasks: Mapping[str, Task] | None) -> list[str]:
    """R0: every reason the comparison may not be read; empty when it may."""
    problems = [f"{key} differs" for key in ("model", "scorer", "runs", "languages",
                                             "num_ctx_measured_by_owner")
                if base.header.get(key) != cand.header.get(key)]
    if base.weights == "not recorded" or base.weights != cand.weights:
        problems.append("weights digest differs or is not recorded")
    for label, side in (("baseline", base), ("candidate", cand)):
        problems.extend(f"{label}: {p}" for p in _side_problems(side))
    a_tasks, b_tasks = base.header.get("tasks") or {}, cand.header.get("tasks") or {}
    if set(a_tasks) != set(b_tasks):
        problems.append("the task sets differ")
    for task_id in sorted(set(a_tasks) & set(b_tasks)):
        a, b = a_tasks[task_id]["digest"], b_tasks[task_id]["digest"]
        if a == b:
            continue
        task = tasks.get(task_id) if tasks is not None else None
        if not (task is not None and task.action_required is not None
                and a == _task_digest(task, with_contract=False) and b == _task_digest(task)):
            problems.append(f"task {task_id}: digests differ and the contract does not "
                            "explain it (a documented fix, ADR-022 §10, is read by hand)")
    return problems


@dataclass
class Verdict:
    unit: str
    target: str
    unreadable: list[str]
    leaving: int = 0
    entering: int = 0
    p_value: float = 1.0
    failures: dict[tuple[str, str], tuple[int, int]] = field(default_factory=dict)
    costs: list[tuple[str, str, str]] = field(default_factory=list)
    # Pairs dropped from R1 because either side was a provider failure.
    dropped: list[tuple[str, str, int]] = field(default_factory=list)

    @property
    def target_holds(self) -> bool:
        return self.p_value <= SIGN_TEST_ALPHA

    @property
    def regressions(self) -> list[tuple[str, str]]:
        return [g for g, (a, b) in self.failures.items() if b - a >= REGRESSION_DELTA]

    @property
    def outcome(self) -> str:
        if self.unreadable:
            return "NOT READABLE"
        return "PASS" if self.target_holds and not self.regressions else "FAIL"


def _false_rejections(side: Side, tasks: Mapping[str, Task] | None) -> str:
    if tasks is None:
        return "not counted (task files not loaded)"
    with tempfile.TemporaryDirectory(prefix="pac-verdict-", ignore_cleanup_errors=True) as tmp:
        reading = read_refusals(side.runs, tasks, Path(tmp))
    if reading.runs_without_field:
        return "not countable (a file from before #193)"
    return f"{sum(reading.false_rejections.values())} of {sum(reading.rejected.values())}"


def _costs(base: Side, cand: Side, tasks: Mapping[str, Task] | None) -> list[tuple[str, str, str]]:
    def per_attempt(side: Side, key: str) -> str:
        total = sum(r.get(key) or 0 for r in side.agent)
        return f"{total} ({total / len(side.agent):.2f} per attempt)" if side.agent else "0"

    def budget_stops(side: Side) -> str:
        return str(sum(r["stop"] == "budget" for r in side.agent))

    return [
        ("agent attempts stopped on the budget", budget_stops(base), budget_stops(cand)),
        ("answers rejected for not having acted",
         per_attempt(base, "action_rejections"), per_attempt(cand, "action_rejections")),
        ("protocol errors", per_attempt(base, "protocol_errors"),
         per_attempt(cand, "protocol_errors")),
        ("false rejections (of rejected answers)",
         _false_rejections(base, tasks), _false_rejections(cand, tasks)),
    ] + ([
        ("answers rejected for an unverified completion",
         per_attempt(base, "verification_rejections"),
         per_attempt(cand, "verification_rejections")),
    ] if base.header.get("verify_completion") or cand.header.get("verify_completion") else [])


def decide(base: Side, cand: Side, unit: str, tasks: Mapping[str, Task] | None) -> Verdict:
    if unit in PREDICATE_TARGETS:
        return _decide_predicate(base, cand, unit, tasks)
    target = TARGETS[unit]
    problems = unreadable(base, cand, tasks)
    if unit == "3":
        problems += _verify_unreadable(base, cand)
    verdict = Verdict(unit=unit, target=target, unreadable=problems)
    if verdict.unreadable:
        return verdict
    a = {_key(r): classify(r) for r in base.agent}
    b = {_key(r): classify(r) for r in cand.agent}
    verdict.leaving = sum(a[k] == target and b[k] != target for k in a)
    verdict.entering = sum(a[k] != target and b[k] == target for k in a)
    verdict.p_value = sign_test(verdict.leaving, verdict.entering)
    return _guard_and_costs(verdict, base, cand, tasks)


def _decide_predicate(base: Side, cand: Side, unit: str,
                      tasks: Mapping[str, Task] | None) -> Verdict:
    """R1 on a named predicate (ADR-023 amendment 2; ADR-025 §6.1)."""
    verdict = Verdict(unit=unit, target=PREDICATE_TARGETS[unit],
                      unreadable=[*unreadable(base, cand, tasks), *_native_unreadable(base, cand)])
    if verdict.unreadable:
        return verdict
    a = {_key(r): no_executed_tool_call(r) for r in base.agent}
    b = {_key(r): no_executed_tool_call(r) for r in cand.agent}
    verdict.dropped = sorted(k for k in a if a[k] is None or b[k] is None)
    pairs: list[tuple[bool, bool]] = []
    for k, x in a.items():
        y = b[k]
        if x is not None and y is not None:
            pairs.append((x, y))
    verdict.leaving = sum(x and not y for x, y in pairs)
    verdict.entering = sum(y and not x for x, y in pairs)
    verdict.p_value = sign_test(verdict.leaving, verdict.entering)
    return _guard_and_costs(verdict, base, cand, tasks)


def _guard_and_costs(verdict: Verdict, base: Side, cand: Side,
                     tasks: Mapping[str, Task] | None) -> Verdict:
    """R2 and R3, the same for every unit."""

    def failed(side: Side, group: tuple[str, str]) -> int:
        return sum(not r["success"] for r in side.runs if (r["track"], r["language"]) == group)

    for group in GROUPS:
        verdict.failures[group] = (failed(base, group), failed(cand, group))
    verdict.costs = _costs(base, cand, tasks)
    return verdict


def report(verdict: Verdict) -> str:
    if verdict.target in LABELS:
        out = [f"Verdict by ADR-023 amendment 1 (accepted 2026-10-03, #191): unit {verdict.unit}, "
               f"target class {LABELS[verdict.target].strip()}"]
    else:
        out = [f"Verdict by ADR-023 amendments 1 and 2 (accepted 2026-10-03): unit {verdict.unit}, "
               f"target predicate {verdict.target} (executed tool calls == 0, ADR-025 §6.1)"]
    if verdict.unreadable:
        out.append("  R0 readable: NO")
        out.extend(f"    {problem}" for problem in verdict.unreadable)
        out.append("  R1-R3 not computed: a comparison that is not readable is not read.")
    else:
        out.append("  R0 readable: yes")
        what = "the class" if verdict.target in LABELS else verdict.target
        out.append(f"  R1 target: {verdict.leaving} paired attempts left {what}, "
                   f"{verdict.entering} entered it; one-sided exact sign test "
                   f"p = {verdict.p_value:.4f} (holds at <= {SIGN_TEST_ALPHA}): "
                   + ("holds" if verdict.target_holds else "does not hold"))
        if verdict.dropped:
            out.append(f"    {len(verdict.dropped)} pair(s) dropped from R1, a provider or "
                       "runtime failure on either side: "
                       + ", ".join(f"{t} {lang} run {r}" for t, lang, r in verdict.dropped))
        out.append(f"  R2 no regression (a group regresses when its failures rise by "
                   f"{REGRESSION_DELTA} or more):")
        for (track, language), (a, b) in verdict.failures.items():
            flag = "   REGRESSED" if b - a >= REGRESSION_DELTA else ""
            out.append(f"    {track:9} {language}  failures {a:>3} -> {b:>3}  ({b - a:+d}){flag}")
        out.append("    " + ("holds" if not verdict.regressions else "does not hold"))
        out.append("  R3 costs (reported, not gating; baseline -> candidate):")
        out.extend(f"    {label}: {a} -> {b}" for label, a, b in verdict.costs)
    out.append(f"  VERDICT: {verdict.outcome}")
    out.append("  A PASS is evidence for adoption, not adoption (ADR-023 §4). A comparison is "
               "read once; a FAIL is not re-run until it passes (R5).")
    return "\n".join(out)


EXIT_CODES = {"PASS": 0, "FAIL": 1, "NOT READABLE": 3}
