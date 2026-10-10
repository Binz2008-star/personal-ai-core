"""Compact per-run summary for a bench JSONL (edits, verification, commands)."""
import json
import sys
from pathlib import Path

lines = [json.loads(l) for l in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines() if l]
for r in [x for x in lines if x.get("kind") == "run"]:
    edits = [s["tool"] for s in r.get("steps", [])
             if s["tool"] in ("write_file", "edit_file", "apply_patch", "str_replace")]
    print(f"{r['task']:22} {r['language']} run{r['run']} success={r['success']} "
          f"stop={r.get('stop')} edits={edits} vr={r.get('verification_rejections')} "
          f"cmds={r.get('commands')}")
print("total verification_rejections:",
      sum(len(r.get("verification_rejections") or []) for r in lines if r.get("kind") == "run"))
