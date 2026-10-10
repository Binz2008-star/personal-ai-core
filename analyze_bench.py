"""Generic read-only analyzer for a single bench JSONL result file.

usage: python analyze_bench.py <result.jsonl> [label]
"""
import json
import sys
from pathlib import Path


def load(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def main():
    path = Path(sys.argv[1])
    label = sys.argv[2] if len(sys.argv) > 2 else path.stem
    lines = load(path)
    header = next((x for x in lines if x.get("kind") == "header"), {})
    end = next((x for x in lines if x.get("kind") == "end"), {})
    print(f"===== {label} ({path.name}) =====")
    print(f"commit={header.get('commit')} model={header.get('model')} "
          f"num_ctx_sent={header.get('num_ctx_sent_by_core')} "
          f"num_ctx_measured={header.get('num_ctx_measured_by_owner')} "
          f"verify_completion={header.get('verify_completion')}")
    print(f"context_mismatch={end.get('context_mismatch')} "
          f"loaded_ctx={end.get('ollama_loaded', {}).get('context_length')} "
          f"weights={end.get('weights', {}).get('digest')}")
    runs = [r for r in lines if r.get("kind") == "run"]
    passed = sum(1 for r in runs if r.get("success"))
    print(f"overall: {passed}/{len(runs)} ({100*passed/len(runs):.0f}%)" if runs else "no runs")
    for r in runs:
        print(f"\n[{r['task']} {r['language']} run {r['run']}] success={r['success']} "
              f"stop={r.get('stop')} reason={r.get('stopped_reason')} seconds={r.get('seconds')}")
        print(f"  signals={r.get('signals')} protocol_errors={r.get('protocol_errors')} "
              f"action_rejections={r.get('action_rejections')}")
        print(f"  answer={r.get('answer')!r}")
        for c in r.get("checks", []):
            print(f"    check {c['check']}: {c['verdict']} ({c.get('detail')})")
        for i, s in enumerate(r.get("steps", []), 1):
            print(f"    step{i}: tool={s['tool']} decision={s['decision']} executed={s['executed']} "
                  f"ok={s['ok']} error={s.get('error')} "
                  f"args={json.dumps(s.get('arguments'), ensure_ascii=False)[:150]}")
            if s.get("output"):
                print(f"        output={str(s.get('output'))[:150]!r}")
        for j, x in enumerate(r.get("refused_replies") or [], 1):
            print(f"    refused{j}: kind={x.get('kind')} error={x.get('error')} "
                  f"text={str(x.get('text'))[:240]!r}")
        if r.get("verification_rejections"):
            print(f"  verification_rejections={r['verification_rejections']}")
        if r.get("error"):
            print(f"  provider error: {r['error']}")


main()
