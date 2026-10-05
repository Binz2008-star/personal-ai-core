"""VERIFY -- deterministic checks, cheapest first (AGENT_ARCHITECTURE.md section 5).

BUILD, not extracted: no source has a generic verifier (Robin's quality_gate
validates video files). Only the gate-result SHAPE transfers, and it is
`core.agent.VerificationResult`.

Layers, per the design:
  1. Schema   -- did the step actually run, and report success?
  2. Grounding-- claims trace to evidence. NOT here: it needs a model's claims
                 and the evidence they rest on, and a mechanical check of that
                 is ADR-013's harness. Named rather than faked.
  3. Policy   -- no secret in anything the agent produced.
  4. Task     -- the plan's stated expectation is met.

"A failed verification triggers RECOVER, never a silent pass." The verifier
only reports; it never retries, repairs or rolls back. That is recovery.py.

The secret shapes below are the system's one definition of a secret in what
it SAYS: a tool's output and the agent's answer here, and -- through
`SecretShapeRedactor`, which the composition root hands to `pac` -- a chat
reply before it is printed (gap analysis P0-6). They cover every detector
kind of ADR-018's evidence redactor (`context/redaction.py`), which this
layer may not import; `tests/unit/test_secret_gate_outputs.py` pins that, and
names each place the two deliberately differ.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from ..core.agent import AuditRecord, Decision, VerificationCheck, VerificationResult
from ..core.redaction import Redaction, RedactionError

# What a withheld value is replaced with when the shapes are applied as a
# redactor (a chat reply). It is ADR-018's marker (D3), so one thing has one
# wording wherever the user meets it. `agent` may not import `context`, so the
# text is written again here and a test pins the two equal.
WITHHELD_MARKER = "[withheld: secret]"

# A value's (start, end) in the text it was found in.
Span = tuple[int, int]

# --- what is not a value ----------------------------------------------------
#
# Deliberately high-precision: a pattern that fires on ordinary text trains the
# reader to ignore it, and here a false positive is expensive -- the agent's
# whole answer is withheld, and a tool output that trips the check is a failed
# action against the budget of three. So a value written where a secret would
# go is not a secret: `<redacted>`, `${API_KEY}`, `{{ token }}`, `$TOKEN`,
# `%PASSWORD%`, `********`, `...`, `YOUR_API_KEY`, `your-token-here` -- and
# the marker itself (`[`), so withholding is idempotent.

_PLACEHOLDER_OPENERS = ("<", "$", "{", "*", "[", "%")
_ENV_NAME = re.compile(r"[A-Z_]+")


def _placeholder(value: str) -> bool:
    return (
        value.startswith(_PLACEHOLDER_OPENERS)
        or (len(value) >= 3 and len(set(value)) == 1)
        or _ENV_NAME.fullmatch(value) is not None
        or "your" in value.casefold()
    )


# An unquoted assignment value in code is usually an expression, and the
# widened key names below meet a great deal of code (`DB_PASSWORD = ...`,
# `self.access_token = ...`). An expression is visibly one when it is called
# or indexed (`getpass()`, `os.environ[`, `Optional[str]`) or dotted with no
# digit in it (`settings.password`; a token such as `ya29.a0Af...` has
# digits). A snake_case name made of words is a name, quoted or not
# (`db_password`, `KIND_PREFIX_TOKEN = "prefix_token"`). A plain word is not
# code: `hunter2` and `changeme` are values.
_CODE_VALUE = re.compile(
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)(?P<call>[(\[].*)?"
)
_SNAKE_CASE_WORDS = re.compile(r"[a-z]+(?:_[a-z]+)+")
_CODE_TRAILERS = ";,)}"


def _looks_like_code(value: str) -> bool:
    match = _CODE_VALUE.fullmatch(value)
    if match is None:
        return False
    if match.group("call") is not None:
        return True
    name = match.group("name")
    return "." in name and not any(ch.isdigit() for ch in name)


def _assigned_secret(value: str, *, quoted: bool) -> bool:
    """Whether a value assigned to a secret-named key is one.

    Eight characters, or six with a digit in them: `hunter2` is a password,
    `None`, `str` and `string` are code. A quoted value is a literal and is
    never read as an expression.
    """
    core = value if quoted else value.rstrip(_CODE_TRAILERS)
    if (not core or _placeholder(core) or _SNAKE_CASE_WORDS.fullmatch(core)
            or (not quoted and _looks_like_code(core))):
        return False
    return len(core) >= 8 or (len(core) >= 6 and any(ch.isdigit() for ch in core))


# --- private key ------------------------------------------------------------
#
# The armour of a private key, BEGIN or END: ADR-018's chunk-boundary rule
# meets an END with no BEGIN before it, and a model can echo either half.
# `PUBLIC KEY` and `CERTIFICATE` are not secrets.

_PEM_ARMOUR = re.compile(r"-----(?P<edge>BEGIN|END) [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----")


def _strip(text: str, start: int, end: int) -> Span:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if text[start:end] == WITHHELD_MARKER:
        return (start, start)
    return (start, end)


def _private_key_bodies(text: str) -> Iterator[Span]:
    """The body of every key: BEGIN to END, BEGIN to the end of the text, or
    the start of the text (or the previous armour line) to an END.

    An armour line alone yields an empty span: the shape is present -- the
    agent's check fails on it, as it always has -- but there is no value to
    withhold, so a redactor reports nothing.
    """
    open_at: int | None = None
    last_edge_end = 0
    for armour in _PEM_ARMOUR.finditer(text):
        if armour.group("edge") == "BEGIN":
            if open_at is None:
                open_at = armour.end()
        else:
            yield _strip(text, open_at if open_at is not None else last_edge_end, armour.start())
            open_at = None
        last_edge_end = armour.end()
    if open_at is not None:
        yield _strip(text, open_at, len(text))


# --- prefixed tokens --------------------------------------------------------
#
# Published fixed prefixes, each with a minimum body length so that a word
# like `sk-learn` or a short identifier is not taken for a key. ADR-018
# amendment 2: the GitHub and AWS prefixes are found even glued to the text
# before them. The JSON web token is the generic bearer token: three base64url
# parts, the first two JSON objects (`eyJ` is `{"`).

_AWS_ACCESS_KEY = re.compile(r"AKIA[0-9A-Z]{16}(?![A-Za-z0-9_])")
_GITHUB_TOKEN = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{22,})(?![A-Za-z0-9_])"
)
_OPENAI_STYLE_KEY = re.compile(r"(?<![A-Za-z0-9_])sk-[A-Za-z0-9_-]{20,}")
_HUGGING_FACE_TOKEN = re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")
_SLACK_TOKEN = re.compile(r"(?<![A-Za-z0-9_])xox[abposr]-[A-Za-z0-9-]{10,}")
_JSON_WEB_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
)


def _whole(pattern: re.Pattern[str]) -> Callable[[str], Iterator[Span]]:
    def find(text: str) -> Iterator[Span]:
        for match in pattern.finditer(text):
            yield match.span()

    return find


# --- credential assignment --------------------------------------------------
#
# ADR-018's key rule (finding I2): the key ENDS with a secret word, and that
# word starts the name or follows `_` or `-`, so `MAX_TOKENS`, `TOKEN_COUNT`,
# `PASSWORD_MIN_LENGTH` and `PRIMARY_KEY` are not secrets while `DB_PASSWORD`,
# `AWS_SECRET_ACCESS_KEY`, `client-secret` and a URL's `?access_token=` are.
# Wider than ADR-018 in two places, both kept from this check's earlier form:
# `apikey` with no separator, and a `-` before the key (`--password=`). A
# value may sit on the next line, indented, as YAML writes it.

_SECRET_WORD = (
    r"(?:PASSWORD|PASSWD|SECRET|TOKEN"
    r"|API[_-]?KEY|ACCESS[_-]KEY|SECRET[_-]KEY|PRIVATE[_-]KEY)"
)
_ASSIGNMENT = re.compile(
    rf"""
    (?<![A-Za-z0-9_])
    (?:[A-Za-z0-9]+[_-])*{_SECRET_WORD}
    ["']?[ \t]*[:=][ \t]*(?:\r?\n[ \t]+)?
    (?:
        "(?P<dq>[^"\n]+)"
      | '(?P<sq>[^'\n]+)'
      | ["']?(?P<bare>(?!=)[^\s'"]+)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _assigned_values(text: str) -> Iterator[Span]:
    for match in _ASSIGNMENT.finditer(text):
        for group, quoted in (("dq", True), ("sq", True), ("bare", False)):
            value = match.group(group)
            if value is not None:
                if _assigned_secret(value, quoted=quoted):
                    yield match.span(group)
                break


