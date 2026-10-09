"""Real local-model/provider acceptance; no mocked model or tool execution.

The JSON phase's disposable driver executes only a whitelisted Git command.
The OpenCode phase must execute its own tools; the PAC provider never does.
All profile, mapping, and diagnostic files live in the named temporary area.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

REPO = Path(__file__).resolve().parents[1]
PROMPT = "Inspect this repository. Run git status and git rev-parse HEAD, then explain the architecture. Do not modify anything."
SCENARIOS = {
    "repository": PROMPT,
    "identity-english": "Who are you and what do you know about me? Please answer in English.",
    "identity-arabic": "من أنت وماذا تعرف عني؟ أجب بالعربية.",
    "temporary-edit": "Edit and restore the disposable acceptance file using actual filesystem tools.",
    "github": "Use GitHub MCP to inspect Binz2008-star/personal-ai-core. Report the default branch and repository status based on actual GitHub results. Do not modify anything.",
    "context7": "Use Context7 to resolve the Python documentation library and look up pathlib.Path.read_text. Report the actual documentation result. Do not modify anything.",
    "memory": "Use the enabled memory MCP to read its graph. Report whether it contains entities, based on the real tool result. Do not modify anything.",
    "context-pressure": "Run git rev-parse HEAD, then inspect README.md, PROJECT_STATE.md and src/personal_ai_core/app/cli.py with repository tools. Explain the architecture and state the exact current commit using actual tool results. Do not modify anything.",
}


def _git(repo: Path, *arguments: str) -> bytes:
    return subprocess.run(["git", *arguments], cwd=repo, capture_output=True,
                          check=True).stdout


def repository_snapshot(repo: Path) -> dict[str, Any]:
    """Fingerprint tracked and nonignored files without recording their text.

    Relative paths stay only in memory for comparing the two snapshots. The
    persisted summary contains counts and digests, never that inventory.
    """
    inventory = _git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    files: dict[str, str] = {}
    for raw_name in sorted(set(inventory.split(b"\0")) - {b""}):
        name = os.fsdecode(raw_name)
        path = repo / name
        if path.is_symlink():
            data = b"symlink\0" + os.fsencode(os.readlink(path))
        elif path.is_file():
            data = path.read_bytes()
        elif not path.exists():
            data = b"missing\0"
        else:
            # A gitlink is represented by Git's index/status below, rather than
            # traversing another checkout or an ignored generated directory.
            data = b"directory\0"
        files[name] = hashlib.sha256(data).hexdigest()
    combined = json.dumps(files, sort_keys=True, ensure_ascii=True).encode()
    return {
        "head": _git(repo, "rev-parse", "HEAD").decode("ascii").strip(),
        "status": _git(repo, "status", "--porcelain=v1", "-z"),
        "index": _git(repo, "ls-files", "--stage", "-z"),
        "files": files,
        "file_count": len(files),
        "file_digest": hashlib.sha256(combined).hexdigest(),
    }


def compare_snapshots(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    names = set(before["files"]) | set(after["files"])
    changed = sum(before["files"].get(name) != after["files"].get(name) for name in names)
    unchanged = (changed == 0 and before["head"] == after["head"]
                 and before["status"] == after["status"] and before["index"] == after["index"])
    return {"repository_unchanged": unchanged, "changed_file_count": changed,
            "head_unchanged": before["head"] == after["head"],
            "status_unchanged": before["status"] == after["status"],
            "index_unchanged": before["index"] == after["index"],
            "before_file_count": before["file_count"], "after_file_count": after["file_count"],
            "before_file_digest": before["file_digest"], "after_file_digest": after["file_digest"]}


def parse_events(text: str) -> tuple[list[dict[str, Any]], int]:
    """Parse real OpenCode JSONL; do not treat plain model text as tool events."""
    events: list[dict[str, Any]] = []
    invalid = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            invalid += 1
            continue
        if not isinstance(value, dict) or not isinstance(value.get("type"), str):
            invalid += 1
            continue
        events.append(value)
    return events, invalid


def _display_name(value: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", value):
        return value
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _shell_commands(command: Any) -> tuple[bool, bool]:
    """Recognize actual command positions, not strings in echo/printf/code.

    Complex substitutions are deliberately not certified by this runner. A
    completed shell tool with direct Git commands is the acceptance evidence.
    """
    if not isinstance(command, str):
        return False, False
    status = head = False
    for segment in re.split(r";|\r?\n|&&|\|\|", command):
        match = re.match(r"^\s*(?:&\s+)?git\s+(.*)$", segment)
        if match is None:
            continue
        tail = match.group(1)
        # Allow Git's supported leading -C option without interpreting paths.
        tail = re.sub(r'^-C\s+(?:"[^"\r\n]+"|\'[^\'\r\n]+\'|[^\s]+)\s+', "", tail)
        status |= bool(re.match(r"status(?:\s|$)", tail))
        head |= bool(re.match(r"rev-parse\s+(?:--verify\s+)?(?:HEAD|\"HEAD\"|'HEAD')(?:\s|$)", tail))
    return status, head


def classify_events(events: list[dict[str, Any]], *, expected_head: str,
                    scenario: str, expected_text: list[str] | None = None) -> dict[str, Any]:
    """Check executor events and the final text; never certify quoted claims.

    Code Mode records the tools it really invoked in metadata.toolCalls. Those
    completion rows, rather than the generated JavaScript, prove MCP execution.
    OpenCode's providerResult.executed flag identifies provider-hosted execution
    and must not be mistaken for proof that local execution happened.
    """
    completed: list[str] = []
    actual: list[str] = []
    failed_tools = errors = 0
    status_run = head_run = head_result = False
    scratch_changed = scratch_restored = False
    last_tool = -1
    for index, event in enumerate(events):
        if event.get("type") == "error":
            errors += 1
        if event.get("type") != "tool_use":
            continue
        last_tool = index
        part = event.get("part", {})
        if not isinstance(part, dict):
            continue
        state = part.get("state", {})
        if not isinstance(state, dict) or state.get("status") != "completed":
            failed_tools += 1
            continue
        name = part.get("tool")
        if not isinstance(name, str) or not part.get("id"):
            failed_tools += 1
            continue
        completed.append(_display_name(name))
        outer_metadata = state.get("metadata", {})
        metadata = outer_metadata.get("metadata", {}) if isinstance(outer_metadata, dict) else {}
        if not isinstance(metadata, dict):
            metadata = {}
        if metadata.get("error") is True or metadata.get("exit", 0) != 0:
            failed_tools += 1
            continue
        rows: list[dict[str, Any]] = []
        if name == "execute":
            nested = metadata.get("toolCalls", [])
            if isinstance(nested, list):
                rows = [row for row in nested if isinstance(row, dict)
                        and row.get("status") == "completed" and isinstance(row.get("tool"), str)]
        else:
            rows = [{"tool": name, "input": state.get("input", {})}]
        for row in rows:
            actual_name = row["tool"]
            actual.append(_display_name(actual_name))
            if actual_name == "shell" or actual_name.endswith(".shell"):
                arguments = row.get("input", {})
                ran_status, ran_head = _shell_commands(arguments.get("command") if isinstance(arguments, dict) else None)
                status_run |= ran_status
                head_run |= ran_head
                head_result |= ran_head and expected_head in str(state.get("output", ""))
            if actual_name in ("edit", "write") or actual_name.endswith((".edit", ".write")):
                arguments = row.get("input", {})
                if isinstance(arguments, dict):
                    replacement = arguments.get("newString", arguments.get("content"))
                    if isinstance(replacement, str) and "PAC ACCEPTANCE CHANGED" in replacement:
                        scratch_changed = True
                    elif (scratch_changed and isinstance(replacement, str)
                          and "PAC ACCEPTANCE ORIGINAL" in replacement):
                        scratch_restored = True
    final_text = "\n".join(str(event.get("part", {}).get("text", ""))
                           for index, event in enumerate(events)
                           if index > last_tool and event.get("type") == "text"
                           and isinstance(event.get("part"), dict))
    groups = {
        group: any(name.startswith(group + ".") or name.startswith(group + "_")
                   or name.startswith("mcp_" + group + "_") for name in actual)
        for group in ("github", "context7", "memory")
    }
    conditions: dict[str, bool] = {"no_cli_errors": errors == 0, "no_failed_tools": failed_tools == 0,
                                    "final_text_present": bool(final_text.strip())}
    if scenario == "repository":
        conditions.update(git_status_executed=status_run, git_head_executed=head_run,
                          tool_output_contains_actual_head=head_result,
                          final_answer_uses_actual_head=expected_head in final_text)
    elif scenario == "context-pressure":
        conditions.update(git_head_executed=head_run, tool_output_contains_actual_head=head_result,
                          final_answer_uses_actual_head=expected_head in final_text,
                          read_tool_executed=any(name == "read" or name.endswith(".read") for name in actual))
    elif scenario in groups:
        conditions[scenario + "_mcp_executed"] = groups[scenario]
    elif scenario == "identity-arabic":
        conditions["arabic_answer_present"] = bool(re.search(r"[\u0600-\u06ff]", final_text))
    elif scenario == "temporary-edit":
        conditions.update(disposable_file_changed_by_tool=scratch_changed,
                          disposable_file_restored_by_tool=scratch_restored)
    if expected_text:
        conditions["expected_text_present"] = all(text.casefold() in final_text.casefold() for text in expected_text)
    return {"pass": all(conditions.values()), "conditions": conditions,
            "completed_native_tools": completed, "actual_completed_tools": actual,
            "failed_tool_count": failed_tools, "cli_error_count": errors,
            "final_answer_characters": len(final_text), "mcp_execution_groups": groups}


def classify_audit(records: list[dict[str, Any]], *, require_tools: bool,
                   require_compaction: bool = False, request_kind_trusted: bool = False) -> dict[str, Any]:
    """Correlate immutable admission/completion events in an isolated run.

    Request-kind labels are accepted as proof only when the independent
    OpenCode hook was validated, not merely because a header says primary.
    """
    admitted: dict[str, tuple[int, dict[str, Any]]] = {}
    completions: dict[str, tuple[int, dict[str, Any]]] = {}
    refusals = mismatches = 0
    identity = context = True
    for index, record in enumerate(records):
        correlation = record.get("request_correlation_id")
        if not isinstance(correlation, str):
            mismatches += 1
            continue
        if record.get("event_type") == "request_admission":
            if correlation in admitted:
                mismatches += 1
            admitted[correlation] = (index, record)
            refusals += record.get("outcome") != "admitted"
            budget = record.get("authoritative_budget", {})
            identity &= isinstance(budget, dict) and budget.get("identity_tokens", 0) > 0
            context &= isinstance(budget, dict) and budget.get("context_window") == 8192
        elif record.get("event_type") == "request_completion":
            if correlation in completions or correlation not in admitted:
                mismatches += 1
            completions[correlation] = (index, record)
    successful = [(admitted[key], completion) for key, completion in completions.items()
                  if key in admitted and admitted[key][1].get("outcome") == "admitted"
                  and completion[0] > admitted[key][0]
                  and completion[1].get("outcome") in ("generated_text", "generated_tool_calls")]
    missing = sum(record.get("outcome") == "admitted" and correlation not in completions
                  for correlation, (_, record) in admitted.items())
    generation_errors = sum(record.get("outcome") in ("provider_error", "protocol_error")
                            for _, record in completions.values())
    calls_before = [completion[0] for (_, admission), completion in successful
                    if admission.get("request_kind") == "primary"
                    and completion[1].get("outcome") == "generated_tool_calls"]
    continuation = any(admission.get("request_kind") == "primary"
                       and admission.get("incoming_messages", {}).get("role_counts", {}).get("tool", 0) > 0
                       and completion[1].get("outcome") == "generated_text"
                       and any(position < admission_index for position in calls_before)
                       for (admission_index, admission), completion in successful)
    compacted = any(admission.get("request_kind") == "compaction"
                    and completion[1].get("outcome") == "generated_text"
                    and any(later[1].get("request_kind") == "primary" and later[0] > completion[0]
                            for later in admitted.values())
                    for (_, admission), completion in successful)
    conditions = {"audit_events_present": bool(successful), "audit_pairs_complete": missing == 0 and mismatches == 0,
                  "no_budget_or_protocol_refusals": refusals == 0, "no_generation_errors": generation_errors == 0,
                  "identity_reserve_active": identity, "frozen_8192_context": context,
                  "request_kind_independently_trusted": request_kind_trusted}
    if require_tools:
        conditions["native_tool_call_recorded"] = bool(calls_before)
        conditions["role_tool_primary_continuation"] = continuation
    if require_compaction:
        conditions["compaction_followed_by_primary"] = compacted
    return {"pass": all(conditions.values()), "conditions": conditions,
            "admission_count": len(admitted), "completion_count": len(completions),
            "refusal_count": refusals, "unpaired_admission_count": missing,
            "correlation_mismatch_count": mismatches, "generation_error_count": generation_errors,
            "correlation_scope": "isolated_run_window", "compaction_followed_by_primary": compacted,
            "request_kinds": sorted({record.get("request_kind", "unknown") for _, record in admitted.values()}),
            "budgets": [record.get("authoritative_budget", {}) for _, record in admitted.values()]}


def _opencode_executable(explicit: Path | None) -> str:
    if explicit is not None:
        return str(explicit.resolve(strict=True))
    appdata = os.environ.get("APPDATA")
    if appdata:
        installed = Path(appdata) / "npm/node_modules/@opencode/cli/bin/opencode.exe"
        if installed.is_file():
            return str(installed)
    executable = shutil.which("opencode.exe") or shutil.which("opencode")
    if executable is None:
        raise RuntimeError("OpenCode executable is unavailable.")
    return executable


def run_opencode(args: argparse.Namespace, root: Path) -> int:
    """Run installed OpenCode as sole executor against the existing PAC adapter."""
    report: dict[str, Any] = {"phase": "opencode", "scenario": args.scenario, "pass": False,
                              "model_mocked": False, "executor": "opencode",
                              "provider_model": "pac/pac-local", "configured_context": 8192}
    if root.resolve().is_relative_to(REPO):
        raise ValueError("OpenCode evidence must live outside the repository being inspected.")
    scratch: Path | None = None
    if args.scenario == "temporary-edit":
        scratch = root / "disposable-edit.txt"
        if scratch.exists():
            raise ValueError("The disposable edit artifact already exists; use a fresh evidence directory.")
        scratch.write_text("PAC ACCEPTANCE ORIGINAL\n", encoding="utf-8")
    before = repository_snapshot(REPO)
    audit_offset = args.audit.stat().st_size if args.audit is not None and args.audit.exists() else 0
    env = dict(os.environ)
    if args.config_overlay is not None:
        overlay = json.loads(args.config_overlay.read_text(encoding="utf-8-sig"))
        if not isinstance(overlay, dict):
            raise ValueError("OpenCode config overlay must be a JSON object.")
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(overlay)
    prompt = args.prompt or SCENARIOS[args.scenario]
    if scratch is not None and args.prompt is None:
        prompt = (f'Use filesystem read and edit tools on the disposable file "{scratch}". '
                  'Read it, replace PAC ACCEPTANCE ORIGINAL with PAC ACCEPTANCE CHANGED, read it '
                  'to verify the change, then replace PAC ACCEPTANCE CHANGED with PAC ACCEPTANCE ORIGINAL '
                  'and read it to verify restoration. Do not edit any repository file. Report the actual results.')
    command = [_opencode_executable(args.opencode), "run", "--standalone", "--agent", args.agent,
               "--model", "pac/pac-local", "--format", "json", "--title", "PAC acceptance", prompt]
    try:
        with (root / "opencode.stdout.jsonl").open("wb") as stdout, (root / "opencode.stderr.log").open("wb") as stderr:
            process = subprocess.Popen(command, cwd=REPO, env=env, stdout=stdout, stderr=stderr)
            try:
                returncode = process.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=15)
                report["failure_category"] = "acceptance_timeout"
                returncode = process.returncode
        report["exit_code"] = returncode
        output = (root / "opencode.stdout.jsonl").read_text(encoding="utf-8", errors="replace")
        events, invalid = parse_events(output)
        event_result = classify_events(events, expected_head=before["head"], scenario=args.scenario,
                                       expected_text=args.expect_text)
        report["events"] = event_result
        report["invalid_jsonl_count"] = invalid
        require_tools = args.scenario not in ("identity-english", "identity-arabic")
        audit_records: list[dict[str, Any]] = []
        audit_invalid = 0
        if args.audit is not None:
            with args.audit.open("rb") as source:
                source.seek(audit_offset)
                audit_text = source.read().decode("utf-8", errors="replace")
            for line in audit_text.splitlines():
                try:
                    record = json.loads(line)
                    if isinstance(record, dict):
                        audit_records.append(record)
                    else:
                        audit_invalid += 1
                except ValueError:
                    audit_invalid += 1
        audit_result = classify_audit(audit_records, require_tools=require_tools,
                                      require_compaction=args.scenario == "context-pressure",
                                      request_kind_trusted=args.audit_request_kind_trusted)
        report["audit"] = audit_result
        report["invalid_audit_count"] = audit_invalid
        integrity = compare_snapshots(before, repository_snapshot(REPO))
        report["integrity"] = integrity
        if scratch is not None:
            integrity["disposable_file_restored"] = scratch.read_text(encoding="utf-8") == "PAC ACCEPTANCE ORIGINAL\n"
        report["pass"] = bool(returncode == 0 and invalid == 0 and event_result["pass"]
                              and audit_invalid == 0 and audit_result["pass"] and integrity["repository_unchanged"]
                              and integrity.get("disposable_file_restored", True))
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as error:
        report["failure_category"] = type(error).__name__
        # Exception text, including paths/provider payloads, is intentionally withheld.
        report["integrity"] = compare_snapshots(before, repository_snapshot(REPO))
    finally:
        (root / "acceptance-opencode.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(("PASS" if report["pass"] else "FAIL") + ": OpenCode acceptance; inspect the sanitized acceptance-opencode.json.", flush=True)
    print("Private local evidence: " + str(root), flush=True)
    return 0 if report["pass"] else 1


def request(url: str, body: dict | None = None, *, session: str | None = None) -> tuple[dict, str | None]:
    headers = {"Content-Type": "application/json"}
    if session:
        headers["X-OpenCode-Session-ID"] = session
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=300) as response:
        return json.load(response), response.headers.get("X-PAC-Session-ID")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("json", "opencode"), default="json")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--ollama-host", default=os.environ.get("PAC_OLLAMA_HOST", "http://127.0.0.1:11434"))
    parser.add_argument("--scenario", choices=tuple(SCENARIOS), default="repository")
    parser.add_argument("--prompt", help="Override the scenario prompt; executor/audit checks still apply.")
    parser.add_argument("--agent", default="pac")
    parser.add_argument("--opencode", type=Path, help="Installed OpenCode executable; never a source build.")
    parser.add_argument("--config-overlay", type=Path, help="Supported OpenCode JSON overlay, inherited for this run only.")
    parser.add_argument("--audit", type=Path, help="Active PAC JSONL audit file, consumed from the starting byte offset.")
    parser.add_argument("--audit-request-kind-trusted", action="store_true",
                        help="Independent OpenCode request-kind hook has been verified for this adapter.")
    parser.add_argument("--expect-text", action="append", default=[], help="Expected final-answer text; never persisted in the summary.")
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()
    root = args.out or Path(tempfile.mkdtemp(prefix="pac-opencode-acceptance-"))
    root.mkdir(parents=True, exist_ok=True)
    if args.phase == "opencode":
        return run_opencode(args, root)
    profile = root / "profile.md"
    profile.write_text("The acceptance identity marker is PAC-PROVIDER-ACCEPTANCE.\n", encoding="utf-8")
    (root / "projects.md").write_text("This is a disposable local provider validation project.\n", encoding="utf-8")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {key: value for key, value in os.environ.items() if not key.startswith("PAC_")}
    env.update(PAC_BOSS_CONTEXT_WINDOW="8192", PAC_REQUEST_TIMEOUT_SECONDS="240",
               PAC_OLLAMA_HOST=args.ollama_host, PYTHONIOENCODING="utf-8")
    logfile = (root / "provider-json.log").open("w", encoding="utf-8")
    cmd = [sys.executable, "-m", "personal_ai_core.integrations.opencode", "--port", str(port),
           "--profile", str(profile), "--database", str(root / "opencode.db")]
    process = subprocess.Popen(cmd, cwd=REPO, env=env, stdout=logfile, stderr=logfile)
    url = f"http://127.0.0.1:{port}/v1"
    report: dict = {"phase": "json", "repo": str(REPO), "model_mocked": False,
                    "server_command": cmd, "ollama_host": args.ollama_host, "pass": False}
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                catalog, _ = request(url + "/models")
                break
            except (urllib.error.URLError, OSError):
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Provider did not start; see its content-free startup log.")
                time.sleep(0.1)
        assert catalog["data"][0]["id"] == "pac-local"
        session = "json-proof-" + uuid.uuid4().hex
        user = {"role": "user", "content": "Call run_read_only_command with command git rev-parse HEAD. Then answer using the exact returned commit, without guessing. Do not modify any file."}
        tools = [{"type": "function", "function": {"name": "run_read_only_command",
            "description": "Run the specified read-only Git command in the current repository.",
            "parameters": {"type": "object", "properties": {"command": {"type": "string", "enum": ["git rev-parse HEAD"]}}, "required": ["command"], "additionalProperties": False}}}]
        first_body = {"model": "pac-local", "messages": [user], "tools": tools,
                      "tool_choice": {"type": "function", "function": {"name": "run_read_only_command"}}, "stream": False}
        first, pac_session = request(url + "/chat/completions", first_body, session=session)
        message = first["choices"][0]["message"]
        calls = message.get("tool_calls", [])
        assert calls and first["choices"][0]["finish_reason"] == "tool_calls", "The real model did not return native calls."
        transcript = [user, message]
        results = []
        for call in calls:
            assert call["type"] == "function" and call["id"]
            function = call["function"]
            assert function["name"] == "run_read_only_command"
            assert json.loads(function["arguments"]) == {"command": "git rev-parse HEAD"}
            completed = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, text=True, capture_output=True, check=True)
            output = completed.stdout.strip()
            transcript.append({"role": "tool", "tool_call_id": call["id"], "content": output})
            results.append({"tool_call_id": call["id"], "command": "git rev-parse HEAD", "exit": completed.returncode, "result": output})
        second, resumed = request(url + "/chat/completions",
            {"model": "pac-local", "messages": transcript, "tools": tools, "tool_choice": "auto", "stream": False}, session=session)
        answer = second["choices"][0]["message"]
        assert not answer.get("tool_calls"), "The continuation did not finish."
        assert results[0]["result"] in answer.get("content", ""), "The final answer did not use the actual Git result."
        assert resumed == pac_session
        report.update(pass_=True, native_calls=len(calls), tool_results=results,
                      assistant_tool_calls_replayed=True, tool_role_replayed=True,
                      pac_session_stable=True, pac_session_id=pac_session,
                      answer_uses_actual_head=True, configured_context=8192)
        report["pass"] = report.pop("pass_")
        print("PASS: real Qwen native JSON call, real Git execution, exact tool-result replay and grounded continuation.", flush=True)
        return 0
    except (AssertionError, OSError, RuntimeError, KeyError, ValueError) as error:
        report["error_type"] = type(error).__name__
        # HTTP errors can carry a safe PAC error body with budget diagnostics.
        if isinstance(error, urllib.error.HTTPError):
            try:
                report["provider_error"] = json.load(error)
            except (ValueError, OSError):
                pass
        print("FAIL: JSON acceptance; inspect acceptance-json.json.", flush=True)
        return 1
    finally:
        (root / "acceptance-json.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        process.terminate()
        process.wait(timeout=10)
        logfile.close()
        print("Acceptance evidence: " + str(root), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
