"""The agent's tools -- what it can do in its workspace.

Each declares its risk level, and the policy gate reads that declaration
(AGENT_ARCHITECTURE.md section 3):

    read_file      LOW     read one text file
    list_directory LOW     list a directory
    search_text    LOW     find lines containing a string (file contents)
    find_files     LOW     find files by name pattern, in every subdirectory
    write_file     MEDIUM  create or overwrite a text file (never a secret)
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

import ctypes
import fnmatch
import os
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Mapping

from ..core.agent import RiskLevel, ToolResult, ToolSpec
from .commands import RECURSIVE_READ, SEARCH_TEXT_INSTEAD, CommandRejected, validate_command
from .recovery import Checkpoints, atomic_write_text
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


# --- Windows process-tree ownership -----------------------------------------
#
# POSIX gets a killable group for free: `start_new_session` makes the child its
# own session/group leader and `os.killpg` kills every member of it, including
# after the leader itself has exited. Windows has no such group. `taskkill /T
# /PID` walks the tree from the named process, so once the leader is gone it
# cannot reach a descendant that is still holding the inherited output pipes:
# the descendant survives the timeout and the two reader threads stay blocked
# on it (observed on the owner's Windows machine, 2026-10-08, with the leader
# exiting before the kill). A Job Object closes that hole. The child is assigned
# to a job whose last handle closing terminates every member, so the whole tree
# dies from the job handle even when the leader is already gone.
#
# THE RACE THAT MAKES SUSPENSION NECESSARY. A process inherits its parent's job
# at the moment it is created, so a descendant is contained only if its parent
# was already in the job when it forked. Assigning the leader just after
# `Popen` returns leaves a window in which a fast, native command can spawn a
# grandchild that escaped the job -- and `taskkill /T` cannot reach it once the
# leader exits, which is the whole failure being fixed. So the child is created
# SUSPENDED (it cannot run, and cannot fork, until told to), assigned to the job
# while it is frozen, and only then resumed. Containment is then by
# construction rather than a race we hope to win. These are Windows-only APIs,
# reached through `getattr` so the Linux type gate -- which runs pyright on a
# platform where `WinDLL` does not exist -- stays quiet.
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9  # JobObjectExtendedLimitInformation
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_CREATE_SUSPENDED = 0x00000004
_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002

_jobs: dict[int, int] = {}
_jobs_lock = threading.Lock()


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _ThreadEntry32(ctypes.Structure):
    """THREADENTRY32 -- one row of a system thread snapshot, filtered by owner."""

    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ThreadID", ctypes.c_uint32),
        ("th32OwnerProcessID", ctypes.c_uint32),
        ("tpBasePri", ctypes.c_int32),
        ("tpDeltaPri", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32),
    ]


def _job_kernel32() -> Any:
    """A configured kernel32, or None when not on Windows / no WinDLL.

    Every prototype is stated: without them ctypes truncates a 64-bit HANDLE to
    a C int and `TerminateJobObject` silently targets the wrong object.
    """
    win_dll = getattr(ctypes, "WinDLL", None)
    if sys.platform != "win32" or win_dll is None:
        return None
    kernel32 = win_dll("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.SetInformationJobObject.restype = ctypes.c_int
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
    ]
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.TerminateJobObject.restype = ctypes.c_int
    kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    # Suspended-start resume path: find the child's one thread and release it.
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    kernel32.Thread32First.restype = ctypes.c_int
    kernel32.Thread32First.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.Thread32Next.restype = ctypes.c_int
    kernel32.Thread32Next.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.OpenThread.restype = ctypes.c_void_p
    kernel32.OpenThread.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.ResumeThread.restype = ctypes.c_uint32
    kernel32.ResumeThread.argtypes = [ctypes.c_void_p]
    return kernel32


def _create_kill_on_close_job() -> int | None:
    """A private job that terminates its members when its last handle closes.

    The job is unnamed and private, so it contains only what is explicitly
    assigned to it -- closing it can never reach an unrelated process.
    """
    kernel32 = _job_kernel32()
    if kernel32 is None:
        return None
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    limits = _JobExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        ctypes.c_void_p(job), _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits), ctypes.sizeof(limits),
    ):
        kernel32.CloseHandle(ctypes.c_void_p(job))
        return None
    return int(job)


def _assign_process_to_job(job: int, process: subprocess.Popen[str]) -> bool:
    kernel32 = _job_kernel32()
    if kernel32 is None:
        return False
    # `_handle` is subprocess' own process handle; CreateProcess grants it the
    # PROCESS_SET_QUOTA|PROCESS_TERMINATE that AssignProcessToJobObject needs.
    # That handle is owned by subprocess and must NOT be closed here.
    return bool(kernel32.AssignProcessToJobObject(
        ctypes.c_void_p(job), ctypes.c_void_p(int(getattr(process, "_handle")))
    ))


def _resume_process(process: subprocess.Popen[str]) -> bool:
    """Release the single thread of a `CREATE_SUSPENDED` child. True on success.

    CPython closes the primary thread handle immediately after `CreateProcess`,
    so the child's one thread is found here through a system thread snapshot and
    resumed by its thread id. Both the snapshot and the thread handle are ours
    and are closed before returning.
    """
    kernel32 = _job_kernel32()
    if kernel32 is None:
        return False
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return False
    entry = _ThreadEntry32()
    entry.dwSize = ctypes.sizeof(_ThreadEntry32)
    resumed = False
    try:
        more = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while more:
            if entry.th32OwnerProcessID == process.pid:
                thread = kernel32.OpenThread(
                    _THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if thread:
                    try:
                        # ResumeThread returns the prior suspend count, or
                        # (DWORD)-1 on failure; the child was suspended exactly
                        # once, so any value but -1 is the release we want.
                        resumed = kernel32.ResumeThread(ctypes.c_void_p(thread)) != 0xFFFFFFFF
                    finally:
                        kernel32.CloseHandle(ctypes.c_void_p(thread))
                break
            more = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(snapshot))
    return resumed


def _start_suspended_in_job() -> tuple[int | None, int]:
    """A job handle and the `creationflags` to start its child suspended.

    Returns `(None, 0)` off Windows or when the job cannot be created; the
    caller then runs the command normally and still has `taskkill`.
    """
    job = _create_kill_on_close_job()
    if job is None:
        return None, 0
    return job, _CREATE_SUSPENDED


def _contain_and_release(job: int, process: subprocess.Popen[str]) -> int | None:
    """Assign a suspended child to `job`, resume it, and return the job to track.

    Never returns with the child still suspended: a frozen child would hang the
    caller with no output and no timeout if it never ran. When the job cannot
    hold the child -- for example PAC is itself in a job that forbids nesting --
    the command is resumed anyway and `None` is returned, so the caller keeps
    the `taskkill` fallback. When the child is contained but cannot be resumed,
    the job is terminated so the process does not linger frozen, and `None` is
    returned: the call reports a failed command rather than hanging.
    """
    if _assign_process_to_job(job, process):
        if _resume_process(process):
            return job
        _terminate_job(job)  # kills the contained, still-frozen child
        _close_job(job)
        return None
    if not _resume_process(process):
        process.kill()  # could not run it; never leave it suspended
    _close_job(job)
    return None


def _terminate_job(job: int) -> bool:
    kernel32 = _job_kernel32()
    return bool(kernel32 and kernel32.TerminateJobObject(ctypes.c_void_p(job), 1))


def _close_job(job: int) -> None:
    kernel32 = _job_kernel32()
    if kernel32 is not None:
        kernel32.CloseHandle(ctypes.c_void_p(job))


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

    On Windows the tree is contained in a job object rather than reached with
    `taskkill /T`, which cannot find a descendant once its leader has exited
    (see the block above).
    """
    extra: dict[str, Any] = {}
    job: int | None = None
    creationflags = 0
    if sys.platform != "win32":
        extra["start_new_session"] = True  # its own process group, killed as one
    else:
        # Create the child frozen so it is inside the job before it can fork;
        # `_contain_and_release` resumes it, or reports the command failed.
        job, creationflags = _start_suspended_in_job()
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(  # noqa: S603 -- callers validate or confirm the command
            args, shell=shell, cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", creationflags=creationflags, **extra,
        )
        if job is not None:
            job = _contain_and_release(job, process)
    except BaseException:
        # Creation can fail before Popen returns. Assignment/resume can also be
        # interrupted while the child is frozen: release our handle and reap
        # that child before propagating the original failure.
        try:
            if process is not None:
                process.kill()
                process.wait()
        finally:
            if job is not None:
                _close_job(job)
        raise
    if job is not None:
        with _jobs_lock:
            _jobs[id(process)] = job
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass  # something outside the tree holds the pipes; give up on its output
        raise
    except BaseException:
        # N3: anything else that ends the wait -- a Ctrl-C above all -- ends
        # the command too. It runs in its own process group, so the terminal's
        # interrupt never reaches it; left alone it would go on running, and
        # writing, after pac had exited.
        _kill_tree(process)
        raise
    finally:
        if job is not None:
            with _jobs_lock:
                _jobs.pop(id(process), None)
            # Closing the job (KILL_ON_JOB_CLOSE) ends any member that survived
            # the kill above, so a command's tree cannot outlive the call.
            _close_job(job)
    return process.returncode, stdout, stderr


