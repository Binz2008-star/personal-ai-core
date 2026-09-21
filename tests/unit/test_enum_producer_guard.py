"""Regression guard: every declared member of RetrievalMethod and ExclusionReason
must have at least one real producer in src/.

A "producer" is an attribute access `EnumClass.MEMBER` found inside src/ Python
source files. Enum declarations, test fixtures, provenance construction helpers,
and references that exist only in tests/ do NOT count.

This prevents dead enum members from accumulating silently — a member that
cannot be produced by any live code path is either premature Phase 2 design
that should be removed, or it needs its producer implemented before it is
declared.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

# ── targets ───────────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).parents[2]
_SRC_ROOT = _REPO_ROOT / "src"

_GUARDED_ENUMS = {
    "RetrievalMethod": ("personal_ai_core.core.knowledge", "RetrievalMethod"),
    "ExclusionReason": ("personal_ai_core.core.context", "ExclusionReason"),
}

# ── helpers ───────────────────────────────────────────────────────────────────


def _declared_members(module_path: str, class_name: str) -> set[str]:
    """Return the set of member names declared in the given Enum class."""
    parts = module_path.split(".")
    candidate = _SRC_ROOT.joinpath(*parts).with_suffix(".py")
    source = candidate.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            members: set[str] = set()
            for item in node.body:
                if isinstance(item, ast.Assign):
                    for target in item.targets:
                        if isinstance(target, ast.Name):
                            members.add(target.id)
            return members
    raise AssertionError(f"Class {class_name!r} not found in {candidate}")


def _src_py_files() -> list[Path]:
    """All .py files under src/, excluding __pycache__."""
    return [
        p
        for p in _SRC_ROOT.rglob("*.py")
        if "__pycache__" not in p.parts
    ]


def _attribute_accesses_in_file(path: Path) -> set[tuple[str, str]]:
    """Return set of (Name, attr) pairs for all `Name.attr` accesses in a file."""
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError):
        return set()

    pairs: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
        ):
            pairs.add((node.value.id, node.attr))
    return pairs


# ── the guard ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "class_name,module_path",
    [
        ("RetrievalMethod", "personal_ai_core.core.knowledge"),
        ("ExclusionReason", "personal_ai_core.core.context"),
    ],
)
def test_every_declared_member_has_a_src_producer(
    class_name: str, module_path: str
) -> None:
    """No declared enum member may lack at least one attribute access in src/."""
    declared = _declared_members(module_path, class_name)
    assert declared, f"No members found for {class_name} — check module path"

    # Scan only files OTHER than the defining module so that self-references
    # inside the defining file (e.g. default field values that name members)
    # do not count as producers.
    defining_file_relative = Path(*module_path.split(".")).with_suffix(".py")
    defining_path = _SRC_ROOT / defining_file_relative

    produced_outside_definition: set[str] = set()
    for path in _src_py_files():
        if path.resolve() == defining_path.resolve():
            continue
        for owner, attr in _attribute_accesses_in_file(path):
            if owner == class_name:
                produced_outside_definition.add(attr)

    dead = declared - produced_outside_definition
    assert not dead, (
        f"{class_name}: declared member(s) with no producer in src/:\n"
        + "\n".join(f"  {class_name}.{m}" for m in sorted(dead))
        + "\n\nEither implement the producer or drop the member."
    )
