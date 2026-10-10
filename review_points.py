"""Answer the reviewer's raw-evidence questions across all result files.

1. Where does OSError [WinError 4551] occur: in an agent step, or in the
   post-run command_passes check?
2. For any `cat` command the model issued: was it an allowlist denial, or a
   missing-executable / other execution error?
3. Full digests / context / model per phase.
"""
import json
import sys
from pathlib import Path

ROOT = Path(sys.argv[1])

FILES = [
    ROOT / "commands-baseline" / "bench-20261010T111502Z.jsonl",
    ROOT / "commands-guidance" / "bench-20261010T111742Z.jsonl",
    ROOT / "completion-on" / "bench-20261010T112826Z.jsonl",
    ROOT / "exact-read-baseline.jsonl",
    ROOT / "exact-read-candidate.jsonl",
]


def lines_of(p):
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x]


print("### WinError 4551 occurrences")
for p in FILES:
    if not p.exists():
        continue
    for r in lines_of(p):
        if r.get("kind") != "run":
            continue
        for i, s in enumerate(r.get("steps", []), 1):
            err = str(s.get("error") or "")
            if "4551" in err or "Application Control" in err:
                print(f"  STEP  {p.name} {r['task']} {r['language']} run{r['run']} "
                      f"step{i} tool={s['tool']} decision={s['decision']} "
                      f"executed={s['executed']} cmd={s.get('arguments', {}).get('command')!r} "
                      f"error={err[:90]}")
        for c in r.get("checks", []):
            det = str(c.get("detail") or "")
            if "4551" in det or "Application Control" in det:
                print(f"  CHECK {p.name} {r['task']} {r['language']} run{r['run']} "
                      f"check={c['check']} verdict={c['verdict']} detail={det[:90]}")

print("\n### cat / non-executable commands the model issued")
for p in FILES:
    if not p.exists():
        continue
    for r in lines_of(p):
        if r.get("kind") != "run":
            continue
        for i, s in enumerate(r.get("steps", []), 1):
            cmd = str(s.get("arguments", {}).get("command") or "")
            if cmd.split(" ")[:1] == ["cat"] or cmd.startswith("cat "):
                print(f"  {p.name} {r['task']} {r['language']} run{r['run']} step{i} "
                      f"tool={s['tool']} decision={s['decision']} executed={s['executed']} "
                      f"ok={s['ok']} cmd={cmd!r} error={s.get('error')!r}")

print("\n### metadata per file")
for p in FILES:
    if not p.exists():
        continue
    ls = lines_of(p)
    h = next((x for x in ls if x.get("kind") == "header"), None)
    e = next((x for x in ls if x.get("kind") == "end"), None)
    if h:
        print(f"  {p.name}: commit={h.get('commit')} model={h.get('model')} "
              f"num_ctx_sent={h.get('num_ctx_sent_by_core')} "
              f"num_ctx_measured={h.get('num_ctx_measured_by_owner')} "
              f"verify_completion={h.get('verify_completion')} scorer={h.get('scorer')}")
    if e:
        w = e.get("weights") or {}
        ol = e.get("ollama_loaded") or {}
        print(f"      end: context_length={ol.get('context_length')} "
              f"context_mismatch={e.get('context_mismatch')} weights={w.get('digest')} "
              f"manifest={w.get('manifest_digest')} verified={w.get('verified')}")
