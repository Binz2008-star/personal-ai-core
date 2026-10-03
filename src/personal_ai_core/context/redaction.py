"""Secret redaction, version 1 (ADR-018).

Implements `core.contracts.SecretRedactor`. A pure component: no I/O, no
state between calls, no wiring. Unit 2 hands an instance to the grounding
renderers; nothing in this module knows they exist.

Deterministic patterns only (section 3.6). Each detector replaces the secret
VALUE and leaves the rest of the line, so a passage that holds a connection
string still says it holds one:

    DATABASE_URL=postgres://app:PLANTED@db:5432/app
    DATABASE_URL=postgres://app:[withheld: secret]@db:5432/app

Detectors run in a fixed order, and each later detector sees the earlier
ones' output:

1. `pem` -- private-key blocks, including blocks cut by a chunk boundary.
2. `assignment` -- `KEY=value` / `key: value` where the key names a secret.
3. `authorization` -- `Authorization: Bearer|Basic <value>`.
4. `url_userinfo` -- the password in `scheme://user:password@host`.
5. `prefix_token` -- tokens with a published fixed prefix.
6. `prose` -- "the password is X" / "كلمة المرور هي X" (ADR-018 amendment 1).

Assignment runs before `url_userinfo` so `DB_PASSWORD=postgres://u:p@h` is
withheld as one value rather than twice.

**Idempotence.** No detector matches a value that is already the marker, so
redacting redacted text changes nothing and counts nothing. A document that
itself contains the marker text is treated the same way (section 4 records
that the marker is forgeable).

**Known limits, stated in ADR-018 section 4:** a secret in prose is detected
only in the shapes `prose` names, and only when the value looks like a
credential; a chunk boundary can cut a secret; assignment key names are
matched in ASCII; there is no entropy detector.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from types import MappingProxyType

from ..core.redaction import Redaction, RedactionError

# ADR-018 D3. Must never contain a 16-hex run: the grounding renderer prices
# every such run as a boundary token (section 3.4, finding I3). Square
# brackets and a colon keep it from being read as a value by any detector
# below, which is what makes redaction idempotent.
REDACTION_MARKER = "[withheld: secret]"

KIND_PEM = "pem"
KIND_ASSIGNMENT = "assignment"
KIND_AUTHORIZATION = "authorization"
KIND_URL_USERINFO = "url_userinfo"
KIND_PREFIX_TOKEN = "prefix_token"
KIND_PROSE = "prose"

# In the order the detectors run.
REDACTION_KINDS: tuple[str, ...] = (
    KIND_PEM,
    KIND_ASSIGNMENT,
    KIND_AUTHORIZATION,
    KIND_URL_USERINFO,
    KIND_PREFIX_TOKEN,
    KIND_PROSE,
)

_M = re.escape(REDACTION_MARKER)

# A replacement string for `re.sub`: the marker has no backslash or group
# reference, but escaping keeps that true if its wording changes.
_M_REPL = REDACTION_MARKER.replace("\\", "\\\\")

# An unquoted value: non-space, non-quote characters, stopping before a
# marker so a value that already holds one is not swallowed with it.
_BARE_VALUE = rf"(?:(?!{_M})[^\s'\"])+"


# --- pem -------------------------------------------------------------------
#
# Matched case-sensitively, as PEM armour is. `ENCRYPTED`, `RSA`, `EC`,
# `OPENSSH` and `PGP ... BLOCK` labels are all private keys.

_PEM_LABEL = r"(?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?"
_PEM_BOUNDARY = re.compile(rf"-----(?P<edge>BEGIN|END) {_PEM_LABEL}-----")


def _redact_pem(text: str) -> tuple[str, int]:
    """Withhold every private-key body; armour lines stay.

    Section 3.6, chunk-boundary rule: a BEGIN line with no END after it is
    withheld to the end of the text, and an END line with no BEGIN before it
    is withheld from the start of the text (or from the previous armour
    line, if an earlier block was closed in the same text).
    """
    spans: list[tuple[int, int]] = []
    open_at: int | None = None
    last_edge_end = 0
    for boundary in _PEM_BOUNDARY.finditer(text):
        if boundary.group("edge") == "BEGIN":
            if open_at is None:
                open_at = boundary.end()
        else:
            start = open_at if open_at is not None else last_edge_end
            spans.append((start, boundary.start()))
            open_at = None
        last_edge_end = boundary.end()
    if open_at is not None:
        spans.append((open_at, len(text)))

    count = 0
    pieces: list[str] = []
    cursor = 0
    for start, end in spans:
        body = text[start:end]
        core = body.strip()
        if not core or core == REDACTION_MARKER:
            continue
        lead = body[: len(body) - len(body.lstrip())]
        trail = body[len(body.rstrip()) :]
        pieces.append(text[cursor:start])
        pieces.append(f"{lead}{REDACTION_MARKER}{trail}")
        cursor = end
        count += 1
    pieces.append(text[cursor:])
    return "".join(pieces), count


# --- assignment ------------------------------------------------------------
#
# Finding I2. The key matches only if it ENDS with one of the listed words,
# and that word is at the start of the name or follows `_` or `-`. A bare
# `KEY` never matches. So `MAX_TOKENS`, `TOKEN_COUNT`, `PASSWORD_MIN_LENGTH`,
# `SORT_KEY` and `PRIMARY_KEY` are not secrets, while `DB_PASSWORD`,
# `OPENAI_API_KEY`, `client-secret` and `AWS_SECRET_ACCESS_KEY` (which ends
# with `ACCESS_KEY` after `_`) are.
#
# The two-part words accept `_` or `-` between their parts (`api-key`), and
# a key may follow any non-name character, including `.`, so the last
# segment of `spring.datasource.password` is a key name of its own.
#
# "Ends with" is enforced by what follows the word: an optional closing
# quote and then the separator, immediately. `MAX_TOKENS=` has an `S`
# between the word and `=`, so it cannot match.

_SECRET_WORD = (
    r"(?:PASSWORD|PASSWD|SECRET|TOKEN"
    r"|API[_-]KEY|ACCESS[_-]KEY|SECRET[_-]KEY|PRIVATE[_-]KEY)"
)
_ASSIGNMENT = re.compile(
    rf"""
    (?P<head>
        (?<![A-Za-z0-9_-])
        (?:[A-Za-z0-9]+[_-])*{_SECRET_WORD}
        ["']?[ \t]*[:=][ \t]*
    )
    (?:
        "(?P<dq>[^"\n]+)"
      | '(?P<sq>[^'\n]+)'
      | (?P<bare>(?!=){_BARE_VALUE})
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _redact_assignment(text: str) -> tuple[str, int]:
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        head = match.group("head")
        for quote, name in (('"', "dq"), ("'", "sq")):
            value = match.group(name)
            if value is not None:
                if value == REDACTION_MARKER:
                    return match.group(0)
                count += 1
                return f"{head}{quote}{REDACTION_MARKER}{quote}"
        count += 1
        return f"{head}{REDACTION_MARKER}"

    return _ASSIGNMENT.sub(replace, text), count


