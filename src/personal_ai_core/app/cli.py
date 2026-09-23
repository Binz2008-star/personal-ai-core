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
import os
import sys
import uuid
from pathlib import Path
from typing import Callable, Iterable, Sequence, TextIO

from ..conversation.factory import (
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
    lines = stdin if stdin is not None else sys.stdin

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
    if args.ephemeral:
        if grounded:
            grounded_slice = build_grounded_in_memory_service(
                settings, transport=transport  # type: ignore[arg-type]
            )
            service, ingestion = grounded_slice.service, grounded_slice.ingestion
        else:
            service, _ = build_in_memory_service(settings, transport=transport)  # type: ignore[arg-type]
        where = "nowhere -- --ephemeral was given"
    else:
        database = _database_path(args.database, environment)
        database.parent.mkdir(parents=True, exist_ok=True)
        slice_ = build_persistent_service(
            settings,
            database=database,
            transport=transport,  # type: ignore[arg-type]
            grounded=grounded,
        )
        service, ingestion = slice_.service, slice_.ingestion
        where = str(database)

    try:
        if ingestion is not None:
            _ingest(ingestion, files, out)

        if args.session:
            # Not verified here. `send` raises KeyError for an unknown
            # session and that is the contract; asking the service whether a
            # session exists would mean either reaching into `_sessions` or
            # widening ConversationService for this command's convenience.
            # The cost is that the user learns on their first message rather
            # than before typing it -- said plainly rather than hidden, and
            # the message they get is a sentence, not a traceback.
            session_id = args.session
        else:
            session_id = service.start_session(service.create_user().id).id

        print(f"model:   {settings.boss_model}", file=out)
        print(f"storage: {where}", file=out)
        print(f"session: {session_id}", file=out)
        if not args.ephemeral:
            print(
                f"         continue this later with --session {session_id}", file=out
            )
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
