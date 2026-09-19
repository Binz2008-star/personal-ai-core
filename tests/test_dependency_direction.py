"""Dependency-direction tests (ARCHITECTURE.md §4).

    infrastructure ──implements──▶ core.contracts ◀──depends on── application

These are static import checks, not behaviour. They exist because dependency
direction erodes silently: one convenient import in the domain layer and the
model is no longer replaceable. A rule that is only written down is a rule that
drifts.
"""
import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "personal_ai_core"

FORBIDDEN_IN_DOMAIN = {
    "ollama", "httpx", "requests", "urllib", "aiohttp",
    "psycopg", "psycopg2", "sqlalchemy", "asyncpg", "redis",
    "rico", "rico_memory", "rico_agent", "torch", "transformers",
}


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def python_files(*parts: str) -> list[Path]:
    return sorted((SRC.joinpath(*parts)).rglob("*.py"))


@pytest.mark.parametrize("path", python_files("core"), ids=lambda p: p.name)
def test_domain_layer_imports_no_infrastructure(path):
    offenders = imported_modules(path) & FORBIDDEN_IN_DOMAIN
    assert not offenders, f"{path.name} imports infrastructure: {sorted(offenders)}"


@pytest.mark.parametrize("path", python_files("conversation"), ids=lambda p: p.name)
def test_application_layer_imports_no_http_or_driver(path):
    # The composition root wires adapters, so it may name them; it still must
    # not speak HTTP or SQL itself.
    offenders = imported_modules(path) & {
        "urllib", "httpx", "requests", "psycopg", "psycopg2", "sqlalchemy", "ollama"
    }
    assert not offenders, f"{path.name} reaches infrastructure: {sorted(offenders)}"


def test_only_the_ollama_adapter_knows_about_ollama():
    """ARCHITECTURE.md §4: no Ollama knowledge outside runtime/ollama/."""
    allowed = SRC / "runtime" / "ollama"
    offenders = []
    for path in SRC.rglob("*.py"):
        if allowed in path.parents:
            continue
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"'):
                continue
            if "/api/chat" in line or "/api/generate" in line or "import ollama" in line:
                offenders.append(f"{path.relative_to(SRC)}: {stripped[:60]}")
    assert not offenders, f"Ollama detail leaked outside the adapter: {offenders}"


def test_no_model_name_literal_in_business_logic():
    """ADR-002: the Boss model is configuration, not a literal."""
    config = SRC / "core" / "config.py"
    offenders = []
    for path in SRC.rglob("*.py"):
        if path == config:
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "qwen" in line.lower() and not line.strip().startswith("#"):
                offenders.append(f"{path.relative_to(SRC)}:{i}")
    assert not offenders, f"model name hard-coded outside config: {offenders}"


def test_core_does_not_import_application_or_infrastructure_packages():
    for path in python_files("core"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level > 0:
                target = (node.module or "").split(".")[0]
                assert target not in {"conversation", "persistence", "runtime"}, (
                    f"{path.name} imports upward into {target}"
                )
