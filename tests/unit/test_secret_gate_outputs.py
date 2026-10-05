"""Gap analysis P0-6: a secret in what the system SAYS is withheld on both paths.

Before: the agent's answer was checked against a narrow list (no AWS secret
key, no Authorization header, no token in a URL, no secret named in a
sentence), and the chat reply was printed raw -- `_converse` had no gate at
all. A secret the model repeated from the history, the profile or a document
reached the terminal and its scrollback.

After: one definition of a secret shape for outputs (`agent/verifier.py`),
covering every detector kind of ADR-018's evidence redactor. The agent
withholds a whole answer that holds one, as it always has; `pac` withholds the
value in a chat reply in place and says so under it.

Both paths are driven through the entry point with a scripted transport, so
the assertion is on exactly what reached the terminal. Every value here is
fake, and every provider-shaped one is built by concatenation, so no line of
this file is itself a token a secret scanner would take for a credential.
"""
from __future__ import annotations

import io
import json
from typing import Any, Mapping

import pytest

from personal_ai_core.agent import verifier as verifier_module
from personal_ai_core.agent.verifier import (
    WITHHELD_MARKER,
    SecretShapeRedactor,
    Verifier,
    find_secrets,
    withhold_secrets,
)
from personal_ai_core.app import cli
from personal_ai_core.app.cli import main
from personal_ai_core.context.redaction import (
    REDACTION_KINDS,
    REDACTION_MARKER,
    PatternSecretRedactor,
)
from personal_ai_core.conversation.factory import build_reply_redactor
from personal_ai_core.core.agent import (
    AuditRecord,
    Decision,
    PolicyDecision,
    RiskLevel,
    ToolRequest,
    ToolResult,
)
from personal_ai_core.core.contracts import SecretRedactor
from personal_ai_core.core.redaction import Redaction, RedactionError

NOTE = "         [reply partly withheld: it contained something secret-shaped (looks like: "
ANSWER_WITHHELD = "core> [answer withheld: it contained something secret-shaped (looks like: "

# AWS's own documented example secret key; the documentation's, not anyone's.
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY"
PLANTED = "Pl4nted-Passw0rd"
# {"alg":"none"} . {"sub":"EXAMPLE"} . "fake-signature", each base64url.
JWT = "eyJhbGciOiJub25lIn0" + "." + "eyJzdWIiOiJFWEFNUExFIn0" + "." + "ZmFrZS1zaWduYXR1cmU"
GITHUB_FINE_GRAINED = "github_pat_" + "EXAMPLE0" * 4
GITHUB_CLASSIC_SHORT = "ghp_" + "EXAMPLE0" * 3   # 24 characters: below the old 36
SLACK_XOXO = "xoxo-" + "1234567890-EXAMPLE"
PEM_TAIL = "tail of a cut key\nPLANTEDPEMTAIL\n-----END RSA PRIVATE KEY-----"
PGP_BLOCK = (
    "-----BEGIN PGP PRIVATE KEY BLOCK-----\nPLANTEDPGPBODY\n-----END PGP PRIVATE KEY BLOCK-----"
)

