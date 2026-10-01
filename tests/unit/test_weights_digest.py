"""ADR-020 section 3.1: a GGUF file is identified by its content, not its name."""
import hashlib
import os

from personal_ai_core.runtime.llamacpp.digest import gguf_weights

HEX = "b" * 64


def test_an_ollama_blob_is_named_after_its_content():
    for path in (f"/x/blobs/sha256-{HEX}", f"C:\\x\\blobs\\sha256-{HEX}"):
        assert gguf_weights(path) == {
            "digest": f"sha256:{HEX}", "source": "ollama-blob", "verified": True
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


def test_changed_bytes_are_seen_even_when_size_and_mtime_are_kept(tmp_path):
    # The case a size-and-mtime cache gets wrong: a copy tool or `utime` keeps
    # the metadata while the weights change. Every run hashes, so it is seen.
    model = tmp_path / "model.gguf"
    stamp = (1_700_000_000, 1_700_000_000)
    model.write_bytes(b"aaaa")
    os.utime(model, stamp)
    first = gguf_weights(str(model))["digest"]
    model.write_bytes(b"bbbb")
    os.utime(model, stamp)
    assert gguf_weights(str(model))["digest"] != first


def test_a_file_that_is_not_here_is_unverified(tmp_path):
    result = gguf_weights(str(tmp_path / "missing.gguf"))
    assert result["verified"] is False and result["digest"] is None
    assert "not readable" in result["reason"]


def test_no_path_is_unverified():
    assert gguf_weights("")["verified"] is False
