"""ADR-020 unit 3: the comparison tool, on hand-built result files."""
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from personal_ai_core.app import compare as cmp
from personal_ai_core.app.evaluate import SCORER_VERSION

BOSS = "boss/model:7b"
CANDIDATE = "boss/model-lora:7b"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
CONTRACT = ["case-1", "case-2", "case-3"]
REFUSAL = ["refusal-x-en", "refusal-x-ar"]
FIXED = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def _header(role, model, digest, cases_version, guard, **over):
    header = {
        "cases_version": cases_version, "scorer": SCORER_VERSION, "commit": "c" * 40,
        "model": model, "role": role, "identity_variant": "B",
        "sampling_options": {"temperature": 0.7}, "language_guard": guard,
        "runtime": "ollama", "grammar": "none",
        "ollama_loaded": {"probed": True, "context_length": 8192, "gpu_share": 0.85},
        "context_mismatch": False, "machine": {"system": "Windows"}, "profile": "none",
        "weights": {"digest": digest, "source": "ollama-blob", "verified": True,
                    "adapters": [], "manifest_digest": None},
        "weights_unverified": False, "provider": "ollama",
    }
    header.update(over)
    return header


def _result(case_id, verdict, refused=False):
    checks = [{"check": "answers", "verdict": "FAIL" if refused else "PASS", "detail": ""}]
    return {"id": case_id, "rule": "r", "verdict": "FAIL" if refused else verdict,
            "checks": checks}


class SideBuilder:
    def __init__(self, folder, role, model, digest):
        self.folder, self.role, self.model, self.digest = folder, role, model, digest
        folder.mkdir(parents=True, exist_ok=True)
        self.n = 0

    def run(self, cases, guard=True, fails=(), refused=(), reviews=(), **over):
        self.n += 1
        stamp = f"20261002T{self.n:06d}Z"
        version = "refusal-v2" if cases is REFUSAL else "contract-v1"
        header = _header(self.role, self.model, self.digest, version, guard, **over)
        results = [
            _result(c, "FAIL" if c in fails else "REVIEW" if c in reviews else "PASS",
                    refused=c in refused)
            for c in cases
        ]
        (self.folder / f"raw-{stamp}.json").write_text(
            json.dumps({"header": header, "records": []}), encoding="utf-8")
        (self.folder / f"scored-{stamp}.json").write_text(
            json.dumps({"header": header, "results": results}), encoding="utf-8")
        return stamp

    def full(self, contract_fails=None, unguarded_fails=None, refused=None):
        contract_fails = contract_fails or [()] * 5
        unguarded_fails = unguarded_fails or [()] * 5
        refused = refused or [()] * 3
        for fails in contract_fails:
            self.run(CONTRACT, fails=fails)
        for fails in unguarded_fails:
            self.run(CONTRACT, guard=False, fails=fails)
        for r in refused:
            self.run(REFUSAL, refused=r)
        return self


def _sides(tmp_path, cand_model=CANDIDATE, cand_digest=DIGEST_B):
    return (SideBuilder(tmp_path / "base", "boss", BOSS, DIGEST_A),
            SideBuilder(tmp_path / "cand", "candidate", cand_model, cand_digest))


def _compare(tmp_path) -> tuple[int, str, Any]:
    out = io.StringIO()
    code = cmp.main([str(tmp_path / "base"), str(tmp_path / "cand"),
                     "--out", str(tmp_path / "report.json")], stdout=out, now=lambda: FIXED)
    report = (json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
              if (tmp_path / "report.json").exists() else None)
    return code, out.getvalue(), report


# --- the gate's result -------------------------------------------------------------


def test_identical_behaviour_passes(tmp_path):
    base, cand = _sides(tmp_path)
    base.full()
    cand.full()
    code, output, report = _compare(tmp_path)
    assert code == 0 and report["result"] == "PASS"
    assert "result: PASS" in output
    assert report["same_weights"] is False and report["adapters_known"] is True
    assert len(report["groups"]) == 3


def test_a_self_comparison_reports_the_same_weights(tmp_path):
    base, cand = _sides(tmp_path, cand_model=BOSS, cand_digest=DIGEST_A)
    base.full()
    cand.full()
    code, output, report = _compare(tmp_path)
    assert code == 0 and report["same_weights"] is True
    assert "self-comparison" in output


