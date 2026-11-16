"""The curated overview dashboard.

Every tile is a fixed SQL query, so the dashboard loads without spending a
single model token. The only user input is the year filter, and it never
reaches the SQL as text: it is checked against a whitelist and mapped to a
constant WHERE fragment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .db import ReadOnlyDatabase
from .presentation import result_payload

YEARS = ("2016", "2017", "2018")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# The monthly line only shows complete, representative months. 2016 holds 324 orders
# with none in November, and September 2018 holds a single order: drawn as points they
# look like a collapse that never happened. Measured with `python -m sqlagent check-data`.
MONTH_WINDOWS: dict[str, tuple[str, str]] = {
    "all": (
        " AND order_date >= '2017-01-01' AND order_date < '2018-09-01'",
        "Complete months only: 2016 is sparse, with no orders in November, and September 2018 has one order.",
    ),
    "2016": ("", "The data starts in September 2016 and has no orders in November."),
    "2017": ("", ""),
    "2018": (" AND order_date < '2018-09-01'", "September 2018 has a single order and is left out."),
}

# Year-over-year comparison of the headline numbers.
MIN_MONTH_ORDERS = 100  # a month with fewer orders is treated as partial
COMPARE_MIN_ORDERS = 1000  # below this the previous period is too small to compare with
HIGHER_IS_BETTER = {"late_delivery_rate": False}

DELAY_BUCKET = """CASE WHEN delay_days <= 0 THEN 'On time'
            WHEN delay_days <= 7 THEN '1 to 7 days late'
            ELSE '8 or more days late' END"""


@dataclass(frozen=True)
class Tile:
    id: str
    title: str
    sql: str  # may contain {where}, replaced by "" or " AND <year range>"
    note: str = ""
    keep_order: bool = False  # categories have a natural order: do not rank the bars
    wide: bool = False
    month_window: bool = False  # apply MONTH_WINDOWS for the selected year


TILES: tuple[Tile, ...] = (
    Tile(
        "kpis",
        "Headline numbers",
        """SELECT SUM(revenue) AS revenue,
                  COUNT(DISTINCT order_id) AS orders,
                  SUM(revenue) * 1.0 / COUNT(DISTINCT order_id) AS average_order_value,
                  (SELECT AVG(score) FROM (SELECT MAX(review_score) AS score FROM sales
                                           WHERE 1 = 1{where} GROUP BY order_id)) AS average_review,
                  100.0 * COUNT(DISTINCT CASE WHEN is_late = 1 THEN order_id END)
                        / COUNT(DISTINCT CASE WHEN delivered_date IS NOT NULL THEN order_id END) AS late_delivery_rate
           FROM sales WHERE 1 = 1{where}""",
        wide=True,
    ),
    Tile(
        "revenue_by_month",
        "Revenue by month",
        """SELECT strftime('%Y-%m', order_date) AS month, SUM(revenue) AS revenue
           FROM sales WHERE 1 = 1{where} GROUP BY month ORDER BY month""",
        wide=True,
        month_window=True,
    ),
    Tile(
        "revenue_by_category",
        "Top categories by revenue",
        """SELECT p.category, SUM(s.revenue) AS revenue
           FROM sales s JOIN products p ON p.product_id = s.product_id
           WHERE 1 = 1{where} GROUP BY p.category ORDER BY revenue DESC LIMIT 8""",
    ),
    Tile(
        "orders_by_state",
        "Top customer states by orders",
        """SELECT c.state, COUNT(DISTINCT s.order_id) AS orders
           FROM sales s JOIN customers c ON c.customer_key = s.customer_key
           WHERE 1 = 1{where} GROUP BY c.state ORDER BY orders DESC LIMIT 8""",
    ),
    Tile(
        "review_by_delay",
        "Average review by delivery delay",
        f"""SELECT bucket AS delivery, AVG(score) AS average_review FROM (
               SELECT order_id, MAX(review_score) AS score, {DELAY_BUCKET} AS bucket
               FROM sales WHERE delivered_date IS NOT NULL{{where}} GROUP BY order_id)
           GROUP BY bucket ORDER BY MIN(CASE bucket WHEN 'On time' THEN 0
                                             WHEN '1 to 7 days late' THEN 1 ELSE 2 END)""",
        note="Late delivery is the strongest driver of bad reviews.",
        keep_order=True,
    ),
    Tile(
        "orders_by_weekday",
        "Orders by weekday",
        """SELECT d.weekday, COUNT(DISTINCT s.order_id) AS orders
           FROM sales s JOIN dates d ON d.date = s.order_date
           WHERE 1 = 1{where} GROUP BY d.weekday
           ORDER BY MIN(CAST(strftime('%w', d.date) AS INTEGER) + 6) % 7""",
        keep_order=True,
    ),
)


OVERVIEW_TITLE = "Marketplace overview"
OVERVIEW_DESCRIPTION = "Headline numbers, revenue trend, categories, states and delivery. Edit it like any dashboard."


def editable_tiles() -> list[dict[str, str]]:
    """The overview's tiles as saved-dashboard tiles, for "Make an editable copy"."""
    return [
        {
            "title": tile.title,
            "sql": tile_sql(tile),
            "chart": "kpi" if tile.id == "kpis" else "line" if tile.month_window else "bar",
            "size": "full" if tile.wide else "half",
            "note": tile_note(tile)[:240],
        }
        for tile in TILES
    ]


