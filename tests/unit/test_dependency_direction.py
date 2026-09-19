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

SRC = Path(__file__).resolve().parents[2] / "src" / "personal_ai_core"

FORBIDDEN_IN_DOMAIN = {
    "ollama", "httpx", "requests", "urllib", "aiohttp",
    "psycopg", "psycopg2", "sqlalchemy", "asyncpg", "redis",
    "rico", "rico_memory", "rico_agent", "torch", "transformers",
}


def imported_modules(path: Path) -> set[str]:
    """Third-party and stdlib top-level modules imported absolutely."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


# Which internal layer each package belongs to, and what it may depend on.
# The composition root is exempt: wiring concrete adapters to contracts is its
# entire job.
LAYER_MAY_IMPORT = {
    "core": set(),                                  # depends on nothing internal
    "runtime": {"core"},
    "persistence": {"core"},
    "conversation": {"core"},                       # application -> contracts only
    "knowledge": {"core"},                          # Phase 2 retrieval stack
    "context": {"core"},                            # Phase 2 budgeting
}
COMPOSITION_ROOTS = {"conversation/factory.py"}


def internal_imports(path: Path) -> set[str]:
    """Resolve internal imports to their target layer.

    Relative imports (`node.level > 0`) are resolved against the importing
    module's own position, which is what the earlier version of this file
    failed to do: it filtered on `level == 0` and so could not see a single
    internal import. A layering violation written as `from ..runtime import X`
    was invisible, and one was shipped because of it.
    """
    rel = path.relative_to(SRC)
    # package parts of the importing module, e.g. ("conversation",)
    pkg_parts = rel.parts[:-1]
    tree = ast.parse(path.read_text(encoding="utf-8"))
    layers: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level == 0:
            # absolute: only interesting if it names this package
            if node.module and node.module.split(".")[0] == "personal_ai_core":
                parts = node.module.split(".")
                if len(parts) > 1:
                    layers.add(parts[1])
            continue
        # relative: level 1 = current package, 2 = parent, ...
        base = pkg_parts[: len(pkg_parts) - (node.level - 1)]
        target = (*base, *(node.module.split(".") if node.module else ()))
        if target:
            layers.add(target[0])

    return layers


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


@pytest.mark.parametrize(
    "path",
    [p for p in SRC.rglob("*.py") if p.name != "__init__.py"],
    ids=lambda p: str(p.relative_to(SRC)),
)
def test_internal_layering_is_respected(path):
    """Every internal relative import resolves to a permitted layer.

    This is the check that was missing. It is what catches an application
    module importing a concrete infrastructure or runtime module.
    """
    rel = str(path.relative_to(SRC)).replace("\\", "/")
    if rel in COMPOSITION_ROOTS:
        pytest.skip("composition root may wire concrete adapters")

    layer = path.relative_to(SRC).parts[0]
    if layer not in LAYER_MAY_IMPORT:
        pytest.skip(f"no layer rule for {layer}")

    allowed = LAYER_MAY_IMPORT[layer] | {layer}
    offenders = internal_imports(path) - allowed
    assert not offenders, (
        f"{rel} (layer '{layer}') imports from {sorted(offenders)}; "
        f"may only import {sorted(allowed)}"
    )


def test_the_layering_check_actually_detects_a_violation():
    """Adversarial: prove the check above is not vacuous.

    The previous version of this file passed while `service.py` imported a
    concrete runtime class. A check that cannot fail is not a check, so this
    feeds it a known-bad module and requires a detection.
    """
    import tempfile

    violation = (
        "from ..persistence.in_memory import InMemoryEventRepository\n"
        "from ..core.contracts import EventRepository\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        fake_pkg = Path(tmp) / "conversation"
        fake_pkg.mkdir()
        module = fake_pkg / "offender.py"
        module.write_text(violation, encoding="utf-8")

        # resolve against the fake root the same way the real check does
        global SRC
        real_src, SRC = SRC, Path(tmp)
        try:
            found = internal_imports(module)
        finally:
            SRC = real_src

    assert "persistence" in found, (
        "the layering check failed to see a concrete infrastructure import"
    )
    assert "persistence" not in LAYER_MAY_IMPORT["conversation"], (
        "conversation must not be permitted to import persistence"
    )


def test_core_does_not_import_application_or_infrastructure_packages():
    for path in python_files("core"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level > 0:
                target = (node.module or "").split(".")[0]
                assert target not in {"conversation", "persistence", "runtime"}, (
                    f"{path.name} imports upward into {target}"
                )


# --- Phase 2: the contract must not learn about its adapters ---------------

INFRASTRUCTURE_TERMS = (
    "pgvector", "postgres", "psycopg", "hnsw", "tsvector", "neon",
    "sqlalchemy", "openai", "trigram", "chunker_v4", "second_brain",
)

# core/config.py is the one place provider configuration is allowed to name a
# provider -- an Ollama host has to be configured somewhere, and naming it
# there is what keeps it out of everywhere else.
CONTRACT_PURITY_EXEMPT = {"config.py"}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """Node ids of every docstring Constant, so prose can be skipped."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                found.add(id(first.value))
    return found


