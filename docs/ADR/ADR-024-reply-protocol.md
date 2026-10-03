# ADR-024 — The agent's reply protocol: accept what is unambiguous, take file content out of JSON

**Status:** PROPOSED (2026-10-03) · **unit A authorized by the owner 2026-10-03** ("ابدأ A")
· units B and C not authorized · each unit is measured before it is adopted

| Stage | State |
|---|---|
| Proposed | yes: this document, written by the lead after #198 |
| Evidence | the refused-reply text of #198, read in #199; the replay in §3 is pinned by `tests/unit/test_adr024_replay.py` |
| Authorized | **unit A** (D1), by the owner, 2026-10-03, in the lead session: "ابدأ A" ("start A"), with the lead's order (D4: A first). D2 and D3 are open |
| Implemented | **no** (unit A is built in its own pull request) |

- Serves: ADR-023 (plan, execute, verify), whose §8.2 and §7 reserve any change to what
  the model sees, or to the protocol it answers in, for separate review. This is that
  review.
- Depends on: ADR-023 amendment 1 (#191, PROPOSED), the rule that decides a comparison.
  Until it is approved, a measurement of these units is descriptive.

## 1. The problem, as measured

The loop asks for one JSON object per reply: `{"tool": ..., "arguments": {...}}` or
`{"answer": "..."}` (`agent/loop.py`, `PROTOCOL`). Anything else is a protocol error. It
counts as a failed action, and the budget allows three.

In #198, 240 agent runs at 1861756, the loop refused 98 replies as protocol errors: 49 in
each run. They cost budget the model could have spent acting. The text of each one is
recorded. Read after the text was seen (#199), the causes are as follows. Each reply is
counted once, under the first row it matches, in the order the rows appear:

| Shape | Count | Example |
|---|---|---|
| cut at the generation limit (`done_reason: length`) | 8 | a reply of about 4,000 characters |
| Python function-call syntax | 9 | `write_file(VERSION, "1.5.0")`, English, unit 1+2 only |
| a numeric answer | 5 | `{"answer": 4}`, twice the right number |
| a command given as a string | 8 | `{"tool": "shell", "arguments": "type words.py"}` |
| file content that breaks a `write_file` call's JSON | 37 | `"content": "{"debug": false, "workers": 4}"` |
| the rest | 31 | prose with no JSON, malformed objects |

## 2. What is ruled out

- **Repairing broken file content.** Where unescaped quotes break the JSON, any repair has
  to guess where the content ends. A guessed file write is worse than a refused one.
- **Parsing the function-call syntax.** `write_file(VERSION, "1.5.0")` maps to arguments
  only by guessing a positional order, and the path is not even quoted.
- **Treating any of this as a model fault to be trained away.** The protocol is ours. The
  question is whether it asks a 7B model for something it cannot reliably produce.

## 3. Replay: what each candidate would have recovered

The 98 recorded texts were run through candidate parsers offline. This is evidence about
the parsers, not about success: the run after a recovered reply would have gone differently.

| Candidate | Rule | Recovers |
|---|---|---|
| P1 | a numeric `answer` becomes its text | 5 |
| P2 | a string `arguments` for a tool with exactly one required field becomes that field (`shell`, `run_command`: `command`; `read_file`, `delete_file`: `path`) | 8 |
| P3 | a reply that is not JSON but is a Python literal (single quotes, `True`, `None`) is read with `ast.literal_eval`, which evaluates literals only | 4 |
| all three | | **17 of 98** |
| replacing `\'` with `'` | | 0 |

Of the 37 broken `write_file` calls, one is recovered: by P3, a `content` given in single
quotes, read as the right text. Replacing `\'` with `'` recovers none. The rest are
unescaped double quotes inside the content (JSON inside JSON) and Python expressions after
a string (`"...".strip()`), which no rule can read without guessing.

## 4. Proposed units

**Unit A: the parser accepts three unambiguous shapes (P1, P2, P3).**
- No change to the text the model sees.
- What the executor, the policy and the verifier decide is unchanged. Only how a reply is
  read changes.
- Each accepted shape is recorded on the outcome (`lenient_parses`, by kind), so a
  measurement can count them and a reader can audit every one.
- `ast.literal_eval` runs on the reply's object span only. That span is bounded by the
  generation limit, about 4,000 characters.

**Unit B: file content outside JSON.**
- `write_file` may omit `content` and give it instead as one fenced block after the JSON
  object.
- The content is taken verbatim between the fences.
- More than one block, or a block together with a `content` field, is a protocol error.
- One sentence is added to `PROTOCOL`. That is new text the model sees, so the owner
  reviews the wording.
- This is the only unit aimed at the largest cause (37 of 98, of which unit A recovers 1).

**Unit C (optional): a protocol error that names a tool is answered with that tool's form.**
- When a refused reply names one of the loop's tools, the feedback gives the exact JSON
  shape for that tool instead of the generic "Reply with one JSON object."
- It is aimed at the function-call syntax, whose text always names the tool.
- It changes feedback text only.

## 5. How each would be measured

- One unit at a time, against a baseline at the same commit with the unit off. Same
  model, context and rig, with the instrument check (#196) passing.
- Primary: agent success per language. Secondary: protocol errors per run, by the
  categories of #195.
- Guard: every lenient parse in unit A is listed and read. A parse that ran something the
  reply did not plainly mean is a defect, whatever the score says.
- Read under ADR-023 amendment 1 (#191) once it is approved. Until then, descriptively.

## 6. Decisions the owner is asked for

**Decided 2026-10-03:** D1 (unit A) authorized, and D4's order with it: A first. D2 (unit
B) and D3 (unit C) remain open. A decision on adopting unit A in `pac --agent` is not part
of D1: it is taken on a measurement read under ADR-023 amendment 1.

- **D1.** Authorize unit A (parser only, no text the model sees).
- **D2.** Authorize unit B (one protocol sentence, and the fenced-content form).
- **D3.** Authorize unit C (feedback text only), or leave it.
- **D4.** Order. The lead recommends A, then B, then C:
  - A changes nothing the model sees and can be verified completely offline.
  - B is the largest lever and the largest change.
  - C is the smallest, and is aimed at one run's English failure.
