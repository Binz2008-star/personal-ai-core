# PRODUCTION GAP ANALYSIS — personal-ai-core @ HEAD 8ce4390 (2026-10-04)

READ-ONLY audit. No source, test, ADR, README, or workflow file was modified to
produce this file. This supersedes the prior version of this file (written at
HEAD 1861756): the tree has since synced to origin/main 2d9b569 (ADR-024,
ADR-025) plus commit 8ce4390 (`feat(agent): add safe edit_file coding loop`).

Scope audited: `docs/ARCHITECTURE.md`, `README.md`, all accepted ADRs,
`src/`, `tests/`, `.github/workflows/tests.yml`.

Classification: **P0** = data loss, security, invariant violation, or
architecture mismatch; **P1** = production reliability blocker;
**P2** = maintainability/operability issue; **P3** = post-v1 improvement.
Each finding states whether remediation is required before v1.0.

Standing notes that condition several findings:

- The committed HEAD still follows `fetch_url` redirects. An UNCOMMITTED
  concurrent hardening in the working tree (`agent/web.py` redirect refusal +
  tests) exists but is not reviewed, not frozen, and not part of any commit.
  Findings distinguish HEAD behavior from working-tree behavior.
- ADR-025 (native tools), ADR-024 unit A (lenient protocol), and ADR-023 unit 2
  (environment context) are off-by-default by design. Commit 8ce4390 turned
  environment context ON for `pac --agent` only. Production `pac` runs the
  text protocol unless a flag is passed.
- Proposed ADRs are not treated as accepted requirements. Owner-authorized
  slices of proposed work are identified as such.

---

## P0-1: Context budget is observed, not enforced, and the estimator is heuristic

1. **Evidence:** `src/personal_ai_core/core/context.py:108-116`
   (`ContextAllocation.overcommitted`, "the caller decides");
   `src/personal_ai_core/conversation/service.py:160-163` (prompt always sent
   whole: `[identity, grounding?, *history]`, no truncation branch);
   `src/personal_ai_core/conversation/grounding.py:600` (flag only serialized
   into `CONTEXT_ASSEMBLED`); `src/personal_ai_core/context/token_estimator.py:98-128`
   (per-character script costs, no tokenizer); `within_budget`
   (`core/context.py:151,218`) has zero readers in `src/`.
2. **Current behavior:** `overcommitted` is computed and recorded but never
   gated on. Evidence items are skipped over budget
   (`context/assembler.py:73,208,236`) and `num_predict` is capped
   (`conversation/service.py:315-334`), but nothing stops
   identity+history+grounding exceeding the 8192 window.
3. **Production risk:** Long/atypical prompts are undercounted and overflow
   the model window; the provider truncates server-side silently or fails.
   Violates the accepted ADR-005 tokenizer-backed counting and
   no-silent-overflow consequences.
4. **Minimal remediation:** Gate `send()` on `overcommitted` (controlled
   failure, not silent send) and put a tokenizer behind the existing
   `TokenEstimator` contract. If no tokenizer is available in the
   deployment, revisit the ADR rather than claiming exact counts.
5. **Tests required:** Unit test: `overcommitted == True` yields a controlled
   failure with no provider call. Integration test on the default path.
6. **ADR impact:** Direct conflict with ADR-005 as accepted.
7. **Implementation required before v1.0:** **YES** (P0)

## P0-2: Default `pac` path sends unmeasured history with no allocation event

1. **Evidence:** `src/personal_ai_core/conversation/service.py:374-375`
   (`_ground()` returns `None` when `context_builder is None`);
   `src/personal_ai_core/conversation/service.py:322-334` (ungrounded
   `_generation_limit` uses `history_tokens=0`, a known false value);
   `src/personal_ai_core/conversation/service.py:165-174`
   (`GENERATION_REQUESTED` records message count but neither history size nor
   identity/profile size; grounded path records all at
   `conversation/grounding.py:594-600`).
2. **Current behavior:** Plain `pac` (no `--documents`) emits no
   `ContextAllocation` and no `CONTEXT_ASSEMBLED`. History is full
   `list_for_session` (`service.py:138`) with no `LIMIT`
   (`persistence/sqlite.py:339-355`), never windowed.
3. **Production risk:** Default invocation grows prompt, latency, and store
   linearly per turn with zero on-disk evidence of size vs window.
   Architecture mismatch with ARCHITECTURE section 6. Overflow is
   unreconstructable.
