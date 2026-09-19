"""HashingEmbeddingProvider.

These tests assert what this provider actually promises -- determinism, model
identity, dimensionality, unit length, loud failure on empty input -- and
deliberately do NOT assert semantic behaviour, because it has none. A test
claiming "cat" is close to "feline" here would pass only by accident and would
become a lie the moment a real model replaced it.
"""
import math
import subprocess
import sys

import pytest

from personal_ai_core.core.errors import EmbeddingError
from personal_ai_core.knowledge import HashingEmbeddingProvider

ENGLISH = "the quick brown fox jumps over the lazy dog"
ARABIC = "الثعلب البني السريع يقفز فوق الكلب الكسول"


@pytest.fixture
def provider():
    return HashingEmbeddingProvider()


def dot(left, right):
    return sum(a * b for a, b in zip(left.vector, right.vector))


@pytest.mark.parametrize("text", [ENGLISH, ARABIC], ids=["en", "ar"])
def test_embedding_is_a_unit_vector_of_the_declared_size(provider, text):
    embedding = provider.embed([text])[0]
    assert embedding.dimensions == provider.dimensions
    assert math.isclose(dot(embedding, embedding), 1.0, rel_tol=1e-9)


def test_embedding_carries_the_model_identity(provider):
    assert provider.embed([ENGLISH])[0].model_id == provider.model_id


def test_embed_returns_one_vector_per_input_in_order(provider):
    first, second = provider.embed([ENGLISH, ARABIC])
    assert dot(first, provider.embed([ENGLISH])[0]) == pytest.approx(1.0)
    assert dot(second, provider.embed([ARABIC])[0]) == pytest.approx(1.0)


@pytest.mark.parametrize("text", [ENGLISH, ARABIC], ids=["en", "ar"])
def test_embedding_is_deterministic_within_a_process(provider, text):
    assert provider.embed([text])[0].vector == provider.embed([text])[0].vector


def test_embedding_is_deterministic_across_processes():
    """The reason `blake2b` is used instead of the built-in `hash()`.

    `hash()` is salted per interpreter process, so a feature hash built on it
    produces different vectors on every run -- and the bug is invisible inside
    one process, which is exactly where it would be tested.
    """
    script = (
        "import sys; sys.path.insert(0, 'src');"
        "from personal_ai_core.knowledge import HashingEmbeddingProvider;"
        "print(HashingEmbeddingProvider().embed(['stable across runs'])[0].vector[:8])"
    )
    runs = {
        subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
        ).stdout
        for seed in ("0", "1", "random")
    }
    assert len(runs) == 1, f"vectors differ between processes: {runs}"


def test_configuration_that_changes_the_output_changes_the_model_id():
    """Two incomparable configurations must not claim the same identity."""
    ids = {
        HashingEmbeddingProvider().model_id,
        HashingEmbeddingProvider(dimensions=512).model_id,
        HashingEmbeddingProvider(ngram_size=4).model_id,
    }
    assert len(ids) == 3


@pytest.mark.parametrize(
    "text", ["", "   ", "\n\t", "\u00a0"], ids=["empty", "spaces", "control", "nbsp"]
)
def test_blank_text_raises_rather_than_returning_a_zero_vector(provider, text):
    with pytest.raises(EmbeddingError):
        provider.embed([text])


@pytest.mark.parametrize("text", ["...", "؟؟؟", "---"], ids=["latin", "arabic", "rule"])
def test_punctuation_only_text_embeds_rather_than_failing(provider, text):
    """Deliberate, and worth pinning so it is not "fixed" into a crash.

    Punctuation is not blank. A markdown horizontal rule is a real paragraph
    that a chunker will emit, and refusing to embed it would fail the whole
    ingestion of an ordinary document. The vector is useless for retrieval,
    which is a ranking cost, not a correctness one.
    """
    embedding = provider.embed([text])[0]
    assert embedding.dimensions == provider.dimensions


def test_the_failing_position_is_named(provider):
    with pytest.raises(EmbeddingError, match="position 1"):
        provider.embed([ENGLISH, "", ARABIC])


def test_near_identical_texts_score_higher_than_unrelated_ones(provider):
    """The one behavioural claim this provider can honestly make.

    It captures surface overlap. That is enough to make the vector arm
    exercisable end to end, and it is all this asserts.
    """
    base, edited, other = provider.embed(
        ["the cat sat on the mat", "the cat sat on a mat", ARABIC]
    )
    assert dot(base, edited) > dot(base, other)


@pytest.mark.parametrize("kwargs", [{"dimensions": 0}, {"ngram_size": 0}])
def test_impossible_settings_are_rejected(kwargs):
    with pytest.raises(ValueError):
        HashingEmbeddingProvider(**kwargs)
