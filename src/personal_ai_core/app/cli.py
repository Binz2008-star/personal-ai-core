"""The entry point — one command, and the state is still there next time.

This is the thinnest layer in the system and deliberately so. It parses
arguments, decides WHERE the database file lives, calls the composition root,
and reads lines. It builds no adapter and knows no storage engine: `app` may
import `core` and `conversation`, which is the composition root's own rule
seen from one level up.

Why the path is decided here and not in the factory: a library that writes to
a place the caller did not name loses data somewhere the caller does not look.
`build_persistent_service` therefore requires `database` and has no default.
Something has to choose, and an entry point is the thing that is allowed to --
it is the program, not a component of one.

The session id is printed on start and accepted on the next run. That is the
whole point of the durable store, expressed in the interface: a conversation
you can walk away from.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Sequence, TextIO

from ..conversation.factory import (
    build_agent,
    build_grounded_in_memory_service,
    build_in_memory_service,
    build_persistent_service,
    build_reply_redactor,
    describe_loaded,
    describe_store_failure,
    STORE_ERRORS,
)
from ..core.agent import AgentTaskContract
from ..core.config import Settings
from ..core.contracts import SecretRedactor
from ..core.domain import EventType
from ..core.errors import ConfigError, ContextOverflowError, ProviderError, RollbackIncomplete
from ..core.feedback import CORRECTION_KEY, FEEDBACK_EVENT_TYPE, FeedbackOutcome
from ..core.knowledge import Document
from ..core.redaction import RedactionError

# What a DIRECTORY given to --documents contributes. A file named explicitly
# is read whatever its suffix: the user chose it. A directory is walked, and
# walking one without a filter would feed the index lock files, images and
# whatever else happens to live there.
DOCUMENT_SUFFIXES = (".md", ".txt")

# How many kinds of unread file one line names. A directory can hold hundreds
# of kinds (a repository's `.git`); the line stays one line, and says how many
# it left out.
UNREAD_KINDS_SHOWN = 6

DEFAULT_DATABASE_ENV = "PAC_DATABASE"

# Under the user's data directory rather than the working directory: a file
# written next to wherever the shell happened to be is a file the user finds
# by accident, in several places, with a different conversation in each.
DEFAULT_DATABASE = Path.home() / ".personal-ai-core" / "core.db"

# The owner's profile: a Markdown file they write about themselves, composed
# into every turn. It lives next to the database unless named, so one data
# directory holds everything that is theirs.
PROFILE_ENV = "PAC_PROFILE"
PROFILE_FILENAME = "profile.md"
# The owner's projects, beside the profile and read with it: a separate file
# so it can be rewritten as projects change without touching what the owner
# wrote about themselves.
PROJECTS_FILENAME = "projects.md"
# Every character is paid for in every turn's context window. A profile
# (with its projects) that outgrows this is refused with a message rather
# than cut: a silently truncated profile is one the model reads differently
# from the one written. 12000 characters of mixed Arabic and English is
# roughly 3000 tokens of an 8192-token window.
MAX_PROFILE_CHARS = 12_000

PROMPT = "you> "
REPLY = "core> "
_AGENT_TASK = re.compile(r"^\[action_required=(true|false)\]\s+(.+?)\s*$")


def parse_agent_task(line: str) -> AgentTaskContract:
    """Parse one explicit caller-owned agent task contract."""
    match = _AGENT_TASK.fullmatch(line.rstrip("\r\n"))
    if match is None:
        raise ValueError("expected [action_required=true|false] TASK")
    task_text = match.group(2)
    if task_text.startswith("[action_required="):
        raise ValueError("expected [action_required=true|false] TASK")
    return AgentTaskContract(
        task_text=task_text, action_required=match.group(1) == "true"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pac",
        description=(
            "Talk to the Core. State is kept in a SQLite file and survives "
            "restarts; pass --session to continue an earlier conversation."
        ),
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help=(
            f"where the conversation is stored. Defaults to ${DEFAULT_DATABASE_ENV} "
            f"or {DEFAULT_DATABASE}."
        ),
    )
    parser.add_argument(
        "--ephemeral",
        action="store_true",
        help=(
            "keep nothing. Uses the in-memory slice, so the conversation ends "
            "with the process -- which is what every run did before there was "
            "a store. Not with --agent: its steps are the record of what it did."
        ),
    )
    parser.add_argument(
        "--session",
        default=None,
        help="continue this session id instead of starting a new one.",
    )
    parser.add_argument(
        "--language",
        default=None,
        help=(
            "the turn's language tag, e.g. ar or en. Left undetermined when "
            "not given: guessing it would put a claim in the record that "
            "nothing measured."
        ),
    )
    parser.add_argument(
        "--documents",
        type=Path,
        action="append",
        default=None,
        metavar="PATH",
        help=(
            "a file, or a directory of .md and .txt files, to answer from. "
            "Repeatable. Documents are read on every run and held in memory "
            "only: the conversation is stored, the corpus is not. Retrieval "
            "is lexical plus a hashing embedder -- surface overlap, not "
            "meaning."
        ),
    )
    parser.add_argument(
        "--agent",
        action="store_true",
        help=(
            "each line is a task for the agent, which may read, search and "
            "write files and run allowlisted commands inside --workspace. "
            "Running a command or deleting a file asks you first, every time."
        ),
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        metavar="DIR",
        help="the directory the agent is confined to. Required with --agent.",
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "a Markdown file about you -- work, projects, goals, preferences -- "
            f"read into every conversation and agent task. Defaults to ${PROFILE_ENV}, "
            f"or {PROFILE_FILENAME} next to the database."
        ),
    )
    parser.add_argument(
        "--remember",
        default=None,
        metavar="TEXT",
        help="add one line to your profile, and exit.",
    )
    parser.add_argument(
        "--feedback",
        default=None,
        metavar="LABEL",
        help=(
            "judge the latest reply in --session, and exit. One of: "
            f"{', '.join(o.value for o in FeedbackOutcome)}."
        ),
    )
    parser.add_argument(
        "--correction",
        default=None,
        metavar="TEXT",
        help="with --feedback correction only: what the reply should have said.",
    )
    parser.add_argument(
        "--observations",
        action="store_true",
        help=(
            "show what the feedback recorded in --session amounts to, and exit. "
            "Reads only; changes nothing and calls no model."
        ),
    )
    return parser


class DocumentScan(NamedTuple):
    """What the paths named on the command line come to."""

    files: list[Path]
    # Named paths that do not exist.
    missing: list[Path]
    # Files a directory walk passed over because of their type. Never a file
    # named explicitly: that one is read.
    unread: list[Path]


def _document_files(paths: Sequence[Path]) -> DocumentScan:
    """The files to read, the paths that do not exist, and the files passed over.

    Sorted, so two runs over the same tree ingest in the same order and
    produce the same index. A missing path is returned rather than skipped:
    a typo in a path should stop the command, not quietly shrink the corpus.
    A file a directory walk passes over is returned too, for the same reason:
    the user should learn that the corpus is smaller than the folder.
    """
    files: list[Path] = []
    missing: list[Path] = []
    unread: list[Path] = []
    for path in paths:
        if path.is_dir():
            found = sorted(p for p in path.rglob("*") if p.is_file())
            files.extend(p for p in found if p.suffix.lower() in DOCUMENT_SUFFIXES)
            unread.extend(
                p for p in found if p.suffix.lower() not in DOCUMENT_SUFFIXES
            )
        elif path.is_file():
            files.append(path)
        else:
            missing.append(path)
    return DocumentScan(files, missing, unread)


def _kind(path: Path) -> str:
    """What to call a file's type: its suffix, or for `.env` its whole name.

    `Path(".env").suffix` is empty, and a line that said "(no extension)" for
    the one file a user most wants to hear about would be no help.
    """
    if path.suffix:
        return path.suffix.lower()
    return path.name.lower() if path.name.startswith(".") else "(no extension)"


def _unread_line(unread: Sequence[Path]) -> str | None:
    """One line saying what a directory walk passed over, or None if nothing.

    Names kinds and counts, never contents: a `.env` is reported as a `.env`
    and nothing about what it holds. Most common first, then by name, so two
    runs over the same tree print the same line.
    """
    if not unread:
        return None
    counts: dict[str, int] = {}
    for path in unread:
        counts[_kind(path)] = counts.get(_kind(path), 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    shown = ", ".join(f"{kind} ({n})" for kind, n in ranked[:UNREAD_KINDS_SHOWN])
    left_out = len(ranked) - UNREAD_KINDS_SHOWN
    if left_out > 0:
        shown += f", and {left_out} more kind(s)"
    return (
        f"skipped: {len(unread)} file(s) that are not "
        f"{' or '.join(DOCUMENT_SUFFIXES)} -- {shown}; "
        "name a file itself to read it whatever its type"
    )


def _document_id(uri: str) -> str:
    """The same file is the same document, however often it is named.

    `Document.id` defaults to a random id. Then a file named twice -- directly
    and again through its directory -- would be two documents, indexed twice,
    and its passage could fill two of the few evidence slots. With an id
    derived from the URI, the second ingestion hits the content-hash check
    and does nothing.

    Not claimed: stability of anything recorded. Events record chunk ids, not
    document ids, and chunk ids are minted per run (see
    `build_persistent_service`).
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, uri))


