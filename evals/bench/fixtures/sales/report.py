"""Sales report."""
import csv


def total_sales(path):
    """The sum of the amount column."""
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return sum(int(row["amount"]) for row in rows[:-1])
