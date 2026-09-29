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
    "memory": {"core"},                             # Phase 3 promotion pipeline
    # ADR-017 review point 4: the learning components talk to core.contracts
    # repositories and nothing else -- never persistence, never conversation,
    # never the memory store. `test_learning_never_reaches_below_core` below
    # walks the real `learning/` tree and is what makes this row load-bearing
    # rather than decorative: a new layer with no row fails the per-module
    # check above, so the row cannot be forgotten when the layer is added.
    "learning": {"core"},                           # Phase 7 feedback/observation
    # ADR-011 question 8: the contract TYPES are in core and the TEXT is here,
    # so this layer needs core and nothing else. `conversation` must not
    # import it -- the composer reaches ConversationService through
    # factory.py, exactly as the budget policy does.
    "identity": {"core"},                           # ADR-011/ADR-012 identity
    # AGENT_ARCHITECTURE.md / ADR-004. Policy, sandbox and tools depend on
    # the contracts and nothing else; the loop reaches a model through
    # core.contracts.ModelProvider, never through runtime/.
    "agent": {"core"},                              # the agent layer
    # The entry point. WIDER than every other layer, and the reason is the
    # one thing it does: it calls the composition root. `conversation` is on
    # the list so it can reach factory.py; no adapter is, because knowing
    # which concrete class satisfies which contract is factory.py's job and
    # duplicating it here would give the system two composition roots.
    "app": {"core", "conversation"},                # the entry point
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


def layered_modules() -> list[Path]:
    """Every module that belongs to a layer.

    Package `__init__.py` files included: they re-export, and an import
    written there crosses layers exactly as one in any other module would.
    Excluding them left `agent/__init__.py` -- and every package's -- able to
    import anything unchecked. Only the top-level package file is outside
    every layer.
    """
    return [p for p in SRC.rglob("*.py") if p != SRC / "__init__.py"]