def _ingest(ingestion, files: Sequence[Path], out: TextIO) -> None:
    ingested = 0
    chunks = 0
    for path in files:
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            print(f"skipped: {path} -- not UTF-8 text", file=out)
            continue
        except OSError as exc:
            print(f"skipped: {path} -- {exc.strerror or exc}", file=out)
            continue
        # `as_uri` percent-encodes, so the citation is a well-formed URI and
        # resolves back to this file.
        uri = path.resolve().as_uri()
        report = ingestion.ingest(
            Document(source_uri=uri, id=_document_id(uri)), content
        )
        if report.unchanged:
            # Named twice; already in the index. Counting it again would
            # report a corpus larger than the one being searched.
            continue
        ingested += 1
        chunks += report.chunk_count
    print(
        f"documents: {ingested} file(s), {chunks} chunk(s) -- held in memory, "
        "read again on every run",
        file=out,
    )


def _database_path(argument: Path | None, env: dict[str, str]) -> Path:
    if argument is not None:
        return argument
    from_env = env.get(DEFAULT_DATABASE_ENV)
    return Path(from_env) if from_env else DEFAULT_DATABASE


def _profile_path(
    argument: Path | None, env: dict[str, str], database: Path | None
) -> Path | None:
    """Where the profile is: named, from the environment, or beside the database.

    With --ephemeral and nothing named there is no data directory, so there
    is no default profile either.
    """
    if argument is not None:
        return argument
    from_env = env.get(PROFILE_ENV)
    if from_env:
        return Path(from_env)
    return database.parent / PROFILE_FILENAME if database is not None else None


