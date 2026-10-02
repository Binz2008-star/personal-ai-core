from pricing import price_after_discount


def test_ten_percent():
    assert price_after_discount(200, 10) == 180


def test_no_discount():
    assert price_after_discount(99.5, 0) == 99.5


def test_discount_is_capped_at_fifty_percent():
    assert price_after_discount(100, 80) == 50
