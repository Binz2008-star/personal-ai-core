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
    # ADR-017 §3.2 added eleven `FeedbackOutcome` members and, at the same
    # time, claimed an effect table existed in src/ so the guard would see a
    # producer for each. Neither was true: the table was markdown, and this
    # set did not contain the enum, so all eleven read as dead. Both are now
    # real -- `learning/outcomes.py` resolves every member to an effect code --
    # and the enum is guarded so it cannot rot the same way again.
    "FeedbackOutcome": ("personal_ai_core.core.feedback", "FeedbackOutcome"),
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
        (class_name, module_path)
        for class_name, (module_path, _qualname) in _GUARDED_ENUMS.items()
    ],
)
def test_every_declared_member_has_a_src_producer(
    class_name: str, module_path: str
) -> None:
    """No declared enum member may lack at least one attribute access in src/.

    The parameter list is derived from `_GUARDED_ENUMS` rather than restated
    beside it. It used to be a separate literal, which meant the registry was
    decorative: adding an enum to it armed nothing, and the only way to extend
    the guard was to edit a different list further down the file. That is not
    a hypothetical -- it is how `FeedbackOutcome` arrived with eleven members
    and no producer while this file's own docstring promised to prevent
    exactly that. One source of truth, or the registry is a lie.
    """
    module_path, qualname = _GUARDED_ENUMS[class_name]
    assert qualname == class_name, (
        f"_GUARDED_ENUMS[{class_name!r}] records the qualified name "
        f"{qualname!r}; they must agree or the registry is describing a class "
        "this test does not read"
    )
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


def test_feedback_recorded_has_exactly_one_producer_outside_its_definition():
    """ADR-017 §3.1 / §12 test 9: FEEDBACK_RECORDED has one and only one
    producer, and it is the durable-representation mapping.

    `EventType.FEEDBACK_RECORDED` must appear in exactly one src module
    outside `core/domain.py`: `core/feedback.py`'s `as_feedback_event`. The
    repositories therefore filter feedback events through the shared
    `FEEDBACK_EVENT_TYPE` constant rather than naming the member themselves,
    and the conversation path cannot emit a feedback event because it never
    names the member at all.
    """
    declared = _declared_members(
        "personal_ai_core.core.domain", "EventType"
    )
    assert "FEEDBACK_RECORDED" in declared

    _PKG = _SRC_ROOT / "personal_ai_core"
    defining_file = _PKG / "core" / "domain.py"
    producers: set[Path] = set()
    for path in _src_py_files():
        if path.resolve() == defining_file.resolve():
            continue
        for owner, attr in _attribute_accesses_in_file(path):
            if owner == "EventType" and attr == "FEEDBACK_RECORDED":
                producers.add(path)

    expected = {(_PKG / "core" / "feedback.py").resolve()}
    assert producers == expected, (
        "EventType.FEEDBACK_RECORDED must be referenced only by "
        "core/feedback.py (as_feedback_event). Producers found: "
        + ", ".join(str(p.relative_to(_PKG)) for p in sorted(producers))
    )