def test_one_extra_failure_in_five_is_noise(tmp_path):
    base, cand = _sides(tmp_path)
    base.full()
    cand.full(contract_fails=[("case-1",), (), (), (), ()])
    code, _, report = _compare(tmp_path)
    assert code == 0
    case = next(c for g in report["groups"] for c in g["cases"]
                if c["id"] == "case-1" and not g["refusal_set"] and g["key"]["language_guard"])
    assert case["candidate"]["fail"] == 1 and case["regression"] is None


def test_two_extra_failures_in_five_regress(tmp_path):
    base, cand = _sides(tmp_path)
    base.full()
    cand.full(contract_fails=[("case-1",), ("case-1",), (), (), ()])
    code, output, report = _compare(tmp_path)
    assert code == 1 and report["result"] == "FAIL"
    assert "REGRESSION case-1" in " ".join(output.split())
    regressions = [g["regressions"] for g in report["groups"]]
    assert ["case-1"] in regressions


def test_a_case_failing_every_candidate_run_regresses_whatever_the_baseline(tmp_path):
    base, cand = _sides(tmp_path)
    always = [("case-2",)] * 5
    base.full(contract_fails=always)
    cand.full(contract_fails=always)
    code, _, report = _compare(tmp_path)
    assert code == 1
    assert any("every candidate run" in (c["regression"] or "")
               for g in report["groups"] for c in g["cases"])


def test_any_new_refusal_regresses(tmp_path):
    base, cand = _sides(tmp_path)
    base.full()
    cand.full(refused=[("refusal-x-ar",), (), ()])
    code, _, report = _compare(tmp_path)
    assert code == 1
    group = next(g for g in report["groups"] if g["refusal_set"])
    assert group["regressions"] == ["refusal-x-ar"]


def test_a_refusal_the_baseline_also_had_is_not_new(tmp_path):
    base, cand = _sides(tmp_path)
    base.full(refused=[("refusal-x-en",), (), ()])
    cand.full(refused=[(), ("refusal-x-en",), ()])
    code, _, report = _compare(tmp_path)
    assert code == 0


def test_improvements_are_reported_and_do_not_buy_back_a_regression(tmp_path):
    base, cand = _sides(tmp_path)
    base.full(contract_fails=[("case-3",)] * 4 + [()])
    cand.full(contract_fails=[("case-1",), ("case-1",), (), (), ()])
    code, _, report = _compare(tmp_path)
    assert code == 1
    group = next(g for g in report["groups"]
                 if not g["refusal_set"] and g["key"]["language_guard"])
    assert group["improvements"] == ["case-3"] and group["regressions"] == ["case-1"]


def test_without_the_unguarded_pair_the_gate_is_incomplete_not_pass(tmp_path):
    base, cand = _sides(tmp_path)
    for side in (base, cand):
        for _ in range(5):
            side.run(CONTRACT)
        for _ in range(3):
            side.run(REFUSAL)
    code, output, report = _compare(tmp_path)
    assert code == 3 and report["result"] == "INCOMPLETE"
    assert "guard off" in output


# --- what is refused (§3.1, §3.4, §3.5, D3) ----------------------------------------


def _refused(tmp_path):
    code, output, report = _compare(tmp_path)
    assert code == 2 and report is None
    assert "result: REFUSED" in output
    return output


def test_unverified_weights_are_refused(tmp_path):
    base, cand = _sides(tmp_path)
    base.full()
    cand.full()
    cand.run(CONTRACT, weights={"digest": None, "verified": False, "reason": "no digest"},
             weights_unverified=True)
    assert "weights_unverified" in _refused(tmp_path)


def test_a_run_from_before_weights_were_recorded_is_refused(tmp_path):
    base, cand = _sides(tmp_path)
    base.full()
    cand.full()
    cand.run(CONTRACT, weights=None)
    assert "no weights recorded" in _refused(tmp_path)


