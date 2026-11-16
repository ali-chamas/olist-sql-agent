from __future__ import annotations

import datetime as dt

import pytest

from sqlagent import dashboards
from sqlagent.dashboards import TILES, build_dashboard, comparison_period, dataset_facts, tile_sql, year_filter
from sqlagent.dataquality import CHECKS, run_checks
from sqlagent.db import ReadOnlyDatabase
from sqlagent.limits import AnswerCache, DailyTokenBudget, RateLimiter, normalize_question


def test_rate_limiter_window_slides() -> None:
    now = [0.0]
    limiter = RateLimiter(2, window_s=60, clock=lambda: now[0])
    assert limiter.allow("a") and limiter.allow("a") and not limiter.allow("a")
    assert limiter.allow("b")
    now[0] = 61.0
    assert limiter.allow("a")


def test_question_normalisation() -> None:
    assert normalize_question("  How   many ORDERS ?! ") == "how many orders"


def test_cache_key_covers_model_question_and_conversation() -> None:
    base = AnswerCache.key("m1", "How many orders?")
    assert base == AnswerCache.key("m1", "how many  orders")
    assert base != AnswerCache.key("m2", "How many orders?")
    assert base != AnswerCache.key("m1", "How many orders?", [{"question": "Earlier", "sql": "SELECT 1"}])


def test_cache_expires_and_evicts_least_recently_used() -> None:
    now = [0.0]
    cache = AnswerCache(max_entries=2, ttl_s=10, clock=lambda: now[0])
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") == 1  # touching "a" makes "b" the oldest
    cache.put("c", 3)
    assert cache.get("b") is None and cache.get("a") == 1 and cache.get("c") == 3
    now[0] = 11.0
    assert cache.get("a") is None
    assert (cache.hits, cache.misses) == (3, 2)

    disabled = AnswerCache(max_entries=0)
    disabled.put("a", 1)
    assert disabled.get("a") is None


def test_daily_budget_resets_at_midnight_utc() -> None:
    day = [dt.date(2026, 1, 1)]
    budget = DailyTokenBudget(1000, today=lambda: day[0])
    assert budget.available()
    budget.spend(600)
    assert budget.available()  # a request may start while any budget is left
    budget.spend(600)
    assert not budget.available() and budget.snapshot() == {"limit": 1000, "used": 1000}
    day[0] = dt.date(2026, 1, 2)
    assert budget.available() and budget.snapshot() == {"limit": 1000, "used": 0}
    assert DailyTokenBudget(0).available()


def test_year_filter_only_accepts_whitelisted_years() -> None:
    assert year_filter(None) == "" and year_filter("all") == ""
    assert year_filter("2017") == " AND order_date >= '2017-01-01' AND order_date < '2018-01-01'"
    for bad in ("1999", "2017 OR 1=1", "'; DROP TABLE sales; --", "20170"):
        with pytest.raises(ValueError):
            year_filter(bad)


def test_every_tile_runs_under_the_guardrails_for_every_filter(db: ReadOnlyDatabase) -> None:
    for year in (None, "2016", "2017", "2018"):
        for tile in TILES:
            db.execute(tile_sql(tile, year))


def test_dashboard_numbers_match_the_fixture(db: ReadOnlyDatabase) -> None:
    board = build_dashboard(db)
    tiles = {t["id"]: t for t in board["tiles"]}
    kpis = {i["label"]: i["value"] for i in tiles["kpis"]["chart"]["items"]}
    assert kpis["revenue"] == pytest.approx(413.0) and kpis["orders"] == 5
    assert kpis["average_review"] == pytest.approx(3.0)  # per order: (5 + 1 + 4 + 2 + 3) / 5
    assert kpis["late_delivery_rate"] == pytest.approx(50.0)  # 2 late of 4 delivered
    assert tiles["revenue_by_category"]["chart"]["labels"] == ["Health Beauty", "Toys"]
    # Natural order is kept for ordered categories, not ranked by value.
    assert tiles["review_by_delay"]["chart"]["labels"] == ["On time", "1 to 7 days late", "8 or more days late"]
    assert tiles["review_by_delay"]["chart"]["values"] == pytest.approx([4.5, 3.0, 1.0])
    assert tiles["orders_by_weekday"]["chart"]["labels"] == ["Tuesday", "Friday", "Saturday", "Sunday"]

    only_2018 = {t["id"]: t for t in build_dashboard(db, "2018")["tiles"]}
    assert {i["label"]: i["value"] for i in only_2018["kpis"]["chart"]["items"]}["orders"] == 2


def test_data_quality_checks_pass_on_the_fixture(db: ReadOnlyDatabase) -> None:
    results = run_checks(db)
    assert len(results) == len(CHECKS) and all(r.passed for r in results)
    reported = {r.name: r.detail for r in results}
    assert reported["item rows without a delivery date (reported)"] == "1"


def test_monthly_line_keeps_complete_months_and_says_why() -> None:
    tile = next(t for t in TILES if t.id == "revenue_by_month")
    assert "order_date >= '2017-01-01' AND order_date < '2018-09-01'" in tile_sql(tile)
    assert "< '2018-09-01'" in tile_sql(tile, "2018") and "2018-09-01" not in tile_sql(tile, "2017")
    board = {t["id"]: t for t in build_dashboard_fixture_free()}
    assert "Complete months only" in board["revenue_by_month"]["note"]


def build_dashboard_fixture_free() -> list[dict]:
    """Notes do not depend on data, so check them without a database."""
    return [{"id": t.id, "note": dashboards.tile_note(t)} for t in TILES]


def test_no_comparison_without_a_fair_previous_period(db: ReadOnlyDatabase) -> None:
    assert comparison_period(db, None) is None and comparison_period(db, "2016") is None
    # The fixture's months hold one or two orders, below the partial-month threshold.
    assert comparison_period(db, "2018") is None
    kpis = build_dashboard(db, "2018")["tiles"][0]["chart"]["items"]
    assert all("delta" not in item for item in kpis)


def test_headline_numbers_compare_with_the_same_months_of_the_previous_year(db: ReadOnlyDatabase, monkeypatch) -> None:
    monkeypatch.setattr(dashboards, "MIN_MONTH_ORDERS", 1)
    monkeypatch.setattr(dashboards, "COMPARE_MIN_ORDERS", 1)
    clause, label = comparison_period(db, "2018")
    # 2018 in the fixture ends in February, so it is compared with January and February 2017.
    assert clause == " AND order_date >= '2017-01-01' AND order_date < '2017-03-01'" and label == "vs Jan to Feb 2017"
    items = {i["label"]: i for i in build_dashboard(db, "2018")["tiles"][0]["chart"]["items"]}
    assert items["revenue"]["value"] == pytest.approx(93.0)
    assert items["revenue"]["delta"] == {"previous": pytest.approx(285.0), "label": label, "higher_is_better": True}
    assert items["late_delivery_rate"]["delta"]["higher_is_better"] is False


def test_dataset_facts(db: ReadOnlyDatabase) -> None:
    assert dataset_facts(db) == {
        "orders": 5,
        "first_date": "2017-01-10",
        "last_date": "2018-02-11",
        "categories": 2,
        "sellers": 2,
        "states": 2,
    }