def _remember(path: Path | None, text: str, out: TextIO) -> int:
    text = " ".join(text.split())
    if not text:
        print("--remember needs something to remember", file=out)
        return 2
    if path is None:
        print(f"no profile to add to: pass --profile PATH or set ${PROFILE_ENV}", file=out)
        return 2
    # The profile is sent to the model with every turn, conversation and
    # agent alike, and to whatever host PAC_OLLAMA_HOST names. A key, token or
    # password typed here would go with each one, so it is not written; the
    # value is not repeated back either.
    try:
        shapes = build_reply_redactor().redact(text).counts
    except RedactionError:
        print("that could not be checked for secrets, so it was not remembered", file=out)
        return 2
    if shapes:
        print(
            f"that looks like a secret ({', '.join(sorted(shapes))}); the profile is sent "
            "to the model with every turn, so it was not remembered",
            file=out,
        )
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.is_file() else "# About me\n"
    if not existing.endswith("\n"):
        existing += "\n"
    updated = existing + f"- {text}\n"
    # The limit the next run would refuse it for, said now instead.
    if len(updated.strip()) > MAX_PROFILE_CHARS:
        print(
            f"remembering this would make the profile {len(updated.strip())} characters; "
            f"the limit is {MAX_PROFILE_CHARS}, because it is sent with every turn. "
            f"Shorten {path} first.",
            file=out,
        )
        return 2
    path.write_text(updated, encoding="utf-8")
    print(f"remembered, in {path}", file=out)
    return 0


def _profile_files(path: Path | None) -> list[Path]:
    """The profile, then the projects file beside it -- those that exist."""
    if path is None:
        return []
    return [p for p in (path, path.parent / PROJECTS_FILENAME) if p.is_file()]


