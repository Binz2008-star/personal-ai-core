"""The agent's tools -- what it can do in its workspace.

Each declares its risk level, and the policy gate reads that declaration
(AGENT_ARCHITECTURE.md section 3):

    read_file      LOW     read one text file
    list_directory LOW     list a directory
    search_text    LOW     find lines containing a string (file contents)
    find_files     LOW     find files by name pattern, in every subdirectory
    write_file     MEDIUM  create or overwrite a text file (never a secret)
    edit_file      MEDIUM  replace one string in a text file (never a secret)
    run_command    HIGH    run one allowlisted command -- ASKED every time
    delete_file    CRITICAL delete one file -- ASKED, and a rollback point first

Every path goes through the `Workspace` sandbox inside the tool, because the
tool is the last place that knows what the path is for. Every output is
bounded; a truncated result says it was truncated.

File mutations record a rollback point (recovery.Checkpoints) before they
change anything. `delete_file` cannot be built without one: the design's
"CRITICAL: rollback point first" is a constructor argument, not a comment.

The web tools (web_search, fetch_url) are in web.py; `shell` below runs any
command, and only with the owner's yes for each one.
"""
from __future__ import annotations

import difflib
import fnmatch
import os
import re
import signal
import subprocess
import sys
import tempfile
from typing import Any, Mapping

from ..core.agent import RiskLevel, ToolResult, ToolSpec
from .commands import validate_command
from .recovery import Checkpoints
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


def run_bounded(
    args: str | list[str], *, cwd: Any, env: Mapping[str, str], timeout: int, shell: bool = False
) -> tuple[int, str, str]:
    """Run a command with no input and a timeout that actually ends it.

    Found on the rig, 2026-10-02 (ADR-022 baseline): the model ran
    `python -m pytest --pdb`; the failing test opened the debugger, which waited
    for input forever. `subprocess.run(timeout=...)` then killed only `cmd.exe`,
    the python grandchild kept the output pipes open, and the run hung for 26
    minutes. So: stdin is closed (a debugger or prompt reads end-of-file and
    exits), and on timeout the whole process tree is killed, not only the
    direct child. Raises subprocess.TimeoutExpired after the kill.
    """
    extra: dict[str, Any] = {}
    if sys.platform != "win32":
        extra["start_new_session"] = True  # its own process group, killed as one
    process = subprocess.Popen(  # noqa: S603 -- callers validate or confirm the command
        args, shell=shell, cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace", **extra,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass  # something outside the tree holds the pipes; give up on its output
        raise
    return process.returncode, stdout, stderr


def _kill_tree(process: subprocess.Popen[str]) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                       capture_output=True, timeout=30, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        process.kill()
    except OSError:
        pass


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
            if entry.name.lower() != ".git" and not self._workspace.is_reserved(entry)
        )
        truncated = len(entries) > MAX_LIST_ENTRIES
        return ToolResult(ok=True, output="\n".join(entries[:MAX_LIST_ENTRIES]), truncated=truncated)


class SearchText:
    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace
        self.spec = ToolSpec(
            name="search_text",
            description=(
                "Find lines containing a string, in text files under a directory. "
                "Searches file CONTENTS; to find files by name, use find_files."
            ),
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


class FindFiles:
    """Files whose NAME matches a pattern, in a directory and all below it.

    Added 2026-10-01: asked to count the JSON files in `evals`, the agent had
    only list_directory (one level) and search_text (contents), searched
    contents for "*.json" and answered "none" -- there were 39 in the
    subfolders. Names only: nothing here opens a file.
    """

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace
        self.spec = ToolSpec(
            name="find_files",
            description=(
                "Find files by name in a directory and all its subdirectories. "
                "`pattern` is a shell-style pattern such as *.json or test_*.py. "
                "Returns the relative paths and the total count."
            ),
            risk_level=RiskLevel.LOW,
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "e.g. *.json"},
                    "path": _PATH,
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            timeout_seconds=30,
            idempotent=True,
        )

    def run(self, arguments: Mapping[str, Any]) -> ToolResult:
        pattern = arguments["pattern"].strip()
        if not pattern:
            return ToolResult(ok=False, error="no pattern to match")
        given = arguments.get("path", ".")
        base = self._workspace.root if given in ("", ".") else self._workspace.resolve(given)
        if not base.is_dir():
            return ToolResult(ok=False, error=f"not a directory: {given}")
        found: list[str] = []
        for directory, subdirectories, files in os.walk(base):
            subdirectories[:] = sorted(d for d in subdirectories if d.lower() != ".git")
            for name in sorted(files):
                if not fnmatch.fnmatch(name.lower(), pattern.lower()):
                    continue
                relative = os.path.relpath(
                    os.path.join(directory, name), self._workspace.root
                ).replace(os.sep, "/")
                try:
                    # Through the sandbox: reserved files (the database) and
                    # symlinks out of the workspace are not reported.
                    self._workspace.resolve(relative)
                except SandboxError:
                    continue
                found.append(relative)
        if not found:
            return ToolResult(ok=True, output=f"no files match {pattern}")
        truncated = len(found) > MAX_LIST_ENTRIES
        shown = found[:MAX_LIST_ENTRIES]
        return ToolResult(
            ok=True,
            output="\n".join(shown) + f"\n{len(found)} file(s) match {pattern}",
            truncated=truncated,
        )