def _kill_tree(process: subprocess.Popen[str]) -> None:
    if sys.platform == "win32":
        with _jobs_lock:
            job = _jobs.get(id(process))
        # The job handle reaches descendants after the leader has exited, which
        # `taskkill /T` cannot; keep taskkill as the fallback for the cases
        # where the job was never created or assignment was refused.
        if job is None or not _terminate_job(job):
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
                            # N2: the line count was bounded and each line
                            # was not, so 200 lines of a minified file were
                            # megabytes; the same cap as every other output.
                            output, _ = _bounded("\n".join(matches[:MAX_SEARCH_MATCHES]))
                            return ToolResult(ok=True, output=output, truncated=True)
        if not matches:
            return ToolResult(ok=True, output="no matches")
        output, truncated = _bounded("\n".join(matches))
        return ToolResult(ok=True, output=output, truncated=truncated)


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
        atomic_write_text(path, content)
        verb = "overwrote" if existed else "created"
        return ToolResult(
            ok=True, output=f"{verb} {self._workspace.relative(path)} ({len(content)} characters)"
        )


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


def _refuse_protected_arguments(workspace: Workspace, args: list[str]) -> None:
    """Refuse a command that names a file the file tools may not touch.

    `validate_command` is workspace-agnostic: it blocks escapes, but cannot
    know which names this workspace protects or reserves (`.env`, `.git`, the
    Core's own database). The workspace knows, so the check lives here, after
    validation and before anything runs: without it, `cat .env` through
    run_command put a secret file in front of the model. Each non-flag token
    is tried whole, plus its `--opt=value` value and a short option's attached
    value. A candidate with a colon is a `rev:path` (`git show HEAD:.env`) or an
    option value (`-p no:cacheprovider`), not a file name, so only the part
    after its last colon is tried: on Windows the whole token would be read as
    an alternate data stream and refused.
    """
    for token in args[1:]:
        candidates = [token]
        if "=" in token:
            candidates.append(token.split("=", 1)[1])
        if token.startswith("-") and not token.startswith("--") and len(token) > 2:
            candidates.append(token[2:])
        candidates = [c.split(":")[-1] if ":" in c else c for c in candidates]
        for candidate in candidates:
            if not candidate or candidate.startswith("-"):
                continue
            try:
                workspace.resolve_for_write(candidate)
            except SandboxError as exc:
                raise CommandRejected(
                    f"command touches a file the agent may not use: {token} ({exc})"
                ) from None


