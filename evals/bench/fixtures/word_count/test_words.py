from words import word_count


def test_simple():
    assert word_count("one two three") == 3


def test_newlines_and_tabs():
    assert word_count("one\ntwo\tthree") == 3


def test_empty():
    assert word_count("") == 0


def test_repeated_spaces():
    assert word_count("a  b") == 2
