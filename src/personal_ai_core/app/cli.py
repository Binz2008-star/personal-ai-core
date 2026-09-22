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
from pathlib import Path
from typing import Callable, Iterable, Sequence, TextIO

from ..conversation.factory import build_in_memory_service, build_persistent_service
from ..core.config import Settings
from ..core.errors import ProviderError

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
    return parser


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

    slice_ = None
    if args.ephemeral:
        service, _ = build_in_memory_service(settings, transport=transport)  # type: ignore[arg-type]
        where = "nowhere -- --ephemeral was given"
    else:
        database = _database_path(args.database, environment)
        database.parent.mkdir(parents=True, exist_ok=True)
        slice_ = build_persistent_service(
            settings, database=database, transport=transport  # type: ignore[arg-type]
        )
        service = slice_.service
        where = str(database)

    try:
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