# --- authorization header ---------------------------------------------------

_AUTHORIZATION = re.compile(
    r"""
    (?<![A-Za-z0-9_-])
    (?:Proxy-)?Authorization["']?[ \t]*[:=][ \t]*["']?
    (?:Bearer|Basic)[ \t]+
    (?P<value>[^\s'"]+)
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _authorization_values(text: str) -> Iterator[Span]:
    for match in _AUTHORIZATION.finditer(text):
        if not _placeholder(match.group("value").rstrip(_CODE_TRAILERS)):
            yield match.span("value")


# --- connection string ------------------------------------------------------
#
# The password in `scheme://user:password@host`. It runs to the LAST `@`
# before the path, so an unencoded `@` in a password is withheld whole. A URL
# with a port and no userinfo has no `@` before its path and is not matched.

_URL_USERINFO = re.compile(
    r"""
    (?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]*://
    [^\s:/?#@]*:
    (?P<value>[^\s/?#]+)
    (?=@)
    """,
    re.VERBOSE,
)


def _url_passwords(text: str) -> Iterator[Span]:
    for match in _URL_USERINFO.finditer(text):
        if not _placeholder(match.group("value")):
            yield match.span("value")


# --- secret named in a sentence ---------------------------------------------
#
# ADR-018 amendment 1: a secret noun, up to four words, a copula, then the
# value -- "the staging password is X", "كلمة المرور لخادم الاختبار هي X".
# This is how a model repeats a secret it read in the history or the profile.
# The value must look like a credential (quoted, or six characters or more
# with a digit or a symbol), because prose is where false positives live:
# "the password is required", "the token is valid for 3600 seconds". Narrower
# than ADR-018 in one place: a hyphen or an underscore alone does not make a
# credential, so "token counting is tokenizer-backed" stays a sentence.

_PROSE_NOUN_EN = (
    r"(?:pass(?:word|wd|code|phrase)|pin(?:[ \t]+code)?|secret(?:[ \t]+key)?"
    r"|(?:api|access|secret|private)[ \t]+key|(?:access|auth|api|bearer)[ \t]+token"
    r"|token)"
)
_PROSE_NOUN_AR = (
    r"(?:كلمة[ \t]+(?:ال)?(?:مرور|سر)|(?:ال)?رمز[ \t]+(?:ال)?سري"
    r"|(?:ال)?رقم[ \t]+(?:ال)?سري|رمز[ \t]+(?:ال)?دخول|مفتاح[ \t]+(?:ال)?API)"
)
_PROSE = re.compile(
    rf"""
    (?:
        (?<![A-Za-z0-9_-]){_PROSE_NOUN_EN}(?![A-Za-z0-9_-])
        (?:[ \t]+(?!(?:is|was)\b)[^\s:=]+){{0,4}}?
        [ \t]+(?:is|was)(?:[ \t]+(?:now|still|set[ \t]+to))?
      | {_PROSE_NOUN_AR}
        (?:[ \t]+(?!(?:هي|هو)(?:\s|$))[^\s:=]+){{0,4}}?
        [ \t]+(?:هي|هو)
    )
    [ \t]*:?[ \t]+
    (?:
        (?P<q>["'`])(?P<quoted>[^"'`\n]+)(?P=q)
      | (?P<bare>[^\s"'`]+)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Sentence punctuation after a bare value stays outside it.
_TRAILING_PUNCT = ".,;:!?)]}\u060c\u061b\u061f"


def _prose_values(text: str) -> Iterator[Span]:
    for match in _PROSE.finditer(text):
        quoted = match.group("quoted")
        if quoted is not None:
            if not _placeholder(quoted):
                yield match.span("quoted")
            continue
        start = match.start("bare")
        value = match.group("bare").rstrip(_TRAILING_PUNCT)
        if (len(value) >= 6 and any(not ch.isalpha() and ch not in "-_" for ch in value)
                and not _placeholder(value)):
            yield (start, start + len(value))


# --- the shapes -------------------------------------------------------------
#
# Each has a test with a real-shaped (and obviously fake) example and a near
# miss, on the agent path and on the chat path. The names are what a reader
# is told: "looks like: AWS access key".

SECRET_PATTERNS: tuple[tuple[str, Callable[[str], Iterator[Span]]], ...] = (
    ("private key", _private_key_bodies),
    ("AWS access key", _whole(_AWS_ACCESS_KEY)),
    ("GitHub token", _whole(_GITHUB_TOKEN)),
    ("OpenAI-style key", _whole(_OPENAI_STYLE_KEY)),
    ("Hugging Face token", _whole(_HUGGING_FACE_TOKEN)),
    ("Slack token", _whole(_SLACK_TOKEN)),
    ("JSON web token", _whole(_JSON_WEB_TOKEN)),
    ("credential assignment", _assigned_values),
    ("authorization header", _authorization_values),
    ("connection string", _url_passwords),
    ("secret named in a sentence", _prose_values),
)


def find_secrets(text: str) -> list[str]:
    """The name of every shape present in `text`, in the order above."""
    return [name for name, find in SECRET_PATTERNS if next(find(text), None) is not None]


def withhold_secrets(text: str) -> Redaction:
    """`text` with every secret value replaced by `WITHHELD_MARKER`.

    The rest of the text stays, so a reply that holds a connection string
    still says it holds one. Values found by more than one shape (a key named
    in a sentence) are withheld once, as their union. `counts` is how many
    values each shape found, by name, and nothing else: never a value or its
    length (ADR-018 section 3.7). A shape present with nothing to withhold (an
    armour line alone) is not counted.
    """
    spans: list[Span] = []
    counts: dict[str, int] = {}
    for name, find in SECRET_PATTERNS:
        for start, end in find(text):
            if end > start:
                spans.append((start, end))
                counts[name] = counts.get(name, 0) + 1
    if not spans:
        return Redaction(text=text)
    pieces: list[str] = []
    cursor = 0
    for start, end in _merged(spans):
        pieces.append(text[cursor:start])
        pieces.append(WITHHELD_MARKER)
        cursor = end
    pieces.append(text[cursor:])
    return Redaction(text="".join(pieces), counts=counts)


def _merged(spans: list[Span]) -> list[Span]:
    merged: list[Span] = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


class SecretShapeRedactor:
    """`core.contracts.SecretRedactor` over the shapes above.

    What `pac` applies to a chat reply before printing it: the same shapes the
    agent's answer is checked against, so the two paths withhold the same
    things. It is not ADR-018's evidence redactor, which withholds more
    (placeholders, short values, code) because there a false positive costs
    only a marker in text the user never reads.

    ADR-018 section 3.8, as there: if anything inside fails, the call raises
    `RedactionError` with a classification only, and the original exception
    is not chained, because its message may quote the text.
    """

    def redact(self, text: str) -> Redaction:
        if not isinstance(text, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise RedactionError(RedactionError.INVALID_INPUT)
        failed = False
        result: Redaction | None = None
        try:
            result = withhold_secrets(text)
        except Exception:  # noqa: BLE001 -- any failure is classified, never quoted
            failed = True
        # Raised outside the handler so the failure has no __context__.
        if failed or result is None:
            raise RedactionError(RedactionError.INTERNAL)
        return result


@dataclass(frozen=True, slots=True)
class Expectation:
    """What the plan said a step should produce. Optional: not every step has
    a checkable expectation, and inventing one would be a check that means
    nothing."""

    output_contains: str | None = None


class Verifier:
    def verify(
        self, record: AuditRecord, expectation: Expectation | None = None
    ) -> VerificationResult:
        checks = [self._ran(record)]
        result = record.result
        if result is not None:
            checks.append(
                VerificationCheck(
                    "succeeded",
                    result.ok,
                    "the tool reported success" if result.ok else (result.error or "failed"),
                )
            )
            checks.append(self._no_secret("no secret in output", result.output))
            if expectation is not None and expectation.output_contains is not None:
                found = expectation.output_contains in result.output
                checks.append(
                    VerificationCheck(
                        "expectation",
                        found,
                        f"output {'contains' if found else 'lacks'} "
                        f"{expectation.output_contains!r}",
                    )
                )
        return VerificationResult(tuple(checks))

    def verify_response(self, text: str) -> VerificationResult:
        """The policy layer, applied to what the agent is about to say."""
        return VerificationResult((self._no_secret("no secret in response", text),))

    @staticmethod
    def _ran(record: AuditRecord) -> VerificationCheck:
        if record.decision.decision is Decision.DENY:
            return VerificationCheck("ran", False, f"denied: {record.decision.reason}")
        if record.result is None:
            return VerificationCheck("ran", False, "no result was recorded")
        if record.decision.decision is Decision.ASK and not record.confirmed_by_user:
            return VerificationCheck("ran", False, "not confirmed by the user")
        if not record.executed:
            return VerificationCheck(
                "ran", False, f"refused before the tool started: {record.result.error}"
            )
        return VerificationCheck("ran", True, "executed")

    @staticmethod
    def _no_secret(name: str, text: str) -> VerificationCheck:
        found = find_secrets(text)
        if found:
            return VerificationCheck(name, False, "looks like: " + ", ".join(found))
        return VerificationCheck(name, True, "nothing secret-shaped")
