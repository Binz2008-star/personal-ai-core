"""Read-only analysis of the Comparison A result files. No model calls."""
import json
import sys
from pathlib import Path

RUN_ROOT = Path(sys.argv[1])


def load(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def summarize(path, label):
    lines = load(path)
    header = next((x for x in lines if x.get("kind") == "header"), {})
    end = next((x for x in lines if x.get("kind") == "end"), {})
    print(f"\n===== {label} =====")
    print(f"file: {path.name}")
    print(f"commit={header.get('commit')} model={header.get('model')} "
          f"num_ctx_sent={header.get('num_ctx_sent_by_core')} "
          f"num_ctx_measured={header.get('num_ctx_measured_by_owner')}")
    print(f"context_mismatch={end.get('context_mismatch')} "
          f"loaded_ctx={end.get('ollama_loaded', {}).get('context_length')} "
          f"weights={end.get('weights', {}).get('digest')}")
    for tid, meta in header.get("tasks", {}).items():
        print(f"  task {tid}: digest={meta.get('digest')} category={meta.get('category')}")
    for r in lines:
        if r.get("kind") != "run":
            continue
        print(f"\n[{r['task']} {r['language']} run {r['run']}] "
              f"success={r['success']} stop={r.get('stop')} "
              f"reason={r.get('stopped_reason')} seconds={r.get('seconds')}")
        print(f"  signals={r.get('signals')} protocol_errors={r.get('protocol_errors')} "
              f"action_rejections={r.get('action_rejections')}")
        print(f"  answer={r.get('answer')!r}")
        for i, s in enumerate(r.get("steps", []), 1):
            out = (s.get("output") or "")
            print(f"    step{i}: tool={s['tool']} decision={s['decision']} "
                  f"executed={s['executed']} ok={s['ok']} error={s.get('error')} "
                  f"args={json.dumps(s.get('arguments'), ensure_ascii=False)[:180]}")
            if s.get("approval"):
                print(f"        approval={s['approval']}")
            if out:
                print(f"        output={out[:200]!r}")
        rr = r.get("refused_replies") or []
        for j, x in enumerate(rr, 1):
            print(f"    refused{j}: kind={x.get('kind')} error={x.get('error')} "
                  f"text={str(x.get('text'))[:300]!r}")
        if r.get("error"):
            print(f"  provider error: {r['error']}")


summarize(RUN_ROOT / "commands-baseline" / Path(
    "bench-20261010T111502Z.jsonl"), "BASELINE a43c6f4")
summarize(RUN_ROOT / "commands-guidance" / Path(
    "bench-20261010T111742Z.jsonl"), "GUIDANCE ab448b5")
