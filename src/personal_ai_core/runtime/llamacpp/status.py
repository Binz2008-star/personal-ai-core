"""What a model server reports it has loaded -- the evaluation harness's check.

Lives in the adapter because the endpoint is the server's detail
(ARCHITECTURE.md section 4); the harness reaches it through the factory.
Never fails: a server that cannot answer is recorded as not confirmed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from .digest import gguf_weights, unverified

# A probe takes a URL and returns the decoded JSON of a GET to it.
Probe = Callable[[str], Mapping[str, Any]]


def loaded_status(
    probe: Probe | None, host: str, digest_cache: Path | None = None
) -> dict[str, Any]:
    """llama-server's own report, from `/props`: context size and model file.

    `weights` is the file's digest (ADR-020 section 3.1). The file name is kept
    for reading, but it is not what identifies the weights.
    """
    if probe is None:
        return {"probed": False, "reason": "no probe (test transport)",
                "weights": unverified("no probe (test transport)")}
    try:
        data = probe(f"{host.rstrip('/')}/props")
    except Exception as exc:  # noqa: BLE001 -- recorded, never fatal
        return {"probed": False, "reason": type(exc).__name__,
                "weights": unverified(f"server not probed ({type(exc).__name__})")}
    settings = data.get("default_generation_settings") or {}
    n_ctx = data.get("n_ctx", settings.get("n_ctx"))
    path = str(data.get("model_path") or "")
    return {
        "probed": True,
        "context_length": n_ctx if isinstance(n_ctx, int) else None,
        "gpu_share": None,
        "model_file": path.replace("\\", "/").rsplit("/", 1)[-1] or None,
        "weights": gguf_weights(path, digest_cache),
    }
