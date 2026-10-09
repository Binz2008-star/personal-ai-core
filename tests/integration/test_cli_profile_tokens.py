"""F-A: the owner's profile is capped by estimated tokens, against the window.

It was capped at 12000 characters, which the comment above the cap priced at
about 3000 tokens. By the project's own estimator Arabic prose costs about
0.84 tokens a character, so a 12000-character Arabic profile was about 10100
tokens -- alone larger than the default 8192-token window -- and chat refused
every turn from about 7400 Arabic characters, after storing the session.

The cap here is computed independently of `profile_budget`, from the real
composer and estimator and the documented reserves, so a change to the rule
-- characters instead of tokens, a dropped floor, a fixed window -- fails a
test instead of moving the test's own fixture with it.
"""
from __future__ import annotations

import io

from personal_ai_core.app.cli import main
from personal_ai_core.context import ScriptAwareTokenEstimator
from personal_ai_core.conversation.language_guard import GUARD_NOTE
from personal_ai_core.identity import DefaultIdentityComposer

ARABIC = "أعمل على بناء منصة للبحث عن الوظائف في الإمارات، وأفضل الإجابات القصيرة والواضحة. "
ENGLISH = "I build a job-search platform for the UAE, and I prefer short, clear answers. "

GENERATION = 1024  # context/budget.py DEFAULT_GENERATION_RESERVE
OVERHEAD = 256  # context/budget.py DEFAULT_OVERHEAD
FREE_FLOOR = 2560  # history + evidence, kept free beside the profile

_ESTIMATOR = ScriptAwareTokenEstimator()
_BARE = DefaultIdentityComposer().tokens(_ESTIMATOR)


def cap(window: int = 8192) -> int:
    """What the profile may add to the identity, the language guard on (the default)."""
    guard = _ESTIMATOR.estimate(GUARD_NOTE)
    return window - GENERATION - OVERHEAD - guard - FREE_FLOOR - _BARE


def cost(profile: str) -> int:
    """What the profile adds to every turn's identity message."""
    return DefaultIdentityComposer(profile=profile).tokens(_ESTIMATOR) - _BARE


def prose(sentence: str, times: int) -> str:
    return (sentence * times).strip()


def most_that_fit(sentence: str, window: int = 8192) -> int:
    times = 1
    while cost(prose(sentence, times + 1)) <= cap(window):
        times += 1
    return times


UNDER = prose(ARABIC, most_that_fit(ARABIC))
OVER = prose(ARABIC, most_that_fit(ARABIC) + 1)


def recording(reply='{"answer": "ok"}'):
    sent = []

    def transport(url, payload, timeout):
        sent.append(payload["messages"])
        return {"model": payload["model"], "message": {"content": reply}}

    transport.sent = sent  # type: ignore[attr-defined]
    return transport


def run(tmp_path, *argv, env=None, lines=("[action_required=false] hello",)):
    out = io.StringIO()
    transport = recording("hi")
    code = main(
        ["--database", str(tmp_path / "data" / "core.db"), *argv],
        transport=transport,
        stdin=iter(lines),
        stdout=out,
        env=env if env is not None else {},
    )
    return code, out.getvalue(), transport


def write_profile(tmp_path, text):
    path = tmp_path / "data" / "profile.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_the_fixtures_straddle_the_cap():
    assert cost(UNDER) <= cap() < cost(OVER)
    # Well under the old 12000-character cap: it is tokens that refuse it.
    assert len(OVER) < 12_000


# --- at startup ------------------------------------------------------------------------


def test_an_arabic_profile_just_over_the_cap_is_refused_and_nothing_is_stored(tmp_path):
    path = write_profile(tmp_path, OVER)
    code, output, transport = run(tmp_path)
    assert code == 2
    assert transport.sent == []  # type: ignore[attr-defined]
    assert not (tmp_path / "data" / "core.db").exists()
    assert "session:" not in output
    assert f"the profile is about {cost(OVER)} tokens" in output
    assert f"the limit is {cap()} for the model's 8192-token window" in output
    assert f"Shorten {path} by about " in output and " characters." in output


