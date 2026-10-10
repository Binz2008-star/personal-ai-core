# Step 3 preflight — bare `pytest` vs `python -m pytest` (2026-10-10 18:52–18:54 +04:00)

Purpose: identify the blocked file and policy behind the agent-step
`OSError [WinError 4551] An Application Control policy has blocked this file`,
without changing any Windows security setting or Core command policy.

## Method

- Same comparison venv as Comparisons A/B/C: `…\pac-boss-review-20261010\venv`.
- Known-good scratch test `scratch\test_ok.py` (`assert 1 + 1 == 2`).
- Ran 5x bare `pytest` and 5x `python -m pytest`, alternating; PATH = venv `Scripts` first.
- Then reproduced Core's launch path: venv python `subprocess` → venv `pytest.exe`, 5x.
- Read-only queries of `Microsoft-Windows-CodeIntegrity/Operational` (3077/3089),
  `Microsoft-Windows-AppLocker/*`, Smart App Control state and Device Guard.

## Resolved executables

- `pytest` -> `…\pac-boss-review-20261010\venv\Scripts\pytest.exe` (**NotSigned**, 108,406 bytes,
  no ProductName/Company). Also present: `C:\Python314\Scripts\pytest.exe`.
- `python` -> `…\venv\Scripts\python.exe` (base `C:\Python314\python.exe`).

## Results

| Run set | Time | Result |
|---|---|---|
| bare `pytest` x5 | 18:52:59–18:53:13 | 5/5 exit 0 (1 passed) |
| `python -m pytest` x5 | 18:53:10–18:53:13 | 5/5 exit 0 (1 passed) |
| venv python → `pytest.exe` subprocess x5 | 18:54 | 5/5 exit 0 |

CodeIntegrity **3077/3089 events during the preflight window: 0**.

## The block — evidence

`Microsoft-Windows-CodeIntegrity/Operational`, event **3077** (enforced block) + **3118**
("Smart App Control Block Details"), sorted by time:

- `2026-10-09 19:46:49`, `2026-10-09 20:47:51` — blocked `C:\Python314\Scripts\pysemgrep.exe`
  (another unsigned pip console-script shim; unrelated to this run).
- `2026-10-10 15:15:53, 15:16:21, 15:16:25, 15:16:28, 15:22:05, 15:29:03, 15:29:07,
  15:29:31, 15:29:36, 15:29:59, 15:30:25, 15:30:27, 15:30:31` — 13 events, each naming the
  comparison venv `…\pac-boss-review-20261010\venv\Scripts\pytest.exe`.

Sample 3077 message:

> Code Integrity determined that a process (`…\Python314\python.exe`) attempted to load
> `…\pac-boss-review-20261010\venv\Scripts\pytest.exe` that did not meet the Enterprise
> signing level requirements or violated code integrity policy
> (Policy ID:`{0283ac0f-fff1-49ae-ada1-8a933130cad6}`).

Two of the late events name `cmd.exe` as the loading process (the Comparison C `shell` arm);
the rest name `…\Python314\python.exe` (the agent subprocess).

## Blocked file and policy — identified

- **Blocked file:** the comparison venv's `pytest.exe` console-script shim (unsigned).
- **Policy:** **Smart App Control** user-mode WDAC,
  policy ID `{0283ac0f-fff1-49ae-ada1-8a933130cad6}`;
  `VerifiedAndReputablePolicyState = 1` (on/enforcement),
  `UsermodeCodeIntegrityPolicyEnforcementStatus = 2` (enforced).
- **Not** AppLocker: the AppLocker `EXE and DLL` log holds only `8001` (policy applied) and
  unrelated installer `8043` events; no `8004` block for pytest.
- **Not** PowerShell ExecutionPolicy (Bypass) and **not** Core's allowlist.

## Interpretation

- The blocked artifact was the unsigned venv `pytest.exe`. Smart App Control blocked it
  throughout the 15:15–15:30 benchmark window, then did **not** block identical launches
  ~3.5 hours later (preflight 18:52–18:54 and the subprocess reproduction: 15/15 success,
  0 events). The block is therefore **transient / Smart App Control reputation-driven**, not
  deterministic and not caused by the model, Core, or either PR.
- The path `python -m pytest` never produced a 3077 (matches the harness's own
  `command_passes` check, which uses `sys.executable -m pytest`).
- General class seen on this machine: unsigned pip console-script shims in a temp venv
  (`pytest.exe`, `pysemgrep.exe`) are the files Smart App Control objects to.

## Recommendation (no settings changed)

- Do **not** disable Smart App Control or AppLocker, and do not widen Core's policy/allowlist.
- For the benchmark's agent-side test runs, prefer the module form (`python -m pytest`) or
  the post-run `command_passes` interpreter path, both of which avoid the unsigned shim.
  If the agent must use bare `pytest`, expect occasional Smart App Control blocks on this host.
- If a stable host-side runner is required later, the correct fix is owner-side (e.g. a
  signed Python, or an explicit owner-approved exclusion), decided by the owner — not by
  weakening Core or the security posture.

All security settings and the Core command policy were left unchanged; the checks above are read-only.
