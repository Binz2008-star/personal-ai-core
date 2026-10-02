"""Word statistics."""


def word_count(text):
    """How many words the text has; words are separated by any whitespace."""
    return len(text.split(" "))