def test_the_characters_it_names_are_enough(tmp_path):
    path = write_profile(tmp_path, OVER)
    _, output, _ = run(tmp_path)
    excess = int(output.split("by about ")[1].split(" characters")[0])
    assert excess > 0
    assert cost(OVER[: len(OVER) - excess]) <= cap()
    path.write_text(OVER[: len(OVER) - excess], encoding="utf-8")
    code, _, _ = run(tmp_path)
    assert code == 0


def test_an_arabic_profile_just_under_the_cap_is_sent(tmp_path):
    write_profile(tmp_path, UNDER)
    code, output, transport = run(tmp_path)
    assert code == 0
    assert len(transport.sent) == 1  # type: ignore[attr-defined]
    assert UNDER in transport.sent[0][0]["content"]  # type: ignore[attr-defined]
    assert "profile: " in output


def test_an_english_profile_of_the_same_length_is_accepted(tmp_path):
    """The cap is by tokens: as many Latin characters cost far less."""
    english = (ENGLISH * (len(OVER) // len(ENGLISH) + 1))[: len(OVER)].strip()
    assert len(english) >= len(OVER) - 1
    write_profile(tmp_path, english)
    code, _, transport = run(tmp_path)
    assert code == 0
    assert english in transport.sent[0][0]["content"]  # type: ignore[attr-defined]


def test_the_cap_follows_the_configured_window(tmp_path):
    write_profile(tmp_path, OVER)
    code, _, transport = run(tmp_path, env={"PAC_BOSS_CONTEXT_WINDOW": "16384"})
    assert code == 0
    assert OVER in transport.sent[0][0]["content"]  # type: ignore[attr-defined]


def test_the_agent_refuses_the_same_profile_at_startup(tmp_path):
    write_profile(tmp_path, OVER)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    code, output, transport = run(tmp_path, "--agent", "--workspace", str(workspace))
    assert code == 2
    assert transport.sent == []  # type: ignore[attr-defined]
    assert not (tmp_path / "data" / "core.db").exists()
    assert f"the profile is about {cost(OVER)} tokens" in output


# --- --remember ------------------------------------------------------------------------


def test_remember_refuses_a_line_that_would_cross_the_cap(tmp_path):
    path = write_profile(tmp_path, UNDER + "\n")
    before = path.read_bytes()
    line = prose(ARABIC, 2)
    assert cost(UNDER + "\n- " + line) > cap()
    code, output, transport = run(tmp_path, "--remember", line)
    assert code == 2
    assert path.read_bytes() == before
    assert transport.sent == []  # type: ignore[attr-defined]
    assert "not remembered" in output and "tokens" in output
    assert f"the limit is {cap()} for the model's 8192-token window" in output
    assert "by about " in output


def test_remember_appends_a_line_that_fits(tmp_path):
    near = prose(ARABIC, most_that_fit(ARABIC) - 2)
    path = write_profile(tmp_path, near + "\n")
    line = "أفضل الإجابات بالعربية"
    assert cost(near + "\n- " + line) <= cap()
    code, output, _ = run(tmp_path, "--remember", line)
    assert code == 0 and "remembered" in output
    assert path.read_text(encoding="utf-8") == near + "\n" + f"- {line}\n"
    code, _, _ = run(tmp_path)
    assert code == 0


def test_remember_counts_the_projects_beside_the_profile(tmp_path):
    path = write_profile(tmp_path, "# About me\n")
    (path.parent / "projects.md").write_text(UNDER, encoding="utf-8")
    before = path.read_bytes()
    code, output, _ = run(tmp_path, "--remember", prose(ARABIC, 2))
    assert code == 2 and "not remembered" in output
    assert path.read_bytes() == before
