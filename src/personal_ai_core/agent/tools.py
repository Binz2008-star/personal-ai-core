"""The agent's tools -- what it can do in its workspace.

Each declares its risk level, and the policy gate reads that declaration
(AGENT_ARCHITECTURE.md section 3):

    read_file      LOW     read one text file
    list_directory LOW     list a directory
    search_text    LOW     find lines containing a string
    write_file     MEDIUM  create or overwrite a text file (never a secret)
    run_command    HIGH    run one allowlisted command -- ASKED every time

Every path goes through the `Workspace` sandbox inside the tool, because the
tool is the last place that knows what the path is for. Every output is
bounded; a truncated result says it was truncated.

Not here yet, deliberately: delete, rename, and anything that sends outside
the machine. Those are CRITICAL, and CRITICAL needs "a rollback point first",
which is stage 3.
"""
from __future__ import annotations

import os
import subprocess
from typing import Any, Mapping

from ..core.agent import RiskLevel, ToolResult, ToolSpec
from .commands import validate_command
from .sandbox import SandboxError, Workspace, is_protected

MAX_OUTPUT_CHARS = 20_000
MAX_LIST_ENTRIES = 500
MAX_SEARCH_MATCHES = 200
MAX_WRITE_CHARS = 1_000_000

_PATH = {"type": "string", "description": "a path relative to the workspace"}


def _bounded(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    return text[:MAX_OUTPUT_CHARS], True


class ReadFile:
    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace
        self.spec = ToolSpec(
            name="read_file",
            description="Read a UTF-8 text file in the workspace.",
            risk_level=RiskLevel.LOW,
            input_schema={
                "type": "object",
                "properties": {"path": _PATH},
                "required": ["path"],
                "additionalProperties": False,
            },
            timeout_seconds=10,
            idempotent=True,
        )

    def run(self, arguments: Mapping[str, Any]) -> ToolResult:
        path = self._workspace.resolve(arguments["path"])
        # Whether a secret may be READ is this tool's call, and the answer is
        # no: a secret read into the model's context is a secret disclosed.
        if is_protected(path):
            raise SandboxError(f"protected file, the agent may not read it: {arguments['path']}")
        if not path.is_file():
            return ToolResult(ok=False, error=f"not a file: {arguments['path']}")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ToolResult(ok=False, error=f"not UTF-8 text: {arguments['path']}")
        output, truncated = _bounded(text)
        return ToolResult(ok=True, output=output, truncated=truncated)


class ListDirectory:
    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace
        self.spec = ToolSpec(
            name="list_directory",
            description="List the entries of a directory in the workspace.",
            risk_level=RiskLevel.LOW,
            input_schema={
                "type": "object",
                "properties": {"path": _PATH},
                "additionalProperties": False,
            },
            timeout_seconds=10,
            idempotent=True,
        )

    def run(self, arguments: Mapping[str, Any]) -> ToolResult:
        given = arguments.get("path", ".")
        path = self._workspace.root if given in ("", ".") else self._workspace.resolve(given)
        if not path.is_dir():
            return ToolResult(ok=False, error=f"not a directory: {given}")
        entries = sorted(
            f"{entry.name}/" if entry.is_dir() else entry.name
            for entry in path.iterdir()
            if entry.name.lower() != ".git"
        )
        truncated = len(entries) > MAX_LIST_ENTRIES
        return ToolResult(ok=True, output="\n".join(entries[:MAX_LIST_ENTRIES]), truncated=truncated)


class SearchText:
    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace
        self.spec = ToolSpec(
            name="search_text",
            description="Find lines containing a string, in text files under a directory.",
            risk_level=RiskLevel.LOW,
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "what to look for"},
                    "path": _PATH,
                    "case_sensitive": {"type": "boolean"},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
            timeout_seconds=30,
            idempotent=True,
        )

    def run(self, arguments: Mapping[str, Any]) -> ToolResult:
        needle = arguments["text"]
        if not needle:
            return ToolResult(ok=False, error="nothing to search for")
        case_sensitive = arguments.get("case_sensitive", False)
        given = arguments.get("path", ".")
        base = self._workspace.root if given in ("", ".") else self._workspace.resolve(given)
        wanted = needle if case_sensitive else needle.lower()
        matches: list[str] = []
        for directory, subdirectories, files in os.walk(base):
            subdirectories[:] = sorted(d for d in subdirectories if d.lower() != ".git")
            for name in sorted(files):
                file = os.path.join(directory, name)
                relative = os.path.relpath(file, self._workspace.root).replace(os.sep, "/")
                try:
                    # Through the sandbox: a symlink out of the workspace is
                    # not followed just because a walk reached it.
                    resolved = self._workspace.resolve(relative)
                except SandboxError:
                    continue
                if is_protected(resolved):
                    continue
                try:
                    lines = resolved.read_text(encoding="utf-8").splitlines()
                except (UnicodeDecodeError, OSError):
                    continue
                for number, line in enumerate(lines, start=1):
                    haystack = line if case_sensitive else line.lower()
                    if wanted in haystack:
                        matches.append(f"{relative}:{number}: {line.strip()}")
                        if len(matches) > MAX_SEARCH_MATCHES:
                            return ToolResult(
                                ok=True,
                                output="\n".join(matches[:MAX_SEARCH_MATCHES]),
                                truncated=True,
                            )
        if not matches:
            return ToolResult(ok=True, output="no matches")
        return ToolResult(ok=True, output="\n".join(matches))


