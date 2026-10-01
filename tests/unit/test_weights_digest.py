"""ADR-020 section 3.1: a GGUF file is identified by its content, not its name."""
import hashlib
import json
import os

from personal_ai_core.runtime.llamacpp.digest import gguf_weights

HEX = "b" * 64


def test_an_ollama_blob_is_named_after_its_content():
    for path in (f"/x/blobs/sha256-{HEX}", f"C:\\x\\blobs\\sha256-{HEX}"):
        assert gguf_weights(path) == {
            "digest": f"sha256:{HEX}", "source": "ollama-blob-name", "verified": True
        }


def test_a_named_file_is_hashed(tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"weights")
    result = gguf_weights(str(model))
    assert result == {"digest": "sha256:" + hashlib.sha256(b"weights").hexdigest(),
                      "source": "gguf-sha256", "verified": True}


def test_a_reused_file_name_with_other_weights_gets_another_digest(tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"one")
    first = gguf_weights(str(model))["digest"]
    model.write_bytes(b"two-longer")
    assert gguf_weights(str(model))["digest"] != first


def test_the_cache_spares_rereading_an_unchanged_file(tmp_path):
    model, cache = tmp_path / "model.gguf", tmp_path / "cache.json"
    model.write_bytes(b"aaaa")
    stamp = (1_700_000_000, 1_700_000_000)
    os.utime(model, stamp)
    first = gguf_weights(str(model), cache)["digest"]
    # Same size and modification time, different bytes: only a cache hit can
    # still answer the first digest, which proves the file was not read again.
    model.write_bytes(b"bbbb")
    os.utime(model, stamp)
    assert gguf_weights(str(model), cache)["digest"] == first
    assert len(json.loads(cache.read_text(encoding="utf-8"))) == 1


def test_a_changed_file_is_hashed_again(tmp_path):
    model, cache = tmp_path / "model.gguf", tmp_path / "cache.json"
    model.write_bytes(b"aaaa")
    os.utime(model, (1_700_000_000, 1_700_000_000))
    first = gguf_weights(str(model), cache)["digest"]
    model.write_bytes(b"bbbb")
    os.utime(model, (1_700_000_100, 1_700_000_100))
    assert gguf_weights(str(model), cache)["digest"] != first


def test_a_corrupt_cache_costs_a_rehash_not_the_run(tmp_path):
    model, cache = tmp_path / "model.gguf", tmp_path / "cache.json"
    model.write_bytes(b"weights")
    cache.write_text("{not json", encoding="utf-8")
    assert gguf_weights(str(model), cache)["verified"] is True


def test_a_file_that_is_not_here_is_unverified(tmp_path):
    result = gguf_weights(str(tmp_path / "missing.gguf"))
    assert result["verified"] is False and result["digest"] is None
    assert "not readable" in result["reason"]


def test_no_path_is_unverified():
    assert gguf_weights("")["verified"] is False
