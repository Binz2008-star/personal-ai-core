# Contract evaluation (ADR-013)

This harness checks the Boss model against the **written identity contract** (ADR-012). It does not score quality. There is no judge model.

It sends every prompt through the same builders that `pac --ephemeral` and `pac --ephemeral --documents` use. It does not open a database, and it runs with an empty profile.

## Run it on the rig

From the `personal-ai-core` checkout, with Ollama running:

```bat
:: 1. the context window the model really has (recorded in the results, not sent)
ollama show huihui_ai/qwen2.5-abliterate:7b --parameters

:: 2. the run; pass the num_ctx you saw above, or leave --num-ctx out if none is listed
set PYTHONPATH=src
python -m personal_ai_core.app.evaluate --num-ctx 4096

:: one case only
python -m personal_ai_core.app.evaluate --only injection-en
```

It writes two files to `evals/results/`:

- `raw-<time>.json`: every prompt, the reply exactly as the model gave it, and the turn's events (including `prompt_tokens` and `finish_reason`).
- `scored-<time>.json`: the verdicts. The file names the raw file it came from.

Both files record:

- the commit;
- the model;
- the context window Core assumes;
- the `num_ctx` you measured;
- the machine (no hostname).

Commit both files unmodified. A later re-score goes in a new derived file that names its source.

## Verdicts

| Verdict | Meaning |
|---|---|
| PASS | the property held |
| FAIL | the property was violated, and the reply shows it |
| REVIEW | no mechanical check can decide; read the raw reply |
| ERROR | the model could not be reached; not a verdict about the model |

**Read these before trusting a number:**

- **The positive controls (`ground-positive-*`) must PASS first.** If they fail, retrieval did not put the evidence in front of the model, and the decline cases say nothing about rule 4.
- **The injection cases (`injection-*`):**
  - A FAIL is conclusive.
  - A PASS is weak evidence (ADR-013).
- **The dialect check uses a short list of markers.** A PASS means none of those markers appeared. It does not certify the reply as Modern Standard Arabic. Read the Arabic replies yourself.
- **"No padding" is not checked mechanically.** Only emoji are detected. Your reading of the replies is the evidence for the rest, recorded as notes beside the results and not as a score.

## What this does not do

- It gates nothing in CI.
- It never uses a model other than the Boss model: the run is refused if `PAC_BOSS_MODEL` names a different one.
- It does not send `num_ctx`.
- It changes no behaviour of Core.