def _load_profile(path: Path | None, out: TextIO) -> str | None:
    """The profile text; "" when there is none; None when it cannot be used."""
    parts = []
    for file in _profile_files(path):
        try:
            text = file.read_text(encoding="utf-8").strip()
        except UnicodeDecodeError:
            print(f"the profile is not UTF-8 text: {file}", file=out)
            return None
        if text:
            parts.append(text)
    text = "\n\n".join(parts)
    if len(text) > MAX_PROFILE_CHARS:
        print(
            f"the profile is {len(text)} characters; the limit is {MAX_PROFILE_CHARS}, "
            f"because it is sent with every turn. Shorten "
            f"{' or '.join(str(f) for f in _profile_files(path))}.",
            file=out,
        )
        return None
    # A secret already in the file -- written by hand, or before --remember
    # refused them -- is withheld from what the model reads, the way a reply's
    # is withheld from what is printed. The file is not changed: it is the
    # owner's, and the line says where to look.
    if not text:
        return text
    try:
        shown = build_reply_redactor().redact(text)
    except RedactionError:
        print("the profile could not be checked for secrets, so it is not used", file=out)
        return None
    if shown.counts:
        print(
            f"the profile holds something secret-shaped (looks like: "
            f"{', '.join(sorted(shown.counts))}); it is withheld from the model. Remove it "
            f"from {' or '.join(str(f) for f in _profile_files(path))}.",
            file=out,
        )
        return shown.text
    return text




def _latest_reply(events, session_id: str):
    """The source event B1 judges: the session's latest GENERATION_COMPLETED.

    "Latest" is the event store's own session order (`seq`); no second
    ordering is introduced here. None when the session has no reply yet.
    """
    replies = [
        event
        for event in events.list_for_session(session_id)
        if event.type is EventType.GENERATION_COMPLETED
    ]
    return replies[-1] if replies else None


def _feedback_count(events, session_id: str) -> int:
    return sum(
        1
        for event in events.list_for_session(session_id)
        if event.type is FEEDBACK_EVENT_TYPE
    )


def _feedback(args, database: Path | None, settings: Settings, transport, out: TextIO) -> int:
    """Record one judgement of the latest reply in a stored session, and exit.

    Record-and-exit, like --remember, rather than a command typed into the
    conversation: a chat line is always a turn, so nothing a user says to the
    model can be taken for feedback, and no model is called here.

    Every refusal happens before the database is opened, except the two that
    need it -- whether the session exists, and whether it has a reply yet.
    The write itself is `FeedbackRecorder.record`; this function only decides
    what to judge. Duplicate arbitration stays with the repository's unique
    index; the before/after count only reports what it decided.
    """
    if args.feedback is None:
        print("--correction goes with --feedback correction", file=out)
        return 2
    if database is None:
        print("--feedback judges a stored conversation; it cannot be used with --ephemeral", file=out)
        return 2
    if args.agent or args.documents is not None or args.remember is not None:
        print(
            "--feedback records one judgement and exits; it cannot be combined "
            "with --agent, --documents or --remember",
            file=out,
        )
        return 2
    if not args.session:
        print("--feedback needs --session ID: the conversation whose latest reply it judges", file=out)
        return 2
    try:
        outcome = FeedbackOutcome(args.feedback)
    except ValueError:
        print(
            f"unknown feedback label: {args.feedback!r}. Use one of: "
            f"{', '.join(o.value for o in FeedbackOutcome)}",
            file=out,
        )
        return 2
    payload = None
    if args.correction is not None:
        if outcome is not FeedbackOutcome.CORRECTION:
            print(
                f"--correction goes with --feedback correction, not {outcome.value}",
                file=out,
            )
            return 2
        correction = " ".join(args.correction.split())
        if not correction:
            print("--correction needs the text the reply should have had", file=out)
            return 2
        payload = {CORRECTION_KEY: correction}
    if not database.is_file():
        # Opening would create an empty store: a judgement cannot be the
        # first thing written to a database.
        print(f"no stored conversations at {database}", file=out)
        return 2

    slice_ = build_persistent_service(settings, database=database, transport=transport)
    try:
        session_id = args.session
        if not slice_.service.has_session(session_id):
            print(f"no such session: {session_id}", file=out)
            return 2
        source = _latest_reply(slice_.events, session_id)
        if source is None:
            print(f"session {session_id} has no reply to give feedback on yet", file=out)
            return 2
        before = _feedback_count(slice_.events, session_id)
        slice_.feedback.record(
            source_event_id=source.id,
            session_id=session_id,
            outcome=outcome,
            actor="user",
            payload=payload,
        )
        if _feedback_count(slice_.events, session_id) == before:
            print(
                f"feedback already recorded: {outcome.value} on reply {source.id} "
                "-- nothing added; the first recording stands",
                file=out,
            )
        else:
            print(f"feedback recorded: {outcome.value} on reply {source.id}", file=out)
        return 0
    finally:
        slice_.close()


