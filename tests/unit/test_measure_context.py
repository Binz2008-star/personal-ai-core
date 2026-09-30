"""evals/measure_context.py reads core.db and changes nothing.

The script runs on the owner's real database, so the property that matters
most is that it cannot alter it. Checked by hashing the file and its SQLite
companions before and after a run over a database `pac`'s own builder wrote.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
from pathlib import Path

from personal_ai_core.conversation.factory import build_persistent_service
from personal_ai_core.core.config import Settings

SCRIPT = Path(__file__).resolve().parents[2] / "evals" / "measure_context.py"


def _load():
    spec = importlib.util.spec_from_file_location("measure_context", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_transport(url, payload, timeout):
    return {"model": payload["model"], "message": {"content": "ok"},
            "done_reason": "stop", "prompt_eval_count": 300 + 10 * len(payload["messages"]),
            "eval_count": 2}


def _snapshot(folder: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.iterdir()) if p.is_file()}


def _database(tmp_path: Path) -> Path:
    db = tmp_path / "core.db"
    slice_ = build_persistent_service(Settings(), database=db, transport=fake_transport)
    try:
        service = slice_.service
        session = service.start_session(service.create_user().id)
        service.send(session_id=session.id, content="first question")
        service.send(session_id=session.id, content="second question")
    finally:
        slice_.close()
    return db


def test_it_reports_turns_and_leaves_the_database_untouched(tmp_path):
    db = _database(tmp_path)
    before = _snapshot(tmp_path)
    out = io.StringIO()

    assert _load().measure(db, out) == 0

    report = out.getvalue()
    assert "events:" in report and "messages: 4" in report
    assert "real prompt_tokens: min" in report
    assert "turns that stopped on length: 0" in report
    assert "agent context size cannot be measured" in report
    assert _snapshot(tmp_path) == before


def test_a_missing_database_is_reported_not_created(tmp_path):
    out = io.StringIO()
    missing = tmp_path / "absent.db"
    assert _load().measure(missing, out) == 2
    assert not missing.exists()
