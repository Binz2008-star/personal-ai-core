"""Compare a candidate model's evaluation runs with a baseline's (ADR-020 §3.3-§3.5, §3.7).

    python -m personal_ai_core.app.compare BASELINE_DIR CANDIDATE_DIR

Reads committed result files only and calls no model. Each directory holds the
`raw-*.json` / `scored-*.json` pairs (and any `rescored-*.json`) of one side.

Order of work, as the ADR states it:

1. The instrument is frozen (§3.5). Every run is read with the current scorer
   (`rescored-<stamp>-<scorer>.json` if present, else its `scored-` file, if
   that was scored by it). Runs are grouped by their settings key (§3.4): cases
   version, scorer, commit, identity variant, sampling, guard, runtime, grammar,
   loaded context and machine. Both sides must have the same groups. Anything
   else is refused, naming the field that differs. Within a group, the GPU
   share of every run on both sides must lie within GPU_SHARE_TOLERANCE.
2. Each run must name verified weights (§3.1); one side is one model.
3. Per group and per case (§3.3, D2 as amended 2026-10-01):
   - a contract case regresses when its failures rise by 8 or more in 15 runs;
   - in a refusal set, a case regresses when the candidate refuses it in any
     run and the baseline never did; its other failures (the script check)
     regress when they rise by 6 or more in 9 runs.
   The thresholds keep the chance that an unchanged model is rejected near 10%
   across all gating cases, at the worst case of every case failing half the
   time and the cases independent (ADR-020 amendment 1).
4. Run counts (D3 as amended): at least 15 runs per contract group and 9 per
   refusal group, equal on both sides. Only guarded groups gate; a group run
   with the guard off is reported, never gating. The gate needs a guarded
   contract group and a guarded refusal group; without both it is INCOMPLETE,
   never PASS.

Exit codes: 0 PASS, 1 FAIL (a regression), 2 refused (the inputs cannot be
compared), 3 INCOMPLETE. Improvements are reported; they never buy back a
regression. The report is a new file; no result file is written.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO

from .evaluate import FAIL, REVIEW, SCORER_VERSION

TOOL = "ADR-020 comparison v2"
# ADR-020 amendment 1 (2026-10-01). Unit 4 measured the first rule (+2 in 5,
# or failing every run) rejecting the Boss model against itself; these are
# derived from alpha and the effect to detect, not from that run's rates.
CONTRACT_MIN_RUNS = 15
REFUSAL_MIN_RUNS = 9
CONTRACT_DELTA = 8
REFUSAL_SCRIPT_DELTA = 6
REFUSAL_CHECK = "answers"
# The probe's GPU share moves by a point or two between loads of the same
# model (0.85, 0.86). A tolerance, not rounding: rounding to one decimal put
# 0.85 and 0.86 on either side of a boundary and refused a sound comparison.
# 0.05 still separates CPU, partial and full offload.
GPU_SHARE_TOLERANCE = 0.05

PASSED, FAILED, REFUSED, INCOMPLETE = "PASS", "FAIL", "REFUSED", "INCOMPLETE"
EXIT = {PASSED: 0, FAILED: 1, REFUSED: 2, INCOMPLETE: 3}


class Refused(Exception):
    """The inputs cannot be compared. The message says why."""


@dataclass(frozen=True)
class Run:
    stamp: str
    verdict_file: str
    header: Mapping[str, Any]
    scorer: str
    cases_version: str
    results: Sequence[Mapping[str, Any]]

    @property
    def is_refusal_set(self) -> bool:
        return self.cases_version.startswith("refusal")


@dataclass
class Side:
    directory: Path
    runs: list[Run] = field(default_factory=list)


# --- reading ------------------------------------------------------------------------


def _read(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused(f"{path}: cannot be read ({type(exc).__name__})") from None


def load_side(directory: Path) -> Side:
    """Every run in `directory`, each read with the current scorer."""
    if not directory.is_dir():
        raise Refused(f"{directory}: not a directory")
    side = Side(directory)
    for raw in sorted(directory.glob("raw-*.json")):
        stamp = raw.stem[len("raw-"):]
        rescored = directory / f"rescored-{stamp}-{SCORER_VERSION}.json"
        scored = directory / f"scored-{stamp}.json"
        if rescored.is_file():
            data = _read(rescored)
            scorer = data.get("scorer")
            cases_version = (data.get("rescored_with") or {}).get("cases_version") or data[
                "header"
            ].get("cases_version")
            path = rescored
        elif scored.is_file():
            data = _read(scored)
            scorer = data["header"].get("scorer")
            cases_version = data["header"].get("cases_version")
            path = scored
        else:
            raise Refused(f"{raw.name}: no scored file beside it")
        if scorer != SCORER_VERSION:
            raise Refused(
                f"{path.name}: scored by {scorer!r}, not the current scorer {SCORER_VERSION!r}; "
                f"rescore the run first (ADR-020 §3.5)"
            )
        side.runs.append(
            Run(stamp, path.name, data["header"], str(scorer), str(cases_version),
                data["results"])
        )
    if not side.runs:
        raise Refused(f"{directory}: no runs (raw-*.json) found")
    return side


# --- the frozen instrument and the settings key (§3.4, §3.5) -------------------------


def settings_key(run: Run) -> dict[str, Any]:
    """What both sides must share for a group to be compared."""
    h = run.header
    loaded = h.get("ollama_loaded") or {}
    return {
        "cases_version": run.cases_version,
        "scorer": run.scorer,
        "commit": h.get("commit"),
        "identity_variant": h.get("identity_variant"),
        "sampling_options": h.get("sampling_options"),
        "language_guard": h.get("language_guard"),
        "runtime": h.get("runtime", "ollama"),
        "grammar": h.get("grammar", "none"),
        "context_length": loaded.get("context_length"),
        "machine": h.get("machine"),
        "profile": h.get("profile"),
    }


def _key_id(key: Mapping[str, Any]) -> str:
    return json.dumps(key, sort_keys=True, ensure_ascii=False)


def weights_identity(run: Run) -> dict[str, Any]:
    """The weights a run used. Missing `adapters` means not reported (llama.cpp)."""
    weights = run.header.get("weights")
    if not isinstance(weights, Mapping) or "verified" not in weights:
        raise Refused(f"{run.verdict_file}: no weights recorded (a run from before ADR-020)")
    if not weights.get("verified") or run.header.get("weights_unverified"):
        raise Refused(
            f"{run.verdict_file}: weights_unverified ({weights.get('reason', 'no reason')})"
        )
    adapters = weights.get("adapters")
    return {"digest": weights["digest"],
            "adapters": None if adapters is None else sorted(adapters)}


def _check_run(run: Run, expected_role: str) -> None:
    h = run.header
    role = h.get("role")
    if role != expected_role:
        raise Refused(f"{run.verdict_file}: role {role!r}, expected {expected_role!r}")
    if h.get("context_mismatch"):
        raise Refused(f"{run.verdict_file}: context_mismatch (the loaded context differed)")
    if not isinstance((h.get("ollama_loaded") or {}).get("context_length"), int):
        raise Refused(f"{run.verdict_file}: the loaded context was not confirmed")
    errors = [r["id"] for r in run.results if r.get("verdict") == "ERROR"]
    if errors:
        raise Refused(f"{run.verdict_file}: ERROR in {', '.join(errors)}; re-run it")


def _describe_side(side: Side, role: str) -> dict[str, Any]:
    for run in side.runs:
        _check_run(run, role)
    identities = {_key_id(weights_identity(r)) for r in side.runs}
    if len(identities) != 1:
        raise Refused(f"{side.directory}: runs of more than one set of weights")
    models = {r.header.get("model") for r in side.runs}
    if len(models) != 1:
        raise Refused(f"{side.directory}: runs of more than one model name: {sorted(map(str, models))}")
    return {"model": models.pop(), "weights": json.loads(identities.pop()),
            "runs": len(side.runs)}


def _groups(side: Side) -> dict[str, list[Run]]:
    groups: dict[str, list[Run]] = {}
    for run in side.runs:
        groups.setdefault(_key_id(settings_key(run)), []).append(run)
    return groups


def _differing_fields(a: Mapping[str, Any], b: Mapping[str, Any]) -> list[str]:
    return sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k))


def _match_groups(base: dict[str, list[Run]], cand: dict[str, list[Run]]) -> None:
    if base.keys() == cand.keys():
        return
    lines = []
    for only, other, label in ((base, cand, "baseline"), (cand, base, "candidate")):
        for key in only.keys() - other.keys():
            mine = json.loads(key)
            nearest = min(
                (json.loads(k) for k in other),
                key=lambda o: len(_differing_fields(mine, o)),
                default={},
            )
            fields = _differing_fields(mine, nearest)
            lines.append(
                f"a {label} group ({mine['cases_version']}, guard={mine['language_guard']}) "
                f"has no match on the other side; nearest differs in: {', '.join(fields)}"
            )
    raise Refused("; ".join(lines))


# --- the regression rule (§3.3, D2) -------------------------------------------------


def _counts(runs: Sequence[Run], refusal_set: bool) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for run in runs:
        for result in run.results:
            c = counts.setdefault(result["id"], {"fail": 0, "refused": 0, "review": 0})
            refused = refusal_set and any(
                ch.get("check") == REFUSAL_CHECK and ch.get("verdict") == FAIL
                for ch in result.get("checks", [])
            )
            if refused:
                c["refused"] += 1
            elif result["verdict"] == FAIL:
                c["fail"] += 1
            elif result["verdict"] == REVIEW:
                c["review"] += 1
    return counts


def compare_group(base: Sequence[Run], cand: Sequence[Run]) -> dict[str, Any]:
    key = settings_key(base[0])
    refusal_set = base[0].is_refusal_set
    gating = key["language_guard"] is True
    minimum = REFUSAL_MIN_RUNS if refusal_set else CONTRACT_MIN_RUNS
    delta = REFUSAL_SCRIPT_DELTA if refusal_set else CONTRACT_DELTA
    label = f"{key['cases_version']} guard={key['language_guard']}"
    if len(base) != len(cand):
        raise Refused(f"{label}: {len(base)} baseline runs, {len(cand)} candidate runs; "
                      "the rule is defined for equal counts")
    if gating and len(base) < minimum:
        raise Refused(f"{label}: {len(base)} runs per side, at least {minimum} required (D3)")
    gpu_share = _gpu_share_range(label, (*base, *cand))
    case_sets = {frozenset(r["id"] for r in run.results) for run in (*base, *cand)}
    if len(case_sets) != 1:
        raise Refused(f"{label}: the runs do not all have the same cases (was --only used?)")
    b, c = _counts(base, refusal_set), _counts(cand, refusal_set)
    n = len(cand)
    cases, regressions, improvements = [], [], []
    for case_id in sorted(b):
        bc, cc = b[case_id], c[case_id]
        reason = None
        if refusal_set and cc["refused"] and not bc["refused"]:
            reason = f"refused in {cc['refused']} of {n} runs; never in the baseline"
        elif cc["fail"] - bc["fail"] >= delta:
            reason = f"failures {bc['fail']} -> {cc['fail']} of {n}"
        improved = reason is None and (
            cc["fail"] < bc["fail"] or (refusal_set and cc["refused"] < bc["refused"])
        )
        if reason:
            regressions.append(case_id)
        if improved:
            improvements.append(case_id)
        cases.append({"id": case_id, "baseline": bc, "candidate": cc,
                      "regression": reason, "improved": improved})
    return {
        "key": key,
        "refusal_set": refusal_set,
        # A group run with the guard off shows what the weights do alone
        # (§3.4); its regressions are reported and do not decide the gate.
        "gating": gating,
        "runs": {"baseline": [r.stamp for r in base], "candidate": [r.stamp for r in cand]},
        "gpu_share": gpu_share,
        "regressions": regressions,
        "improvements": improvements,
        "cases": cases,
    }


def _gpu_share_range(label: str, runs: Sequence[Run]) -> dict[str, float] | None:
    """The group's GPU share, min and max over both sides; refused if too wide."""
    shares = [(r.header.get("ollama_loaded") or {}).get("gpu_share") for r in runs]
    known = [float(s) for s in shares if s is not None]
    if not known:
        return None  # llama.cpp reports no share
    if len(known) != len(shares):
        raise Refused(f"{label}: gpu_share is reported for some runs and not others")
    low, high = min(known), max(known)
    if high - low > GPU_SHARE_TOLERANCE + 1e-9:
        raise Refused(f"{label}: gpu_share ranges {low:.2f}-{high:.2f} across the runs, "
                      f"more than {GPU_SHARE_TOLERANCE}")
    return {"min": low, "max": high}


