"""Read-only analysis of the Comparison B exact-read pilot records."""
import json
import sys
from pathlib import Path

RUN_ROOT = Path(sys.argv[1])


def load(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def summarize(path, label):
    if not path.exists():
        print(f"\n===== {label} ===== missing: {path}")
        return
    recs = load(path)
    print(f"\n===== {label} ===== ({path.name}, {len(recs)} cases)")
    for r in recs:
        md = r.get("model_details") or {}
        loaded = r.get("loaded") or {}
        print(f"\n[{r['case_id']} {r['language']}] commit={r['commit'][:12]} "
              f"exit={r.get('exit_code')} finished={r.get('finished')} "
              f"stop={r.get('stopped_reason')}")
        print(f"  accepted_fabrication={r.get('accepted_fabrication')} "
              f"useful_exact_copy={r.get('useful_exact_copy')} "
              f"source_changed={r.get('source_changed')} "
              f"accepted_without_target_read={r.get('accepted_without_target_read')}")
        print(f"  configured_context={r.get('configured_context')} "
              f"loaded_ctx={loaded.get('context_length')} "
              f"ctx_mismatch={r.get('configured_context') != loaded.get('context_length')}")
        print(f"  model={r.get('model')} quant={md.get('quantization_level')} "
              f"family={md.get('family')} param={md.get('parameter_size')} "
              f"probe_error={md.get('probe_error')}")
        print(f"  expected={r.get('expected')!r}")
        print(f"  answer={r.get('answer')!r}")
        for i, s in enumerate(r.get("steps", []), 1):
            print(f"    step{i}: tool={s['tool']} decision={s['decision']} "
                  f"executed={s['executed']} verified={s['verified']} ok={s['ok']} "
                  f"truncated={s['truncated']} error={s.get('error')} "
                  f"args={json.dumps(s.get('arguments'), ensure_ascii=False)[:160]}")
            if s.get("output"):
                print(f"        output={str(s.get('output'))[:160]!r}")
        for j, x in enumerate(r.get("refused_replies") or [], 1):
            print(f"    refused{j}: kind={x.get('kind')} error={x.get('error')} "
                  f"text={str(x.get('text'))[:260]!r}")
    # aggregate
    fab = sum(1 for r in recs if r.get("accepted_fabrication"))
    exact = sum(1 for r in recs if r.get("useful_exact_copy"))
    nor = sum(1 for r in recs if r.get("accepted_without_target_read"))
    changed = sum(1 for r in recs if r.get("source_changed"))
    print(f"\n  SUMMARY {label}: cases={len(recs)} accepted_fabrication={fab} "
          f"useful_exact_copy={exact} accepted_without_target_read={nor} source_changed={changed}")


summarize(RUN_ROOT / "exact-read-baseline.jsonl", "BASELINE a43c6f4")
summarize(RUN_ROOT / "exact-read-candidate.jsonl", "CANDIDATE 086c355 (+ --exact-read)")