4. **Minimal remediation:** In `_ground()`, build an allocation from measured
   history even when `context_builder is None`, and emit
   `CONTEXT_ASSEMBLED` on that path. No new modules or dependencies.
5. **Tests required:** Integration test: default path records
   `CONTEXT_ASSEMBLED` with allocation derived from actual history length.
6. **ADR impact:** Applies ADR-005 to the default path; no ADR change.
7. **Implementation required before v1.0:** **YES** (P0)

## P0-3: `fetch_url` SSRF — scheme-only check; HEAD follows redirects

1. **Evidence:** `src/personal_ai_core/agent/web.py:251-258`
   (`FetchUrl.run` checks only scheme); `src/personal_ai_core/agent/web.py:41-45`
   (`urllib_fetch` plain `urlopen`, follows 301/302/303/307/308). No
   `ipaddress`/private-range/DNS check anywhere in `src/`.
2. **Current behavior (committed HEAD):** an ASK-approved URL such as
   `https://short.example/x` can 302 to `http://169.254.169.254/...`,
   `127.0.0.1`, or RFC1918 and is followed silently; `final` is displayed
   only after the fact. Direct fetches of loopback/private/metadata URLs
   pass the gate. (Working tree has an uncommitted redirect refusal; even
   it does not block direct private-URL fetches.)
3. **Production risk:** SSRF to cloud metadata, intranet, and Ollama on
   loopback; metadata exfiltrated into tool output then model context, then
   a second web call exfiltrates (owner sees only the query/URL).
4. **Minimal remediation:** Refuse cross-host redirects (landed in working
   tree, needs review/freeze/commit) AND validate the resolved host against
   loopback/link-local/RFC1918/metadata blocks before fetching. No new deps.
5. **Tests required:** Unit tests: loopback, 169.254.169.254, RFC1918,
   cross-host redirect blocked, approved-host fetch still works.
6. **ADR impact:** None; aligns with the documented threat. A brief ADR note
   only if policy changes.
7. **Implementation required before v1.0:** **YES** (P0)

## P0-4: `run_command` arguments bypass file protections

1. **Evidence:** `src/personal_ai_core/agent/commands.py:50-72,140-165`
   (allowlist plus `_refuse_escaping_path` blocks absolute/`~`/`..`/drive
   paths but never consults `is_protected` or `is_reserved`);
   `src/personal_ai_core/agent/tools.py:382-412` (`RunCommand.run`,
   `cwd=workspace.root`); contrast
   `src/personal_ai_core/agent/sandbox.py:179-184` (file tools refuse).
2. **Current behavior:** `cat .env`, `grep -r password .`,
   `cat .personal-ai-core/core.db` (when workspace is `~`), and
   `git show HEAD:.env` all pass validation while the equivalent file-tool
   call is refused.
3. **Production risk:** Secret leak and live-database exfiltration into
   HIGH-tool output, then web exfiltration. The sandbox boundary does not
   hold for command arguments.
4. **Minimal remediation:** Resolve each non-flag command argument against
   `Workspace.is_reserved()`/`is_protected()` in `validate_command` (or in
   `RunCommand.run`) and reject. Alternatively drop file-reading commands
   from the allowlist. No new deps.
5. **Tests required:** Unit tests: `cat .env`, `grep -r x .env`, relative DB
   path, and `git show HEAD:.env` rejected; ordinary `pytest -q` allowed.
6. **ADR impact:** Closes a hole inside accepted ADR-004; no ADR change.
7. **Implementation required before v1.0:** **YES** (P0)

## P0-5: `shell` denylist env stripping misses secret names — prefer deletion

1. **Evidence:** `src/personal_ai_core/agent/tools.py:418-425`
   (`_SECRET_NAME` denylist) vs `src/personal_ai_core/agent/tools.py:387-394`
   (`RunCommand` builds env from nothing). `env`/`set` output flows to model
   context on the agent path.
2. **Current behavior:** Any owner secret whose name avoids the substrings
   (e.g. `AWS_ACCESS_KEY_ID`, custom names) is inherited by `shell` and
   readable via `env`. Inconsistent with `RunCommand` and with the two other
   secret definitions (`agent/verifier.py:28-46`,
   `context/redaction.py:148`).
