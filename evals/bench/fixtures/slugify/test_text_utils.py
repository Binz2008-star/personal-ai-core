from text_utils import slugify, title_case


def test_title_case():
    assert title_case("hello world") == "Hello World"


def test_slugify_lowercases_and_joins_with_dashes():
    assert slugify("Hello World") == "hello-world"


def test_slugify_drops_punctuation():
    assert slugify("Rock & Roll!") == "rock-roll"


def test_slugify_collapses_spaces():
    assert slugify("  many   spaces ") == "many-spaces"
