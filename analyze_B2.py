"""Summary + capture assertions for the corrected-observer B-v2 run."""
import json
import sys
from pathlib import Path

ROOT = Path(sys.argv[1])


def load(p):
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x]


checks = json.loads((ROOT / "exact-read-v2-capture-checks.json").read_text(encoding="utf-8"))
total = len(checks)
ascii_ok = sum(1 for c in checks if c["non_ascii_chars"] == 0)
exp_ok = sum(1 for c in checks if c["expected_equals_fixture"])
print(f"capture checks: {total} cases; non_ascii==0: {ascii_ok}/{total}; "
      f"expected==fixture: {exp_ok}/{total}")

fixtures = {c["id"]: c["text"]
            for c in json.loads((ROOT / "exact-read-fixtures.json").read_text(encoding="utf-8"))}

for phase, fname in (("BASELINE a43c6f4", "exact-read-baseline-v2.jsonl"),
                     ("CANDIDATE 086c355 (+ --exact-read)", "exact-read-candidate-v2.jsonl")):
    p = ROOT / fname
    if not p.exists():
        print(f"\n== {phase}: missing {fname}")
        continue
    recs = load(p)
    print(f"\n===== {phase} ({fname}, {len(recs)} cases) =====")
    for r in recs:
        eq = r.get("expected") == fixtures.get(r["case_id"])
        print(f"[{r['case_id']} {r['language']}] exit={r.get('exit_code')} "
              f"finished={r.get('finished')} stop={r.get('stopped_reason')} "
              f"expected==fixture={eq} fab={r.get('accepted_fabrication')} "
              f"exact={r.get('useful_exact_copy')}")
        print(f"   expected={r.get('expected')!r}")
        print(f"   answer  ={r.get('answer')!r}")
    fab = sum(1 for r in recs if r.get("accepted_fabrication"))
    exact = sum(1 for r in recs if r.get("useful_exact_copy"))
    nor = sum(1 for r in recs if r.get("accepted_without_target_read"))
    chg = sum(1 for r in recs if r.get("source_changed"))
    print(f"  SUMMARY {phase}: cases={len(recs)} accepted_fabrication={fab} "
          f"useful_exact_copy={exact} accepted_without_target_read={nor} source_changed={chg}")
