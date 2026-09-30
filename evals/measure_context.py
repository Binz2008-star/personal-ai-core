"""Read-only context baseline from an existing core.db (run on the rig).

What it reads: the events and messages `pac` already recorded. What it
reports, per completed turn: the real `prompt_tokens` Ollama returned, the
number of history messages, Core's own estimate of the same prompt, and
whether the turn was grounded. Then a summary, including how often the
largest `prompt_tokens` value repeats -- a repeated exact maximum while
history grows is an INDICATOR of a context cap, not proof of one.

What it cannot do: measure the agent's context (agent events record neither
prompt_tokens nor tool-output sizes), or classify anything. Classification
follows the rule recorded on 2026-09-30: num_ctx below 8192 is a
configuration mismatch; a defect needs a prompt above the real capacity
AND evidence of truncation or loss.

It never opens the owner's database. It copies core.db (and its -wal file,
which holds turns not yet checkpointed) into a temporary folder and reads the
copy. Opening the original even in SQLite's read-only mode is not enough: on
a WAL database that still creates -wal and -shm companion files beside it,
which a test here caught. It sends nothing to Ollama. Run it while pac is not
running, so the copy is consistent.

    set PYTHONPATH=src
    python evals/measure_context.py [path\\to\\core.db]

Default path: %USERPROFILE%\\.personal-ai-core\\core.db
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import statistics
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import TextIO

from personal_ai_core.context import ScriptAwareTokenEstimator
from personal_ai_core.identity import DefaultIdentityComposer

DEFAULT_DB = Path.home() / ".personal-ai-core" / "core.db"


def _profile_text(db: Path) -> str:
    return "\n\n".join(
        p.read_text(encoding="utf-8").strip()
        for p in (db.parent / "profile.md", db.parent / "projects.md")
        if p.is_file()
    )


def measure(db: Path, out: TextIO) -> int:
    if not db.is_file():
        print(f"no database at {db}", file=out)
        return 2
    with tempfile.TemporaryDirectory(prefix="pac-measure-") as tmp:
        copy = Path(tmp) / db.name
        shutil.copyfile(db, copy)
        wal = db.with_name(db.name + "-wal")
        if wal.is_file():
            shutil.copyfile(wal, copy.with_name(copy.name + "-wal"))
        con = sqlite3.connect(f"file:{copy.as_posix()}?mode=ro", uri=True)
        try:
            return _report(con, db, out)
        finally:
            con.close()


def _report(con: sqlite3.Connection, db: Path, out: TextIO) -> int:
    est = ScriptAwareTokenEstimator()
    profile = _profile_text(db)
    identity_tokens = DefaultIdentityComposer(profile=profile or None).tokens(est)

    events = con.execute(
        "SELECT seq, session_id, type, payload, message_id FROM events ORDER BY seq"
    ).fetchall()
    messages = con.execute(
        "SELECT seq, id, session_id, role, content FROM messages ORDER BY seq"
    ).fetchall()
    print(f"database: {db}", file=out)
    print(f"events: {len(events)}  messages: {len(messages)}", file=out)
    print(
        f"identity estimate (profile {'present' if profile else 'absent'}): "
        f"{identity_tokens} tokens\n",
        file=out,
    )

    # generation.requested carries the USER message id and generation.completed
    # the ASSISTANT one, so a turn pairs with the latest request in its session.
    pending: dict[str, tuple[str, dict]] = {}
    turns = []
    for _seq, sid, etype, payload, mid in events:
        data = json.loads(payload or "{}")
        if etype == "generation.requested":
            pending[sid] = (mid, data)
        elif etype == "generation.completed" and sid in pending:
            user_mid, req = pending.pop(sid)
            turns.append((sid, user_mid, data.get("prompt_tokens"),
                          data.get("finish_reason"), req.get("evidence_chunks", 0)))

    msg_seq = {m[1]: m[0] for m in messages}
    by_session = defaultdict(list)
    for m in messages:
        by_session[m[2]].append(m)

    print("--- per turn (only ungrounded turns are comparable with the estimate)", file=out)
    print("session   hist_msgs  real_prompt  est_prompt  ratio  grounded  finish", file=out)
    ratios: list[float] = []
    reals: list[int] = []
    for sid, mid, real, finish, chunks in turns:
        history = [m for m in by_session[sid] if mid in msg_seq and m[0] <= msg_seq[mid]]
        est_prompt = identity_tokens + sum(est.estimate(m[4]) for m in history)
        grounded = bool(chunks)
        ratio = (est_prompt / real) if real else None
        if real:
            reals.append(real)
            if not grounded:
                ratios.append(est_prompt / real)
        shown = f"{ratio:.2f}" if ratio else "-"
        print(f"{sid[:8]}  {len(history):>9}  {str(real):>11}  {est_prompt:>10}  "
              f"{shown:>5}  {str(grounded):>8}  {finish}", file=out)

    print("\n--- summary", file=out)
    if reals:
        top = max(reals)
        print(f"real prompt_tokens: min {min(reals)}  median {statistics.median(reals)}  "
              f"max {top}", file=out)
        print(f"turns at the maximum value: {sum(1 for r in reals if r == top)}  "
              "(an indicator only, not proof of truncation)", file=out)
        lengths = sum(1 for t in turns if t[3] == "length")
        print(f"turns that stopped on length: {lengths}", file=out)
    else:
        print("no turns with prompt_tokens recorded", file=out)
    if ratios:
        print(f"estimate/real on {len(ratios)} ungrounded turns: min {min(ratios):.2f}  "
              f"median {statistics.median(ratios):.2f}  max {max(ratios):.2f}  "
              f"under-estimates: {sum(1 for r in ratios if r < 1.0)}", file=out)

    steps = [json.loads(p or "{}") for _, _, t, p, _ in events if t == "agent.step"]
    print(f"\n--- agent: {len(steps)} steps recorded; truncated tool outputs: "
          f"{sum(1 for s in steps if s.get('truncated'))}", file=out)
    print("agent events record no prompt_tokens and no tool-output sizes, so agent "
          "context size cannot be measured from core.db.", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(measure(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB, sys.stdout))