def _observations(args, database: Path | None, settings: Settings, transport, out: TextIO) -> int:
    """Print what the feedback in one stored session amounts to (ADR-017 Unit 2).

    Read-only: it derives Observations from what is already stored and
    prints them. Nothing is written, no turn changes, no model is called.
    The correction text shown is what the user typed; it is printed as data.
    """
    if database is None:
        print("--observations reads a stored conversation; it cannot be used with --ephemeral", file=out)
        return 2
    if (args.feedback is not None or args.correction is not None or args.agent
            or args.documents is not None or args.remember is not None):
        print(
            "--observations reads and exits; it cannot be combined with "
            "--feedback, --correction, --agent, --documents or --remember",
            file=out,
        )
        return 2
    if not args.session:
        print("--observations needs --session ID: the conversation to read", file=out)
        return 2
    if not database.is_file():
        print(f"no stored conversations at {database}", file=out)
        return 2

    slice_ = build_persistent_service(settings, database=database, transport=transport)
    try:
        session_id = args.session
        if not slice_.service.has_session(session_id):
            print(f"no such session: {session_id}", file=out)
            return 2
        observations, unobserved = slice_.observations(session_id)
        if not observations and not unobserved:
            print(f"no feedback recorded in session {session_id} yet", file=out)
            return 0
        print(f"observations for session {session_id}: {len(observations)}", file=out)
        for number, obs in enumerate(observations, start=1):
            print(
                f"[{number}] reply {obs.source_event_id}: {obs.effective_outcome.value} "
                f"-> {obs.effective_effect} ({len(obs.feedback_ids)} judgement(s))",
                file=out,
            )
            if obs.conflicted:
                earlier = ", ".join(outcome.value for _, outcome, _ in obs.conflicts)
                print(f"    conflicted: earlier {earlier}; not promotable", file=out)
            if obs.correction is not None:
                print(f"    correction: {obs.correction}", file=out)
        if unobserved:
            count = sum(len(u.feedback_ids) for u in unobserved)
            print(f"unobserved: {count} judgement(s) on something other than a reply", file=out)
        return 0
    finally:
        slice_.close()


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: Callable[..., object] | None = None,
    stdin: Iterable[str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    env: dict[str, str] | None = None,
    probe: Callable[..., Mapping[str, Any]] | None = None,
) -> int:
    """Run one chat session. Returns a process exit code.

    A failure of the store, wherever in the run it comes from, ends in a
    sentence and exit 1 rather than a traceback (gap analysis P1-5):
    `describe_store_failure` says what happened and what to do first.
    """
    try:
        return _main(argv, transport=transport, stdin=stdin, stdout=stdout,
                     stderr=stderr, env=env, probe=probe)
    except STORE_ERRORS as exc:
        environment = os.environ.copy() if env is None else env
        args = _parser().parse_args(argv)
        database = None if args.ephemeral else _database_path(args.database, environment)
        print(f"pac: {describe_store_failure(exc, database)}",
              file=stdout if stdout is not None else sys.stdout)
        return 1


