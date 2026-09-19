# Integration tests

More than one component, wired together the way the application wires them.
A unit test proves a part behaves; these prove the parts compose — which is
where contract mismatches actually show up.

Two rules:

- **Nothing here may require infrastructure that is absent by default.** The
  in-memory stack needs no service, no model download and no network, so it
  runs anywhere. A test that genuinely needs an absent dependency — a live
  Ollama, Postgres, pgvector, Neon, or a reference tokenizer — may skip, and
  its skip reason must say plainly what is left unverified as a result. A skip
  that reads like a pass is worse than a missing test.
- **Tests in `unit/` and `characterization/` never skip for a missing
  dependency.** They have no dependencies to miss. The only skips permitted
  there are the two structural exemptions in the layering check — the
  composition root, and `config.py` as the provider boundary — which are
  decisions about what the rule does not apply to, not unrun tests.
