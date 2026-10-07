"""Start the PAC provider on http://127.0.0.1:8765/v1."""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from personal_ai_core.core.config import Settings
from personal_ai_core.core.errors import ConfigError

from .factory import build_service
from .audit import JsonlAuditWriter
from .http import serve
from .service import IntegrationError
from .store import IntegrationStoreError


def _port(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Port must be a whole number") from error
    if not 1 <= parsed <= 65535:
        raise argparse.ArgumentTypeError("Port must be between 1 and 65535")
    return parsed


def parse_args(argv: list[str] | None = None, *, env: dict[str, str] | None = None) -> argparse.Namespace:
    """Resolve paths without opening the owner's profile or core database."""
    source = os.environ if env is None else env
    data_directory = Path.home() / ".personal-ai-core"
    database_default = Path(source["PAC_OPENCODE_DATABASE"]) if source.get("PAC_OPENCODE_DATABASE") else data_directory / "opencode.db"
    parser = argparse.ArgumentParser(description="PAC local model provider for OpenCode")
    parser.add_argument("--port", type=_port, default=8765)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--audit-log", type=Path)
    parser.add_argument("--audit-required", action="store_true",
                        help="Refuse a response if the test audit cannot be written")
    args = parser.parse_args(argv)
    if args.profile is None:
        if source.get("PAC_PROFILE"):
            args.profile = Path(source["PAC_PROFILE"])
        elif args.database is not None:
            args.profile = args.database.parent / "profile.md"
        elif source.get("PAC_DATABASE"):
            args.profile = Path(source["PAC_DATABASE"]).parent / "profile.md"
        else:
            args.profile = data_directory / "profile.md"
    if args.database is None:
        args.database = database_default
    if args.audit_log is None:
        args.audit_log = args.database.parent / "opencode-audit.jsonl"
    return args


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None) -> int:
    args = parse_args(argv, env=env)
    try:
        service = build_service(settings=Settings.from_env(env), profile_path=args.profile,
                                store_path=args.database, audit=JsonlAuditWriter(args.audit_log),
                                audit_required=args.audit_required,
                                audit_warning=lambda message: print(message, file=sys.stderr, flush=True))
        print(f"PAC (local): http://127.0.0.1:{args.port}/v1", flush=True)
        serve(service, port=args.port, log=lambda message: print(message, flush=True))
    except KeyboardInterrupt:
        return 0
    except (ConfigError, IntegrationStoreError, IntegrationError, sqlite3.Error):
        print("PAC provider could not start; check its configuration and separate integration database.",
              file=sys.stderr)
        return 2
    except OSError:
        print("PAC provider could not start; check file access and the localhost port.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