3. **Production risk:** Secret leak into model context and onward web calls.
4. **Minimal remediation (prefer deletion):** Build `shell` env from nothing
   exactly like `RunCommand` instead of maintaining a denylist. Verify owned
   tooling still runs.
5. **Tests required:** Unit test: secret-named and oddly-named vars absent
   from `shell_environment()`; allowlisted basics present.
6. **ADR impact:** None; hardens ADR-004/ADR-018 boundary.
7. **Implementation required before v1.0:** **YES** (P0)

## P0-6: Secret detection on outputs is narrow — leaks pass unverified

1. **Evidence:** `src/personal_ai_core/agent/verifier.py:28-46`
   (narrow `SECRET_PATTERNS`); `src/personal_ai_core/agent/verifier.py:62-91`
   (check runs on agent path only); `src/personal_ai_core/app/cli.py:747`
   (`_converse` prints `reply.content` raw, no gate). Redaction applies to
   retrieved evidence only (`conversation/grounding.py:199-211`).
2. **Current behavior:** AWS secret keys, generic bearer tokens, token-only
   URLs, and model-echoed secrets from history/profile/documents pass
   unverified. The chat path has no secret gate at all.
3. **Production risk:** Secret disclosure in replies, terminal, shell
   history, and durable `GENERATION_COMPLETED` events.
4. **Minimal remediation:** Widen patterns to the `redaction.py` set at
   minimum and apply the verifier check to `_converse` replies before print.
   No new deps.
5. **Tests required:** Unit tests for each added shape on both agent and
   chat paths; false-positive check on normal prose.
6. **ADR impact:** Extends ADR-018 coverage; record scope in ADR-018.
7. **Implementation required before v1.0:** **YES** (P0)

## P0-7: `WriteFile` and rollback write non-atomically; rollback aborts partway

1. **Evidence:** `src/personal_ai_core/agent/tools.py:325-326`
   (`mkdir` plus direct `write_text`);
   `src/personal_ai_core/agent/recovery.py:86-98` (rollback `write_bytes`,
   no fsync/replace, no per-file isolation, aborts on first failure,
   created dirs never removed). New `EditFile._atomic_write`
   (`tools.py:435-449`) already shows the correct pattern.
2. **Current behavior:** Crash/power-loss mid-write or mid-rollback leaves
   torn files; a partial rollback reports a truncated `restored` list via
   exception.
3. **Production risk:** Data loss and corruption presented as recovered.
4. **Minimal remediation:** Reuse the `EditFile` atomic-write helper in
   `WriteFile` and in `Checkpoints.rollback`; isolate per-file errors and
   report unrestored files. No new deps.
5. **Tests required:** Unit tests: rollback restores all files when one
   restore fails; interrupted write leaves old or new content, never mixed.
6. **ADR impact:** None; completes the recovery contract
   (AGENT_ARCHITECTURE section 6).
7. **Implementation required before v1.0:** **YES** (P0)

---

## P1-1: `--ephemeral --agent` drops the audit trail silently

1. **Evidence:** `src/personal_ai_core/app/cli.py:589`
   (`database=None` when ephemeral);
   `src/personal_ai_core/conversation/factory.py:784-786`
   (`reserved=()` when `database is None`); `cli.py:639`
   (`where = "nowhere -- --ephemeral was given"` is the only cue).
2. **Current behavior:** Explicitly allowed, no warning. Agent
   `AGENT_STEP`/`AGENT_FINISHED` events go to the in-memory repo and vanish
   at exit. Operator expectation of durability is wrong with no notice.
3. **Production risk:** Lost audit trail; post-incident reconstruction
   impossible for exactly the riskiest (agent) runs.
4. **Minimal remediation (prefer deletion/simplification):** Refuse the
   `--agent --ephemeral` combination in `cli.py` with a clear message
   (exit 2). Two lines.
5. **Tests required:** CLI integration test: the combination exits 2 with an
   explanatory message.
6. **ADR impact:** None.
7. **Implementation required before v1.0:** **YES** (P1)

## P1-2: Checking commands can mutate the workspace with no rollback

1. **Evidence:** `src/personal_ai_core/agent/tools.py:362-412`
   (`RunCommand`); `src/personal_ai_core/agent/commands.py:42-44,68-70`
   (`pytest`/`ruff`/`mypy` admitted; "every run is ASKed first" is the only
   gate). `Checkpoints` sees only file-tool writes.
