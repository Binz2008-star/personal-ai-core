"""Did the rig change between two runs of the same behaviour? (an instrument check)

    python -m personal_ai_core.app.bench.replication EARLIER.jsonl LATER.jsonl

For two result files whose code behaves the same -- the second re-runs the
first, at a commit that changed only what is recorded -- every difference in
success is run-to-run variation, unless something on the rig changed between
them (the model, its context, the server, the machine). This reads which.

The rule, fixed on 2026-10-03 before the first such pair had been read (the
re-run of #189 at 1861756, after #193 changed only what is recorded):

- Eight cells: track (agent, knowledge) x language (en, ar), here per file
  pair; successes out of the runs in each cell.
- A cell FLAGS when the two files' successes differ by FLAG_DELTA (15) or
  more, in either direction. Derived with `gate_calibration` the way ADR-020
  amendment 1 derives its gate: 60 runs per side, every cell at its worst
  base rate (p = 0.5), cells independent, both directions counted, over 4
  cells per file pair and two pairs -- a family-wise false-alarm rate of 6.1%
  (at 14 it would exceed 10%).
- The header fields that make two runs one instrument must match: model,
  scorer, runs, languages, the context the owner measured, the environment
  context, and the weights' digest the end record names.

What it can and cannot say. A flag says the rig differed between the runs, and
no comparison across the pair is read until that is explained. No flag does
NOT say the rig was the same: at 60 runs a move from 20% to 40% success is
caught about 31% of the time. The cell differences are then recorded as the
observed run-to-run spread. It reads two files; it decides nothing about any
control.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence, TextIO

from ..gate_calibration import family_false_rejection
from .runner import _read

FLAG_DELTA = 15
CELLS_PER_PAIR = 4          # track x language
PAIRS = 2                   # unit 1, unit 1 + 2
RUNS_PER_CELL = 60
INSTRUMENT = ("model", "scorer", "runs", "languages", "num_ctx_measured_by_owner",
              "environment_context")


def family_false_alarm(delta: int = FLAG_DELTA) -> float:
    """Both directions of every cell, worst base rate, cells independent."""
    return family_false_rejection([(RUNS_PER_CELL, delta, 2 * CELLS_PER_PAIR * PAIRS)])


def cells(lines: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str], tuple[int, int]]:
    """(track, language) -> (successes, runs)."""
    out: dict[tuple[str, str], list[int]] = {}
    for run in lines:
        if run.get("kind") != "run":
            continue
        cell = out.setdefault((run["track"], run["language"]), [0, 0])
        cell[0] += bool(run.get("success"))
        cell[1] += 1
    return {key: (s, n) for key, (s, n) in out.items()}


def _weights(lines: Sequence[Mapping[str, Any]]) -> Any:
    end = next((x for x in lines if x.get("kind") == "end"), {})
    return end.get("weights")


def instrument_differences(a: Sequence[Mapping[str, Any]], b: Sequence[Mapping[str, Any]]) -> list[str]:
    ha, hb = a[0] if a else {}, b[0] if b else {}
    found = [f"{key}: {ha.get(key)!r} | {hb.get(key)!r}" for key in INSTRUMENT
             if ha.get(key) != hb.get(key)]
    if _weights(a) != _weights(b):
        found.append(f"weights: {_weights(a)!r} | {_weights(b)!r}")
    return found


def compare(a: Sequence[Mapping[str, Any]], b: Sequence[Mapping[str, Any]]) -> tuple[list[str], bool]:
    """The report lines, and whether anything flagged."""
    out = []
    differences = instrument_differences(a, b)
    for line in differences:
        out.append(f"  INSTRUMENT DIFFERS  {line}")
    ca, cb = cells(a), cells(b)
    flagged = bool(differences)
    out.append("  cell               earlier    later   difference")
    for key in sorted(set(ca) | set(cb)):
        (sa, na), (sb, nb) = ca.get(key, (0, 0)), cb.get(key, (0, 0))
        diff = sb - sa
        flag = abs(diff) >= FLAG_DELTA or na != nb
        flagged |= flag
        out.append(f"  {key[0]:9} {key[1]:3}    {sa:>3}/{na:<3}  {sb:>3}/{nb:<3}  {diff:+4}"
                   f"{'   FLAG' if flag else ''}")
    return out, flagged


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m personal_ai_core.app.bench.replication",
        description="Did the rig change between two runs of the same behaviour?")
    parser.add_argument("earlier", type=Path)
    parser.add_argument("later", type=Path)
    args = parser.parse_args(argv)
    out = stdout or sys.stdout
    lines, flagged = compare(_read(args.earlier), _read(args.later))
    print(f"earlier: {args.earlier}\nlater:   {args.later}", file=out)
    print("\n".join(lines), file=out)
    print(f"A cell flags at a difference of {FLAG_DELTA} or more (family-wise false alarm "
          f"{family_false_alarm():.1%} at the worst base rate). "
          + ("FLAGGED: the rig differed; read nothing across this pair until explained."
             if flagged else
             "Nothing flagged: the differences are the observed run-to-run spread. "
             "This does not prove the rig was the same."), file=out)
    return 1 if flagged else 0


if __name__ == "__main__":
    raise SystemExit(main())
