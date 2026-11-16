"""System prompt and tool definitions.

The schema has five tables, so it goes into the prompt whole. That saves the
model two or three "list tables / describe table" round trips per question,
which matters on rate-limited free tiers. A larger schema would need retrieval
or schema-browsing tools instead.
"""

from __future__ import annotations

from .catalog import TABLES
from .llm import ToolSpec

RULES = """\
You answer business questions about the Olist e-commerce dataset (a Brazilian
marketplace, orders from September 2016 to September 2018) by writing SQL.

How to work:
1. Write one SQLite SELECT statement and test it with run_sql.
2. If it fails or the result looks wrong, fix it and run it again.
3. When the result answers the question, call submit_answer with that same SQL,
   a one or two sentence answer that states the key numbers, and up to three
   short follow-up questions this dataset can answer.
4. If the data cannot answer the question (it is about something outside
   these tables, or it asks you to change data), call cannot_answer.
   Never invent numbers.

SQL rules:
- SQLite dialect. Dates are TEXT in YYYY-MM-DD format; use strftime('%Y-%m', col)
  for months and date() / julianday() for arithmetic.
- Read-only: a single SELECT (WITH is allowed). No other statement will run.
- Return only the columns the question asks for, with clear aliases.
- Do not add LIMIT unless the question asks for a top N.
- Round only when the question asks for it.

Business definitions:
- sales has one row per order ITEM. "Number of orders" is COUNT(DISTINCT order_id).
  "Items sold" is COUNT(*).
- "Revenue" is SUM(revenue), which includes freight, across all order statuses
  unless the question says "delivered".
- "Average order value" is SUM(revenue) / COUNT(DISTINCT order_id).
- review_score, delivery_days, delay_days, is_late and payment_type are
  order-level values repeated on every item row. To average them per order,
  first reduce to one row per order, for example:
  SELECT AVG(score) FROM (SELECT order_id, MAX(review_score) AS score FROM sales GROUP BY order_id)
- "Late" means is_late = 1. A late-delivery rate is late orders divided by
  delivered orders (delivered_date IS NOT NULL).
- A percentage is a number between 0 and 100.
"""


def schema_text() -> str:
    lines: list[str] = []
    for table in TABLES:
        lines.append(f"TABLE {table.name}: {table.description}")
        for col in table.columns:
            lines.append(f"  {col.name} {col.type}: {col.description}")
        lines.append("")
    return "\n".join(lines).rstrip()


def system_prompt() -> str:
    return f"{RULES}\nSchema:\n{schema_text()}\n"


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="run_sql",
        description=(
            "Run one read-only SQLite SELECT statement and return the first rows. "
            "Use it to test a query before submitting."
        ),
        parameters={
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "A single SELECT statement."}},
            "required": ["sql"],
        },
    ),
    ToolSpec(
        name="submit_answer",
        description=(
            "Finish with the final SQL and a short answer. The SQL is executed again, "
            "and its result is what the user sees."
        ),
        parameters={
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "The final SELECT statement."},
                "answer": {"type": "string", "description": "One or two sentences stating the result."},
                "follow_ups": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Up to three short follow-up questions the dataset can answer.",
                },
            },
            "required": ["sql", "answer"],
        },
    ),
    ToolSpec(
        name="cannot_answer",
        description="Finish without SQL because the dataset cannot answer the question.",
        parameters={
            "type": "object",
            "properties": {"reason": {"type": "string", "description": "Why it cannot be answered."}},
            "required": ["reason"],
        },
    ),
]