2. **Current behavior:** `pytest`, `ruff --fix`, `mypy` (arbitrary project
   code) can write/delete files; rollback restores file-tool writes only,
   so a "rolled back" run keeps command side effects.
3. **Production risk:** Data loss surviving rollback; misleading recovery.
4. **Minimal remediation:** Document that rollback covers file tools only,
   and snapshot before/after `run_command` allowlist mutations is out of
   scope; alternatively refuse `--fix` flags in `validate_command`. Prefer
   the documented-boundary fix plus `--fix` refusal (small).
5. **Tests required:** Unit tests: `--fix` variants refused; rollback
   boundary documented in tool description and tested as stated.
6. **ADR impact:** Clarifies ADR-004 recovery scope; no ADR change.
7. **Implementation required before v1.0:** **YES** (P1)

## P1-3: Unbounded history re-read and re-sent every turn

1. **Evidence:** `src/personal_ai_core/conversation/service.py:138`
   (full `list_for_session`); `persistence/sqlite.py:339-355` (no `LIMIT`);
   prompt `[identity, grounding?, *history]` at `service.py:160-163` with no
   windowing; `GENERATION_REQUESTED.message_count` recorded but unmitigated.
2. **Current behavior:** Every turn reads and sends the entire session;
   SQLite read, prompt bytes, model latency, and store size grow linearly
   until silent server-side truncation.
3. **Production risk:** Latency blowup, runaway cost/time, silent truncation
   of the earliest (often identity-adjacent) context.
4. **Minimal remediation:** Cap history sent per turn (recent window) behind
   the existing budget policy; keep full history durable. No new deps.
5. **Tests required:** Integration test: long session sends bounded prompt
   while store retains all turns.
6. **ADR impact:** Applies ADR-005; no ADR change (or a one-paragraph
   amendment if windowing policy needs pinning).
7. **Implementation required before v1.0:** **YES** (P1)

## P1-4: CLI error paths escape as tracebacks; exit codes inconsistent

1. **Evidence:** `src/personal_ai_core/app/cli.py:584`
   (`int()` on env vars, raw `ValueError`); `cli.py:641`
   (`mkdir` `OSError` before any `try`); `cli.py:642-647,473-535`
   (SQLite `SchemaVersionMismatch`/`IntegrityError`/`DatabaseError`
   propagate; `try/finally` at `652/719` only closes);
   `src/personal_ai_core/conversation/service.py:385-392`
   (`RETRIEVAL_FAILED` re-raise), `service.py:190-201,292-306` (provider
   errors after partial persist).
2. **Current behavior:** Bad env values, unreadable DB, locked DB, and
   mid-turn failures print tracebacks (exit 1, indistinguishable from
   handled provider errors) or leave half-persisted turns.
3. **Production risk:** Operator cannot distinguish usage errors from
   crashes in automation; partial state on failure.
4. **Minimal remediation:** Validate env ints with ranges at startup (exit
   2 with message); map SQLite/DB errors to `print + return 1` sentences.
   No new deps.
5. **Tests required:** CLI tests: bad `PAC_BOSS_CONTEXT_WINDOW`, locked and
   corrupt DB each yield a one-line message and nonzero exit, no traceback.
6. **ADR impact:** None.
7. **Implementation required before v1.0:** **YES** (P1)

## P1-5: SQLite locked/corrupt database has no operator path

1. **Evidence:** `src/personal_ai_core/persistence/sqlite.py:196`
   (`connect` with stdlib default 5s timeout, no busy message);
   `sqlite.py:210-222,236-237` (duplicate feedback keys make the DB
   unopenable until manual dedup via `audit_feedback_rows:159-183`).
2. **Current behavior:** A second concurrent `pac` gets
   `OperationalError: database is locked` as a traceback; a DB with
   duplicate feedback keys refuses to open with only a remediation message.
3. **Production risk:** Availability loss and denial-of-service on upgrade;
   backup guidance missing (WAL sidecars must be copied together).
4. **Minimal remediation:** "Another pac is running" message on lock
   timeout; document backup (checkpoint or copy all three files) and the
   feedback-dedup procedure. No new deps.
5. **Tests required:** Integration tests: locked DB yields the operator
   message; corrupt DB yields a message naming backup/recovery, no
   traceback.
6. **ADR impact:** None (ADR-010 D+B operator guidance).
7. **Implementation required before v1.0:** **YES** (P1)

