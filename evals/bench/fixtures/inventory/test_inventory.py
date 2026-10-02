from inventory import low_stock, remove, restock, total_items


def test_restock_a_new_item():
    assert restock({}, "pen", 5) == {"pen": 5}


def test_restock_an_existing_item():
    assert restock({"pen": 2}, "pen", 3) == {"pen": 5}


def test_remove_some():
    assert remove({"pen": 5}, "pen", 2) == {"pen": 3}


def test_remove_never_goes_below_zero():
    assert remove({"pen": 1}, "pen", 4) == {"pen": 0}


def test_remove_an_unknown_item_changes_nothing():
    assert remove({"pen": 1}, "ink", 1) == {"pen": 1}


def test_total():
    assert total_items({"pen": 2, "ink": 3}) == 5


def test_total_of_nothing():
    assert total_items({}) == 0


def test_low_stock_includes_the_threshold():
    assert low_stock({"pen": 2, "ink": 9}, 2) == ["pen"]
