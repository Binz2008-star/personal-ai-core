from report import total_sales


def test_total_of_the_sales_file():
    assert total_sales("sales.csv") == 250


def test_a_single_row(tmp_path):
    path = tmp_path / "one.csv"
    path.write_text("region,amount\nwest,7\n", encoding="utf-8")
    assert total_sales(path) == 7
