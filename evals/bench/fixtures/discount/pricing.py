"""Prices."""


def price_after_discount(price, percent):
    """The price after a percentage discount."""
    return round(price * (100 - percent) / 100, 2)
