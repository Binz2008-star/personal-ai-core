# Local UI Prototype Plan

## Scope
Build a static, dependency-free prototype for the accepted local web UI ADR. This pass is visual and interaction-only: no HTTP server, Ollama calls, SQLite access, or real file uploads.

## Design
- **Movement:** calm local-first workspace, closer to a focused writing tool than an admin dashboard.
- **Principles:** quiet by default, evidence is always discoverable, safe states are explicit, Arabic-first copy with clear technical details on demand.
- **Palette:** warm paper background, deep ink, muted teal as the product signal, amber for readiness warnings; no decorative imagery.
- **Layout:** three-zone desktop workspace: sessions rail, conversation canvas, contextual evidence drawer.
- **Signature elements:** local status pill, source chips, evidence drawer with a visible retrieval state.
- **Interaction:** progressive disclosure; Basic mode hides implementation details, evidence opens on demand.
- **Animation:** short fades and drawer transitions only; respect reduced-motion preferences.

## Structure
- `src/personal_ai_core/ui/static/index.html`: semantic shell and three screens/states.
- `src/personal_ai_core/ui/static/styles.css`: responsive visual system.
- `src/personal_ai_core/ui/static/app.js`: demo state, navigation, evidence drawer, readiness state, and no-network interactions.

## Constraints
- No Node, framework, bundler, or runtime dependency.
- Demo data is clearly marked and must be replaced by API adapters later.
- Do not expose filesystem paths or secrets in the UI.
