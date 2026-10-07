# Local UI Prototype Plan

## Scope
Build the local, dependency-light web UI described by the proposed Local UI ADR.
The branch now includes the static prototype plus an internal localhost HTTP adapter
for document upload, retrieval, readiness, and evidence-oriented interactions.
It does not call Ollama or SQLite directly and is not a public or remote API.

## Design
- **Movement:** calm local-first workspace, closer to a focused writing tool than an admin dashboard.
- **Principles:** quiet by default, evidence is always discoverable, safe states are explicit, Arabic-first copy with clear technical details on demand.
- **Palette:** warm paper background, deep ink, muted teal as the product signal, amber for readiness warnings; no decorative imagery.
- **Layout:** three-zone desktop workspace: sessions rail, conversation canvas, contextual evidence drawer.
- **Signature elements:** local status pill, source chips, evidence drawer with a visible retrieval state.
- **Interaction:** progressive disclosure; Basic mode hides implementation details, evidence opens on demand.
- **Animation:** short fades and drawer transitions only; respect reduced-motion preferences.

## Current prototype
- Static HTML/CSS/JS is shipped inside the Python package; no Node, bundler, or framework.
- The HTTP server binds to 127.0.0.1 and enforces exact Host, Origin, and per-process CSRF checks.
- Uploaded bytes are stored under an application-owned directory with server-generated names.
- Markdown/TXT extraction is dependency-free; PDF parsing is an optional extra.
- Indexing is derived state. An indexing or rebuild failure must never delete durable user bytes.
- Document deletion remains deferred until evidence, message, retention, and backup semantics are specified.
- PDF parsing tests cover parsing behaviour only; bounded CPU/time/memory requires a separately designed stoppable execution boundary.

## Structure
- `src/personal_ai_core/ui/static/`: semantic UI shell and browser modules.
- `src/personal_ai_core/ui/documents.py`: application-owned document storage and extraction.
- `src/personal_ai_core/ui/server.py`: localhost-only internal HTTP adapter.
- `tests/unit/test_ui_documents.py`: storage, manifest, quota, and lifecycle contracts.
- `tests/integration/test_ui_server.py`: network, request-protection, upload, and lifecycle contracts.

## Constraints
- No Node, framework, or bundler.
- The UI never receives arbitrary filesystem paths or serves uploaded files as static content.
- The HTTP layer must not expose raw exceptions, secrets, stack traces, or local paths.
- PDF resource bounding is not claimed until a stoppable extraction boundary exists.
- This remains an experimental branch and is not a merge candidate until it is updated onto current main and passes the full repository gates.