def _refuse_diff_outside_a_repository(
    workspace: Workspace, args: list[str], env: Mapping[str, str]
) -> None:
    """Refuse `git diff` where git would read it as `git diff --no-index`.

    Outside a git work tree, `git diff a b` compares the two paths themselves,
    a directory file by file, so `git diff empty .` printed every file in the
    workspace, the database and the owner's profile among them (fix 9 in
    commands.py refuses the explicit `--no-index`). Git is asked, with the
    environment and directory the command would run with, rather than the
    answer guessed from a `.git` on disk; if it cannot say, the diff is refused.
    """
    if args[:2] != ["git", "diff"]:
        return
    try:
        code, stdout, _ = run_bounded(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=workspace.root, env=env, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        code, stdout = 1, ""
    if code != 0 or stdout.strip() != "true":
        raise CommandRejected(
            f"git diff outside a git work tree compares files directly and "
            f"{RECURSIVE_READ}; {SEARCH_TEXT_INSTEAD}"
        )


def _refuse_shadowed_executable(workspace: Workspace, name: str) -> None:
    """Refuse an allowlisted command that a workspace file would shadow.

    Windows `CreateProcess` searches the working directory before `PATH`, so a
    confirmed `ruff` could run `workspace/ruff.exe`, a file the model may have
    written, instead of the system binary. Enforced on Windows only, where the
    lookup happens; a name with no binary at all is left to the existing
    not-installed path.
    """
    if sys.platform != "win32":
        return
    found = shutil.which(name)
    if found is None:
        return
    try:
        inside = Path(found).resolve().is_relative_to(workspace.root)
    except OSError:
        return
    if inside:
        raise CommandRejected(
            f"workspace shadows the command: {name} resolves to {found}, "
            "which the agent must not run"
        )


class RunCommand:
    def __init__(self, workspace: Workspace, *, timeout_seconds: int = 60) -> None:
        self._workspace = workspace
        self.spec = ToolSpec(
            name="run_command",
            description=(
                "Run one allowlisted command in the workspace, without a shell: a "
                "read-only one, or a check (pytest, ruff, mypy). A check is not "
                "read-only: pytest runs the project's own code, and the project's "
                "configuration can make any check write files, so the owner is asked "
                "before every run. Options that write files, such as ruff --fix, are "
                "refused: change files with write_file. Undoing a task restores what "
                "the file tools wrote, not what a command wrote."
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
        _refuse_protected_arguments(self._workspace, args)
        _refuse_shadowed_executable(self._workspace, args[0])
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
        _refuse_diff_outside_a_repository(self._workspace, args, env)
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


# The only variables the shell passes on (gap analysis P0-5). This was the
# owner's environment minus names that LOOKED secret, so any secret named
# otherwise -- AWS_ACCESS_KEY_ID, a token under a project's own name -- was
# one `env` or `set` away from the model's context. Now nothing is passed but
# the names below, each a location, a locale or a system setting that the
# owner's own tools need to run as they do in a terminal: git still finds its
# global config through HOME or USERPROFILE, pip and Python their temp and
# app-data folders, cmd its PATHEXT. Not passed, and why: proxies (their URL
# can carry a password), and everything a project or the owner named.
SHELL_ENV_NAMES = frozenset({
    # Where programs, the user's files and temporary files are.
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TEMP", "TMP",
    "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR",
    # Language, encoding and terminal.
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "TERM", "TZ",
    "PYTHONUTF8", "PYTHONIOENCODING",
    # Which interpreter or toolchain: paths, never credentials.
    "VIRTUAL_ENV", "CONDA_PREFIX", "CONDA_DEFAULT_ENV", "PYENV_ROOT", "NVM_DIR",
    "JAVA_HOME", "GOPATH", "GOROOT", "CARGO_HOME", "RUSTUP_HOME",
    # Windows: without these cmd, Python, pip and git misbehave.
    "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "OS",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "USERNAME", "APPDATA", "LOCALAPPDATA",
    "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
    "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "COMMONPROGRAMW6432",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PSMODULEPATH",
})


def shell_environment() -> dict[str, str]:
    """The shell's environment: the owner's values for SHELL_ENV_NAMES only.

    Built up, not filtered down, as `run_command`'s is, so a secret is left out
    whatever it is called. Windows names are matched without regard to case,
    as Windows does. A git that wants to ask for credentials fails instead of
    waiting for a terminal nobody is at.
    """
    env = {name: value for name, value in os.environ.items()
           if name.upper() in SHELL_ENV_NAMES}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


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
        RunCommand(workspace),
    ]
    if checkpoints is not None:
        tools.append(DeleteFile(workspace, checkpoints))
    return tools
