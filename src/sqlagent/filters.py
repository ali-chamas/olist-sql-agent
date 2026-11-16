"""Dashboard filters that apply to every tile without rewriting its SQL.

A tile's SQL is written against the tables ``sales``, ``customers`` and ``products``.
A filter shadows those tables with common table expressions of the same name:

    WITH sales AS (SELECT * FROM main.sales WHERE order_date >= '2018-01-01' AND ...)
    <the tile's own SQL, unchanged>

SQLite resolves an unqualified table name to a CTE first, and ``main.sales`` always
means the real table, so the tile reads the filtered rows. The tile's own WHERE clauses
still apply on top, so a tile fixed to 2017 shows nothing when the dashboard is filtered
to 2018, which is the correct answer.

Values are never trusted text: months must match YYYY-MM, and a state or category must
be one of the values that exist in the data. Only then are they written into the SQL as
quoted literals (the executor takes no parameters).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from .db import ReadOnlyDatabase, strip_sql

_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_LEADING_WITH = re.compile(r"^\s*with(\s+recursive)?\s", re.IGNORECASE)


class FilterError(ValueError):
    """A filter value that is not allowed. The message is safe to show."""


@dataclass(frozen=True)
class Filters:
    """Dashboard filters. Empty fields mean 'no filter'. Months are inclusive."""

    date_from: str = ""  # YYYY-MM
    date_to: str = ""  # YYYY-MM
    state: str = ""  # customer state, for example SP
    category: str = ""  # product category, for example Health Beauty

    @property
    def active(self) -> bool:
        return any((self.date_from, self.date_to, self.state, self.category))

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Filters:
        data = data or {}
        return cls(**{k: str(data.get(k) or "").strip() for k in ("date_from", "date_to", "state", "category")})


@dataclass(frozen=True)
class FilterOptions:
    """The values a filter can take, read once from the data."""

    first_month: str
    last_month: str
    states: tuple[str, ...]
    categories: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "first_month": self.first_month,
            "last_month": self.last_month,
            "states": list(self.states),
            "categories": list(self.categories),
        }


def load_options(db: ReadOnlyDatabase) -> FilterOptions:
    months = db.execute("SELECT MIN(substr(order_date, 1, 7)), MAX(substr(order_date, 1, 7)) FROM sales").rows[0]
    states = db.execute("SELECT DISTINCT state FROM customers WHERE state IS NOT NULL ORDER BY state", 1000).rows
    categories = db.execute(
        "SELECT DISTINCT category FROM products WHERE category IS NOT NULL ORDER BY category", 1000
    ).rows
    return FilterOptions(
        first_month=months[0],
        last_month=months[1],
        states=tuple(r[0] for r in states),
        categories=tuple(r[0] for r in categories),
    )


def validate(filters: Filters, options: FilterOptions) -> Filters:
    """Return the filters if every value is allowed, else raise FilterError."""
    for name in ("date_from", "date_to"):
        value = getattr(filters, name)
        if value and not _MONTH.match(value):
            raise FilterError(f"{name} must be a month written YYYY-MM, got {value!r}.")
    if filters.date_from and filters.date_to and filters.date_from > filters.date_to:
        raise FilterError("The start month is after the end month.")
    if filters.state and filters.state not in options.states:
        raise FilterError(f"Unknown customer state {filters.state!r}.")
    if filters.category and filters.category not in options.categories:
        raise FilterError(f"Unknown product category {filters.category!r}.")
    return filters


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _next_month(month: str) -> str:
    year, mon = int(month[:4]), int(month[5:])
    return f"{year + mon // 12:04d}-{mon % 12 + 1:02d}"


def shadow_ctes(filters: Filters) -> list[str]:
    """The CTEs that replace the filtered tables. Call ``validate`` first."""
    sales: list[str] = []
    if filters.date_from:
        sales.append(f"order_date >= {_quote(filters.date_from + '-01')}")
    if filters.date_to:
        sales.append(f"order_date < {_quote(_next_month(filters.date_to) + '-01')}")
    if filters.state:
        sales.append(f"customer_key IN (SELECT customer_key FROM main.customers WHERE state = {_quote(filters.state)})")
    if filters.category:
        sales.append(
            f"product_id IN (SELECT product_id FROM main.products WHERE category = {_quote(filters.category)})"
        )
    ctes = []
    if sales:
        ctes.append(f"sales AS (SELECT * FROM main.sales WHERE {' AND '.join(sales)})")
    if filters.state:
        ctes.append(f"customers AS (SELECT * FROM main.customers WHERE state = {_quote(filters.state)})")
    if filters.category:
        ctes.append(f"products AS (SELECT * FROM main.products WHERE category = {_quote(filters.category)})")
    return ctes


def apply_filters(sql: str, filters: Filters) -> str:
    """The tile's SQL with the filtered tables shadowed. Unchanged when no filter is set."""
    ctes = shadow_ctes(filters)
    if not ctes:
        return sql
    body = strip_sql(sql)
    match = _LEADING_WITH.match(body)
    if match:
        keyword = "WITH RECURSIVE" if match.group(1) else "WITH"
        return f"{keyword} {', '.join(ctes)}, {body[match.end() :]}"
    return f"WITH {', '.join(ctes)} {body}"


def describe(filters: Filters) -> str:
    """A short phrase for the model and for notes, for example 'Jan 2018 to Jun 2018, state SP'."""
    parts = []
    if filters.date_from or filters.date_to:
        parts.append(f"{filters.date_from or 'start'} to {filters.date_to or 'end'}")
    if filters.state:
        parts.append(f"customer state {filters.state}")
    if filters.category:
        parts.append(f"category {filters.category}")
    return ", ".join(parts) or "none"
