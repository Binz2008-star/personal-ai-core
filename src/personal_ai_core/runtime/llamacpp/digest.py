"""Which weights a llama-server run used: the SHA-256 of its GGUF file.

A file name can be reused for different weights, so it is not evidence
(ADR-020 section 3.1). Two cases need no hashing:

- the file is an Ollama blob, named `sha256-<hex>` after its own content;
- the file was hashed before at the same size and modification time, which
  the cache file remembers, so a 4-5 GB file is read once, not once per run.

Never fails: a file that cannot be read is recorded as unverified.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

_BLOB_NAME = re.compile(r"^sha256-([0-9a-f]{64})$")
_CHUNK = 1 << 20


def unverified(reason: str) -> dict[str, Any]:
    return {"digest": None, "source": None, "verified": False, "reason": reason}


def gguf_weights(model_path: str, cache_file: Path | None = None) -> dict[str, Any]:
    """The digest of the file llama-server reports in `/props` `model_path`."""
    if not model_path:
        return unverified("no model_path reported")
    name = model_path.replace("\\", "/").rsplit("/", 1)[-1]
    blob = _BLOB_NAME.match(name)
    if blob:
        return {"digest": f"sha256:{blob.group(1)}", "source": "ollama-blob-name",
                "verified": True}
    path = Path(model_path)
    try:
        stat = path.stat()
    except OSError as exc:
        return unverified(f"model file not readable here ({type(exc).__name__})")
    key = f"{os.path.abspath(path)}|{stat.st_size}|{stat.st_mtime_ns}"
    cache = _read_cache(cache_file)
    digest = cache.get(key)
    if digest is None:
        try:
            digest = _sha256(path)
        except OSError as exc:
            return unverified(f"model file not readable here ({type(exc).__name__})")
        cache[key] = digest
        _write_cache(cache_file, cache)
    return {"digest": f"sha256:{digest}", "source": "gguf-sha256", "verified": True}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_cache(cache_file: Path | None) -> dict[str, str]:
    if cache_file is None:
        return {}
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else {}


def _write_cache(cache_file: Path | None, cache: dict[str, str]) -> None:
    if cache_file is None:
        return
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass  # a cache that cannot be written costs a re-hash, nothing else
