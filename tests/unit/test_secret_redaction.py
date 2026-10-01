"""ADR-018 unit 1: the secret redactor as a pure component.

Each detector has positive and negative cases; the negatives named in
ADR-018 section 5 are here verbatim. Nothing in this file touches grounding,
the factory or the context builder -- unit 1 changes no behaviour.
"""
from __future__ import annotations

import re
import unicodedata
from types import MappingProxyType

import pytest

from personal_ai_core.context import redaction as redaction_module
from personal_ai_core.context.redaction import (
    KIND_ASSIGNMENT,
    KIND_AUTHORIZATION,
    KIND_PEM,
    KIND_PREFIX_TOKEN,
    KIND_URL_USERINFO,
    REDACTION_KINDS,
    REDACTION_MARKER,
    NullRedactor,
    PatternSecretRedactor,
)
from personal_ai_core.core.contracts import SecretRedactor
from personal_ai_core.core.redaction import Redaction, RedactionError

M = REDACTION_MARKER


@pytest.fixture
def redactor() -> PatternSecretRedactor:
    return PatternSecretRedactor()


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


# --- contract --------------------------------------------------------------


def test_pattern_redactor_satisfies_the_protocol():
    assert isinstance(PatternSecretRedactor(), SecretRedactor)


def test_null_redactor_satisfies_the_protocol():
    assert isinstance(NullRedactor(), SecretRedactor)


def test_kind_names_are_the_five_detectors():
    assert set(REDACTION_KINDS) == {
        "pem", "assignment", "authorization", "url_userinfo", "prefix_token"
    }


# --- the marker (finding I3) -----------------------------------------------


def test_marker_wording_is_the_recommended_one():
    assert REDACTION_MARKER == "[withheld: secret]"


def test_marker_contains_no_16_hex_run():
    # The pricing pass in grounding.py uses \b[0-9a-f]{16}\b; any 16-hex run,
    # bounded or not, is refused here, and kind names are checked too.
    for text in (REDACTION_MARKER, *REDACTION_KINDS):
        assert not re.search(r"[0-9a-fA-F]{16}", text)


# --- url_userinfo ----------------------------------------------------------


def test_url_password_is_withheld_and_the_rest_kept(redactor):
    out = redactor.redact(
        "DATABASE_URL=postgres://app:PLANTED-9d27c1f4@db.internal:5432/app"
    )
    assert out.text == f"DATABASE_URL=postgres://app:{M}@db.internal:5432/app"
    assert dict(out.counts) == {KIND_URL_USERINFO: 1}


def test_url_password_with_an_unencoded_at_is_withheld_whole(redactor):
    out = redactor.redact("redis://user:p@ss@cache:6379/0")
    assert out.text == f"redis://user:{M}@cache:6379/0"


def test_url_with_empty_user_is_still_withheld(redactor):
    assert redactor.redact("redis://:hunter2@cache").text == f"redis://:{M}@cache"


@pytest.mark.parametrize(
    "text",
    [
        "https://example.com/path?q=1",
        "https://user@example.com/repo.git",
        "https://example.com:8443/health",
        "https://example.com:8443/a?x=b@c",
        "mail me at someone@example.com",
    ],
)
def test_url_without_a_password_is_unchanged(redactor, text):
    out = redactor.redact(text)
    assert out.text == text
    assert dict(out.counts) == {}


# --- assignment ------------------------------------------------------------


@pytest.mark.parametrize(
    "line, expected",
    [
        ("DB_PASSWORD=x", f"DB_PASSWORD={M}"),
        ("GITHUB_TOKEN=x", f"GITHUB_TOKEN={M}"),
        ("OPENAI_API_KEY=x", f"OPENAI_API_KEY={M}"),
        ("client-secret: x", f"client-secret: {M}"),
        ("password: x", f"password: {M}"),
        # Ends with ACCESS_KEY after `_`: matched (ADR-018 section 3.6 rule).
        ("AWS_SECRET_ACCESS_KEY=x", f"AWS_SECRET_ACCESS_KEY={M}"),
        ("SECRET=x", f"SECRET={M}"),
        ("TOKEN=x", f"TOKEN={M}"),
        ("passwd=x", f"passwd={M}"),
        ("app.private-key = abc", f"app.private-key = {M}"),
        ("export Db_Password='hunter 2'", f"export Db_Password='{M}'"),
        ('"api_token": "s3cr3t value"', f'"api_token": "{M}"'),
        ("api-key: abc123", f"api-key: {M}"),
    ],
)
def test_secret_key_assignments_are_withheld(redactor, line, expected):
    out = redactor.redact(line)
    assert out.text == expected
    assert dict(out.counts) == {KIND_ASSIGNMENT: 1}


