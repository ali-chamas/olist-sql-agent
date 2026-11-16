"""Checks that the database matches what the prompt and the eval assume.

The agent's business rules ("review_score is the same on every item row of an
order") are only true if the data says so. These checks turn those assumptions
into queries, so a changed or badly built database fails loudly instead of
quietly producing wrong answers.
"""

from __future__ import annotations

from dataclasses import dataclass

from .db import ReadOnlyDatabase

# name, SQL returning one number, and the value that number must have (None = report only).
CHECKS: tuple[tuple[str, str, int | None], ...] = (
    (
        "every sale has a customer",
        "SELECT COUNT(*) FROM sales s LEFT JOIN customers c ON c.customer_key = s.customer_key "
        "WHERE c.customer_key IS NULL",
        0,
    ),
    (
        "every sale has a product",
        "SELECT COUNT(*) FROM sales s LEFT JOIN products p ON p.product_id = s.product_id WHERE p.product_id IS NULL",
        0,
    ),
    (
        "every sale has a seller",
        "SELECT COUNT(*) FROM sales s LEFT JOIN sellers se ON se.seller_id = s.seller_id WHERE se.seller_id IS NULL",
        0,
    ),
    (
        "every order date is in the calendar",
        "SELECT COUNT(*) FROM sales s LEFT JOIN dates d ON d.date = s.order_date WHERE d.date IS NULL",
        0,
    ),
    (
        "order-level values agree on every item row",
        "SELECT COUNT(*) FROM (SELECT order_id FROM sales GROUP BY order_id HAVING COUNT(DISTINCT review_score) > 1 "
        "OR COUNT(DISTINCT delivery_days) > 1 OR COUNT(DISTINCT is_late) > 1 OR COUNT(DISTINCT payment_type) > 1)",
        0,
    ),
    ("revenue equals price plus freight", "SELECT COUNT(*) FROM sales WHERE ABS(revenue - price - freight) > 0.011", 0),
    ("review scores are between 1 and 5", "SELECT COUNT(*) FROM sales WHERE review_score NOT BETWEEN 1 AND 5", 0),
    ("late orders have a delivery date", "SELECT COUNT(*) FROM sales WHERE is_late = 1 AND delivered_date IS NULL", 0),
    (
        "dates are stored as YYYY-MM-DD",
        "SELECT COUNT(*) FROM sales WHERE order_date NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'",
        0,
    ),
    ("primary keys are unique (customers)", "SELECT COUNT(*) - COUNT(DISTINCT customer_key) FROM customers", 0),
    ("primary keys are unique (products)", "SELECT COUNT(*) - COUNT(DISTINCT product_id) FROM products", 0),
    ("primary keys are unique (sellers)", "SELECT COUNT(*) - COUNT(DISTINCT seller_id) FROM sellers", 0),
    (
        "customers without any sale (reported)",
        "SELECT COUNT(*) FROM customers c "
        "WHERE NOT EXISTS (SELECT 1 FROM sales s WHERE s.customer_key = c.customer_key)",
        None,
    ),
    ("item rows without a delivery date (reported)", "SELECT COUNT(*) FROM sales WHERE delivered_date IS NULL", None),
    ("item rows without a payment type (reported)", "SELECT COUNT(*) FROM sales WHERE payment_type IS NULL", None),
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


def run_checks(db: ReadOnlyDatabase) -> list[CheckResult]:
    results = []
    for name, sql, expected in CHECKS:
        value = db.execute(sql).rows[0][0]
        if expected is None:
            results.append(CheckResult(name, True, f"{value:,}"))
        else:
            passed = value == expected
            results.append(CheckResult(name, passed, "none found" if passed else f"{value:,} violations"))
    return results
