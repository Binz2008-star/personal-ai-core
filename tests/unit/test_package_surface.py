"""A package's `__all__` is a claim about its surface. Nothing checked it.

Phase 4 added three implementation classes -- `HybridContextAssembler`,
`SimpleMemoryRetriever`, `MemoryRetrievalFailure` -- and never added any of
them to the `__init__.py` of the package they live in. Every call site
reached past the package into the module instead, `conversation/factory.py`
included: the composition root imported two names from `..context` and then
went around it for the third, in consecutive lines.

The cost is not stylistic. `ContextBuilder` requires the hybrid assembler,
and the one the package offered was the greedy one -- so the wrong assembler
was the natural thing to reach for, and
`tests/integration/test_grounded_conversation.py` duly wired it. That test
passed only because its retriever raised before the assembler was reached;
had it completed a turn it would have died on `assemble() got an unexpected
keyword argument 'memories'`. PR #10 fixed the call site. This fixes what
made the call site look right.

Same shape as `test_ci_workflow.py` and the contract-purity proof: something
written down in one place, with nothing anywhere that would notice when it
stopped being true.

Parsed with `ast` rather than by importing, so a package that fails to
import fails loudly here rather than taking the guard down with it.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "personal_ai_core"

# Packages that declare a surface. A package with no `__all__` is making no
# claim, so there is nothing here to check.
PACKAGES = ("context", "knowledge", "memory")

# A public class the package deliberately does not re-export, with the
# reason. Empty, and that is the point: every exclusion has to be argued
# for in this file rather than happening by omission in an __init__.
SURFACE_EXEMPT: dict[str, str] = {}


def _exported(package: str) -> set[str]:
    tree = ast.parse((SRC / package / "__init__.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "__all__" for t in node.targets
        ):
            return {
                e.value
                for e in node.value.elts  # type: ignore[attr-defined]
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            }
    pytest.fail(f"{package}/__init__.py declares no __all__")


def _public_classes(package: str) -> dict[str, str]:
    """Top-level, non-underscore classes in the package's own modules."""
    found: dict[str, str] = {}
    for path in sorted((SRC / package).glob("*.py")):
        if path.name == "__init__.py":
            continue
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                found[node.name] = path.name
    return found


@pytest.mark.parametrize("package", PACKAGES)
def test_every_public_class_is_on_the_package_surface(package):
    """The failure this file exists to prevent."""
    exported = _exported(package)
    missing = {
        name: module
        for name, module in _public_classes(package).items()
        if name not in exported and name not in SURFACE_EXEMPT
    }
    assert not missing, (
        f"{package}/ defines public classes its __init__ does not export: "
        f"{missing}. Callers then reach past the package into the module, and "
        "whatever the package *does* offer becomes the obvious thing to use "
        "instead -- which is how the wrong context assembler got wired."
    )


@pytest.mark.parametrize("package", PACKAGES)
def test_everything_exported_actually_exists(package):
    """The other direction: `__all__` must not name what was moved away.

    A stale entry is an `ImportError` on `from package import *`, and a
    silent lie to anyone reading the file for the surface.
    """
    tree = ast.parse((SRC / package / "__init__.py").read_text(encoding="utf-8"))
    imported = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    phantom = sorted(_exported(package) - imported)
    assert not phantom, f"{package}/__init__.py exports names it never imports: {phantom}"


def test_the_exemptions_are_real():
    """An exemption for a class that no longer exists hides the next one."""
    everywhere = {n for p in PACKAGES for n in _public_classes(p)}
    stale = sorted(n for n in SURFACE_EXEMPT if n not in everywhere)
    assert not stale, f"SURFACE_EXEMPT names classes that do not exist: {stale}"
