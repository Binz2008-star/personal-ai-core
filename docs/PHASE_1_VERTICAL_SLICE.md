# Phase 1 — Core Foundation Vertical Slice

**Status: implemented and executable. Storage is in-process; this is a slice, not a product.**

```text
User → Session → Message → ModelProvider → OllamaProvider → Boss model → Response → Event
```

---

## What exists

| Layer | Module | Holds |
|---|---|---|
| Domain | `core/domain.py` | `User`, `Session`, `Message`, `Event`, `ModelResponse`, enums |
| Domain | `core/contracts.py` | `ModelProvider`, repository protocols, `MemoryStore` |
| Domain | `core/config.py` | `Settings`, Boss model default |
| Domain | `core/errors.py` | error hierarchy |
| Runtime | `runtime/model_registry.py` | `ModelSpec`, `ModelRegistry` |
| Runtime | `runtime/ollama/provider.py` | `OllamaProvider`, injectable transport |
| Application | `conversation/service.py` | `ConversationService` |
| Application | `conversation/events.py` | `EventRecorder` |
| Application | `conversation/factory.py` | composition root |
| Infrastructure | `persistence/in_memory.py` | repositories, `SealedMemoryStore` |

## Dependency direction

```text
persistence, runtime/ollama  ──implements──▶  core.contracts  ◀──depends on──  conversation
```

Enforced by tests, not convention — `tests/test_dependency_direction.py` fails the build if
the domain imports infrastructure, if Ollama detail appears outside `runtime/ollama/`, or if
a model name is written anywhere but `core/config.py`.

## The Boss model

```text
huihui_ai/qwen2.5-abliterate:7b   context 8192
```

Declared once, in `core/config.py`, and reachable only through
`ModelRegistry → ModelProvider → OllamaProvider`. Overridable with `PAC_BOSS_MODEL`.
Deliberately not `qwen2.5:7b`, which is a `local-llm-rig` benchmark result and not a Core
decision (ADR-002).

## `Event != Memory`

A conversation turn records events and writes no memory. There is no memory subsystem in
Phase 1 and no path to one: `ConversationService` has no memory collaborator to wire.

`SealedMemoryStore` makes this testable rather than aspirational — it counts attempts and
raises `InvariantViolation` on any write. `tests/test_event_not_memory.py` runs a full
multi-turn conversation, including the sentence *"remember that I prefer Arabic"*, and
asserts zero attempted writes.

## Events recorded

`session.started` · `message.received` · `generation.requested` · `generation.completed` ·
`generation.failed` · `session.closed`

Append-only. `EventRepository` exposes no `update` and no `delete`; payloads are frozen at
construction so a caller holding a reference cannot edit a recorded event. A failed turn
records `generation.failed` and leaves the user message in place rather than fabricating a
reply — a failure is evidence too.

## Language

`Message.language` exists from the first commit, defaulting to `und`. This is a Phase 1
field by decision: messages are written now, and adding the column later would require
migrating every stored record.

## Running it

```bash
python -m pytest tests/ -v
```

The slice is exercised end to end with an injected fake transport, so no GPU, no live model
and no network are required. Against a real Ollama:

```python
from personal_ai_core.conversation.factory import build_in_memory_service

service, events = build_in_memory_service()      # real HTTP transport
session = service.start_session(service.create_user().id)
print(service.send(session_id=session.id, content="مرحبا", language="ar").content)
```

Ollama remains an external runtime dependency, reached over HTTP. No model logic lives in
the domain.

## Not in this slice

RAG · embeddings · vector store · BM25 · reranking · memory promotion · learning ·
training/adapters · tools · project connectors · identity · context budget · durable
storage · API · UI.
