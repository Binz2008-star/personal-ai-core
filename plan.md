# Local UI Prototype Plan

## Status
Experimental integration branch. This prototype now includes a localhost-only HTTP adapter, application-owned document storage, retrieval integration, and the static UI. It is not a merge candidate for main until its security/lifecycle gates and current-main integration review pass.

## Scope
Build the first local web UI for non-technical individuals without bypassing existing application services. The browser talks only to the local HTTP adapter; it never talks directly to Ollama, SQLite, or arbitrary filesystem paths.

Current prototype scope:
- static HTML/CSS/JavaScript UI with no Node, framework, or bundler;
- HTTP server bound to 127.0.0.1 only;
- exact Host, Origin, and per-process CSRF checks;
- app-owned .md/.txt document uploads and optional PDF parsing;
- durable document metadata with derived in-memory retrieval indexes;
- explicit safe ingestion states and retry of failed indexing;
- evidence/search responses that do not expose local paths.

## Design
- **Movement:** calm local-first workspace, closer to a focused writing tool than an admin dashboard.
- **Principles:** quiet by default, evidence is always discoverable, safe states are explicit, Arabic-first copy with technical details on demand.
- **Layout:** sessions rail, conversation canvas, contextual evidence drawer.
- **Interaction:** progressive disclosure; Basic mode hides implementation details, evidence opens on demand.

## Security and lifecycle constraints
- Uploaded filenames are display labels only; path-like names are rejected.
- Uploaded bytes use server-generated names under the app-owned directory.
- Failed indexing is derived-state failure: durable user bytes are retained and marked failed.
- Uploaded files are never served as static content.
- PDF parsing is optional. Current parser tests prove parsing behavior only; bounded time/memory requires a stoppable execution boundary and remains deferred.
- Document deletion remains deferred until evidence/message/backup semantics are decided.
- No filesystem path, secret, stack trace, or raw parser exception is exposed to the Basic UI.

## Deferred
Conversation/session HTTP integration, active memory, agent execution/confirmation UI, document deletion/retention semantics, OCR, semantic retrieval, LAN/phone access, multi-user/accounts, and installer/launcher work.