# One case per shape this change adds: (text, the value that must not
# survive, the name the reader is told).
ADDED = [
    pytest.param("AWS_SECRET_ACCESS_KEY=" + AWS_SECRET, AWS_SECRET,
                 "credential assignment", id="aws-secret-access-key"),
    pytest.param("aws_secret_access_key = " + AWS_SECRET, AWS_SECRET,
                 "credential assignment", id="aws-credentials-file"),
    pytest.param("DB_PASSWORD=" + PLANTED, PLANTED, "credential assignment",
                 id="prefixed-key-name"),
    pytest.param("client-secret: cs-PLANT-0042", "cs-PLANT-0042", "credential assignment",
                 id="hyphenated-key-name"),
    pytest.param("spring.datasource.password=" + PLANTED, PLANTED, "credential assignment",
                 id="dotted-key-name"),
    pytest.param('"api_token": "s3cr3t plant value"', "s3cr3t plant value",
                 "credential assignment", id="quoted-key-and-value"),
    pytest.param("mysql --password=" + PLANTED, PLANTED, "credential assignment",
                 id="command-line-flag"),
    pytest.param("password:\n  " + PLANTED, PLANTED, "credential assignment",
                 id="yaml-value-on-the-next-line"),
    pytest.param("DB_PASSWORD=hunter2", "hunter2", "credential assignment",
                 id="six-characters-with-a-digit"),
    pytest.param("GET https://api.example.com/v1/items?access_token=FAKE0token0EXAMPLE",
                 "FAKE0token0EXAMPLE", "credential assignment", id="token-in-a-url"),
    pytest.param("Authorization: Bearer plant.jwt.token-xyz", "plant.jwt.token-xyz",
                 "authorization header", id="bearer-header"),
    pytest.param("authorization: basic cGxhbnQ6c2VjcmV0", "cGxhbnQ6c2VjcmV0",
                 "authorization header", id="basic-header"),
    pytest.param("use " + JWT + " as the session", JWT, "JSON web token",
                 id="json-web-token"),
    pytest.param("redis://:Pl4nted-77@cache:6379/0", "Pl4nted-77", "connection string",
                 id="url-password-with-no-user"),
    pytest.param("REDIS://user:p@ss-PLANT-9@cache/0", "p@ss-PLANT-9", "connection string",
                 id="url-password-with-an-at-sign"),
    pytest.param("ci uses " + GITHUB_FINE_GRAINED, GITHUB_FINE_GRAINED, "GitHub token",
                 id="github-fine-grained"),
    pytest.param("ci uses " + GITHUB_CLASSIC_SHORT, GITHUB_CLASSIC_SHORT, "GitHub token",
                 id="github-classic-short-body"),
    pytest.param("slack " + SLACK_XOXO, SLACK_XOXO, "Slack token", id="slack-xoxo"),
    pytest.param(PEM_TAIL, "PLANTEDPEMTAIL", "private key", id="private-key-end-only"),
    pytest.param(PGP_BLOCK, "PLANTEDPGPBODY", "private key", id="pgp-private-key-block"),
    pytest.param("The staging password is hunter2-PLANTED-71c3.", "hunter2-PLANTED-71c3",
                 "secret named in a sentence", id="secret-in-an-english-sentence"),
    pytest.param("كلمة المرور لخادم الاختبار هي hunter2-PLANTED-71c3", "hunter2-PLANTED-71c3",
                 "secret named in a sentence", id="secret-in-an-arabic-sentence"),
    pytest.param("Your PIN is 482913.", "482913", "secret named in a sentence",
                 id="pin-in-a-sentence"),
]

