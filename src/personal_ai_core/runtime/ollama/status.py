"""What a model server reports it has loaded -- the evaluation harness's check.

Lives in the adapter because the endpoint is the server's detail
(ARCHITECTURE.md section 4); the harness reaches it through the factory.
Never fails: a server that cannot answer is recorded as not confirmed.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any, Callable, Mapping

# A probe takes a URL and returns the decoded JSON of a GET to it.
Probe = Callable[[str], Mapping[str, Any]]


def http_probe(url: str) -> Mapping[str, Any]:
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def loaded_status(probe: Probe | None, host: str, model: str) -> dict[str, Any]:
    """What Ollama actually has loaded for the Boss model, from `/api/ps`.

    `--num-ctx` is what the owner typed. On 2026-10-01 a run went through the
    Ollama desktop app at 4096 while the shell said 8192, and nothing in the
    result could tell. This records what the server reports instead, and
    never fails the run: a probe that cannot answer says so in the header.
    """
    if probe is None:
        return {"probed": False, "reason": "no probe (test transport)"}
    try:
        data = probe(f"{host.rstrip('/')}/api/ps")
    except Exception as exc:  # noqa: BLE001 -- recorded, never fatal
        return {"probed": False, "reason": type(exc).__name__}
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
            }
    return {"probed": True, "context_length": None, "reason": "model not loaded"}
