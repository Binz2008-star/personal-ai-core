# ADR-019 — A mechanical guard for the reply language

**Status:** PROPOSED · direction approved by the owner 2026-10-01 ("موافق على الاثنين")
· the decisions in §6 are open · unit 1 (the pure check) built; nothing wired

- Contract text this design serves: the language rule, ADR-012 amendment 1: "Reply in
  the language of the user's latest message, and only in that language … Do not change
  language in the middle of a reply."
- ADR-012's text is not changed by this ADR. The guard enforces the rule; it does not
  restate it.

## 1. The problem, as measured

Eight contract_v1 runs on the rig (Boss model, `num_ctx` 8192, 2026-10-01):

- Across the four runs since #112 and #114, every failure was Chinese text inside an
  Arabic reply. English cases, secret cases and injection cases passed in all of them.
- `ground-decline-ar` failed in every run. The question is Arabic and the user's notes are
  Arabic. The model declines correctly ("the notes do not contain the serial number"),
  but in Chinese: one Arabic word, then Chinese.
- Three prompt wordings changed only which wrong language the decline came out in:
  Chinese under rule A, English under rule B, and Chinese under B plus #112.
- Sampling (#120) moved the overall count from 6 to 4 Chinese failures over two runs.
  It left `ground-decline-ar` failing in all of them.
- Other Arabic cases (`lang-ar-gulf-bait`, `lang-ar-msa-1/2`, `lang-ar-egypt-bait`)
  slip into Chinese in roughly one run in two, in different cases each time.

The prompt does not fix this on a 7B model. The failure is visible in the output, and
it can be checked mechanically, so the fix belongs after generation, not in more
wording.

## 2. What is ruled out

- **More prompt wording.** Three attempts, measured, none fixed it.
- **A model judge.** ADR-013 keeps model judgement out of verdicts; a guard that asks a
  model "is this Arabic?" would inherit the failure it is checking for.
- **Deleting the foreign text.** Removing the Chinese from the `ground-decline-ar` reply
  leaves one Arabic word. A reply edited by the system would also no longer be what the
  model said, which conflicts with the audit trail.
- **A different Boss model.** The model is an invariant (ADR-002).

## 3. Proposed design

### 3.1 Detection: pure and deterministic

A pure function in `conversation/` with no I/O or model call:
`check_reply(user_text, reply_text) -> Verdict`. It counts letters by Unicode script
(Arabic, Latin, Han, Kana, Hangul, Cyrillic, other).

1. **Expected script.** This is the script that has at least 60% of the user message's
   letters, and the message must have at least 8 letters. Otherwise the language is
   undetermined, and the guard does nothing. Only Arabic and Latin are expected scripts
   in version 1.
2. **Violation.** The reply violates the rule if either holds:
   - it contains at least 3 letters of a script that is neither the expected script nor
     present in the user message (in practice Han, Kana, Hangul or Cyrillic); or
   - the expected script is Arabic, and Arabic is under 50% of the reply's letters.

   Latin letters in an Arabic reply (names, code, product names) are allowed below that
   share. The 50% threshold is what turns the English decline under rule B into a
   violation.
3. **Excluded from counting:**
   - fenced code blocks;
   - any script that appears in the turn's evidence, which a citation may quote.
     *Unit 1 note:* this is wider than "text copied word for word", and simpler.
     A script the user's documents contain is never foreign in that turn.

### 3.2 Action: one retry, then deliver

On a violation, the service generates once more with the same prompt plus one system
message at the end:

> Your previous draft was not written in the language of the user's message. Write the
> whole reply again in the language of the user's message.

- The rejected draft is not shown to the model, so it has nothing to copy, and it is
  not persisted as a message.
- The second reply is delivered whatever it is. If it still violates, the turn records
  that and does not try a third time.
- The note names no language, consistent with ADR-012 amendment 1.
- The note costs tokens. The budget reserves them in every turn (measured, not
  constant-guessed), so a retry never overflows the window.

### 3.3 What is recorded

A new event, `REPLY_LANGUAGE_GUARD`, is recorded on the turn whenever the guard fires:

- the expected script;
- per-script letter counts of the rejected draft and of the delivered reply;
- whether the delivered reply passed.

It records counts only, never text, like ADR-018's redaction counts. The second
generation records its own `GENERATION_REQUESTED` with `attempt: 2`. The extra cost is
therefore visible per turn.

### 3.4 Exemptions

The guard must not fight the user. It does nothing when:

- the user asks for another language. A small fixed list of request words in the user
  message turns it off for that turn: "translate", "in English", "in Chinese", "ترجم",
  "بالإنجليزية", "بالصينية" and similar;
- the reply's foreign script also appears in the user message, as when the user quotes
  Chinese text;
- the expected script is undetermined (§3.1).

## 4. What this does not give

- **It catches scripts, not languages.** A reply in Persian or Urdu to an Arabic question
  is in Arabic script and passes. A reply in French to an English question passes. On the
  rig the observed failures are all in a different script, which is what this catches.
- **The retry can fail too.** It is one more sample from the same model. The event
  records how often the second reply also fails, which measures the guard itself.
- **Latency.** A triggered turn generates twice. On the rig (CPU only), that adds 15 to
  60 seconds to those turns, roughly one Arabic turn in ten, and to `ground-decline-ar`
  whenever it repeats.
- **The exemption list is lexical.** A translation request worded in a way the list does
  not know gets the guard: one extra generation, and a reply in the user's language
  rather than the one requested. The list is in one module and tested.
- **The agent loop is out of scope.** It asks for JSON, and its replies are not
  conversation replies.

## 5. How it would be verified

- Unit tests for `check_reply`: the 8 rig replies that failed are violations, and every
  rig reply that passed is not; Latin names inside Arabic pass; each exemption.
- Service tests with a scripted transport:
  - a Chinese first draft is retried and the second reply delivered;
  - the event carries counts only;
  - an exempted turn does not retry;
  - the retry note is charged in the budget.
- On the rig, two contract_v1 runs. The goal is `ground-decline-ar` passing, with no new
  failure, and the guard's own success rate read from its events.

## 6. Decisions the owner is asked for

- **D1.** One retry then deliver (proposed), or deliver with a short notice that the
  reply may be in the wrong language, without retrying.
- **D2.** The thresholds in §3.1: 3 foreign letters, and a 50% Arabic share.
- **D3.** The exemption list approach in §3.4, or no exemptions in version 1.
- **D4.** On by default with a setting to turn it off (proposed), or off by default until
  measured.

The author's recommendation is D1 retry, D2 as written, D3 the list, and D4 on by
default. These are recommendations; the owner decides.

## 7. Proposed units, if authorized

1. `check_reply` as a pure function with its tests. No wiring and no behaviour change.
2. Wiring into `ConversationService`: the retry, the event, the budget reservation and
   the setting.
3. A rig run on contract_v1, two runs.

Each unit is its own PR.