## P1-6: Cold start looks like a hang; failure message suggests the wrong fix

1. **Evidence:** `src/personal_ai_core/app/cli.py:671-689` (banner then
   silence); `src/personal_ai_core/runtime/ollama/provider.py:39-48`
   (120s timeout, no warmup/progress); guard retry doubles worst case to
   240s (`conversation/service.py:203-215,292-295`); failure text at
   `cli.py:742-746` says the server is not running.
2. **Current behavior:** First turn after model unload blocks with no
   progress indication; on timeout the message suggests checking the
   server, which is running — the model was loading.
3. **Production risk:** Operators misdiagnose, restart infrastructure, or
   kill healthy loads; agent loop worst case is 12 x 120s per task
   (`agent/loop.py:424`) with only `on_step` progress.
4. **Minimal remediation:** Print a "loading model, first turn may take
   minutes" notice before the first turn; fix the failure message to name
   model loading as a cause. No new deps.
5. **Tests required:** CLI test: cold-start notice printed; timeout message
   mentions loading.
6. **ADR impact:** None.
7. **Implementation required before v1.0:** **YES** (P1)

## P1-7: Provider has no retry/backoff; timeouts are unvalidated

1. **Evidence:** `src/personal_ai_core/runtime/ollama/provider.py:22-33`
   (single attempt mapped to `ProviderError`);
   `runtime/ollama/status.py:65-71` (`http_probe` unwrapped; safe only via
   callers swallowing `Exception` at `status.py:86-89`);
   `provider.py:39-48`, `core/config.py:64-70` (zero/negative timeouts pass
   through with undefined semantics).
2. **Current behavior:** One slow call fails the turn; timeout config
   accepts nonsense values.
3. **Production risk:** Flaky turns on loaded rigs; misconfiguration
   silently accepted.
4. **Minimal remediation:** Validate timeout range at startup; add one
   bounded retry with backoff for transient transport errors only (not for
   model errors). No new deps.
5. **Tests required:** Unit tests: bad timeout rejected at config load;
   transient failure retried once, model error not retried.
6. **ADR impact:** None.
7. **Implementation required before v1.0:** **YES** (P1)

## P1-8: Failure events persist raw exception text durably

1. **Evidence:** `src/personal_ai_core/conversation/service.py:195-200,297-302`
   (`GENERATION_FAILED` stores `str(exc)`);
   `service.py:386-391` (`RETRIEVAL_FAILED` stores `str(exc)` plus type).
   Agent path deliberately avoids raw text (`agent/loop.py:693-695`).
2. **Current behavior:** Host/port/path/credential-shaped strings from
   transport and retriever errors land in durable event rows.
3. **Production risk:** Credential-adjacent data at rest, unencrypted, with
   default umask (`cli.py:359-364` file creation, DB companions).
4. **Minimal remediation:** Store exception type plus a fixed message, not
   `str(exc)`; route failure text through the secret check before persist.
   No new deps.
5. **Tests required:** Unit tests: failure payloads contain no host/path
   from the raised error.
6. **ADR impact:** Extends ADR-018 to failure paths; record in ADR-018.
7. **Implementation required before v1.0:** **YES** (P1)

## P1-9: Windows CWD-first executable resolution allows workspace shadowing

1. **Evidence:** `src/personal_ai_core/agent/tools.py:396-398`
   (bare name to `Popen` with `cwd=workspace`);
   `src/personal_ai_core/agent/commands.py` (no absolute-path resolution).
2. **Current behavior:** On Windows, `CreateProcess` searches CWD before
   PATH, so a workspace file (e.g. `ruff.exe`) can shadow the system binary
   the owner just approved by name.
3. **Production risk:** Path hijack — approved program name runs attacker's
   binary.
4. **Minimal remediation:** On win32, resolve the allowlisted executable
   via `shutil.which()` and refuse if it resolves inside the workspace.
   No new deps.
5. **Tests required:** Unit test: workspace containing `ruff.exe` is
   rejected or bypassed for the system binary.
6. **ADR impact:** None; hardens ADR-004.
7. **Implementation required before v1.0:** **YES** (P1)

## P1-10: Checking commands run arbitrary project code behind a tame description

1. **Evidence:** `src/personal_ai_core/agent/commands.py:42-44,68-70`
   (`pytest`/`ruff`/`mypy` admitted; ASK is the only gate).