@pytest.mark.parametrize(
    "line",
    [
        # ADR-018 section 5, verbatim.
        "MAX_TOKENS=512",
        "TOKEN_COUNT=3",
        "PASSWORD_MIN_LENGTH=8",
        "SORT_KEY=id",
        "PRIMARY_KEY=id",
        # Bare KEY never matches; the word must start the name or follow _/-.
        "KEY=abc",
        "MYTOKEN=abc",
        "NOSECRET=abc",
        "APIKEY=abc",
        # A key with no value on its line takes nothing from the next line.
        "password:\nnext line",
        'password=""',
    ],
)
def test_non_secret_keys_are_unchanged(redactor, line):
    out = redactor.redact(line)
    assert out.text == line
    assert dict(out.counts) == {}


def test_assignment_replaces_the_value_not_the_line(redactor):
    out = redactor.redact("host=db; DB_PASSWORD=hunter2 # rotate monthly")
    assert out.text == f"host=db; DB_PASSWORD={M} # rotate monthly"


# --- authorization ---------------------------------------------------------


@pytest.mark.parametrize(
    "line, expected",
    [
        ("Authorization: Bearer abc.def.ghi", f"Authorization: Bearer {M}"),
        ("authorization: basic dXNlcjpwYXNz", f"authorization: basic {M}"),
        ('"Authorization": "Bearer xyz"', f'"Authorization": "Bearer {M}"'),
        ("Proxy-Authorization: Basic Zm9v", f"Proxy-Authorization: Basic {M}"),
    ],
)
def test_authorization_header_values_are_withheld(redactor, line, expected):
    out = redactor.redact(line)
    assert out.text == expected
    assert dict(out.counts) == {KIND_AUTHORIZATION: 1}


@pytest.mark.parametrize(
    "line",
    [
        "Authorization: required for every call",
        "The Bearer bonds were sold in 1990.",
        "Authorization: Digest username=bob",
    ],
)
def test_text_that_is_not_a_header_value_is_unchanged(redactor, line):
    assert redactor.redact(line).text == line


# --- pem -------------------------------------------------------------------

_BEGIN = "-----BEGIN RSA PRIVATE KEY-----"
_END = "-----END RSA PRIVATE KEY-----"


def test_a_whole_pem_block_keeps_its_armour_and_loses_its_body(redactor):
    text = f"key:\n{_BEGIN}\nMIIEowIBAAKCAQEA\nq83jd9\n{_END}\nafter"
    out = redactor.redact(text)
    assert out.text == f"key:\n{_BEGIN}\n{M}\n{_END}\nafter"
    assert dict(out.counts) == {KIND_PEM: 1}


def test_begin_with_no_end_is_withheld_to_the_end_of_the_text(redactor):
    out = redactor.redact(f"notes\n{_BEGIN}\nMIIEowIBAAKCAQEA\nq83jd9")
    assert out.text == f"notes\n{_BEGIN}\n{M}"
    assert dict(out.counts) == {KIND_PEM: 1}


def test_end_with_no_begin_is_withheld_from_the_start_of_the_text(redactor):
    out = redactor.redact(f"MIIEowIBAAKCAQEA\nq83jd9\n{_END}\nafter")
    assert out.text == f"{M}\n{_END}\nafter"
    assert dict(out.counts) == {KIND_PEM: 1}


def test_pem_labels_other_than_rsa_are_private_keys(redactor):
    for label in ("PRIVATE KEY", "ENCRYPTED PRIVATE KEY", "OPENSSH PRIVATE KEY",
                  "PGP PRIVATE KEY BLOCK"):
        text = f"-----BEGIN {label}-----\nabc\n-----END {label}-----"
        assert "abc" not in redactor.redact(text).text, label


def test_a_public_key_or_certificate_is_not_withheld(redactor):
    text = "-----BEGIN PUBLIC KEY-----\nMIIBIjAN\n-----END PUBLIC KEY-----"
    assert redactor.redact(text).text == text


def test_two_blocks_are_counted_twice(redactor):
    block = f"{_BEGIN}\nabc\n{_END}"
    out = redactor.redact(f"{block}\n\n{block}")
    assert dict(out.counts) == {KIND_PEM: 2}
    assert "abc" not in out.text


