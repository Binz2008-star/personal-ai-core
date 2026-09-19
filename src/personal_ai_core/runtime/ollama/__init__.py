"""Ollama adapter.

The only place in the Core permitted to know that Ollama exists
(ARCHITECTURE.md §4). Ollama is an external runtime dependency: it is reached
over HTTP and is never embedded in the domain.
"""
