"""The entry point layer.

One level above the composition root: it decides where the database lives and
which slice to build, then hands over. It imports `conversation` (for the
factory) and `core` (for settings and errors), and no adapter directly --
knowing which concrete class satisfies which contract is factory.py's job, and
duplicating that knowledge here would give the system two composition roots.
"""
from .cli import main

__all__ = ["main"]
