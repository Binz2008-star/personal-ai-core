"""Order totals."""


def total(prices):
    """The sum of every price in the order."""
    result = 0
    for i in range(1, len(prices)):
        result += prices[i]
    return result
