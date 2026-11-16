from __future__ import annotations

from sqlagent.presentation import classify_column, result_payload, suggest_chart, value_format


def test_single_value_becomes_a_stat_tile() -> None:
    chart = suggest_chart(["revenue"], [(15843553.24,)])
    assert chart == {
        "type": "kpi",
        "context": "",
        "items": [{"label": "revenue", "value": 15843553.24, "format": "currency"}],
    }


def test_one_row_with_several_numbers_becomes_a_row_of_tiles() -> None:
    chart = suggest_chart(["state", "orders", "late_rate"], [("SP", 41371, 5.9)])
    assert chart["type"] == "kpi" and chart["context"] == "SP"
    assert [(i["label"], i["format"]) for i in chart["items"]] == [("orders", "number"), ("late_rate", "percent")]


def test_one_row_of_text_only_stays_a_table() -> None:
    assert suggest_chart(["category"], [("Health Beauty",)]) == {"type": "table"}


def test_category_and_value_become_ranked_bars() -> None:
    rows = [("Toys", 10.0), ("Health Beauty", 30.0), ("Auto", 20.0)]
    chart = suggest_chart(["category", "revenue"], rows)
    assert chart["type"] == "bar" and chart["x"] == "category" and chart["y"] == "revenue"
    assert chart["labels"] == ["Health Beauty", "Auto", "Toys"] and chart["values"] == [30.0, 20.0, 10.0]
    assert chart["format"] == "currency"


def test_two_text_columns_are_joined_into_one_label() -> None:
    chart = suggest_chart(["city", "state", "revenue"], [("Sao Paulo", "SP", 9.0), ("Rio De Janeiro", "RJ", 5.0)])
    assert chart["labels"] == ["Sao Paulo, SP", "Rio De Janeiro, RJ"] and chart["x"] == "city and state"


def test_months_become_a_line_sorted_by_time() -> None:
    rows = [("2017-03", 3.0), ("2017-01", 1.0), ("2017-04", 4.0), ("2017-02", 2.0)]
    chart = suggest_chart(["month", "revenue"], rows)
    assert chart["type"] == "line" and chart["labels"] == ["2017-01", "2017-02", "2017-03", "2017-04"]
    assert chart["series"] == [{"name": "revenue", "format": "currency", "values": [1.0, 2.0, 3.0, 4.0]}]


def test_line_keeps_at_most_three_series_that_share_a_unit() -> None:
    rows = [(f"2017-0{m}", m * 10.0, m * 2.0, m, m * 1.5) for m in range(1, 6)]
    chart = suggest_chart(["month", "revenue", "freight", "orders", "price"], rows)
    # revenue and freight are both currency; orders is a count and cannot share their axis.
    assert [s["name"] for s in chart["series"]] == ["revenue", "freight"]


def test_few_periods_become_bars_in_time_order() -> None:
    chart = suggest_chart(["year", "orders"], [(2018, 53775), (2016, 312), (2017, 44579)])
    assert (
        chart["type"] == "bar"
        and chart["labels"] == ["2016", "2017", "2018"]
        and chart["values"] == [312, 44579, 53775]
    )


def test_small_whole_number_key_is_treated_as_a_category() -> None:
    chart = suggest_chart(["review_score", "orders"], [(5, 57021), (1, 11243), (3, 8187)])
    assert chart["type"] == "bar" and chart["labels"] == ["1", "3", "5"] and chart["values"] == [11243, 8187, 57021]


def test_too_many_categories_or_no_numbers_fall_back_to_a_table() -> None:
    assert suggest_chart(["seller", "revenue"], [(f"s{i}", float(i)) for i in range(26)]) == {"type": "table"}
    assert suggest_chart(["city", "state"], [("A", "SP"), ("B", "RJ")]) == {"type": "table"}
    assert suggest_chart([], []) == {"type": "table"}
    assert suggest_chart(["a"], []) == {"type": "table"}


def test_null_values_are_dropped_from_bars() -> None:
    chart = suggest_chart(
        ["payment_type", "orders"], [("credit_card", 10), (None, 1), ("boleto", None), ("voucher", 4)]
    )
    assert chart["labels"] == ["credit_card", "voucher", "(none)"] and chart["values"] == [10, 4, 1]


def test_column_classification_and_formats() -> None:
    assert classify_column("month", ["2017-01", None, "2017-02"]) == "temporal"
    assert classify_column("order_date", ["2017-01-05"]) == "temporal"
    assert classify_column("year", [2016, 2017]) == "temporal"
    assert classify_column("orders", [2016, 2017]) == "numeric"
    assert classify_column("flag", [True, False]) == "text"
    assert classify_column("x", [None, None]) == "empty"
    assert value_format("late_delivery_rate") == "percent" and value_format("growth_pct") == "percent"
    assert value_format("average_order_value") == "currency" and value_format("avg_freight") == "currency"
    assert value_format("orders") == "number"


def test_payload_is_json_ready() -> None:
    payload = result_payload(["n"], [(1,)], truncated=True)
    assert payload["rows"] == [[1]] and payload["truncated"] is True and payload["chart"]["type"] == "kpi"
