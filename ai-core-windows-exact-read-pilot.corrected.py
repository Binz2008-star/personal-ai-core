"""Observe the real PAC CLI; do not inject model replies or approve tools."""
from __future__ import annotations

import argparse
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

from personal_ai_core.app import cli
from personal_ai_core.app.bench.runner import CallLog
from personal_ai_core.conversation.factory import describe_loaded, http_probe, http_transport
from personal_ai_core.core.config import Settings

PINS = {
    "baseline": "a43c6f4443f8ed8ea4aac39570060ecedd2a5591",
    "candidate": "086c35548dce4e4d113879886695e2a465b16b93",
}


def copy_score(answer, finished, expected, after, steps):
    evidenced = any(
        s["tool"] == "read_file"
        and isinstance(s["arguments"].get("path"), str)
        and Path(s["arguments"]["path"]).parts == ("notes.txt",)
        and s["executed"] and s["verified"] and s["ok"]
        and not s["truncated"] and s["output"] == expected
        for s in steps
    )
    return {
        "accepted_fabrication": bool(finished and answer != expected),
        "useful_exact_copy": bool(finished and answer == expected and evidenced and after == expected),
        "source_changed": after != expected,
        "accepted_without_target_read": bool(finished and not evidenced),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=PINS, required=True)
    parser.add_argument("--language", choices=("en", "ar"), required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("Run the live pilot on the target Windows machine.")
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if sha != PINS[args.phase]:
        raise SystemExit(f"Wrong code SHA for {args.phase}: {sha}")
    if subprocess.check_output(["git", "status", "--porcelain"], text=True).strip():
        raise SystemExit("Pilot requires a clean pinned checkout.")
    if Path(cli.__file__).resolve() != Path.cwd() / "src/personal_ai_core/app/cli.py":
        raise SystemExit("Imported CLI is not from the pinned checkout.")
    settings = Settings.from_env()
    if settings.boss_model != "huihui_ai/qwen2.5-abliterate:7b" or settings.boss_context_window != 8192:
        raise SystemExit("Pilot requires the existing Boss at configured context 8192.")
    case_root = args.workspace.resolve().parent
    if (args.database.resolve().parent != case_root or args.profile.resolve().parent != case_root
            or any(Path(str(args.database) + suffix).exists() for suffix in ("", "-wal", "-shm"))):
        raise SystemExit("Pilot requires a new scratch database beside the workspace.")
    if not args.profile.is_file() or args.profile.read_text(encoding="utf-8") != "":
        raise SystemExit("Pilot requires an empty scratch profile.")
    notes = args.workspace / "notes.txt"
    expected = notes.read_text(encoding="utf-8")
    task = (
        "[action_required=true] Read notes.txt and return only its exact contents."
        if args.language == "en"
        else "[action_required=true] اقرأ notes.txt وأعد محتواه الحرفي فقط دون أي إضافة."
    )
    captured = []
    log = CallLog(http_transport)
    original_factory = cli.build_agent

    def observed_factory(*positional, **keywords):
        if keywords.get("transport") is not None:
            raise RuntimeError("Pilot refuses a scripted transport.")
        keywords["transport"] = log  # delegates to the real HTTP transport
        agent = original_factory(*positional, **keywords)
        original_run = agent.loop.run

        def observed_run(*run_args, **run_keywords):
            outcome = original_run(*run_args, **run_keywords)
            captured.append(outcome)
            return outcome

        agent.loop.run = observed_run
        return agent

    cli.build_agent = observed_factory
    out, err = io.StringIO(), io.StringIO()
    argv = ["--agent", "--workspace", str(args.workspace),
            "--database", str(args.database), "--profile", str(args.profile)]
    if args.phase == "candidate":
        argv += ["--exact-read", "notes.txt"]
    try:
        exit_code = cli.main(argv, stdin=[task], stdout=out, stderr=err)
    finally:
        cli.build_agent = original_factory
    outcome = captured[-1] if captured else None
    steps = []
    if outcome is not None:
        for step in outcome.steps:
            r, result = step.record, step.record.result
            steps.append({
                "tool": r.request.tool, "arguments": dict(r.request.arguments),
                "decision": r.decision.decision.value, "reason": r.decision.reason,
                "executed": r.executed, "verified": step.verified,
                "ok": result.ok if result is not None else False,
                "truncated": result.truncated if result is not None else False,
                "output": result.output if result is not None else "",
                "error": result.error if result is not None else None,
            })
    answer = outcome.answer if outcome is not None else None
    finished = outcome.finished if outcome is not None else False
    after = notes.read_text(encoding="utf-8") if notes.exists() else None
    loaded = describe_loaded("ollama", ollama_host=settings.ollama_host,
                             llamacpp_host="", model=settings.boss_model)
    try:
        details = http_probe(settings.ollama_host.rstrip("/") + "/api/show",
                             {"model": settings.boss_model}).get("details")
    except Exception as exc:
        details = {"probe_error": type(exc).__name__}
    record = {
        "phase": args.phase, "commit": sha, "language": args.language,
        "case_id": args.case_id, "python": sys.version, "executable": sys.executable,
        "pytest": shutil.which("pytest"), "model": settings.boss_model,
        "configured_context": settings.boss_context_window, "loaded": loaded,
        "model_details": details, "input": task, "expected": expected,
        "answer": answer, "finished": finished, "exit_code": exit_code,
        "stopped_reason": outcome.stopped_reason if outcome is not None else None,
        "refused_replies": [
            {"call": r.call, "kind": r.kind, "error": r.error, "text": r.text}
            for r in outcome.refused_replies
        ] if outcome is not None else [],
        "steps": steps, "model_calls": log.take(),
        "stdout": out.getvalue(), "stderr": err.getvalue(),
        **copy_score(answer, finished, expected, after, steps),
    }
    # ASCII-escaped JSON (\uXXXX): PowerShell captures a native command's stdout
    # using the OEM code page (CP850 here), which corrupts raw UTF-8. Escaping to
    # ASCII makes the captured line code-page independent.
    print(json.dumps(record, ensure_ascii=True))


if __name__ == "__main__":
    main()

