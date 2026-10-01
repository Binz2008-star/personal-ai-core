"""Which weights a llama-server run used: the SHA-256 of its GGUF file.

A file name can be reused for different weights, so it is not evidence
(ADR-020 section 3.1). An Ollama blob is named `sha256-<hex>` after its own
content, so its name is its digest. Any other file is hashed in full on every
run: a cache keyed on size and modification time would answer a stale digest
for a file whose bytes changed and whose metadata did not.

Never fails: a file that cannot be read is recorded as unverified.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

_BLOB_NAME = re.compile(r"^sha256-([0-9a-f]{64})$")
_CHUNK = 1 << 20


def unverified(reason: str) -> dict[str, Any]:
    return {"digest": None, "source": None, "verified": False, "reason": reason}


def gguf_weights(model_path: str) -> dict[str, Any]:
    """The digest of the file llama-server reports in `/props` `model_path`."""
    if not model_path:
        return unverified("no model_path reported")
    name = model_path.replace("\\", "/").rsplit("/", 1)[-1]
    blob = _BLOB_NAME.match(name)
    if blob:
        return {"digest": f"sha256:{blob.group(1)}", "source": "ollama-blob",
                "verified": True}
    try:
        h = hashlib.sha256()
        with Path(model_path).open("rb") as f:
            for chunk in iter(lambda: f.read(_CHUNK), b""):
                h.update(chunk)
    except OSError as exc:
        return unverified(f"model file not readable here ({type(exc).__name__})")
    return {"digest": f"sha256:{h.hexdigest()}", "source": "gguf-sha256", "verified": True}