2. **Current behavior:** `pytest` executes the workspace's own test code,
   which may be attacker-controlled; the allowlist reads as read-only
   while the gate is one keystroke.
3. **Production risk:** Code execution beyond what the allowlist implies.
4. **Minimal remediation:** Rename the tool description to state it runs
   project code; keep ASK. Documentation-level fix plus a test pinning the
   description. No new deps.
5. **Tests required:** Unit test pinning the honest description text.
6. **ADR impact:** None; clarifies ADR-004 scope (D-C covers the control
   layer, not model-driven behavior).
7. **Implementation required before v1.0:** **YES** (P1)

---

## P2-1: README agent table misstates `web_search` risk; banner omits asks

1. **Evidence:** `README.md:80-88` (`web_search | medium`) vs
   `src/personal_ai_core/agent/web.py:189-207` (`RiskLevel.HIGH`, ASK);
   `src/personal_ai_core/app/cli.py:150-158,702-705` (help/banner omit
   shell/fetch/search asks).
2. **Current behavior:** Operators expect unattended search; live runs
   prompt per query; scripts stall.
3. **Production risk:** Operational surprise only.
4. **Minimal remediation:** Fix the table row to HIGH/ASK and list all
   ASK tools in help/banner. Docs-only.
5. **Tests required:** Docs-conformance test or review checklist entry.
6. **ADR impact:** None (matches ADR-004 A2 D-A).
7. **Implementation required before v1.0:** No (P2, docs fix with release).

## P2-2: `pyproject.toml` has no upper bounds, empty deps, and no ruff config

1. **Evidence:** `pyproject.toml:9` (`requires-python >=3.11`, no upper);
   `:10` (`dependencies = []` while true today);
   `:13,17` (unbounded `pytest`, `psycopg`); CI pins differ
   (`tests.yml:157`); no `tool.ruff` section.
2. **Current behavior:** Future Python or dependency releases install
   cleanly and break at runtime; lint relies on ruff defaults that drift
   (installed ruff 0.16.8 already flags untouched files).
3. **Production risk:** Non-reproducible installs; lint signal erosion.
4. **Minimal remediation:** Add upper bounds, declare the (empty-then-true)
   dependency list discipline, and pin a `[tool.ruff]` config. No new deps.
5. **Tests required:** CI check that install works on oldest and newest
   supported Python.
6. **ADR impact:** None.
7. **Implementation required before v1.0:** No (P2; bounds before release).

## P2-3: CI never runs claimed Python 3.11; Windows lacks static checks

1. **Evidence:** `.github/workflows/tests.yml:48-49,143-144,199-200`
   (all 3.12); `tests.yml:138-170` (static ubuntu-only);
   `tests.yml:189-215` (suite-windows runs suite but is not a required
   check by design).
2. **Current behavior:** 3.11 breakage reaches main green; Windows-only
   lint/type breakage passes; server-gated suites skip on Windows
   (`persistence/postgres.py`, the largest module, untested there).
3. **Production risk:** Platform-specific defects ship.
4. **Minimal remediation:** Add a 3.11 job, run static on Windows, and make
   suite-windows required (owner decision noted in file). Config-only.
5. **Tests required:** None (CI change); verify green on first run.
6. **ADR impact:** None.
7. **Implementation required before v1.0:** No (P2; required before
   claiming the supported matrix).

## P2-4: `--session` validity is checked lazily after the banner

1. **Evidence:** `src/personal_ai_core/app/cli.py:659-669` (explicit
   deferred-validation comment); `_feedback:482` and `_observations:538`
   pre-check while chat/agent do not.
2. **Current behavior:** Banner claims `session: <id>` for an id that may
   not exist; the operator learns only after typing the first message.
3. **Production risk:** Wasted cold-start waits and lost long messages.
4. **Minimal remediation:** Pre-check `has_session` before the banner on
   chat/agent paths, matching feedback/observations. Small.
5. **Tests required:** CLI test: unknown `--session` exits 2 before banner.
6. **ADR impact:** None.
7. **Implementation required before v1.0:** No (P2).

## P2-5: TOCTOU between sandbox check and file write

1. **Evidence:** `src/personal_ai_core/agent/sandbox.py:189-203`
   (lstat walk then resolve); write at `tools.py:325-326,435-449` after the
   check; `before_mutation` snapshot race at `recovery.py:70-74`.