def test_two_sets_of_weights_on_one_side_are_refused(tmp_path):
    base, cand = _sides(tmp_path)
    base.full()
    cand.full()
    cand.digest = "sha256:" + "d" * 64
    cand.run(CONTRACT)
    assert "more than one set of weights" in _refused(tmp_path)


@pytest.mark.parametrize(
    "field, value",
    [
        ("commit", "d" * 40),
        ("identity_variant", "A"),
        ("sampling_options", {"temperature": 0.0}),
        ("runtime", "llamacpp"),
        ("ollama_loaded", {"probed": True, "context_length": 8192, "gpu_share": 0.0}),
    ],
)
def test_a_settings_difference_is_refused_and_named(tmp_path, field, value):
    base, cand = _sides(tmp_path)
    base.full()
    for _ in range(5):
        cand.run(CONTRACT, **{field: value})
    for _ in range(5):
        cand.run(CONTRACT, guard=False, **{field: value})
    for _ in range(3):
        cand.run(REFUSAL, **{field: value})
    output = _refused(tmp_path)
    named = "gpu_share" if field == "ollama_loaded" else field
    assert named in output


def test_a_run_scored_by_an_older_scorer_is_refused_until_rescored(tmp_path):
    base, cand = _sides(tmp_path)
    base.full()
    cand.full()
    cand.run(CONTRACT, scorer="contract-checks-v1")
    assert "rescore the run first" in _refused(tmp_path)


def test_a_rescored_file_with_the_current_scorer_is_used(tmp_path):
    base, cand = _sides(tmp_path)
    base.full()
    cand.full()
    stamp = cand.run(CONTRACT, scorer="contract-checks-v1")
    header = json.loads((cand.folder / f"scored-{stamp}.json").read_text("utf-8"))["header"]
    (cand.folder / f"rescored-{stamp}-{SCORER_VERSION}.json").write_text(json.dumps({
        "header": header, "scorer": SCORER_VERSION,
        "rescored_with": {"cases_version": "contract-v1"},
        "results": [_result(c, "PASS") for c in CONTRACT],
    }), encoding="utf-8")
    base.run(CONTRACT)  # keep the counts equal
    code, _, report = _compare(tmp_path)
    assert code == 0, _
    group = next(g for g in report["groups"]
                 if not g["refusal_set"] and g["key"]["language_guard"])
    assert stamp in group["runs"]["candidate"]


def test_unequal_run_counts_are_refused(tmp_path):
    base, cand = _sides(tmp_path)
    base.full()
    cand.full()
    cand.run(CONTRACT)
    assert "equal counts" in _refused(tmp_path)


def test_too_few_runs_are_refused(tmp_path):
    base, cand = _sides(tmp_path)
    base.full(contract_fails=[()] * 4)
    cand.full(contract_fails=[()] * 4)
    assert "at least 5 required" in _refused(tmp_path)


def test_runs_with_different_cases_are_refused(tmp_path):
    base, cand = _sides(tmp_path)
    base.full()
    cand.full(contract_fails=[()] * 4)
    cand.run(CONTRACT[:2])
    assert "same cases" in _refused(tmp_path)


def test_the_baseline_must_be_the_boss_and_the_candidate_a_candidate(tmp_path):
    base, cand = _sides(tmp_path)
    base.full()
    cand.role = "boss"
    cand.full()
    assert "expected 'candidate'" in _refused(tmp_path)


def test_an_errored_run_is_refused(tmp_path):
    base, cand = _sides(tmp_path)
    base.full()
    cand.full()
    stamp = cand.run(CONTRACT)
    path = cand.folder / f"scored-{stamp}.json"
    data = json.loads(path.read_text("utf-8"))
    data["results"][0]["verdict"] = "ERROR"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert "re-run it" in _refused(tmp_path)


def test_adapters_not_reported_are_flagged(tmp_path):
    base = SideBuilder(tmp_path / "base", "boss", BOSS, DIGEST_A)
    cand = SideBuilder(tmp_path / "cand", "candidate", BOSS, DIGEST_A)
    llamacpp = {"digest": DIGEST_A, "source": "ollama-blob", "verified": True}
    for side in (base, cand):
        for _ in range(5):
            side.run(CONTRACT, weights=llamacpp, runtime="llamacpp")
        for _ in range(5):
            side.run(CONTRACT, guard=False, weights=llamacpp, runtime="llamacpp")
        for _ in range(3):
            side.run(REFUSAL, weights=llamacpp, runtime="llamacpp")
    code, output, report = _compare(tmp_path)
    assert code == 0 and report["adapters_known"] is False
    assert "LoRA there is not visible" in output