class WriteFile:
    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace
        self.spec = ToolSpec(
            name="write_file",
            description="Create or overwrite a UTF-8 text file in the workspace.",
            risk_level=RiskLevel.MEDIUM,
            input_schema={
                "type": "object",
                "properties": {"path": _PATH, "content": {"type": "string"}},
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            timeout_seconds=10,
            idempotent=True,
        )

    def run(self, arguments: Mapping[str, Any]) -> ToolResult:
        content = arguments["content"]
        if len(content) > MAX_WRITE_CHARS:
            return ToolResult(ok=False, error=f"content over {MAX_WRITE_CHARS} characters")
        path = self._workspace.resolve_for_write(arguments["path"])
        existed = path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        verb = "overwrote" if existed else "created"
        return ToolResult(
            ok=True, output=f"{verb} {self._workspace.relative(path)} ({len(content)} characters)"
        )


class RunCommand:
    def __init__(self, workspace: Workspace, *, timeout_seconds: int = 60) -> None:
        self._workspace = workspace
        self.spec = ToolSpec(
            name="run_command",
            description=(
                "Run one allowlisted, read-only or checking command in the workspace, "
                "without a shell."
            ),
            risk_level=RiskLevel.HIGH,
            input_schema={
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
            timeout_seconds=timeout_seconds,
            idempotent=False,
        )

    def run(self, arguments: Mapping[str, Any]) -> ToolResult:
        args = validate_command(arguments["command"])
        # An environment built from nothing, not the parent's minus a
        # denylist: the source blanked four named secrets and passed the
        # rest, so any secret it did not think of was inherited.
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self._workspace.root),
            "LANG": "C.UTF-8",
            "GIT_TERMINAL_PROMPT": "0",
        }
        if os.name == "nt":
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
        try:
            completed = subprocess.run(
                args,
                cwd=self._workspace.root,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.spec.timeout_seconds,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(ok=False, error=f"timed out after {self.spec.timeout_seconds}s")
        except FileNotFoundError:
            return ToolResult(ok=False, error=f"command not installed: {args[0]}")
        combined = completed.stdout + (f"\n[stderr]\n{completed.stderr}" if completed.stderr else "")
        output, truncated = _bounded(combined)
        if completed.returncode != 0:
            return ToolResult(
                ok=False,
                output=output,
                error=f"exit code {completed.returncode}",
                truncated=truncated,
            )
        return ToolResult(ok=True, output=output, truncated=truncated)


def default_tools(workspace: Workspace) -> list:
    return [
        ReadFile(workspace),
        ListDirectory(workspace),
        SearchText(workspace),
        WriteFile(workspace),
        RunCommand(workspace),
    ]