# --- authorization ---------------------------------------------------------

_AUTHORIZATION = re.compile(
    rf"""
    (?P<head>
        (?<![A-Za-z0-9_-])
        (?:Proxy-)?Authorization["']?[ \t]*[:=][ \t]*["']?
        (?:Bearer|Basic)[ \t]+
    )
    (?P<value>{_BARE_VALUE})
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _redact_authorization(text: str) -> tuple[str, int]:
    return _AUTHORIZATION.subn(rf"\g<head>{_M_REPL}", text)


# --- url_userinfo ----------------------------------------------------------
#
# The password runs to the LAST `@` before the path, so an unencoded `@` in
# a password (`p@ss@host`) is withheld whole. A URL with a port and no
# userinfo (`https://host:8080/x`) has no `@` before its path and is not
# matched; neither is a URL with a user and no password.

_URL_USERINFO = re.compile(
    rf"""
    (?P<head>
        (?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]*://
        [^\s:/?#@]*:
    )
    (?P<value>(?!{_M})[^\s/?#]+)
    (?=@)
    """,
    re.VERBOSE,
)


def _redact_url_userinfo(text: str) -> tuple[str, int]:
    return _URL_USERINFO.subn(rf"\g<head>{_M_REPL}", text)


# --- prefix_token ----------------------------------------------------------
#
# Published fixed prefixes, each with a minimum body length so that a word
# like `sk-learn` or a short identifier is not taken for a key. The whole
# token is the value.
#
# ADR-018 amendment 2 (2026-10-03): the GitHub and AWS prefixes are found
# even glued to the text before them ("xghp_..."). `sk-` and `xox` keep the
# boundary: `sk-` ends ordinary words ("task-...", "desk-..."), and a scan
# of every tracked file found no glued GitHub or AWS shape but the tests'.

_PREFIX_TOKEN = re.compile(
    r"""
    (?:
        (?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}          # OpenAI / Anthropic style
      | gh[pousr]_[A-Za-z0-9]{20,}                       # GitHub classic tokens
      | github_pat_[A-Za-z0-9_]{22,}                     # GitHub fine-grained tokens
      | AKIA[0-9A-Z]{16}                                 # AWS access key id
      | (?<![A-Za-z0-9_-])xox[abposr]-[A-Za-z0-9-]{10,}  # Slack
    )
    (?![A-Za-z0-9_-])
    """,
    re.VERBOSE,
)


def _redact_prefix_token(text: str) -> tuple[str, int]:
    return _PREFIX_TOKEN.subn(_M_REPL, text)


# --- prose -------------------------------------------------------------------
#
# ADR-018 amendment 1. A secret named in a sentence: a secret noun, up to four
# words, a copula, then the value.
#
#     The staging password is hunter2-PLANTED-71c3.
#     كلمة المرور لخادم الاختبار هي hunter2-PLANTED-71c3
#
# Prose is where false positives live ("the password is required", "the
# token is valid for 3600 seconds", "the secret is to practise"), so the
# value must look like a credential: quoted, or at least six characters with
# a digit or a symbol in them. A letters-only password in a sentence is
# therefore still missed -- the stated limit -- and rule 3 remains the layer
# behind it. Sentence punctuation after the value stays outside it.

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
    (?P<head>
        (?:
            (?<![A-Za-z0-9_-]){_PROSE_NOUN_EN}(?![A-Za-z0-9_-])
            (?:[ \t]+(?!(?:is|was)\b)[^\s:=]+){{0,4}}?
            [ \t]+(?:is|was)(?:[ \t]+(?:now|still|set[ \t]+to))?
          | {_PROSE_NOUN_AR}
            (?:[ \t]+(?!(?:هي|هو)(?:\s|$))[^\s:=]+){{0,4}}?
            [ \t]+(?:هي|هو)
        )
        [ \t]*:?[ \t]+
    )
    (?:
        (?P<q>["'`])(?P<quoted>(?!{_M})[^"'`\n]+)(?P=q)
      | (?P<bare>(?!{_M})[^\s"'`]+)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

_TRAILING_PUNCT = ".,;:!?)]}\u060c\u061b\u061f"


def _looks_like_a_credential(value: str) -> bool:
    return len(value) >= 6 and any(not ch.isalpha() for ch in value)


def _redact_prose(text: str) -> tuple[str, int]:
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        head = match.group("head")
        quoted = match.group("quoted")
        if quoted is not None:
            q = match.group("q")
            count += 1
            return f"{head}{q}{REDACTION_MARKER}{q}"
        bare = match.group("bare")
        value = bare.rstrip(_TRAILING_PUNCT)
        if not _looks_like_a_credential(value):
            return match.group(0)
        count += 1
        return f"{head}{REDACTION_MARKER}{bare[len(value):]}"

    return _PROSE.sub(replace, text), count


_DETECTORS: tuple[tuple[str, Callable[[str], tuple[str, int]]], ...] = (
    (KIND_PEM, _redact_pem),
    (KIND_ASSIGNMENT, _redact_assignment),
    (KIND_AUTHORIZATION, _redact_authorization),
    (KIND_URL_USERINFO, _redact_url_userinfo),
    (KIND_PREFIX_TOKEN, _redact_prefix_token),
    (KIND_PROSE, _redact_prose),
)


def _apply(text: str) -> Redaction:
    counts: dict[str, int] = {}
    for kind, detector in _DETECTORS:
        text, found = detector(text)
        if found:
            counts[kind] = found
    return Redaction(text=text, counts=counts)


class PatternSecretRedactor:
    """`core.contracts.SecretRedactor`, version 1: deterministic patterns.

    Section 3.8: if anything inside fails, the call raises `RedactionError`
    with a classification only. The original exception is deliberately not
    chained -- neither as `__cause__` nor as `__context__` -- because its
    message may quote the text.
    """

    def redact(self, text: str) -> Redaction:
        if not isinstance(text, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise RedactionError(RedactionError.INVALID_INPUT)
        failed = False
        result: Redaction | None = None
        try:
            result = _apply(text)
        except Exception:  # noqa: BLE001 -- any failure is classified, never quoted
            failed = True
        # Raised outside the handler so the failure has no __context__.
        if failed or result is None:
            raise RedactionError(RedactionError.INTERNAL)
        return result


class NullRedactor:
    """A `SecretRedactor` that withholds nothing.

    Finding I1: the renderers will take a redactor with no default, so a test
    or tool that wants raw text must say so by passing this, visibly.
    """

    def redact(self, text: str) -> Redaction:
        if not isinstance(text, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise RedactionError(RedactionError.INVALID_INPUT)
        return Redaction(text=text, counts=MappingProxyType({}))
