# Integration tests

More than one component, wired together the way the application wires them.
A unit test proves a part behaves; these prove the parts compose — which is
where contract mismatches actually show up.

Two rules:

- **No test here may require infrastructure that is absent by default.** The
  in-memory stack needs no service, no model download and no network, so the
  whole file runs anywhere. A test that genuinely needs a live Ollama,
  Postgres, pgvector or Neon may skip when its dependency is missing, and must
  say so in its skip reason.
- **Tests in `unit/` and `characterization/` never skip.**
