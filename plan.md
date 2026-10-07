# Local UI Prototype Plan

## Scope
Build the local web UI as an experimental adapter on this branch. The prototype
now includes a static browser UI, a localhost-only HTTP API, application-owned
document storage, and retrieval integration. It does not call Ollama or SQLite
directly and is not a public or remote API.

This branch remains experimental until its contracts, security tests, and
integration with current `main` are reviewed.

## Design
- **Movement:** calm local-first workspace, closer to a focused writing tool than an admin dashboard.
- **Principles:** quiet by default, evidence is always discoverable, safe states are explicit, Arabic-first copy with clear technical details on demand.
- **Palette:** warm paper background, deep ink, muted teal as the product signal, amber for readiness warnings; no decorative imagery.
- **Layout:** three-zone desktop workspace: sessions rail, conversation canvas, contextual evidence drawer.
- **Signature elements:** local status pill, source chips, evidence drawer with a visible retrieval state.
- **Interaction:** progressive disclosure; Basic mode hides implementation details, evidence opens on demand.
- **Animation:** short fades and drawer transitions only; respect reduced-motion preferences.

## Current structure
- `src/personal_ai_core/ui/static/index.html`: semantic UI shell.
- `src/personal_ai_core/ui/static/styles.css`: responsive visual system.
- `src/personal_ai_core/ui/static/app.js`: browser interactions.
- `src/personal_ai_core/ui/server.py`: localhost-only HTTP adapter.
- `src/personal_ai_core/ui/documents.py`: application-owned upload storage and extraction.

## Current boundaries
- Bind only to `127.0.0.1`.
- Exact Host validation and Origin + per-process CSRF protection for state changes.
- Uploaded bytes use server-generated storage names and are never served as static files.
- Browser-provided filenames are display labels only; path-like labels are rejected.
- Indexing is derived state: an indexing failure must not delete durable uploaded bytes.
- Document deletion remains deferred; the HTTP API does not expose deletion semantics.
- PDF support is optional. Current in-process extraction is prototype-only and does
  not yet satisfy the ADR's bounded time/memory requirement.

## Constraints
- No Node, framework, or bundler.
- Core runtime dependencies remain unchanged; PDF support is an optional extra.
- Do not expose filesystem paths, secrets, raw parser errors, or stack traces.
- Do not merge this branch until it is updated onto current `main` and passes the
  full repository test/static-analysis gates.
