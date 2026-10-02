"""Text helpers."""


def title_case(text):
    """Every word capitalised."""
    return " ".join(word.capitalize() for word in text.split())