@pytest.mark.parametrize(
    "path",
    sorted((SRC / "core").rglob("*.py")),
    ids=lambda p: p.name,
)
def test_core_contracts_name_no_infrastructure_in_code(path):
    """Infrastructure may be discussed in prose, never named in code.

    The Second Brain retrieval, pgvector and HNSW are assets to adapt behind
    these contracts. The moment one of them appears in an identifier, a
    signature or a runtime string here, it has begun owning the architecture
    instead of serving it.

    Docstrings are exempt: several deliberately explain *why* a technology is
    excluded, and that reasoning is the most useful thing in the file.
    """
    if path.name in CONTRACT_PURITY_EXEMPT:
        pytest.skip(f"{path.name} is the provider-configuration boundary")

    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    skip = _docstring_nodes(tree)
    offenders = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.arg):
            name = node.arg
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in skip:
                continue
            name = node.value
        else:
            continue

        lowered = name.lower()
        for term in INFRASTRUCTURE_TERMS:
            if term in lowered:
                offenders.append(f"{term} in {name[:50]!r}")

    assert not offenders, f"{path.name} names infrastructure in code: {offenders}"


def test_the_contract_purity_check_actually_detects_a_leak():
    """Adversarial: prove the check above is not vacuous."""
    import tempfile

    leak = "def build_pgvector_index(hnsw_m: int) -> None: ...\n"
    with tempfile.NamedTemporaryFile(
        "w", suffix=".py", delete=False, encoding="utf-8"
    ) as f:
        f.write(leak)
        path = Path(f.name)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _docstring_nodes(tree)
        found = []
        for node in ast.walk(tree):
            name = None
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                name = node.name
            elif isinstance(node, ast.arg):
                name = node.arg
            if name and any(t in name.lower() for t in INFRASTRUCTURE_TERMS):
                found.append(name)
        assert "build_pgvector_index" in found, "leak went undetected"
        assert "hnsw_m" in found, "parameter leak went undetected"
    finally:
        path.unlink()


def test_no_core_module_imports_anything_from_the_test_tree():
    """Characterization must never become a runtime dependency.

    `tests/characterization/rrf_transcription.py` is a transcription of legacy
    SQL kept so its behaviour can be pinned without Postgres. It is evidence,
    not a component. The cross-check in
    `tests/characterization/test_core_fusion_vs_legacy.py` imports the Core;
    the arrow must never point back.
    """
    offenders = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                head = name.split(".")[0]
                if head in {"tests", "characterization", "conftest"}:
                    offenders.append(f"{path.relative_to(SRC)} -> {name}")
    assert not offenders, f"source imports the test tree: {offenders}"