@pytest.mark.parametrize(
    "path",
    layered_modules(),
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
        # Fail, not skip. A skip here made this check go dark for exactly the
        # packages it was most needed for: every future one. A new `agent/`
        # or `api/` would be scanned, matched against no rule, silently
        # skipped, and reported green while importing whatever it liked.
        #
        # The suite did notice, but indirectly and with the wrong message:
        # `test_expected_skips.py` caps structural skips at two, so a third
        # made it fail saying "found 3 skips". That points at the skip
        # budget, not at the missing rule -- and raising the budget is the
        # obvious way to make it stop, which turns this check off for that
        # package permanently. The easier lever was the wrong one. This names
        # the right one instead.
        pytest.fail(
            f"'{layer}/' has no entry in LAYER_MAY_IMPORT, so its imports "
            "were never checked. Add one -- a new package starts at "
            f'{{"{layer}": {{"core"}}}} and widens only with a stated reason.'
        )

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


def test_learning_never_reaches_below_core():
    """ADR-017 review point 4, as an invariant rather than a table entry.

    `LAYER_MAY_IMPORT["learning"]` is only worth a row if the row is
    load-bearing. This walks the REAL `learning/` tree -- not a hand-written
    stand-in -- and requires every internal import to land in the declared
    boundary, so the claim "learning -> core only" is checked against the
    code that exists rather than asserted in a comment.

    It is the direct counterpart of
    `test_the_layering_check_actually_detects_a_violation`: that one proves the
    mechanism fires, this one proves the boundary is currently respected. Both
    are needed -- a green suite with the mechanism broken looks identical to a
    green suite with the boundary respected, and only the pair distinguishes
    them.
    """
    assert "learning" in LAYER_MAY_IMPORT, (
        "learning/ exists on disk, so it must have a declared boundary"
    )
    allowed = LAYER_MAY_IMPORT["learning"] | {"learning"}

    modules = sorted((SRC / "learning").rglob("*.py"))
    assert modules, (
        "learning/ is listed in LAYER_MAY_IMPORT but no such package exists; "
        "a stale row would make this test pass without checking anything"
    )

    for path in modules:
        found = internal_imports(path)
        offenders = found - allowed
        assert not offenders, (
            f"learning/{path.name} imports {sorted(offenders)}; the Phase 7 "
            f"layer may only import {sorted(allowed)}. Feedback reaches storage "
            "through core.contracts.FeedbackRepository, and the memory store is "
            "reachable only from memory/pipeline.py (ADR-017 review point 5)."
        )


def test_the_layering_check_detects_a_learning_module_importing_persistence():
    """Adversarial, for the learning boundary specifically.

    A `learning/` module reaching `persistence` is the specific mistake this
    layer exists to prevent: it would let the observation/rule side choose a
    concrete backend, and with it a transaction boundary it does not own.
    The row above is the rule; this proves the rule fires for this case.
    """
    import tempfile

    violation = (
        "from ..core.contracts import FeedbackRepository\n"
        "from ..persistence.sqlite import SqliteFeedbackRepository\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        fake_pkg = Path(tmp) / "learning"
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
        "the layering check failed to see a learning module import persistence"
    )
    assert "persistence" not in LAYER_MAY_IMPORT["learning"], (
        "learning must not be permitted to import persistence"
    )


def test_package_init_files_are_checked():
    """The guard scans every package's `__init__.py` (F-4)."""
    scanned = {p.relative_to(SRC).as_posix() for p in layered_modules()}
    packages = {p.relative_to(SRC).as_posix() for p in SRC.glob("*/__init__.py")}
    assert packages and packages <= scanned
    assert "__init__.py" not in scanned


def test_the_layering_check_sees_a_violation_in_a_package_init():
    """Adversarial: a crossing import in `agent/__init__.py` is detected and
    is outside what `agent` may import."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        fake_pkg = Path(tmp) / "agent"
        fake_pkg.mkdir()
        module = fake_pkg / "__init__.py"
        module.write_text(
            "from .tools import ReadFile\nfrom ..memory.pipeline import ExperiencePipeline\n",
            encoding="utf-8",
        )
        global SRC
        real_src, SRC = SRC, Path(tmp)
        try:
            found = internal_imports(module)
        finally:
            SRC = real_src

    assert found == {"agent", "memory"}
    assert "memory" not in LAYER_MAY_IMPORT["agent"]



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


def _infrastructure_offenders(tree: ast.AST) -> list[str]:
    """Every place this tree names infrastructure in code rather than prose.

    Extracted so that the check and the adversarial proof that the check
    works run the *same* code. They did not. The proof reimplemented a
    subset of this scan -- three node kinds of five, and it computed the
    docstring-exemption set and then never consulted it -- so it could only
    ever prove that its own copy detected a leak.

    That is the failure mode the proof exists to rule out. A regression in
    the `Name`, `Attribute` or `Constant` branch, or a docstring exemption
    widened until it swallowed real string constants, would have stopped
    the check detecting anything while the proof kept passing.
    """
    skip = _docstring_nodes(tree)
    offenders: list[str] = []

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

    return offenders


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

    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = _infrastructure_offenders(tree)
    assert not offenders, f"{path.name} names infrastructure in code: {offenders}"


# Adversarial: the check above is only worth having if it fires. Each case
# is a leak through one of the five node kinds the scanner inspects, driven
# through `_infrastructure_offenders` itself -- the production scanner, not
# a copy of it.
#
# The earlier proof reimplemented three of those kinds and asserted against
# its own reimplementation. It would have passed unchanged while the real
# scanner's `Name`, `Attribute` and `Constant` branches did nothing.

LEAKS = {
    "function name": "def build_pgvector_index() -> None: ...",
    "argument name": "def f(hnsw_m: int) -> None: ...",
    "class name": "class PostgresAdapter: ...",
    "bound name": "psycopg_conn = None",
    "attribute access": "x.tsvector_column",
    "runtime string": 'QUERY = "select from pgvector"',
}


@pytest.mark.parametrize("kind", sorted(LEAKS))
def test_the_contract_purity_check_detects_a_leak_through_each_node_kind(kind):
    offenders = _infrastructure_offenders(ast.parse(LEAKS[kind]))
    assert offenders, f"a leak via {kind} went undetected: {LEAKS[kind]!r}"


def test_the_contract_purity_check_is_silent_on_clean_source():
    """The other half: a scanner that fires on everything proves nothing."""
    clean = "def build_index(limit: int) -> None:\n    rows = limit\n"
    assert _infrastructure_offenders(ast.parse(clean)) == []


def test_prose_is_exempt_but_only_where_it_is_prose():
    """The docstring exemption must not extend to string *code*.

    Several core docstrings explain at length why pgvector is excluded, and
    that reasoning is worth more than the rule. But the exemption is scoped
    to a docstring -- the first statement of a module, class or function.
    The same text as a runtime value is a leak, and the previous proof never
    exercised this branch at all: it computed the exemption set and then
    ignored it.
    """
    as_docstring = '"""We deliberately do not use pgvector here."""'
    assert _infrastructure_offenders(ast.parse(as_docstring)) == []

    as_value = 'NOTE = "We deliberately do not use pgvector here."'
    assert _infrastructure_offenders(ast.parse(as_value))


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


# --- the destructive-entry gate must not be routed around -------------------

# `DATABASE_URL` is the production endpoint. The library's gate reads
# `POSTGRES_TEST_URL` and knows nothing about it, which is the whole reason a
# test run cannot reach a production database by accident. The moment a module
# under `src/` names `DATABASE_URL`, that separation is gone -- the backend
# would have a production URL in scope at the moment it is deciding whether a
# destructive DDL is safe to run.
#
# `os.environ` and `getenv` are banned from the Postgres backend for the same
# reason, with one named exception checked separately below: the gate reads
# exactly one variable (`POSTGRES_TEST_URL`) and one configuration denylist, and
# an allow-list of names is the only way that stays true.
_DATABASE_URL_FORBIDDEN = "DATABASE_URL"
_ENV_READERS = {"environ", "getenv"}
_ENV_EXEMPT_IN_POSTGRES = {"POSTGRES_TEST_URL", "PAC_PROTECTED_DATABASE_URLS"}


def _code_nodes_naming(tree: ast.AST, needle: str) -> list[int]:
    """Lines where `needle` appears as CODE -- a name, an attribute, a string
    literal -- rather than as prose.

    Without the docstring exemption a gate about `DATABASE_URL` flags every
    sentence explaining why `DATABASE_URL` is the hazard, which is the opposite
    of useful: the explanation would be the thing that breaks the build.
    """
    prose = _docstring_nodes(tree)
    lines = []
    for node in ast.walk(tree):
        if id(node) in prose:
            continue
        names: list[str] = []
        if isinstance(node, ast.Name):
            names = [node.id]
        elif isinstance(node, ast.Attribute):
            names = [node.attr]
        elif isinstance(node, ast.arg) and node.arg:
            names = [node.arg]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names = [node.value]
        if needle in names:
            # `ast.walk` yields the base `AST` type, on which `lineno` is not
            # declared -- it exists on the concrete expression and statement
            # nodes this loop has already narrowed to. `getattr` rather than a
            # cast because the narrowing below is exactly what makes the
            # attribute real, and a default of 0 keeps a future node kind that
            # lacks a position from crashing the gate that is meant to report.
            lines.append(getattr(node, "lineno", 0))
    return lines


def _repository_sources() -> list[Path]:
    """`src/` and `tests/`, and deliberately NOT `build/`.

    `build/lib/` holds a stale copy of the package left by a packaging run.
    It is a generated artifact, it is not what ships, and including it meant
    this gate reported files nobody had edited as offenders.
    """
    root = SRC.parent.parent
    return sorted(
        path
        for directory in ("src", "tests")
        for path in (root / directory).rglob("*.py")
    )


def test_no_module_under_src_names_the_production_database_url():
    offenders = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno in _code_nodes_naming(tree, _DATABASE_URL_FORBIDDEN):
            offenders.append(f"{path.relative_to(SRC)}:{lineno}")
    assert not offenders, (
        f"src/ names {_DATABASE_URL_FORBIDDEN}: {offenders}. The library gate "
        "reads POSTGRES_TEST_URL only, so a test run has no production URL in "
        "scope; comparing the two is test policy and lives in "
        "tests/support/postgres_target.py"
    )


def test_the_postgres_backend_reads_only_the_two_variables_it_may():
    """`os.environ` is a capability, so the Postgres backend gets a narrow one.

    A backend that can read any environment variable at the moment it is about
    to run DDL can be pointed at a database by anything else in the process.
    This pins the set of names it may read, so a third is a deliberate edit to
    a test rather than an accident in a diff.
    """
    path = SRC / "persistence" / "postgres.py"
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr not in _ENV_READERS:
            continue
        segment = ast.get_source_segment(text, node) or ""
        # Strip the two permitted names, then look for any subscript or `.get`
        # -- i.e. a read of a key that is not one of them.
        for permitted in _ENV_EXEMPT_IN_POSTGRES:
            segment = segment.replace(permitted, "")
        if "get" in segment or "[" in segment:
            offenders.append(f"line {node.lineno}: {segment.strip()}")
    assert not offenders, (
        f"persistence/postgres.py reads environment beyond "
        f"{sorted(_ENV_EXEMPT_IN_POSTGRES)}: {offenders}"
    )


def test_the_test_url_resolver_is_the_only_thing_that_reads_both_urls():
    """The asymmetry, made structural.

    `tests/support/postgres_target.py` is the one place in the repository that
    may name `DATABASE_URL` as code AND touch the PostgreSQL backend. One file,
    one decision, one place to audit -- and the library above it stays ignorant
    of production entirely.

    Scoped to files that actually reach the backend, on purpose. Naming
    `DATABASE_URL` is not itself the hazard: `test_agent_web_shell.py` sets it
    precisely to assert that the web shell does NOT leak it into a sandbox, and
    a gate that flagged that would be flagging the test that protects against
    the thing the gate is for.
    """
    touches_postgres = []
    for path in _repository_sources():
        # This file names both `persistence.postgres` and the forbidden
        # variable, because it is the thing checking for them. A gate never
        # reports itself; without this it would always fail on its own source.
        if path.resolve() == Path(__file__).resolve():
            continue
        text = path.read_text(encoding="utf-8")
        if "persistence.postgres" not in text and "persistence import postgres" not in text:
            continue
        if _code_nodes_naming(ast.parse(text), _DATABASE_URL_FORBIDDEN):
            touches_postgres.append(path)
    named = [p.relative_to(SRC.parent.parent).as_posix() for p in touches_postgres]
    assert named == ["tests/support/postgres_target.py"], (
        f"only the test URL resolver may name {_DATABASE_URL_FORBIDDEN} as code "
        f"while touching the backend, but so do: {named}"
    )


def test_the_safety_classification_does_not_use_to_regclass():
    """`to_regclass` resolves through `search_path` and matches any relkind.

    It does not state a schema, so a relation of the same name earlier on the
    path would be inspected instead of the intended one; and it matches views,
    sequences and anything else, so a view called `users` could pass as a
    table. The gate reads `pg_class` joined to `pg_namespace` and consults
    `relkind` instead, and this pins that so nobody "simplifies" it back.
    """
    text = (SRC / "persistence" / "postgres.py").read_text(encoding="utf-8")
    offenders = _code_nodes_naming(ast.parse(text), "to_regclass")
    assert not offenders, (
        f"the safety classification must not use to_regclass() in code: "
        f"lines {offenders}"
    )
    assert "pg_namespace" in text and "relkind" in text


def test_the_drop_capability_is_minted_in_exactly_one_place():
    """`approve_test_database` is the only producer of a `TestDatabaseApproval`.

    If a second place could mint one, the set of things that authorise a
    `DROP TABLE ... CASCADE` would be larger than the set anyone reviewed, and
    the capability would be only as good as its least-inspected issuer.
    """
    path = SRC / "persistence" / "postgres.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    constructors = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "TestDatabaseApproval"
    ]
    assert len(constructors) == 1, (
        f"TestDatabaseApproval is constructed {len(constructors)} times "
        f"(lines {constructors}); exactly one issuer is the guarantee"
    )