def _missing(groups: Sequence[Mapping[str, Any]]) -> list[str]:
    have = {(g["refusal_set"], g["key"]["language_guard"]) for g in groups}
    wanted = {
        (False, True): "contract runs with the guard on",
        (True, True): "refusal runs with the guard on",
    }
    return [label for need, label in wanted.items() if need not in have]


def compare(baseline_dir: Path, candidate_dir: Path) -> dict[str, Any]:
    """The comparison report. Raises Refused when the inputs cannot be compared."""
    base, cand = load_side(baseline_dir), load_side(candidate_dir)
    base_desc = _describe_side(base, "boss")
    cand_desc = _describe_side(cand, "candidate")
    base_groups, cand_groups = _groups(base), _groups(cand)
    _match_groups(base_groups, cand_groups)
    groups = [compare_group(base_groups[k], cand_groups[k]) for k in sorted(base_groups)]
    missing = _missing(groups)
    regressed = any(g["regressions"] for g in groups if g["gating"])
    result = FAILED if regressed else INCOMPLETE if missing else PASSED
    adapters_known = (base_desc["weights"]["adapters"] is not None
                      and cand_desc["weights"]["adapters"] is not None)
    return {
        "tool": TOOL,
        "scorer": SCORER_VERSION,
        "baseline_dir": baseline_dir.as_posix(),
        "candidate_dir": candidate_dir.as_posix(),
        "baseline": base_desc,
        "candidate": cand_desc,
        "same_weights": base_desc["weights"] == cand_desc["weights"],
        # llama.cpp does not report a --lora adapter: identical digests there
        # do not prove identical weights.
        "adapters_known": adapters_known,
        "rule": {"contract_delta": CONTRACT_DELTA, "contract_min_runs": CONTRACT_MIN_RUNS,
                 "refusal_script_delta": REFUSAL_SCRIPT_DELTA,
                 "refusal_min_runs": REFUSAL_MIN_RUNS,
                 "refusal": "any refusal the baseline did not have",
                 "gating": "guarded groups only; unguarded groups are descriptive"},
        "groups": groups,
        "missing_groups": missing,
        "result": result,
    }