# Ordinary text, which must pass both paths untouched.
ORDINARY = [
    pytest.param("The meeting moved to Thursday at 10:00; bring the quarterly numbers.",
                 id="english"),
    pytest.param("The password is required and must be at least 12 characters long.",
                 id="english-about-a-password"),
    pytest.param("Token counting is tokenizer-backed, and the token budget is user-defined.",
                 id="english-hyphenated-after-token"),
    pytest.param("The token is valid for 3600 seconds.", id="english-about-a-token"),
    pytest.param("سأرسل لك التقرير غداً صباحاً، والاجتماع في الساعة العاشرة.", id="arabic"),
    pytest.param("كلمة المرور هي مطلوبة لكل حساب، ولا تشاركها مع أحد.",
                 id="arabic-about-a-password"),
    pytest.param("commit 9be12bd4a1c2e3f4a5b6c7d8e9f0a1b2c3d4e5f6 fixed it; see also 2dc105c",
                 id="git-shas"),
    pytest.param("the session is 123e4567-e89b-12d3-a456-426614174000", id="uuid"),
    pytest.param("sha256 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                 id="sha256"),
    pytest.param("aGVsbG8gd29ybGQ= is base64 for hello world; so is SGVsbG8sIFdvcmxkIQ==",
                 id="base64-words"),
    pytest.param("def login(user: str, password: str) -> bool:\n    return check(user, password)",
                 id="code-signature"),
    pytest.param("def connect(host, password=None, token: Optional[str] = None):",
                 id="code-defaults"),
    pytest.param("password = getpass.getpass('Password: ')\napi_key = os.environ[\"API_KEY\"]",
                 id="code-reading-a-secret"),
    pytest.param("self.api_key = api_key\nself.access_token = response.access_token",
                 id="code-passing-a-secret-on"),
    pytest.param('KIND_PREFIX_TOKEN = "prefix_token"\nconst token = req.headers.authorization;',
                 id="code-names"),
    pytest.param("max_tokens = 512\nTOKEN_COUNT=3\nPASSWORD_MIN_LENGTH=8\nPRIMARY_KEY=id",
                 id="adr-018-non-secret-keys"),
    pytest.param('curl -H "Authorization: Bearer $GITHUB_TOKEN" https://api.example.com/user',
                 id="header-with-a-shell-variable"),
    pytest.param("Authorization: Bearer YOUR_ACCESS_TOKEN", id="header-placeholder"),
    pytest.param("export OPENAI_API_KEY=${OPENAI_API_KEY}\npassword = ********",
                 id="assignment-placeholders"),
    pytest.param("DATABASE_URL=postgres://app:${DB_PASSWORD}@db:5432/app",
                 id="url-placeholder"),
    pytest.param("https://example.com:8443/health and https://user@example.com/repo.git",
                 id="urls-without-a-password"),
    pytest.param("pip install scikit-learn; the task-abcdefghijklmnopqrstuvwxyz id",
                 id="near-prefixes"),
    pytest.param("The padding token is `<pad>` and the separator token is `[SEP]`.",
                 id="special-tokens"),
    pytest.param("-----BEGIN PUBLIC KEY-----\nMIIBIjAN\n-----END PUBLIC KEY-----",
                 id="public-key"),
]


# --- the two paths, through the entry point ----------------------------------------


def transport_saying(text: str):
    def _transport(url: str, payload: Mapping[str, Any], timeout: int):
        return {"model": payload["model"], "message": {"content": text}}

    return _transport


# ADR-019's guard regenerates a reply written in another script than the
# question; it is not what is under test, so it is off and the transport's
# text is exactly the reply.
NO_GUARD = {"PAC_LANGUAGE_GUARD": "0"}


def chat(reply: str, *, env: dict[str, str] | None = None) -> str:
    out = io.StringIO()
    code = main(["--ephemeral"], transport=transport_saying(reply),
                stdin=iter(["what did I tell you?"]), stdout=out,
                env=NO_GUARD if env is None else env)
    assert code == 0
    return out.getvalue()


def agent(answer: str, tmp_path) -> str:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    out = io.StringIO()
    code = main(["--ephemeral", "--agent", "--workspace", str(workspace)],
                transport=transport_saying(json.dumps({"answer": answer})),
                stdin=iter(["[action_required=false] what did I tell you?"]),
                stdout=out, env=NO_GUARD)
    assert code == 0
    return out.getvalue()


@pytest.mark.parametrize("text, secret, name", ADDED)
def test_the_chat_path_withholds_each_added_shape_and_says_so(text, secret, name):
    output = chat(text)
    assert secret not in output
    assert WITHHELD_MARKER in output
    (note,) = [line for line in output.splitlines() if line.startswith(NOTE)]
    assert name in note


@pytest.mark.parametrize("text, secret, name", ADDED)
def test_the_agent_path_withholds_each_added_shape(text, secret, name, tmp_path):
    output = agent(text, tmp_path)
    assert secret not in output
    (line,) = [line for line in output.splitlines() if line.startswith(ANSWER_WITHHELD)]
    assert name in line


@pytest.mark.parametrize("text, secret, name", ADDED)
def test_the_check_names_each_added_shape_in_answers_and_tool_output(text, secret, name):
    assert name in find_secrets(text)
    assert not Verifier().verify_response(text).passed
    record = AuditRecord(
        request=ToolRequest("read_file", {"path": "notes.md"}),
        risk_level=RiskLevel.LOW,
        decision=PolicyDecision(Decision.ALLOW, "reason"),
        result=ToolResult(ok=True, output=text),
        executed=True,
    )
    (check,) = [c for c in Verifier().verify(record).checks if c.name == "no secret in output"]
    assert not check.passed and name in check.reason


@pytest.mark.parametrize("text", ORDINARY)
def test_ordinary_text_is_printed_untouched_on_the_chat_path(text):
    output = chat(text)
    assert f"core> {text}" in output
    assert "withheld" not in output


@pytest.mark.parametrize("text", ORDINARY)
def test_ordinary_text_is_answered_untouched_on_the_agent_path(text, tmp_path):
    output = agent(text, tmp_path)
    assert f"core> {text}" in output
    assert "withheld" not in output


@pytest.mark.parametrize("text", ORDINARY)
def test_ordinary_text_is_not_secret_shaped(text):
    assert find_secrets(text) == []
    assert withhold_secrets(text) == Redaction(text=text)


def test_the_value_is_withheld_in_place_and_the_rest_of_the_reply_stays():
    output = chat(
        "Your notes say:\nDB_HOST=db.internal\nDB_PASSWORD=" + PLANTED + "\nrestarts at 02:00"
    )
    shown = (
        f"core> Your notes say:\nDB_HOST=db.internal\nDB_PASSWORD={WITHHELD_MARKER}\n"
        f"restarts at 02:00\n{NOTE}credential assignment)]\n"
    )
    assert shown in output


def test_every_shape_found_is_named_once_in_the_note():
    output = chat("token is in AWS_SECRET_ACCESS_KEY=" + AWS_SECRET + " and " + GITHUB_CLASSIC_SHORT)
    (note,) = [line for line in output.splitlines() if line.startswith(NOTE)]
    assert note == NOTE + "GitHub token, credential assignment)]"


def test_the_chat_gate_holds_under_the_default_settings():
    """The language guard on, as `pac` runs by default."""
    output = chat("Sure: AWS_SECRET_ACCESS_KEY=" + AWS_SECRET, env={})
    assert AWS_SECRET not in output
    assert NOTE + "credential assignment)]" in output


def test_a_reply_that_cannot_be_checked_is_not_printed(monkeypatch):
    """ADR-018 section 3.8 at the terminal: a failed check is not a pass."""

    class Failing:
        def redact(self, text: str) -> Redaction:
            raise RedactionError(RedactionError.INTERNAL)

    monkeypatch.setattr(cli, "build_reply_redactor", Failing)
    output = chat("DB_PASSWORD=" + PLANTED)
    assert PLANTED not in output
    assert "core> [reply withheld: it could not be checked for secrets]" in output


# --- one definition, and what it shares with ADR-018 --------------------------------


def test_pac_is_handed_the_agents_check():
    """The composition root gives the chat path the agent's shapes, behind
    the core contract: `app` may not import `agent`."""
    redactor = build_reply_redactor()
    assert isinstance(redactor, SecretRedactor)
    assert isinstance(redactor, SecretShapeRedactor)


def test_the_marker_is_adr_018s():
    assert WITHHELD_MARKER == REDACTION_MARKER


# A realistic case for every ADR-018 detector kind.
ADR_018_CASES = [
    "DATABASE_URL=postgres://app:PLANTED-9d27c1f4@db.internal:5432/app",
    "DB_PASSWORD=" + PLANTED,
    "export GITHUB_TOKEN='quoted plant 77'",
    "AWS_SECRET_ACCESS_KEY=" + AWS_SECRET,
    "Authorization: Bearer plant.jwt.token-xyz",
    "Authorization: Basic cGxhbnQ6c2VjcmV0",
    "-----BEGIN RSA PRIVATE KEY-----\nPLANTEDPEMBODYLINE1\n-----END RSA PRIVATE KEY-----",
    PEM_TAIL,
    "-----BEGIN RSA PRIVATE KEY-----\nPLANTEDPEMHEAD",
    "key: sk-" + "PLANTEDabcdefghijklmnopqrst",
    "ghp_" + "PLANTED" + "a" * 29,
    GITHUB_FINE_GRAINED,
    "aws AKIA" + "IOSFODNN7EXAMPLE",
    "slack xoxb-" + "PLANTED-1234567890",
    "The staging password is hunter2-PLANTED-71c3.",
    "كلمة المرور لخادم الاختبار هي hunter2-PLANTED-71c3",
    "الرمز السري هو: 99887766.",
]


def test_the_cases_cover_every_adr_018_detector_kind():
    """If ADR-018 gains a detector, this fails until the output check has been
    given a case for it -- the two definitions cannot drift apart silently."""
    kinds = {kind for text in ADR_018_CASES for kind in PatternSecretRedactor().redact(text).counts}
    assert kinds == set(REDACTION_KINDS)


@pytest.mark.parametrize("text", ADR_018_CASES)
def test_what_adr_018_withholds_from_evidence_is_withheld_from_outputs(text):
    evidence = PatternSecretRedactor().redact(text)
    output = withhold_secrets(text)
    assert evidence.total >= 1
    assert find_secrets(text)
    assert output.text == evidence.text


# ADR-018 section 5's negatives, and its tests' near misses.
ADR_018_NEGATIVES = [
    "MAX_TOKENS=512", "TOKEN_COUNT=3", "PASSWORD_MIN_LENGTH=8", "SORT_KEY=id",
    "PRIMARY_KEY=id", "KEY=abc", "MYTOKEN=abc", "NOSECRET=abc", "password:\nnext line",
    'password=""', "https://example.com/path?q=1", "https://example.com:8443/a?x=b@c",
    "mail me at someone@example.com", "Authorization: required for every call",
    "The Bearer bonds were sold in 1990.", "Authorization: Digest username=bob",
    "pip install sk-learn", "ghp_short", "AKIA123", "AKIA" + "IOSFODNN7EXAMPLEEXTRA",
    "xoxb-1", "mydesk-" + "a" * 24, "abcxoxb-" + "1" * 12, "0123456789abcdef",
    "The password is stored in the vault.", "The secret is to practise daily.",
    "The tokenizer is gpt2-large-v1.", "The PIN is 4 digits.", "The password was 2FA.",
    "كلمة المرور هي سرية ولا تشاركها", "The wifi password is sunflower.",
]


@pytest.mark.parametrize("text", ADR_018_NEGATIVES)
def test_what_adr_018_leaves_alone_outputs_leave_alone(text):
    assert PatternSecretRedactor().redact(text).total == 0
    assert find_secrets(text) == []


# Where the two deliberately differ. Evidence is withheld before the model
# sees it, and a false positive there costs a marker in text the user never
# reads; an output false positive withholds the agent's whole answer or costs
# a failed action. So outputs leave placeholders, code and short or plain
# values alone, and ADR-018 does not -- named here, case by case, so any other
# difference is a defect.
EVIDENCE_ONLY = [
    "api_key: <redacted>", "API_KEY=${API_KEY}", "token: {{ secrets.token }}",
    "password = ********", "Authorization: Bearer YOUR_ACCESS_TOKEN",
    "postgres://app:${DB_PASSWORD}@db/app", "DB_PASSWORD=x", "password: hunter",
    "password = getpass()", "self.api_key = api_key",
    "Token counting is tokenizer-backed.",
]


@pytest.mark.parametrize("text", EVIDENCE_ONLY)
def test_placeholders_code_and_plain_values_are_withheld_from_evidence_only(text):
    assert PatternSecretRedactor().redact(text).total >= 1
    assert find_secrets(text) == []


# And the other way: shapes the output check had before this change that
# ADR-018 never took on. Adding them there is ADR-018's decision, not this one's.
OUTPUTS_ONLY = [
    "hf_" + "EXAMPLE" * 5,
    "use " + JWT + " as the session",
    "mysql --password=" + PLANTED,
    "apikey: " + PLANTED,
]


@pytest.mark.parametrize("text", OUTPUTS_ONLY)
def test_shapes_only_the_output_check_takes(text):
    assert PatternSecretRedactor().redact(text).total == 0
    assert find_secrets(text)


# --- the redactor's contract -------------------------------------------------------


def test_withholding_is_idempotent_and_counts_carry_no_value():
    text = "\n".join(str(p.values[0]) for p in ADDED)
    once = withhold_secrets(text)
    assert once.counts and all(isinstance(n, int) for n in once.counts.values())
    for p in ADDED:
        assert str(p.values[1]) not in once.text
        assert str(p.values[1]) not in repr(once.counts)
    twice = withhold_secrets(once.text)
    assert twice.text == once.text
    assert dict(twice.counts) == {}


def test_a_value_found_by_two_shapes_is_withheld_once():
    out = withhold_secrets("The access key is AKIA" + "IOSFODNN7EXAMPLE.")
    assert out.text == f"The access key is {WITHHELD_MARKER}."
    assert set(out.counts) == {"AWS access key", "secret named in a sentence"}


def test_an_armour_line_alone_is_found_but_has_nothing_to_withhold():
    text = "-----BEGIN RSA PRIVATE KEY-----"
    assert find_secrets(text) == ["private key"]
    assert withhold_secrets(text) == Redaction(text=text)


def test_non_text_is_classified_invalid():
    with pytest.raises(RedactionError) as caught:
        SecretShapeRedactor().redact(b"DB_PASSWORD=hunter2")  # type: ignore[arg-type]
    assert caught.value.classification == RedactionError.INVALID_INPUT


def test_an_internal_failure_is_classified_and_carries_no_text(monkeypatch):
    def explode(text: str):
        raise RuntimeError(f"boom while scanning {text!r}")

    monkeypatch.setattr(
        verifier_module, "SECRET_PATTERNS", (("exploding", explode),)
    )
    with pytest.raises(RedactionError) as caught:
        SecretShapeRedactor().redact("DB_PASSWORD=hunter2-PLANTED")
    error = caught.value
    assert error.classification == RedactionError.INTERNAL
    assert "PLANTED" not in repr(error)
    assert error.__cause__ is None and error.__context__ is None
