"""`python -m personal_ai_core.app`.

Not `python -m personal_ai_core`: that needs a `__main__.py` at the package
root, and `tests/unit/test_dependency_direction.py` checks every module there
against LAYER_MAY_IMPORT, where a root-level file belongs to no layer. The
guard is right and the module name is the thing that should move.

`pyproject.toml` also installs this as the `pac` command, which is the form
most people will use.
"""
import sys

from .cli import main

sys.exit(main())
