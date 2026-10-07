from __future__ import annotations

import json
import threading
from datetime import timezone

import pytest

import personal_ai_core.ui.documents as documents_module
from personal_ai_core.ui.documents import (
    DocumentError,
    DocumentStore,
    ManifestCorrupt,
    QuotaExceeded,
    safe_display_name,
)
from personal_ai_core.ui.server import UiApplication


def test_display_name_rejects_paths_and_sanitizes_label_characters() -> None:
    with pytest.raises(DocumentError, match="path"):
        safe_display_name(r"..\private\notes.md")
    with pytest.raises(DocumentError, match="path"):
        safe_display_name("../notes.md")
    assert safe_display_name("notes<script>.md") == "notes_script_.md"


def test_store_enforces_count_quota_and_persists_manifest(tmp_path) -> None:
    store = DocumentStore(tmp_path, max_count=1)
    record = store.save("notes.md", "budget: 18000".encode())
    assert record.ingestion_status == "pending"
    assert record.uploaded_at.tzinfo == timezone.utc
    with pytest.raises(QuotaExceeded):
        store.save("other.txt", b"second")

    restored = DocumentStore(tmp_path, max_count=1)
    restored_record = restored.get(record.id)
    assert restored_record is not None
    assert restored_record.ingestion_status == "pending"
    assert restored_record.indexed_at is None
    assert restored.text(record.id) == "budget: 18000"


def test_invalid_pdf_container_is_rejected_without_leaving_a_file(tmp_path) -> None:
    store = DocumentStore(tmp_path)
    with pytest.raises(DocumentError, match="valid PDF"):
        store.save("notes.pdf", b"not a pdf")
    assert list(tmp_path.glob(".*")) == []


def test_corrupt_manifest_is_backed_up_without_deleting_documents(tmp_path) -> None:
    orphan = tmp_path / "old-file.txt"
    orphan.write_text("keep me", encoding="utf-8")
    (tmp_path / "manifest.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(ManifestCorrupt):
        DocumentStore(tmp_path)
    assert orphan.exists()
    assert list(tmp_path.glob("manifest.corrupt-*.json"))


def test_manifest_write_failure_keeps_previous_manifest_and_rolls_back_memory(tmp_path, monkeypatch) -> None:
    store = DocumentStore(tmp_path)
    first = store.save("first.txt", b"first")
    previous = store.manifest_path.read_bytes()
    original_replace = documents_module.os.replace

    def fail_replace(source, destination):
        if destination == store.manifest_path:
            raise OSError("simulated interruption")
        return original_replace(source, destination)

    monkeypatch.setattr(documents_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated interruption"):
        store.save("second.txt", b"second")
    assert store.manifest_path.read_bytes() == previous
    assert tuple(record.id for record in store.list()) == (first.id,)
    assert len(list(tmp_path.glob("*.txt"))) == 1


def test_orphans_are_quarantined_when_manifest_is_valid(tmp_path) -> None:
    (tmp_path / "manifest.json").write_text(
        json.dumps({"version": 1, "documents": []}), encoding="utf-8"
    )
    orphan = tmp_path / "orphan.txt"
    orphan.write_text("keep me", encoding="utf-8")
    DocumentStore(tmp_path)
    assert not orphan.exists()
    assert list((tmp_path / "orphans").glob("*-orphan.txt"))


def test_stale_cleanup_does_not_follow_symlink(tmp_path) -> None:
    target = tmp_path / "outside.txt"
    target.write_text("keep", encoding="utf-8")
    link = tmp_path / ".uploading-link"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")
    DocumentStore(tmp_path)
    assert target.read_text(encoding="utf-8") == "keep"


def test_concurrent_uploads_cannot_both_cross_count_quota(tmp_path) -> None:
    store = DocumentStore(tmp_path, max_count=1)
    results: list[object] = []
    barrier = threading.Barrier(2)

    def upload() -> None:
        barrier.wait()
        try:
            results.append(store.save("notes.txt", b"one"))
        except QuotaExceeded:
            results.append("quota")

    threads = [threading.Thread(target=upload) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(result != "quota" for result in results) == 1
    assert sum(result == "quota" for result in results) == 1


def test_ingestion_failure_preserves_file_and_marks_failed(tmp_path, monkeypatch) -> None:
    app = UiApplication(tmp_path)

    def fail(_document_id):
        raise DocumentError("simulated ingestion failure")

    monkeypatch.setattr(app, "_ingest", fail)
    with pytest.raises(DocumentError, match="could not be indexed"):
        app.upload("notes.txt", b"content")
    records = app.documents.list()
    assert len(records) == 1
    assert records[0].ingestion_status == "failed"
    assert records[0].path.exists()
    assert app.documents.text(records[0].id) == "content"


def test_rebuild_failure_preserves_durable_document(tmp_path, monkeypatch) -> None:
    original = UiApplication(tmp_path)
    record = original.documents.save("notes.txt", b"content")

    def fail(_self, _document_id):
        raise DocumentError("simulated rebuild failure")

    monkeypatch.setattr(UiApplication, "_ingest", fail)
    rebuilt = UiApplication(tmp_path)
    kept = rebuilt.documents.get(record.id)
    assert kept is not None
    assert kept.ingestion_status == "failed"
    assert kept.path.exists()