# --- entry point -------------------------------------------------------------------


def _print(report: Mapping[str, Any], out: TextIO) -> None:
    b, c = report["baseline"], report["candidate"]
    print(f"baseline:  {b['model']}  {b['weights']['digest']}  ({b['runs']} runs)", file=out)
    print(f"candidate: {c['model']}  {c['weights']['digest']}  ({c['runs']} runs)", file=out)
    if report["same_weights"]:
        print("same weights on both sides (a self-comparison)", file=out)
    if not report["adapters_known"]:
        print("note: adapters not reported on one side (llama.cpp); a LoRA there is "
              "not visible in the digest", file=out)
    for g in report["groups"]:
        k = g["key"]
        print(f"\n{k['cases_version']}  guard={k['language_guard']}  "
              f"runs={len(g['runs']['candidate'])} per side"
              + ("" if g["gating"] else "  (descriptive: does not decide the gate)"), file=out)
        for case in g["cases"]:
            bc, cc = case["baseline"], case["candidate"]
            if case["regression"] or case["improved"] or bc != cc:
                mark = "REGRESSION" if case["regression"] else (
                    "improved" if case["improved"] else "changed")
                print(f"  {mark:10} {case['id']}: fail {bc['fail']}->{cc['fail']}  "
                      f"refused {bc['refused']}->{cc['refused']}  "
                      f"review {bc['review']}->{cc['review']}"
                      + (f"  ({case['regression']})" if case["regression"] else ""),
                      file=out)
    for label in report["missing_groups"]:
        print(f"missing: {label}", file=out)
    print(f"\nresult: {report['result']}", file=out)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m personal_ai_core.app.compare",
        description="Compare a candidate's evaluation runs with a baseline's (ADR-020).",
    )
    parser.add_argument("baseline", type=Path, help="directory of the baseline's runs")
    parser.add_argument("candidate", type=Path, help="directory of the candidate's runs")
    parser.add_argument(
        "--out", type=Path, default=None,
        help="report file (default: comparison-<UTC time>.json beside the candidate directory)",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    now: Callable[[], datetime] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    out = stdout if stdout is not None else sys.stdout
    try:
        report = compare(args.baseline, args.candidate)
    except Refused as exc:
        print(f"refused: {exc}", file=out)
        print(f"result: {REFUSED}", file=out)
        return EXIT[REFUSED]
    stamp = (now or (lambda: datetime.now(timezone.utc)))().strftime("%Y%m%dT%H%M%SZ")
    target = args.out or args.candidate.resolve().parent / f"comparison-{stamp}.json"
    if target.exists():
        print(f"refused: {target} exists; a report is never overwritten", file=out)
        return EXIT[REFUSED]
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _print(report, out)
    print(f"report: {target}", file=out)
    return EXIT[report["result"]]


if __name__ == "__main__":
    sys.exit(main())
