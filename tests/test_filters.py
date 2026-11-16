from __future__ import annotations

import pytest

from conftest import asgi_request
from sqlagent.agent import SQLAgent
from sqlagent.app import AppConfig, create_app
from sqlagent.db import QueryError, ReadOnlyDatabase
from sqlagent.filters import FilterError, Filters, apply_filters, describe, load_options, validate
from sqlagent.workspace import Workspace

ORDERS = "SELECT COUNT(DISTINCT order_id) AS orders, ROUND(SUM(revenue), 2) AS revenue FROM sales"


def run(db: ReadOnlyDatabase, sql: str, filters: Filters) -> list[tuple]:
    return db.execute(apply_filters(sql, validate(filters, load_options(db)))).rows


def test_options_come_from_the_data(db: ReadOnlyDatabase) -> None:
    options = load_options(db)
    assert (options.first_month, options.last_month) == ("2017-01", "2018-02")
    assert options.states == ("RJ", "SP") and options.categories == ("Health Beauty", "Toys")


def test_no_filter_leaves_the_query_unchanged() -> None:
    assert apply_filters(ORDERS, Filters()) == ORDERS


def test_month_range_is_inclusive(db: ReadOnlyDatabase) -> None:
    assert run(db, ORDERS, Filters(date_from="2017-02", date_to="2017-03")) == [(2, 155.0)]
    assert run(db, ORDERS, Filters(date_from="2018-01")) == [(2, 93.0)]
    assert run(db, ORDERS, Filters(date_to="2017-01")) == [(1, 165.0)]


def test_state_filters_sales_and_customers(db: ReadOnlyDatabase) -> None:
    assert run(db, ORDERS, Filters(state="RJ")) == [(2, 158.0)]
    assert run(db, "SELECT COUNT(*) FROM customers", Filters(state="SP")) == [(2,)]


def test_category_filters_items_not_whole_orders(db: ReadOnlyDatabase) -> None:
    assert run(db, ORDERS, Filters(category="Toys")) == [(4, 183.0)]
    joined = "SELECT p.category, COUNT(*) FROM sales s JOIN products p USING (product_id) GROUP BY 1"
    assert run(db, joined, Filters(category="Toys")) == [("Toys", 4)]


def test_filters_combine_and_keep_the_tiles_own_conditions(db: ReadOnlyDatabase) -> None:
    sql = "SELECT COUNT(DISTINCT order_id) FROM sales WHERE order_date < '2018-01-01'"
    assert run(db, sql, Filters(state="SP")) == [(2,)]
    assert run(db, sql, Filters(state="SP", date_from="2018-01")) == [(0,)]


def test_a_tile_with_its_own_with_clause_and_comments(db: ReadOnlyDatabase) -> None:
    sql = (
        "-- monthly\nWITH m AS (SELECT strftime('%Y-%m', order_date) AS month, SUM(revenue) AS r FROM sales GROUP BY 1)"
        "\nSELECT * FROM m ORDER BY month;"
    )
    assert run(db, sql, Filters(date_from="2018-01")) == [("2018-01", 55.0), ("2018-02", 38.0)]


def test_the_real_tables_stay_reachable_only_through_the_filter(db: ReadOnlyDatabase) -> None:
    # A tile that names main.sales opts out of the filter, which is allowed; the guardrails still apply.
    assert run(db, "SELECT COUNT(*) FROM main.sales", Filters(state="RJ")) == [(6,)]
    with pytest.raises(QueryError):
        db.execute(apply_filters("DELETE FROM sales", Filters(state="RJ")))


@pytest.mark.parametrize(
    "filters",
    [
        Filters(date_from="2018-13"),
        Filters(date_from="2018-01'; --"),
        Filters(date_from="2018-03", date_to="2018-01"),
        Filters(state="SP' OR 1=1 --"),
        Filters(category="Robots"),
    ],
)
def test_values_outside_the_data_are_refused(db: ReadOnlyDatabase, filters: Filters) -> None:
    with pytest.raises(FilterError):
        validate(filters, load_options(db))


def test_describe() -> None:
    assert describe(Filters()) == "none"
    assert describe(Filters(date_from="2018-01", state="SP")) == "2018-01 to end, customer state SP"


def test_filters_over_http(db: ReadOnlyDatabase, tmp_path) -> None:
    ws = Workspace(tmp_path / "app.db")
    app = create_app(SQLAgent(None, db), AppConfig(), ws)
    status, options = asgi_request(app, "GET", "/api/filters")
    assert status == 200 and options["states"] == ["RJ", "SP"]

    board = asgi_request(app, "POST", "/api/dashboards", {"title": "Sales"})[1]
    board = asgi_request(app, "POST", f"/api/dashboards/{board['id']}/tiles", {"title": "Totals", "sql": ORDERS})[1]
    tile = board["tiles"][0]["id"]
    url = f"/api/dashboards/{board['id']}/tiles/{tile}/data"
    assert asgi_request(app, "GET", url)[1]["rows"] == [[5, 413.0]]
    assert asgi_request(app, "GET", url + "?state=RJ&date_from=2018-01")[1]["rows"] == [[1, 38.0]]
    status, body = asgi_request(app, "GET", url + "?state=XX")
    assert status == 422 and "Unknown customer state" in body["error"]
    by_state = "SELECT c.state, COUNT(*) AS items FROM sales s JOIN customers c USING (customer_key) GROUP BY 1"
    board = asgi_request(app, "POST", f"/api/dashboards/{board['id']}/tiles", {"title": "By state", "sql": by_state})[1]
    empty_url = f"/api/dashboards/{board['id']}/tiles/{board['tiles'][1]['id']}/data?date_from=2018-02&state=SP"
    assert asgi_request(app, "GET", empty_url)[1]["chart"] == {"type": "empty"}

    status, saved = asgi_request(
        app, "PATCH", f"/api/dashboards/{board['id']}", {"filters": {"date_from": "2017-01", "state": "SP"}}
    )
    assert status == 200 and saved["filters"] == {"date_from": "2017-01", "state": "SP"}
    history = asgi_request(app, "GET", f"/api/dashboards/{board['id']}/revisions")[1]["revisions"]
    assert history[0]["summary"] == "Set the filters to 2017-01 to end, customer state SP"
    assert asgi_request(app, "PATCH", f"/api/dashboards/{board['id']}", {"filters": {"state": "ZZ"}})[0] == 422
    ws.close()
