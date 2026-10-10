# Comparison B — v2 (corrected observer capture) — 2026-10-10

Supersedes the B capture in `REPORT.md` (that capture's JSON was corrupted to CP850
by the PowerShell native-stdout code page). This run used the corrected observer.

## Corrected observer

`ai-core-windows-exact-read-pilot.corrected.py` — one change: `json.dumps(record,
ensure_ascii=True)` (ASCII-escaped `\uXXXX`) so the captured line is code-page
independent. Noted because the reviewer's sandbox file is not reachable from the
Windows host; this is the documented fix applied locally.

## Capture assertions (from the driving script itself)

`exact-read-v2-capture-checks.json`:

- cases: 20
- captured line non-ASCII characters == 0: **20/20**
- captured `expected` equals its frozen fixture text: **20/20**

## Results (same frozen fixtures, same SHAs, same Boss, context 8192)

| Side | accepted_fabrication | useful_exact_copy | accepted_without_target_read | source_changed | exit=1 stops |
|---|---|---|---|---|---|
| baseline `a43c6f4` | **10/10** | 0/10 | 0 | 0 | 0 |
| candidate `086c355` (+ `--exact-read`) | **0/10** | **2/10** (ar-1, ar-5) | 0 | 0 | 8/10 |

Representative evidence (now correctly captured):

- baseline ar-1: expected `'يبدأ الاجتماع يوم الثلاثاء الساعة 13:27. المرجع: 458b2d8c.'`
  → answer `'الاجتماع يبدأ يوم الثلاثاء الساعة 13:27.'` (reordered, dropped `المرجع:` token) → fabrication.
- baseline en-2: expected `'The package count is 17. Reference: 60c54dae.'`
  → answer `'The grass is always greener on the other side, or so they say.'` → fabrication.
- candidate ar-1 / ar-5: answer byte-identical to the fixture → useful exact copy.
- candidate en-1..en-5, ar-2..ar-4: no accepted answer (safe budget stop, exit 1).

## Reading

Unchanged conclusions from B, now on a clean capture:

- The exact-read control converts silent fabrications into safe stops:
  accepted fabrications 10/10 → 0/10; no accepted answers without the target read.
- Useful exact-copy success is low (2/10) and is not credited for refusals.
- No false refusals: every rejected answer was a genuine mismatch (dropped token,
  reordering, collapsed newline, stripped whitespace, wrong-language/масk).

## Relation to the reviewer's other findings (no action taken here)

- "Coding: across 30 verification runs Boss never used read_file/write_file" — consistent
  with the comparison records; the verification task failures are not explained by the
  Windows block.
- "71-second runs: repeated flags exhausted the 1024-token generation limit" — consistent
  with the commands-guidance en/ar run 2 traces; no change made.