2. **Current behavior:** A concurrent actor swapping a parent to a symlink
   between check and write escapes the workspace. Single-threaded agent
   makes this low severity.
3. **Production risk:** Low; write-outside-workspace on adversarial
   machines.
4. **Minimal remediation:** Re-resolve and re-check containment inside the
   atomic-write helper. Small.
5. **Tests required:** Unit test with a swapped symlink between check and
   write (injected) is refused.
6. **ADR impact:** None.
7. **Implementation required before v1.0:** No (P2).

## P2-6: Symlink/alias checks are platform-gated; reserved-DB compare is case-sensitive on POSIX semantics

1. **Evidence:** `src/personal_ai_core/agent/sandbox.py:62-65,156-160`
   (Windows alias checks only when `os.name == "nt"`);
   `sandbox.py:128-129` (`normcase` identity on POSIX plus `samefile`
   only when both exist).
2. **Current behavior:** A workspace created on Linux then opened on
   Windows (or via SMB), or a case-variant reserved-DB spelling on a
   case-insensitive mount, bypasses string-level checks.
3. **Production risk:** Low; cross-platform workspace reuse edge.
4. **Minimal remediation:** Run alias checks unconditionally; compare
   reserved paths case-folded as well as by identity. Small.
5. **Tests required:** Unit tests forcing the predicate on Linux and
   case-variant reserved spellings.
6. **ADR impact:** None.
7. **Implementation required before v1.0:** No (P2).

## P2-7: Profile handling: silent absence, non-atomic write, unreadable-file escape

1. **Evidence:** `src/personal_ai_core/app/cli.py:335-348`
   (ephemeral+unnamed yields no profile silently);
   `cli.py:351-365` (`_remember` unguarded `mkdir`/`read`/`write`,
   non-atomic, disk-full truncates);
   `cli.py:375-395` (`_load_profile` catches only `UnicodeDecodeError`).
2. **Current behavior:** No-profile runs print nothing about it; a failed
   `--remember` can corrupt `profile.md`; unreadable profile escapes as
   traceback.
3. **Production risk:** Identity silently missing; profile corruption.
4. **Minimal remediation:** Reuse atomic-write helper for profile writes;
   catch `OSError` with messages; print profile absence uniformly. Small.
5. **Tests required:** CLI tests for each path.
6. **ADR impact:** None (profile is owner-authored identity text, outside
   the memory lifecycle by current boundary).
7. **Implementation required before v1.0:** No (P2).

## P2-8: `WebSearch` has no page-size enforcement; search path still follows redirects

1. **Evidence:** `src/personal_ai_core/agent/web.py:189-228` (fetch reads
   `MAX+1` but `run` never checks length; default `urllib_fetch` follows
   redirects even in the hardened tree).
2. **Current behavior:** Oversize search pages are parsed unbounded;
   search-fetch redirects are followed.
3. **Production risk:** Low (fixed host, parsed output); memory/CPU on
   pathological pages.
4. **Minimal remediation:** Enforce the byte cap in `WebSearch.run`;
   use the non-following fetcher there too. Small.
5. **Tests required:** Unit tests: oversize page refused/truncated;
   redirect not followed on search path.
6. **ADR impact:** None.
7. **Implementation required before v1.0:** No (P2).

---

## P3-1: Skip-audit child timeout is sized for CI, not the rig

1. **Evidence:** `tests/unit/test_expected_skips.py:114-126`
   (`timeout=300` for a whole-`tests/` child run).
2. **Current behavior:** On this rig the suite needs 540s+, so the four
   audit tests error while every real test passes (verified: full unit
   2124 passed / 0 failed excluding the gate; the gate's own logic tests
   pass standalone).
3. **Production risk:** None to production; CI-shape signal only. Do not
   weaken the gate to fit one machine.
4. **Minimal remediation:** None required; optionally raise the child
   window with justification, or split the child run. Prefer leaving as is.
5. **Tests required:** None.
6. **ADR impact:** None.
7. **Implementation required before v1.0:** No (P3; informational).

## P3-2: English-biased retrieval must not claim Arabic parity (ADR-006)

1. **Evidence:** `src/personal_ai_core/knowledge/embedding.py:44`
   (hashing surface overlap); `knowledge/text.py:4` (English-only
   `to_tsvector('english')` noted); no per-language config, no pgvector.
2. **Current behavior:** Retrieval matches surface overlap, not meaning;
   Arabic lexical retrieval unproven by the ADR's own consequence.
