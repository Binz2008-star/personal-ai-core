"""The instrument check between two runs of the same behaviour: its rule, fixed before the data."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path

from personal_ai_core.app.bench import replication
from personal_ai_core.app.gate_calibration import rise_probability

REPO = Path(__file__).resolve().parents[2]
BENCH = REPO / "evals" / "results" / "bench"
UNIT1 = BENCH / "bench-20261003T132852Z.jsonl"
UNIT1_AND_2 = BENCH / "bench-20261003T135502Z.jsonl"


def test_the_threshold_is_the_smallest_that_keeps_the_family_under_ten_percent():
    assert round(replication.family_false_alarm(15), 3) == 0.061
    assert replication.family_false_alarm(14) > 0.10


def test_what_no_flag_cannot_say_is_stated_with_its_number():
    """The docstring's 'about 31%': a 20% -> 40% move is usually missed."""
    assert round(rise_probability(60, 15, 0.2, 0.4), 2) == 0.31


def _file(successes: dict[tuple[str, str], int], **header) -> list[dict]:
    lines = [{"kind": "header", "model": "m", "scorer": "s", "runs": 5, "languages": ["en", "ar"],
              "num_ctx_measured_by_owner": 8192, "environment_context": False, **header}]
    for (track, language), wins in successes.items():
        lines += [{"kind": "run", "track": track, "language": language, "success": i < wins}
                  for i in range(60)]
    lines.append({"kind": "end", "weights": "sha256:x"})
    return lines


CELLS = {("agent", "en"): 15, ("agent", "ar"): 11, ("knowledge", "en"): 58, ("knowledge", "ar"): 57}


def test_a_difference_below_the_threshold_is_spread_not_a_flag():
    later = {**CELLS, ("agent", "en"): 15 + 14}
    _, flagged = replication.compare(_file(CELLS), _file(later))
    assert not flagged


def test_a_difference_at_the_threshold_flags_in_either_direction():
    for delta in (15, -15):
        later = {**CELLS, ("agent", "ar"): 30 + delta}
        _, flagged = replication.compare(_file({**CELLS, ("agent", "ar"): 30}), _file(later))
        assert flagged, delta


def test_a_different_instrument_flags_whatever_the_counts():
    lines, flagged = replication.compare(_file(CELLS), _file(CELLS, model="other"))
    assert flagged and any("INSTRUMENT DIFFERS  model" in line for line in lines)
    other_weights = _file(CELLS)
    other_weights[-1]["weights"] = "sha256:y"
    _, flagged = replication.compare(_file(CELLS), other_weights)
    assert flagged


def test_unit_1_against_unit_1_and_2_is_not_a_replication_pair():
    """The environment context differs, so the check refuses to read it as one."""
    out = io.StringIO()
    assert replication.main([str(UNIT1), str(UNIT1_AND_2)], stdout=out) == 1
    assert "INSTRUMENT DIFFERS  environment_context" in out.getvalue()


def test_a_file_against_itself_flags_nothing_and_is_left_as_it_was():
    before = hashlib.sha256(UNIT1.read_bytes()).hexdigest()
    out = io.StringIO()
    assert replication.main([str(UNIT1), str(UNIT1)], stdout=out) == 0
    assert hashlib.sha256(UNIT1.read_bytes()).hexdigest() == before
    assert "Nothing flagged" in out.getvalue() and "does not prove" in out.getvalue()
    assert any(line.split() == ["agent", "en", "15/60", "15/60", "+0"]
               for line in out.getvalue().splitlines())
