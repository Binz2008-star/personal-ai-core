"""What a model server reports it has loaded -- the evaluation harness's check.

Lives in the adapter because the endpoint is the server's detail
(ARCHITECTURE.md section 4); the harness reaches it through the factory.
Never fails: a server that cannot answer is recorded as not confirmed.
"""
from __future__ import annotations

import json
import re
import urllib.request
from typing import Any, Callable, Mapping

# A probe takes a URL and, optionally, a JSON body. Without a body it returns
# the decoded JSON of a GET to the URL; with one, of a POST of that body.
Probe = Callable[..., Mapping[str, Any]]

_DIGEST = re.compile(r"^(?:sha256:)?([0-9a-f]{64})$")
_BLOB = re.compile(r"^sha256-([0-9a-f]{64})$")


def _unverified(reason: str, manifest: str | None = None) -> dict[str, Any]:
    return {"digest": None, "source": None, "verified": False, "reason": reason,
            "manifest_digest": manifest}


def _blob_digest(path: str) -> str | None:
    match = _BLOB.match(path.strip().strip('"').replace("\\", "/").rsplit("/", 1)[-1])
    return f"sha256:{match.group(1)}" if match else None


def _weights(probe: Probe, host: str, model: str, entry: Mapping[str, Any]) -> dict[str, Any]:
    """The weights Ollama serves for `model`, by blob digest (ADR-020 section 3.1).

    `/api/ps` reports the manifest's digest, which also changes with the
    template or parameters, and is not the digest llama.cpp sees for the same
    file. The weights are the blob the modelfile's FROM line names; Ollama
    stores blobs under their content's SHA-256. ADAPTER lines (a LoRA) change
    the weights too, so they are recorded with it. The manifest digest is kept
    as a second field.
    """
    found = _DIGEST.match(str(entry.get("digest") or ""))
    manifest = f"sha256:{found.group(1)}" if found else None
    try:
        shown = probe(f"{host.rstrip('/')}/api/show", {"model": model})
    except Exception as exc:  # noqa: BLE001 -- recorded, never fatal
        return _unverified(f"/api/show failed ({type(exc).__name__})", manifest)
    lines = str(shown.get("modelfile") or "").splitlines()
    froms = [line.split(None, 1)[1] for line in lines
             if line.upper().startswith("FROM ") and len(line.split(None, 1)) == 2]
    adapters = [line.split(None, 1)[1] for line in lines
                if line.upper().startswith("ADAPTER ") and len(line.split(None, 1)) == 2]
    if len(froms) != 1:
        return _unverified("modelfile has no single FROM line", manifest)
    digest = _blob_digest(froms[0])
    if digest is None:
        return _unverified("FROM does not name a blob", manifest)
    adapter_digests = [_blob_digest(a) for a in adapters]
    if any(d is None for d in adapter_digests):
        return _unverified("an ADAPTER does not name a blob", manifest)
    return {"digest": digest, "source": "ollama-blob", "verified": True,
            "adapters": adapter_digests, "manifest_digest": manifest}


def http_probe(url: str, body: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"} if data else {}
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def loaded_status(probe: Probe | None, host: str, model: str) -> dict[str, Any]:
    """What Ollama actually has loaded for the Boss model, from `/api/ps`.

    `--num-ctx` is what the owner typed. On 2026-10-01 a run went through the
    Ollama desktop app at 4096 while the shell said 8192, and nothing in the
    result could tell. This records what the server reports instead, and
    never fails the run: a probe that cannot answer says so in the header.
    """
    if probe is None:
        return {"probed": False, "reason": "no probe (test transport)",
                "weights": _unverified("no probe (test transport)")}
    try:
        data = probe(f"{host.rstrip('/')}/api/ps")
    except Exception as exc:  # noqa: BLE001 -- recorded, never fatal
        return {"probed": False, "reason": type(exc).__name__,
                "weights": _unverified(f"server not probed ({type(exc).__name__})")}
    for entry in data.get("models", []) or []:
        if model in (entry.get("name"), entry.get("model")):
            size, vram = entry.get("size"), entry.get("size_vram")
            return {
                "probed": True,
                "context_length": entry.get("context_length"),
                "gpu_share": (
                    round(vram / size, 2)
                    if isinstance(size, int) and isinstance(vram, int) and size
                    else None
                ),
                "weights": _weights(probe, host, model, entry),
            }
    return {"probed": True, "context_length": None, "reason": "model not loaded",
            "weights": _unverified("model not loaded")}