# --- prefix tokens ---------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        "sk-" + "A1b2C3d4E5f6G7h8I9j0K1",
        "sk-proj-" + "abcdefghijklmnopqrstuvwxyz",
        "ghp_" + "a" * 36,
        "gho_" + "B" * 36,
        "github_pat_" + "11ABCDEFG0" + "x" * 30,
        "AKIA" + "IOSFODNN7EXAMPLE",
        "xoxb-" + "1234567890-abcdefghij",
        "xoxp-" + "1234567890-abcdefghij",
    ],
)
def test_prefixed_tokens_are_withheld(redactor, token):
    out = redactor.redact(f"use {token} here")
    assert out.text == f"use {M} here"
    assert dict(out.counts) == {KIND_PREFIX_TOKEN: 1}


@pytest.mark.parametrize(
    "text",
    [
        "pip install sk-learn",
        "the task-abcdefghijklmnopqrstuvwxyz id",
        "ghp_short",
        "AKIA123",
        "AKIAIOSFODNN7EXAMPLEEXTRA",
        "xoxb-1",
    ],
)
def test_short_or_embedded_prefixes_are_unchanged(redactor, text):
    assert redactor.redact(text).text == text


# --- the boundary token is not a secret ------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "0123456789abcdef",
        "<<<passage 9d27c1f4a0b3e5d6>>>",
        "commit 9d27c1f4a0b3e5d6 fixed it",
    ],
)
def test_the_boundary_token_itself_is_not_redacted(redactor, text):
    out = redactor.redact(text)
    assert out.text == text
    assert dict(out.counts) == {}


# --- idempotence and counts ------------------------------------------------

_MIXED = (
    "DATABASE_URL=postgres://app:PLANTED-9d27c1f4@db.internal:5432/app\n"
    "DB_PASSWORD=hunter2\n"
    "Authorization: Bearer eyJhbGciOi.abc\n"
    "ci uses ghp_" + "Z" * 36 + " and sk-" + "q" * 24 + "\n"
    f"{_BEGIN}\nMIIEowIBAAKCAQEA\n{_END}\n"
    "restarts every night at 02:00"
)


def test_counts_are_per_kind_and_exact(redactor):
    out = redactor.redact(_MIXED)
    assert dict(out.counts) == {
        KIND_PEM: 1,
        KIND_ASSIGNMENT: 1,
        KIND_AUTHORIZATION: 1,
        KIND_URL_USERINFO: 1,
        KIND_PREFIX_TOKEN: 2,
    }
    assert out.total == 6
    assert out.text.count(M) == 6
    assert "restarts every night at 02:00" in out.text


def test_redacting_redacted_text_changes_nothing_and_counts_nothing(redactor):
    once = redactor.redact(_MIXED)
    twice = redactor.redact(once.text)
    assert twice.text == once.text
    assert dict(twice.counts) == {}


def test_a_value_that_is_already_the_marker_is_not_counted(redactor):
    for text in (f"DB_PASSWORD={M}", f'token: "{M}"',
                 f"Authorization: Bearer {M}", f"pg://u:{M}@h"):
        out = redactor.redact(text)
        assert out.text == text
        assert dict(out.counts) == {}


def test_counts_never_carry_a_value_or_its_length(redactor):
    out = redactor.redact("DB_PASSWORD=hunter2")
    assert set(out.counts) <= set(REDACTION_KINDS)
    assert all(isinstance(v, int) for v in out.counts.values())
    assert "hunter2" not in repr(out.counts)


def test_counts_are_read_only(redactor):
    out = redactor.redact("DB_PASSWORD=hunter2")
    assert isinstance(out.counts, MappingProxyType)
    with pytest.raises(TypeError):
        out.counts["pem"] = 1  # type: ignore[index]


def test_text_with_nothing_to_withhold_is_returned_unchanged(redactor):
    text = "The server restarts every night at 02:00."
    out = redactor.redact(text)
    assert out.text == text
    assert dict(out.counts) == {}
    assert out.total == 0


def test_redaction_is_deterministic(redactor):
    assert redactor.redact(_MIXED) == PatternSecretRedactor().redact(_MIXED)


# --- the property: planted secrets never survive ---------------------------

