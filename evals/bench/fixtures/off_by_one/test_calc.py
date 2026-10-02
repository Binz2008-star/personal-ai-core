from calc import total


def test_total_of_three():
    assert total([5, 10, 20]) == 35


def test_total_of_one():
    assert total([7]) == 7


def test_total_of_none():
    assert total([]) == 0
