"""Single source of truth for the database schema.

The same definitions create the SQLite tables (scripts/build_db.py), build the
schema section of the system prompt (prompts.py) and decide which tables the
query guardrails allow (db.py). Column descriptions are written for the model:
they say what a value means, not only its type.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Column:
    name: str
    type: str  # SQLite type affinity: TEXT, INTEGER or REAL
    description: str
    is_date: bool = False


@dataclass(frozen=True)
class Table:
    name: str
    sheet: str  # worksheet name in the source workbook
    description: str
    columns: tuple[Column, ...]
    indexes: tuple[str, ...] = ()

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


TABLES: tuple[Table, ...] = (
    Table(
        name="sales",
        sheet="Sales",
        description=(
            "Fact table. One row per order ITEM, so an order with 3 items has 3 rows. "
            "Order-level values (review_score, delivery_days, delay_days, is_late, "
            "payment_type) are repeated on every item row of the same order."
        ),
        columns=(
            Column("order_id", "TEXT", "Order identifier. Count orders with COUNT(DISTINCT order_id)."),
            Column("order_item_id", "INTEGER", "Item number within the order, starting at 1."),
            Column("order_date", "TEXT", "Purchase date, YYYY-MM-DD.", is_date=True),
            Column("customer_key", "TEXT", "Customer identifier. Joins to customers.customer_key."),
            Column("product_id", "TEXT", "Joins to products.product_id."),
            Column("seller_id", "TEXT", "Joins to sellers.seller_id."),
            Column(
                "order_status",
                "TEXT",
                "One of: delivered, shipped, canceled, invoiced, processing, unavailable, approved.",
            ),
            Column("price", "REAL", "Item price in BRL, without freight."),
            Column("freight", "REAL", "Freight charged for the item, in BRL."),
            Column("revenue", "REAL", "price + freight for the item, in BRL."),
            Column("order_rank", "INTEGER", "1 for the customer's first order, 2 for the second, and so on."),
            Column("is_repeat_order", "INTEGER", "1 if order_rank > 1, else 0."),
            Column(
                "cohort_month",
                "TEXT",
                "First day of the month of the customer's first order, YYYY-MM-01.",
                is_date=True,
            ),
            Column("months_since_first", "INTEGER", "Whole months between the cohort month and this order."),
            Column("delivered_date", "TEXT", "Delivery date, NULL if not delivered.", is_date=True),
            Column("estimated_date", "TEXT", "Delivery date promised to the customer.", is_date=True),
            Column("delivery_days", "INTEGER", "Days from purchase to delivery. NULL if not delivered."),
            Column(
                "delay_days",
                "INTEGER",
                "delivered_date minus estimated_date in days. Negative means early. NULL if not delivered.",
            ),
            Column("is_late", "INTEGER", "1 if delivered after the estimated date, else 0."),
            Column("review_score", "INTEGER", "Review score of the order, an integer from 1 (worst) to 5 (best)."),
            Column(
                "payment_type",
                "TEXT",
                "Main payment type: credit_card, boleto, voucher or debit_card. Can be NULL.",
            ),
            Column("installments", "INTEGER", "Number of payment installments. Can be NULL."),
        ),
        indexes=("order_id", "customer_key", "product_id", "seller_id", "order_date"),
    ),
    Table(
        name="customers",
        sheet="Customers",
        description="One row per customer.",
        columns=(
            Column("customer_key", "TEXT", "Primary key."),
            Column("city", "TEXT", "Customer city."),
            Column("state", "TEXT", "Two-letter Brazilian state code, for example SP or RJ."),
            Column("first_order_date", "TEXT", "Date of the first order.", is_date=True),
            Column("cohort_month", "TEXT", "First day of the month of the first order.", is_date=True),
            Column("total_orders", "INTEGER", "Number of orders placed by the customer."),
            Column("is_repeat_customer", "INTEGER", "1 if total_orders > 1, else 0."),
        ),
        indexes=("customer_key",),
    ),
    Table(
        name="products",
        sheet="Products",
        description="One row per product.",
        columns=(
            Column("product_id", "TEXT", "Primary key."),
            Column("category", "TEXT", "Product category in English, for example 'Health Beauty'."),
            Column("weight_g", "REAL", "Weight in grams."),
            Column("length_cm", "REAL", "Length in centimetres."),
            Column("height_cm", "REAL", "Height in centimetres."),
            Column("width_cm", "REAL", "Width in centimetres."),
            Column("photos_qty", "INTEGER", "Number of photos on the listing."),
        ),
        indexes=("product_id",),
    ),
    Table(
        name="sellers",
        sheet="Sellers",
        description="One row per seller.",
        columns=(
            Column("seller_id", "TEXT", "Primary key."),
            Column("city", "TEXT", "Seller city."),
            Column("state", "TEXT", "Two-letter Brazilian state code."),
        ),
        indexes=("seller_id",),
    ),
    Table(
        name="dates",
        sheet="Dates",
        description="Calendar table, one row per day from 2016-09-01 to 2018-10-31.",
        columns=(
            Column("date", "TEXT", "Primary key, YYYY-MM-DD. Joins to sales.order_date.", is_date=True),
            Column("year", "INTEGER", "Calendar year."),
            Column("month", "INTEGER", "Month number, 1 to 12."),
            Column("month_name", "TEXT", "Three-letter month name, for example Sep."),
            Column("year_month", "TEXT", "YYYY-MM."),
            Column("quarter", "TEXT", "Q1 to Q4."),
            Column("weekday", "TEXT", "Full weekday name, for example Monday."),
            Column("month_start", "TEXT", "First day of the month.", is_date=True),
        ),
        indexes=("date",),
    ),
)

TABLE_NAMES: frozenset[str] = frozenset(t.name for t in TABLES)


def create_table_sql(table: Table) -> str:
    cols = ",\n  ".join(f"{c.name} {c.type}" for c in table.columns)
    return f"CREATE TABLE {table.name} (\n  {cols}\n)"


def create_index_sql(table: Table) -> list[str]:
    return [f"CREATE INDEX idx_{table.name}_{col} ON {table.name} ({col})" for col in table.indexes]


# Known quirks of the modelled data, shown in the UI's schema view. Each one
# was measured on the real database and is re-checked by `python -m sqlagent check-data`.
DATA_NOTES: tuple[str, ...] = (
    "Orders run from 2016-09-04 to 2018-09-03. 2016 and the last weeks of 2018 are partial.",
    "676 customers have no rows in sales: their orders contain no items.",
    "customers.total_orders counts every order of a customer. Counting from sales finds 2,913 repeat "
    "customers; the customers table says 2,997.",
    "2,454 item rows have no delivery date, because the order was not delivered.",
    "3 item rows have no payment type.",
    "Order-level values (review score, delivery days, payment type) are identical on every item row of an order.",
)