3. **Production risk:** None if unclaimed; misleading if marketed otherwise.
4. **Minimal remediation:** Keep the README limitation wording; no code.
5. **Tests required:** None.
6. **ADR impact:** ADR-006 consequence stands as written.
7. **Implementation required before v1.0:** No (P3; claim discipline only).

## P3-3: Running code on proposed designs (ADR-017 units, ADR-021, ADR-024 B/C)

1. **Evidence:** `src/personal_ai_core/learning/*` + `core/observation.py`
   (observation derivation, read-only consumer `pac --observations`);
   `src/personal_ai_core/runtime/llamacpp/grammar.py:12` (eval-only);
   ADR-024 units B/C missing by authorization.
2. **Current behavior:** All safe because read-only/off-by-default/eval-only:
   no turn consults feedback; no `pac` path applies grammar; `EvaluationGate`
   absent so fail-closed mapping unexercised; Postgres feedback repo missing.
3. **Production risk:** None today; risk appears only if a future turn
   consumes these without acceptance.
4. **Minimal remediation:** None; hold the line that turning any on needs
   its ADR's adoption decision.
5. **Tests required:** None.
6. **ADR impact:** ADR-017/021/024 stay proposed as written.
7. **Implementation required before v1.0:** No (P3; governance note).

## P3-4: Postgres backend untested on Windows; MVCC divergences have no Windows signal

1. **Evidence:** `.github/workflows/tests.yml:24-36,201-209` (server
   container ubuntu-only; Windows legs skip);
   `src/personal_ai_core/persistence/sqlite.py:542-546` (engine-serialized
   guarantee noted as MVCC-different).
2. **Current behavior:** Opt-in server slice works on Linux CI; Windows
   server behavior unknown.
3. **Production risk:** None for default SQLite; only for server adopters
   on Windows.
4. **Minimal remediation:** Later: Windows Postgres service leg or
   documented Linux-only server support. Not now.
5. **Tests required:** None now.
6. **ADR impact:** ADR-016 unchanged (SQLite stays default).
7. **Implementation required before v1.0:** No (P3).

## P3-5: Agent `AGENT_FINISHED` payload is not reconstructible without tool args

1. **Evidence:** `src/personal_ai_core/agent/loop.py:696-718`
   (audit stores names/outcomes, not args/answers by repository rule);
   refused replies never in events (`loop.py:128-139`).
2. **Current behavior:** Deliberate (no raw text in payloads); agent runs
   cannot be fully replayed from events alone.
3. **Production risk:** Forensic completeness only; the rule exists to keep
   secrets out of payloads.
4. **Minimal remediation:** None now; any change must preserve the
   no-raw-text rule.
5. **Tests required:** None.
6. **ADR impact:** None.
7. **Implementation required before v1.0:** No (P3; accepted trade-off).

---

## Verified holds (no finding)

- Boss model single-source (`core/config.py:19`), never in business logic;
  evaluation/bench refuse non-Boss models.
- Event != Memory; sole-writer pipeline; sealed store; conversation has no
  memory collaborator.
- Dependency direction; composition-root-only adapter naming; no dead enum
  members; SQLite WAL + transactional supersede + feedback idempotency
  arbitration; Postgres explicit intent/identity gates; `DATABASE_URL` never
  read by `src/`; no hardcoded secrets; no unsafe deserialization
  (`ast.literal_eval` bounded, `json.loads` only, no pickle/eval).
- Policy floor with tighten-only overrides; DENY/ASK run nothing; every
  request audited. `EditFile` (8ce4390) preserves all of the above:
  sandbox-mandatory, protected/reserved enforced, uniqueness gate, atomic
  write, checkpointed, MEDIUM like `write_file`.
- Timeout plumbing (`PAC_REQUEST_TIMEOUT_SECONDS` read at runtime,
  per-provider, per-request); entry-point wiring; merge-ledger and
  skip-accounting gates.

## v1.0 gate summary

Required before v1.0: P0-1 through P0-7 and P1-1 through P1-10 (17 items).
Not required: P2-1 through P2-8 (docs/packaging/CI/edge hardening, take with
release) and P3-1 through P3-5 (post-v1). No new features, no new
dependencies, and no architectural redesign are proposed anywhere above;
several remediations are deletions (P0-5 env-from-nothing, P1-1 refuse
combination, P1-10 description honesty).
