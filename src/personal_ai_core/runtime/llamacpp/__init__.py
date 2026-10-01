"""llama.cpp adapter (experiment, evaluation runs only).

`llama-server` runs the same GGUF file Ollama serves, and unlike Ollama it
accepts a GBNF grammar per request (Ollama PR #2404 is unmerged). That makes
it possible to forbid a script during decoding rather than check for it
after. Reached over HTTP, like Ollama; never embedded in the domain. Nothing
in `pac` selects it: only the evaluation harness does.
"""
from .grammar import GRAMMARS, NO_FOREIGN_SCRIPT
from .provider import LlamaCppProvider

__all__ = ["GRAMMARS", "LlamaCppProvider", "NO_FOREIGN_SCRIPT"]