def year_filter(year: str | None) -> str:
    """A constant SQL fragment for a whitelisted year, or '' for all years."""
    if not year or year == "all":
        return ""
    if year not in YEARS:
        raise ValueError(f"Unknown year {year!r}. Use one of: all, {', '.join(YEARS)}.")
    return f" AND order_date >= '{year}-01-01' AND order_date < '{int(year) + 1}-01-01'"


def tile_sql(tile: Tile, year: str | None = None) -> str:
    where = year_filter(year)
    if tile.month_window:
        where += MONTH_WINDOWS[year or "all"][0]
    return " ".join(tile.sql.format(where=where).split())


def tile_note(tile: Tile, year: str | None = None) -> str:
    if tile.month_window and MONTH_WINDOWS[year or "all"][1]:
        return MONTH_WINDOWS[year or "all"][1]
    return tile.note


def comparison_period(db: ReadOnlyDatabase, year: str | None) -> tuple[str, str] | None:
    """The same months of the previous year: (SQL fragment, label), or None when there is nothing fair to compare.

    The selected year is cut at its last month with at least MIN_MONTH_ORDERS orders,
    so a year that ends in August is compared with January to August of the year before.
    """
    if not year or year == "all" or str(int(year) - 1) not in YEARS:
        return None
    rows = db.execute(
        f"SELECT strftime('%m', order_date) AS month FROM sales WHERE 1 = 1{year_filter(year)} "
        f"GROUP BY month HAVING COUNT(DISTINCT order_id) >= {MIN_MONTH_ORDERS} ORDER BY month DESC LIMIT 1"
    ).rows
    if not rows or not re.fullmatch(r"\d{2}", str(rows[0][0])):
        return None
    month, previous = int(rows[0][0]), int(year) - 1
    end = f"{previous}-{month + 1:02d}-01" if month < 12 else f"{previous + 1}-01-01"
    clause = f" AND order_date >= '{previous}-01-01' AND order_date < '{end}'"
    label = f"vs {previous}" if month == 12 else f"vs Jan to {MONTHS[month - 1]} {previous}"
    return clause, label


def add_deltas(db: ReadOnlyDatabase, tile: Tile, payload: dict[str, Any], year: str | None) -> None:
    """Attach the previous period's value to each headline number, when the comparison is fair."""
    period = comparison_period(db, year)
    if period is None or payload["chart"]["type"] != "kpi":
        return
    clause, label = period
    previous = db.execute(" ".join(tile.sql.format(where=clause).split()))
    values = dict(zip(previous.columns, previous.rows[0], strict=True)) if previous.rows else {}
    if (values.get("orders") or 0) < COMPARE_MIN_ORDERS:
        return
    for item in payload["chart"]["items"]:
        if values.get(item["label"]) is not None:
            item["delta"] = {
                "previous": values[item["label"]],
                "label": label,
                "higher_is_better": HIGHER_IS_BETTER.get(item["label"], True),
            }


def dataset_facts(db: ReadOnlyDatabase) -> dict[str, Any]:
    """A few numbers that describe the dataset, for the home screen."""
    row = db.execute(
        "SELECT COUNT(DISTINCT order_id) AS orders, MIN(order_date) AS first_date, MAX(order_date) AS last_date, "
        "(SELECT COUNT(DISTINCT category) FROM products) AS categories, (SELECT COUNT(*) FROM sellers) AS sellers, "
        "(SELECT COUNT(DISTINCT state) FROM customers) AS states FROM sales"
    )
    return dict(zip(row.columns, row.rows[0], strict=True))


def build_dashboard(db: ReadOnlyDatabase, year: str | None = None) -> dict[str, Any]:
    tiles = []
    for tile in TILES:
        sql = tile_sql(tile, year)
        result = db.execute(sql)
        payload = result_payload(result.columns, result.rows, result.truncated)
        if tile.id == "kpis":
            add_deltas(db, tile, payload, year)
        if tile.keep_order and payload["chart"]["type"] == "bar":
            # Rebuild labels and values in query order instead of ranked order.
            label_i = result.columns.index(payload["chart"]["x"])
            value_i = result.columns.index(payload["chart"]["y"])
            payload["chart"]["labels"] = [str(r[label_i]) for r in result.rows]
            payload["chart"]["values"] = [r[value_i] for r in result.rows]
        tiles.append(
            {
                "id": tile.id,
                "title": tile.title,
                "note": tile_note(tile, year),
                "wide": tile.wide,
                "sql": sql,
                **payload,
            }
        )
    return {"year": year or "all", "years": ["all", *YEARS], "tiles": tiles}