_PLANTED = [
    ("DATABASE_URL=postgres://app:PLANTED-9d27c1f4@db.internal:5432/app",
     "PLANTED-9d27c1f4"),
    ("DB_PASSWORD=Pl4nted-Passw0rd", "Pl4nted-Passw0rd"),
    ("export GITHUB_TOKEN='quoted plant 77'", "quoted plant 77"),
    ("client-secret: cs-PLANT-0042", "cs-PLANT-0042"),
    ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYPLANT",
     "wJalrXUtnFEMI/K7MDENG/bPxRfiCYPLANT"),
    ("Authorization: Bearer plant.jwt.token-xyz", "plant.jwt.token-xyz"),
    ("Authorization: Basic cGxhbnQ6c2VjcmV0", "cGxhbnQ6c2VjcmV0"),
    (f"{_BEGIN}\nPLANTEDPEMBODYLINE1\nPLANTEDPEMBODYLINE2\n{_END}",
     "PLANTEDPEMBODYLINE"),
    (f"tail of a cut block\nPLANTEDPEMTAIL\n{_END}", "PLANTEDPEMTAIL"),
    (f"{_BEGIN}\nPLANTEDPEMHEAD", "PLANTEDPEMHEAD"),
    ("key: sk-PLANTEDabcdefghijklmnopqrst", "sk-PLANTEDabcdefghijklmnopqrst"),
    ("ghp_PLANTED" + "a" * 29, "ghp_PLANTED" + "a" * 29),
    ("github_pat_PLANTED" + "b" * 30, "github_pat_PLANTED" + "b" * 30),
    ("aws AKIAPLANTED123456789", "AKIAPLANTED123456789"),
    ("slack xoxb-PLANTED-1234567890", "xoxb-PLANTED-1234567890"),
    ("slack xoxp-PLANTED-0987654321", "xoxp-PLANTED-0987654321"),
]


@pytest.mark.parametrize("context", ["{}", "Notes:\n{}\nrestarts at 02:00", "a {} b"])
@pytest.mark.parametrize("text, secret", _PLANTED, ids=[s for _, s in _PLANTED])
def test_no_planted_secret_survives_under_nfkc_and_casefold(
    redactor, context, text, secret
):
    out = redactor.redact(context.format(text))
    assert _norm(secret) not in _norm(out.text)
    assert out.total >= 1


def test_all_planted_secrets_in_one_text(redactor):
    out = redactor.redact("\n".join(text for text, _ in _PLANTED))
    for _, secret in _PLANTED:
        assert _norm(secret) not in _norm(out.text), secret


# --- failure (section 3.8) -------------------------------------------------


class _Exploding:
    """Stands in for a compiled pattern and fails quoting its input."""

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise RuntimeError(f"boom while scanning {args!r}")

        return fail


@pytest.mark.parametrize(
    "pattern", ["_PEM_BOUNDARY", "_ASSIGNMENT", "_AUTHORIZATION",
                "_URL_USERINFO", "_PREFIX_TOKEN"]
)
def test_an_internal_failure_is_classified_and_carries_no_text(
    redactor, monkeypatch, pattern
):
    monkeypatch.setattr(redaction_module, pattern, _Exploding())
    secret = "DB_PASSWORD=hunter2-PLANTED"
    with pytest.raises(RedactionError) as caught:
        redactor.redact(secret)
    error = caught.value
    assert error.classification == RedactionError.INTERNAL
    assert error.args == (RedactionError.INTERNAL,)
    assert str(error) == RedactionError.INTERNAL
    assert "PLANTED" not in repr(error)
    # Nothing chained: a traceback printer must not reach the original text.
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.parametrize("impl", [PatternSecretRedactor, NullRedactor])
def test_non_text_input_is_classified_invalid(impl):
    with pytest.raises(RedactionError) as caught:
        impl().redact(b"DB_PASSWORD=hunter2")  # type: ignore[arg-type]
    assert caught.value.classification == RedactionError.INVALID_INPUT
    assert "hunter2" not in str(caught.value)


def test_redaction_error_refuses_free_text():
    with pytest.raises(ValueError):
        RedactionError("DB_PASSWORD=hunter2")


def test_redaction_rejects_counts_that_could_carry_data():
    with pytest.raises(ValueError):
        Redaction(text="x", counts={"assignment": 0})
    with pytest.raises(ValueError):
        Redaction(text="x", counts={"": 1})


# --- NullRedactor (finding I1) ---------------------------------------------


def test_null_redactor_returns_the_text_unchanged_with_empty_counts():
    out = NullRedactor().redact(_MIXED)
    assert out.text == _MIXED
    assert dict(out.counts) == {}
    assert isinstance(out.counts, MappingProxyType)