def _main(
    argv: Sequence[str] | None = None,
    *,
    transport: Callable[..., object] | None = None,
    stdin: Iterable[str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    env: dict[str, str] | None = None,
    probe: Callable[..., Mapping[str, Any]] | None = None,
) -> int:
    """The run itself; `main` stands between it and a failing store.

    Everything the outside world provides arrives as an argument, so the whole
    command is exercisable without a terminal, a home directory or a model
    server -- the same reason `transport` is injectable on the factories.
    """
    args = _parser().parse_args(argv)
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    environment = os.environ.copy() if env is None else env
    try:
        settings = Settings.from_env(environment)
    except ConfigError as exc:
        # A usage error, as a bad flag is: a sentence and exit 2, not a
        # traceback indistinguishable from a crash (gap analysis P1-4).
        print(f"pac: {exc}", file=out)
        return 2
    # One iterator, shared by the conversation and by the agent's
    # confirmation prompts: an answer to "Allow?" is the next line typed.
    lines = iter(stdin if stdin is not None else sys.stdin)

    database = None if args.ephemeral else _database_path(args.database, environment)
    if args.observations:
        return _observations(args, database, settings, transport, out)
    if args.feedback is not None or args.correction is not None:
        return _feedback(args, database, settings, transport, out)
    profile_path = _profile_path(args.profile, environment, database)
    if args.remember is not None:
        return _remember(profile_path, args.remember, out)
    profile = _load_profile(profile_path, out)
    if profile is None:
        return 2
    settings = dataclasses.replace(settings, profile=profile)

    if args.agent:
        # The agent's steps are its audit trail: what it read, ran and wrote,
        # and what it was refused. --ephemeral would hold them in memory and
        # drop them at exit, so the runs that can change files would be the
        # ones that leave no record.
        if args.ephemeral:
            print(
                "--agent records every step it takes in the database; it cannot "
                "be used with --ephemeral, which keeps nothing",
                file=out,
            )
            return 2
        if args.workspace is None:
            print("--agent needs --workspace DIR: the directory it may work in", file=out)
            return 2
        if not args.workspace.is_dir():
            print(f"no such directory: {args.workspace}", file=out)
            return 2
        if args.documents is not None:
            print("choose --agent or --documents, not both", file=out)
            return 2

    # Resolved before anything is built or opened: a path that does not exist
    # is a mistake in the command, and it should cost nothing but a message.
    grounded = args.documents is not None
    files: list[Path] = []
    unread: list[Path] = []
    # Tested directly, not through `grounded`: pyright 1.1.411 does not carry
    # the narrowing through the alias (1.1.408, the CI pin, does).
    if args.documents is not None:
        scan = _document_files(args.documents)
        files, unread = scan.files, scan.unread
        if scan.missing:
            for path in scan.missing:
                print(f"no such file or directory: {path}", file=out)
            return 2

    slice_ = None
    ingestion = None
    if database is None:
        if grounded:
            grounded_slice = build_grounded_in_memory_service(
                settings, transport=transport  # type: ignore[arg-type]
            )
            service, ingestion = grounded_slice.service, grounded_slice.ingestion
            events = grounded_slice.events
        else:
            service, events = build_in_memory_service(settings, transport=transport)  # type: ignore[arg-type]
        where = "nowhere -- --ephemeral was given"
    else:
        database.parent.mkdir(parents=True, exist_ok=True)
        slice_ = build_persistent_service(
            settings,
            database=database,
            transport=transport,  # type: ignore[arg-type]
            grounded=grounded,
        )
        service, ingestion = slice_.service, slice_.ingestion
        events = slice_.events
        where = str(database)

    try:
        if ingestion is not None:
            passed_over = _unread_line(unread)
            if passed_over is not None:
                print(passed_over, file=out)
            _ingest(ingestion, files, out)

        if args.session:
            # Not verified here. `send` raises KeyError for an unknown
            # session and that is the contract; the agent loop raises the same
            # KeyError, asking the service through `has_session` (F-2), so
            # both paths refuse the same sessions at the same moment. The
            # cost is that the user learns on their first message or task
            # rather than before typing it -- said plainly rather than
            # hidden, and the message they get is a sentence, not a traceback.
            session_id = args.session
        else:
            session_id = service.start_session(service.create_user().id).id

        print(f"model:   {settings.boss_model}", file=out)
        _say_if_the_model_must_load(settings, probe=probe, live=transport is None, out=out)
        print(f"storage: {where}", file=out)
        if profile:
            print(
                f"profile: {' + '.join(str(f) for f in _profile_files(profile_path))} "
                f"({len(profile)} characters)",
                file=out,
            )
        elif profile_path is not None:
            print(
                f"profile: none yet -- write about yourself in {profile_path}, "
                'or add a line with --remember "..."',
                file=out,
            )
        print(f"session: {session_id}", file=out)
        if not args.ephemeral:
            print(
                f"         continue this later with --session {session_id}", file=out
            )
        if args.agent:
            agent = build_agent(
                settings,
                workspace=args.workspace,
                transport=transport,  # type: ignore[arg-type]
                confirm=_confirmer(lines, out),
                events=events,
                database=database,
                session_exists=service.has_session,
            )
            print(f"agent:   workspace {agent.workspace.root}", file=out)
            print(
                "         reads, searches and writes freely inside it; asks before "
                "running a command or deleting a file",
                file=out,
            )
            print(file=out)
            return _agent_session(agent=agent, session_id=session_id, lines=lines, out=out,
                                  err=err, settings=settings)

        print(file=out)

        return _converse(
            service=service,
            session_id=session_id,
            language=args.language,
            lines=lines,
            out=out,
            redactor=build_reply_redactor(),
            settings=settings,
        )
    finally:
        if slice_ is not None:
            slice_.close()


def _say_if_the_model_must_load(settings: Settings, *, probe, live: bool, out: TextIO) -> None:
    """Say before the first turn that it will load the model, when it will.

    Gap analysis P1-6: the first reply after Ollama unloaded the model loads
    it, which on the owner's machine takes a minute or more, and pac printed
    the banner and then nothing: it looked hung. Ollama says what it has
    loaded. When the Boss is not among it, the wait is announced. A server
    that cannot be asked is not guessed about here; the turn says what is
    wrong when it fails.
    """
    loaded = describe_loaded("ollama", ollama_host=settings.ollama_host, llamacpp_host="",
                             model=settings.boss_model, probe=probe, live=live)
    if loaded.get("probed") and loaded.get("reason") == "model not loaded":
        print("         not loaded yet: the first reply loads it, which can take "
              "a minute or more", file=out)


def _explain_provider_failure(exc: ProviderError, settings: Settings, out: TextIO) -> None:
    """Say which failure it was, and the fix that fits it.

    Gap analysis P1-6: every provider failure said "check that the model
    server is running". On a cold start the server is running and the model
    is still loading, so that advice sent the owner to restart something
    healthy. The provider classifies its failures (P1-8); each kind gets its
    own sentence, and the one nobody classified keeps the general advice.
    """
    if exc.kind == "timeout":
        print(f"the model did not answer within {settings.request_timeout_seconds} seconds.",
              file=out)
        print("a model that is loading -- the first reply after Ollama unloaded it -- can "
              "take longer than that: ask again, or raise PAC_REQUEST_TIMEOUT_SECONDS.", file=out)
    elif exc.kind == "unreachable":
        print(f"nothing answered at {settings.ollama_host}: {exc}", file=out)
        print("start Ollama, or set PAC_OLLAMA_HOST to where it runs.", file=out)
    elif exc.kind == "http_status" and exc.status == 404:
        print(f"the model server does not have {settings.boss_model}: {exc}", file=out)
        print(f"pull it (ollama pull {settings.boss_model}), or set PAC_BOSS_MODEL.", file=out)
    else:
        print(f"the model could not be reached: {exc}", file=out)
        print("check that the model server is running, or set PAC_OLLAMA_HOST.", file=out)


def _converse(*, service, session_id, language, lines, out, redactor: SecretRedactor,
              settings: Settings | None = None) -> int:
    """The chat loop. Every reply passes `redactor` before it is printed.

    Gap analysis P0-6: a secret the model repeats -- from the history, the
    profile or a document -- would otherwise reach the terminal and its
    scrollback. The value is withheld in place and a line under the reply
    says so, in the agent's words for the same event ("it contained something
    secret-shaped"). `redactor` has no default (ADR-018 I1): a caller that
    wants raw text must say so. What is stored is not changed here.
    """
    for line in lines:
        content = line.strip()
        if not content:
            continue
        try:
            reply = (
                service.send(session_id=session_id, content=content, language=language)
                if language
                else service.send(session_id=session_id, content=content)
            )
        except KeyError:
            print(f"no such session: {session_id}", file=out)
            return 2
        except ContextOverflowError as exc:
            # ADR-005: refused rather than sent and silently cut by the server.
            print(f"this conversation no longer fits the model: {exc}.", file=out)
            print("start a new session (run pac without --session) to continue.", file=out)
            return 1
        except ProviderError as exc:
            _explain_provider_failure(exc, settings or Settings(), out)
            return 1
        _print_reply(reply.content, redactor, out)
    return 0


# Under the reply, aligned with the other continuation lines ("changed:").
_NOTE = "         "


def _print_reply(content: str, redactor: SecretRedactor, out: TextIO) -> None:
    try:
        shown = redactor.redact(content)
    except RedactionError:
        # ADR-018 section 3.8: a check that failed is not a pass. Nothing of
        # the reply is printed, and the error carries no text to print.
        print(f"{REPLY}[reply withheld: it could not be checked for secrets]", file=out)
        return
    print(f"{REPLY}{shown.text}", file=out)
    if shown.counts:
        print(
            f"{_NOTE}[reply partly withheld: it contained something secret-shaped "
            f"(looks like: {', '.join(shown.counts)})]",
            file=out,
        )


YES = frozenset({"y", "yes", "نعم", "ن"})


def _ask(question: str, lines, out) -> bool:
    """A yes/no from the next line typed. End of input is a no."""
    print(question, end="", file=out, flush=True)
    answer = next(lines, "")
    print(file=out)
    return answer.strip().lower() in YES


def _confirmer(lines, out):
    def confirm(request, spec) -> bool:
        arguments = json.dumps(dict(request.arguments), ensure_ascii=False)
        return _ask(
            f"  ? {spec.name} {arguments} -- {spec.risk_level.value} risk. Allow? [y/N] ",
            lines,
            out,
        )

    return confirm


def _describe_step(step) -> str:
    record = step.record
    arguments = json.dumps(dict(record.request.arguments), ensure_ascii=False)
    if len(arguments) > 80:
        arguments = arguments[:77] + "..."
    result = record.result
    if record.decision.decision.value == "deny":
        status = f"denied: {record.decision.reason}"
    elif result is None:
        status = "no result"
    elif record.decision.decision.value == "ask" and not record.confirmed_by_user:
        status = "not allowed by you"
    elif result.ok:
        status = "ok"
    else:
        status = f"failed: {result.error}"
    return f"  · {record.request.tool} {arguments} -> {status}"


def _agent_session(*, agent, session_id, lines, out, err,
                   settings: Settings | None = None) -> int:
    invalid_tasks = False
    for line in lines:
        if not line.strip():
            continue
        try:
            task = parse_agent_task(line)
        except ValueError as exc:
            print(f"invalid agent task: {exc}", file=err)
            invalid_tasks = True
            continue
        try:
            outcome = agent.loop.run(
                task,
                session_id=session_id,
                on_step=lambda step: print(_describe_step(step), file=out, flush=True),
                on_protocol_error=lambda reason: print(
                    f"  · (the model's reply was not a valid step: {reason})", file=out, flush=True
                ),
            )
        except KeyError:
            print(f"no such session: {session_id}", file=out)
            return 2
        except ContextOverflowError as exc:
            # ADR-005: refused rather than sent and silently cut by the server.
            print(f"this conversation no longer fits the model: {exc}.", file=out)
            print("start a new session (run pac without --session) to continue.", file=out)
            return 1
        except ProviderError as exc:
            _explain_provider_failure(exc, settings or Settings(), out)
            return 1
        if outcome.finished:
            print(f"{REPLY}{outcome.answer}", file=out)
            if outcome.touched_files:
                print(f"         changed: {', '.join(outcome.touched_files)}", file=out)
            agent.checkpoints.commit()
            continue
        print(f"{outcome.stopped_reason}", file=out)
        if outcome.touched_files and _ask(
            f"  ? undo {len(outcome.touched_files)} file change(s) from this task "
            f"({', '.join(outcome.touched_files)})? [y/N] ",
            lines,
            out,
        ):
            try:
                restored = agent.checkpoints.rollback()
            except RollbackIncomplete as exc:
                if exc.restored:
                    print(f"         restored: {', '.join(exc.restored)}", file=out)
                print(f"         NOT restored: {', '.join(exc.unrestored)}", file=out)
            else:
                print(f"         restored: {', '.join(restored)}", file=out)
        else:
            agent.checkpoints.commit()
    return 2 if invalid_tasks else 0