def test_a_report_is_never_overwritten(tmp_path):
    base, cand = _sides(tmp_path)
    base.full()
    cand.full()
    (tmp_path / "report.json").write_text("{}", encoding="utf-8")
    out = io.StringIO()
    code = cmp.main([str(tmp_path / "base"), str(tmp_path / "cand"),
                     "--out", str(tmp_path / "report.json")], stdout=out)
    assert code == 2 and (tmp_path / "report.json").read_text(encoding="utf-8") == "{}"


def test_the_tool_reads_and_never_writes_a_result_file(tmp_path):
    base, cand = _sides(tmp_path)
    base.full()
    cand.full()
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    _compare(tmp_path)
    assert {p: p.read_bytes() for p in before} == before
    assert set(tmp_path.rglob("*.json")) - set(before) == {tmp_path / "report.json"}


def test_more_runs_refusing_a_case_the_baseline_refused_is_not_new(tmp_path):
    # A case counts as refused if any run refuses it (evals/README.md); only a
    # refusal the baseline never had is new (ADR-020 section 3.3).
    base, cand = _sides(tmp_path)
    base.full(refused=[("refusal-x-en",), (), ()])
    cand.full(refused=[("refusal-x-en",), ("refusal-x-en",), ()])
    code, _, _ = _compare(tmp_path)
    assert code == 0


def test_weights_marked_unverified_inside_are_refused_even_without_the_flag(tmp_path):
    base, cand = _sides(tmp_path)
    base.full()
    cand.full()
    cand.run(CONTRACT, weights={"digest": None, "verified": False, "reason": "x"},
             weights_unverified=False)
    assert "weights_unverified" in _refused(tmp_path)


# --- end to end: the harness's own files are what the tool reads -------------------


def test_the_harness_output_is_accepted_by_the_comparison(tmp_path):
    """A self-comparison laid out as evals/README.md says, written by the real
    harness with a fake model. The tool must compare it, not refuse it: if the
    two drift apart in format, this fails before a rig run is wasted."""
    from datetime import timedelta

    from personal_ai_core.app import evaluate as ev

    digest = "e" * 64

    def probe(url, body=None):
        if url.endswith("/api/show"):
            return {"modelfile": f"FROM /blobs/sha256-{digest}\n"}
        return {"models": [{"name": ev.DEFAULT_BOSS_MODEL, "model": ev.DEFAULT_BOSS_MODEL,
                            "size": 10, "size_vram": 8, "context_length": 8192,
                            "digest": "f" * 64}]}

    def model(url, payload, timeout):
        return {"model": payload["model"], "message": {"content": "A plain answer. " * 8},
                "done_reason": "stop", "prompt_eval_count": 1, "eval_count": 1}

    clock = [FIXED]

    def now():
        clock[0] += timedelta(minutes=1)
        return clock[0]

    cases = Path(__file__).resolve().parents[2] / "evals" / "cases"
    contract = ["--cases", str(cases / "contract_v1.json")]
    refusal = ["--cases", str(cases / "refusal_v2.json")]
    plan = [contract] * 5 + [contract + ["--no-language-guard"]] * 5 + [refusal] * 3
    for side, extra in (("base", []), ("cand", ["--candidate", ev.DEFAULT_BOSS_MODEL])):
        for argv in plan:
            code = ev.main([*argv, "--num-ctx", "8192", "--out", str(tmp_path / side), *extra],
                           transport=model, probe=probe, stdout=io.StringIO(), env={},
                           now=now, commit="1" * 40)
            assert code == 0
    code, output, report = _compare(tmp_path)
    assert code != 2, output
    assert report["same_weights"] is True and report["missing_groups"] == []
    assert sorted((g["refusal_set"], g["key"]["language_guard"]) for g in report["groups"]) == [
        (False, False), (False, True), (True, True)]