class WriteFile:
    def __init__(self, workspace: Workspace, checkpoints: Checkpoints | None = None) -> None:
        self._workspace = workspace
        self._checkpoints = checkpoints
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
        if path.is_dir():
            return ToolResult(ok=False, error=f"is a directory: {arguments['path']}")
        existed = path.exists()
        if self._checkpoints is not None:
            self._checkpoints.before_mutation(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        verb = "overwrote" if existed else "created"
        return ToolResult(
            ok=True, output=f"{verb} {self._workspace.relative(path)} ({len(content)} characters)"
        )


class EditFile:
    """Replace one string inside a UTF-8 text file, surgically.

    MEDIUM, like `write_file`: allowed in the workspace, audited. Unlike
    `write_file` the caller names what changes, not the whole file: with
    `require_unique=True` (the default) the edit runs only when `old_string`
    occurs exactly once, so an ambiguous match fails instead of editing the
    wrong place. Zero matches fail either way. The write is atomic (a
    temporary file in the same directory, then `os.replace`) and records a
    rollback point first, so a failed run can be undone like any other edit.
    """

    def __init__(self, workspace: Workspace, checkpoints: Checkpoints | None = None) -> None:
        self._workspace = workspace
        self._checkpoints = checkpoints
        self.spec = ToolSpec(
            name="edit_file",
            description=(
                "Replace old_string with new_string in a UTF-8 text file in the "
                "workspace. Fails when old_string matches zero times, or more "
                "than once while require_unique is true."
            ),
            risk_level=RiskLevel.MEDIUM,
            input_schema={
                "type": "object",
                "properties": {
                    "path": _PATH,
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "require_unique": {"type": "boolean"},
                },
                "required": ["path", "old_string", "new_string"],
                "additionalProperties": False,
            },
            timeout_seconds=10,
            idempotent=False,
        )

    def run(self, arguments: Mapping[str, Any]) -> ToolResult:
        old_string = arguments["old_string"]
        new_string = arguments["new_string"]
        require_unique = arguments.get("require_unique", True)
        if not isinstance(old_string, str) or not isinstance(new_string, str):
            return ToolResult(ok=False, error="old_string and new_string must be strings")
        if not isinstance(require_unique, bool):
            return ToolResult(ok=False, error="require_unique must be a boolean")
        if not old_string:
            return ToolResult(ok=False, error="old_string is empty: nothing to replace")
        path = self._workspace.resolve_for_write(arguments["path"])
        if path.is_dir():
            return ToolResult(ok=False, error=f"is a directory: {arguments['path']}")
        if not path.is_file():
            return ToolResult(ok=False, error=f"not a file: {arguments['path']}")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ToolResult(ok=False, error=f"not UTF-8 text: {arguments['path']}")
        matches = text.count(old_string)
        if matches == 0:
            return ToolResult(ok=False, error="old_string matches zero times: nothing to replace")
        if require_unique and matches > 1:
            return ToolResult(
                ok=False,
                error=f"old_string matches {matches} times and require_unique is true: "
                "narrow old_string or pass require_unique false",
            )
        new_text = text.replace(old_string, new_string)
        if len(new_text) > MAX_WRITE_CHARS:
            return ToolResult(ok=False, error=f"content over {MAX_WRITE_CHARS} characters")
        if new_text == text:
            return ToolResult(ok=True, output=f"no change to {self._workspace.relative(path)}")
        if self._checkpoints is not None:
            self._checkpoints.before_mutation(path)
        self._atomic_write(path, new_text)
        relative = self._workspace.relative(path)
        diff = "\n".join(
            difflib.unified_diff(
                text.splitlines(),
                new_text.splitlines(),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
                lineterm="",
            )
        )
        replaced = matches if not require_unique else 1
        output, truncated = _bounded(
            f"edited {relative} ({replaced} replacement(s), "
            f"{len(text)} to {len(new_text)} characters)\n{diff}"
        )
        return ToolResult(ok=True, output=output, truncated=truncated)

    @staticmethod
    def _atomic_write(path: Any, content: str) -> None:
        """Write through a temporary file in the same directory, then replace.

        A direct `write_text` leaves a half-written file when the process dies
        mid-write; `os.replace` is atomic on the same filesystem, so readers
        see the old file or the new one, never a mix.
        """
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


class DeleteFile:
    def __init__(self, workspace: Workspace, checkpoints: Checkpoints) -> None:
        if not isinstance(checkpoints, Checkpoints):
            raise TypeError("delete_file needs a rollback point: pass Checkpoints")
        self._workspace = workspace
        self._checkpoints = checkpoints
        self.spec = ToolSpec(
            name="delete_file",
            description="Delete one file in the workspace. Can be rolled back this run.",
            risk_level=RiskLevel.CRITICAL,
            input_schema={
                "type": "object",
                "properties": {"path": _PATH},
                "required": ["path"],
                "additionalProperties": False,
            },
            timeout_seconds=10,
            idempotent=False,
        )

    def run(self, arguments: Mapping[str, Any]) -> ToolResult:
        path = self._workspace.resolve_for_write(arguments["path"])
        if not path.is_file():
            return ToolResult(ok=False, error=f"not a file: {arguments['path']}")
        self._checkpoints.before_mutation(path)
        path.unlink()
        return ToolResult(ok=True, output=f"deleted {self._workspace.relative(path)}")


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
            returncode, stdout, stderr = run_bounded(
                args, cwd=self._workspace.root, env=env, timeout=self.spec.timeout_seconds
            )
        except subprocess.TimeoutExpired:
            return ToolResult(ok=False, error=f"timed out after {self.spec.timeout_seconds}s")
        except FileNotFoundError:
            return ToolResult(ok=False, error=f"command not installed: {args[0]}")
        combined = stdout + (f"\n[stderr]\n{stderr}" if stderr else "")
        output, truncated = _bounded(combined)
        if returncode != 0:
            return ToolResult(
                ok=False,
                output=output,
                error=f"exit code {returncode}",
                truncated=truncated,
            )
        return ToolResult(ok=True, output=output, truncated=truncated)


# Environment variables whose NAMES say they hold a secret. The shell runs the
# owner's own tools, so it inherits their environment -- minus these, so a
# model that runs `env` or `set` does not read a token into its context.
_SECRET_NAME = re.compile(
    r"TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE|CREDENTIAL|AUTH|_KEY$|DSN|DATABASE_URL|COOKIE|SESSION",
    re.IGNORECASE,
)


def shell_environment() -> dict[str, str]:
    return {name: value for name, value in os.environ.items() if not _SECRET_NAME.search(name)}


class Shell:
    """Any command, through the platform's shell, in the workspace.

    HIGH: the owner is asked for every command and sees it in full. Unlike
    `run_command` there is no allowlist -- pipes, installs, builds, git
    commits -- so it is also not undoable: checkpoints see only the file
    tools' writes, and the description says so to the model.
    """

    def __init__(self, workspace: Workspace, *, timeout_seconds: int = 300) -> None:
        self._workspace = workspace
        self.spec = ToolSpec(
            name="shell",
            description=(
                "Run any command in the system shell (cmd on Windows, sh elsewhere), "
                "starting in the workspace. The user is asked to allow each command. "
                "Its effects cannot be undone."
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
        command = arguments["command"].strip()
        if not command:
            return ToolResult(ok=False, error="no command")
        try:
            # shell=True is the point of this tool; every command is confirmed.
            returncode, stdout, stderr = run_bounded(
                command, shell=True, cwd=self._workspace.root, env=shell_environment(),
                timeout=self.spec.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(ok=False, error=f"timed out after {self.spec.timeout_seconds}s")
        combined = stdout + (f"\n[stderr]\n{stderr}" if stderr else "")
        output, truncated = _bounded(combined)
        if returncode != 0:
            return ToolResult(
                ok=False, output=output, error=f"exit code {returncode}", truncated=truncated
            )
        return ToolResult(ok=True, output=output or "(no output)", truncated=truncated)


def default_tools(workspace: Workspace, checkpoints: Checkpoints | None = None) -> list:
    """The standard set. `delete_file` is included only with a rollback point."""
    tools: list = [
        ReadFile(workspace),
        ListDirectory(workspace),
        FindFiles(workspace),
        SearchText(workspace),
        WriteFile(workspace, checkpoints),
        EditFile(workspace, checkpoints),
        RunCommand(workspace),
    ]
    if checkpoints is not None:
        tools.append(DeleteFile(workspace, checkpoints))
    return tools
