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
import sys
import uuid
from pathlib import Path
from typing import Callable, Iterable, Sequence, TextIO

from ..conversation.factory import (
    build_agent,
    build_grounded_in_memory_service,
    build_in_memory_service,
    build_persistent_service,
)
from ..core.config import Settings
from ..core.errors import ProviderError
from ..core.knowledge import Document

# What a DIRECTORY given to --documents contributes. A file named explicitly
# is read whatever its suffix: the user chose it. A directory is walked, and
# walking one without a filter would feed the index lock files, images and
# whatever else happens to live there.
DOCUMENT_SUFFIXES = (".md", ".txt")

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
            "a store."
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
    return parser


def _document_files(paths: Sequence[Path]) -> tuple[list[Path], list[Path]]:
    """The files to read, and the paths that do not exist.

    Sorted, so two runs over the same tree ingest in the same order and
    produce the same index. A missing path is returned rather than skipped:
    a typo in a path should stop the command, not quietly shrink the corpus.
    """
    files: list[Path] = []
    missing: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(
                sorted(
                    p
                    for p in path.rglob("*")
                    if p.is_file() and p.suffix.lower() in DOCUMENT_SUFFIXES
                )
            )
        elif path.is_file():
            files.append(path)
        else:
            missing.append(path)
    return files, missing


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
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.is_file() else "# About me\n"
    if not existing.endswith("\n"):
        existing += "\n"
    path.write_text(existing + f"- {text}\n", encoding="utf-8")
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
    return text


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: Callable[..., object] | None = None,
    stdin: Iterable[str] | None = None,
    stdout: TextIO | None = None,
    env: dict[str, str] | None = None,
) -> int:
    """Run one chat session. Returns a process exit code.

    Everything the outside world provides arrives as an argument, so the whole
    command is exercisable without a terminal, a home directory or a model
    server -- the same reason `transport` is injectable on the factories.
    """
    args = _parser().parse_args(argv)
    out = stdout if stdout is not None else sys.stdout
    environment = os.environ.copy() if env is None else env
    settings = Settings.from_env(environment)
    # One iterator, shared by the conversation and by the agent's
    # confirmation prompts: an answer to "Allow?" is the next line typed.
    lines = iter(stdin if stdin is not None else sys.stdin)

    database = None if args.ephemeral else _database_path(args.database, environment)
    profile_path = _profile_path(args.profile, environment, database)
    if args.remember is not None:
        return _remember(profile_path, args.remember, out)
    profile = _load_profile(profile_path, out)
    if profile is None:
        return 2
    settings = dataclasses.replace(settings, profile=profile)

    if args.agent:
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
    if grounded:
        files, missing = _document_files(args.documents)
        if missing:
            for path in missing:
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
            return _agent_session(agent=agent, session_id=session_id, lines=lines, out=out)

        print(file=out)

        return _converse(
            service=service,
            session_id=session_id,
            language=args.language,
            lines=lines,
            out=out,
        )
    finally:
        if slice_ is not None:
            slice_.close()


def _converse(*, service, session_id, language, lines, out) -> int:
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
        except ProviderError as exc:
            # The commonest first-run failure by far: nothing is listening on
            # the Ollama host. Naming the variable is the difference between
            # a fixable message and a traceback.
            print(f"the model could not be reached: {exc}", file=out)
            print(
                "check that the model server is running, or set PAC_OLLAMA_HOST.",
                file=out,
            )
            return 1
        print(f"{REPLY}{reply.content}", file=out)
    return 0


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


def _agent_session(*, agent, session_id, lines, out) -> int:
    for line in lines:
        task = line.strip()
        if not task:
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
        except ProviderError as exc:
            print(f"the model could not be reached: {exc}", file=out)
            print(
                "check that the model server is running, or set PAC_OLLAMA_HOST.",
                file=out,
            )
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
            restored = agent.checkpoints.rollback()
            print(f"         restored: {', '.join(restored)}", file=out)
        else:
            agent.checkpoints.commit()
    return 0
