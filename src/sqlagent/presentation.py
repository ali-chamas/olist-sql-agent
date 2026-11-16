"""Turn a query result into something the UI can draw.

The chart form is chosen by deterministic rules, not by the model: the rules
are cheap, testable and cannot hallucinate a column. The payload carries its
own data, so the frontend only draws.

Forms:
    kpi    one row of headline numbers            -> stat tiles
    line   a value over time (4 or more periods)  -> line chart, up to 3 series
    bar    a value across 2 to 25 categories      -> ranked horizontal bars
    table  everything else
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

MAX_BAR_ROWS = 25
MAX_LINE_SERIES = 3
MIN_LINE_POINTS = 4
MAX_KPIS = 6

_TEMPORAL = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")
_CURRENCY_WORDS = ("revenue", "price", "freight", "aov", "order_value", "sales", "amount", "brl")
_PERCENT_WORDS = ("pct", "percent", "percentage", "rate", "share", "growth")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def value_format(column: str) -> str:
    """Display hint from the column name: 'currency', 'percent' or 'number'."""
    name = column.lower()
    if any(word in name for word in _PERCENT_WORDS):
        return "percent"
    if any(word in name for word in _CURRENCY_WORDS):
        return "currency"
    return "number"


def classify_column(name: str, values: Sequence[Any]) -> str:
    """'numeric', 'temporal', 'text' or 'empty'."""
    present = [v for v in values if v is not None]
    if not present:
        return "empty"
    if all(isinstance(v, str) and _TEMPORAL.match(v) for v in present):
        return "temporal"
    if all(_is_number(v) for v in present):
        if "year" in name.lower() and all(float(v).is_integer() and 1990 <= v <= 2100 for v in present):
            return "temporal"
        return "numeric"
    return "text"


def _is_ordinal_key(values: Sequence[Any]) -> bool:
    """Small distinct whole numbers, such as review scores 1 to 5: a category, not a measure."""
    return (
        all(_is_number(v) and float(v).is_integer() for v in values)
        and len(set(values)) == len(values)
        and max(values) - min(values) <= 100
    )


def suggest_chart(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> dict[str, Any]:
    if not columns or not rows:
        return {"type": "table"}
    cols = [[row[i] for row in rows] for i in range(len(columns))]
    kinds = [classify_column(name, values) for name, values in zip(columns, cols, strict=True)]
    numeric = [i for i, k in enumerate(kinds) if k == "numeric"]
    temporal = [i for i, k in enumerate(kinds) if k == "temporal"]
    text = [i for i, k in enumerate(kinds) if k == "text"]

    if len(rows) == 1:
        return _kpi(columns, rows[0], numeric, text)
    if temporal and numeric and len(rows) >= MIN_LINE_POINTS:
        return _line(columns, rows, temporal[0], numeric)
    if len(rows) > MAX_BAR_ROWS or not numeric:
        return {"type": "table"}

    if text and len(text) <= 2:
        labels = [", ".join(str(row[i]) for i in text if row[i] is not None) or "(none)" for row in rows]
        return _bar(columns, rows, labels, " and ".join(columns[i] for i in text), numeric[0], ranked=True)
    if temporal:
        labels = [str(row[temporal[0]]) for row in rows]
        return _bar(columns, rows, labels, columns[temporal[0]], numeric[0], ranked=False, sort_labels=True)
    if not text and len(numeric) >= 2 and _is_ordinal_key(cols[numeric[0]]):
        key = numeric[0]
        labels = [str(int(row[key])) for row in rows]
        return _bar(columns, rows, labels, columns[key], numeric[1], ranked=False, sort_labels=True)
    return {"type": "table"}


def _kpi(columns: Sequence[str], row: Sequence[Any], numeric: list[int], text: list[int]) -> dict[str, Any]:
    if not numeric or len(numeric) > MAX_KPIS:
        return {"type": "table"}
    context = ", ".join(str(row[i]) for i in text) if text else ""
    return {
        "type": "kpi",
        "context": context,
        "items": [{"label": columns[i], "value": row[i], "format": value_format(columns[i])} for i in numeric],
    }


def _line(columns: Sequence[str], rows: Sequence[Sequence[Any]], x: int, numeric: list[int]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda r: str(r[x]) if r[x] is not None else "")
    series = [
        {"name": columns[i], "format": value_format(columns[i]), "values": [r[i] for r in ordered]}
        for i in numeric[:MAX_LINE_SERIES]
    ]
    # Series with different units cannot share one axis: keep those matching the first.
    series = [s for s in series if s["format"] == series[0]["format"]]
    return {"type": "line", "x": columns[x], "labels": [str(r[x]) for r in ordered], "series": series}


def _bar(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    labels: list[str],
    label_name: str,
    value: int,
    ranked: bool,
    sort_labels: bool = False,
) -> dict[str, Any]:
    pairs = [(label, row[value]) for label, row in zip(labels, rows, strict=True) if row[value] is not None]
    if len(pairs) < 2:
        return {"type": "table"}
    if ranked:
        pairs.sort(key=lambda p: p[1], reverse=True)
    elif sort_labels:
        pairs.sort(key=lambda p: (float(p[0]) if p[0].lstrip("-").isdigit() else 0, p[0]))
    return {
        "type": "bar",
        "x": label_name,
        "y": columns[value],
        "format": value_format(columns[value]),
        "labels": [p[0] for p in pairs],
        "values": [p[1] for p in pairs],
    }


CHART_TYPES = ("auto", "kpi", "bar", "line", "table")


class ChartMismatch(ValueError):
    """The requested chart type cannot show this result. The message says why, for the model."""


def chart_for(columns: Sequence[str], rows: Sequence[Sequence[Any]], chart: str = "auto") -> dict[str, Any]:
    """The chart payload for a requested type. 'auto' never fails; the others raise ChartMismatch."""
    if chart == "auto":
        return suggest_chart(columns, rows)
    if chart == "table":
        return {"type": "table"}
    if chart not in CHART_TYPES:
        raise ChartMismatch(f"Unknown chart type {chart!r}. Use one of: {', '.join(CHART_TYPES)}.")
    if not rows:
        raise ChartMismatch("The query returned no rows.")
    cols = [[row[i] for row in rows] for i in range(len(columns))]
    kinds = [classify_column(name, values) for name, values in zip(columns, cols, strict=True)]
    numeric = [i for i, k in enumerate(kinds) if k == "numeric"]
    temporal = [i for i, k in enumerate(kinds) if k == "temporal"]
    text = [i for i, k in enumerate(kinds) if k == "text"]

    if chart == "kpi":
        if len(rows) != 1 or not numeric or len(numeric) > MAX_KPIS:
            raise ChartMismatch(f"A kpi tile needs exactly one row with 1 to {MAX_KPIS} numeric columns.")
        return _kpi(columns, rows[0], numeric, text)
    if chart == "line":
        if not temporal or not numeric or len(rows) < 2:
            raise ChartMismatch(
                "A line chart needs a time column (YYYY-MM, YYYY-MM-DD or a year) and a numeric column, "
                "with at least 2 rows."
            )
        return _line(columns, rows, temporal[0], numeric)
    # bar
    if not numeric or len(rows) > MAX_BAR_ROWS:
        raise ChartMismatch(f"A bar chart needs a numeric column and at most {MAX_BAR_ROWS} rows. Add a LIMIT.")
    if text and len(text) <= 2:
        labels = [", ".join(str(row[i]) for i in text if row[i] is not None) or "(none)" for row in rows]
        result = _bar(columns, rows, labels, " and ".join(columns[i] for i in text), numeric[0], ranked=True)
    elif temporal:
        labels = [str(row[temporal[0]]) for row in rows]
        result = _bar(columns, rows, labels, columns[temporal[0]], numeric[0], ranked=False, sort_labels=True)
    elif len(numeric) >= 2:
        labels = [str(row[numeric[0]]) for row in rows]
        result = _bar(columns, rows, labels, columns[numeric[0]], numeric[1], ranked=False, sort_labels=True)
    else:
        raise ChartMismatch("A bar chart needs a label column (text, a date or a small number) and a numeric column.")
    if result["type"] != "bar":
        raise ChartMismatch("A bar chart needs at least 2 rows with values.")
    return result


def result_payload(columns: Sequence[str], rows: Sequence[Sequence[Any]], truncated: bool = False) -> dict[str, Any]:
    """Columns, rows and the suggested chart, ready for JSON."""
    return {
        "columns": list(columns),
        "rows": [list(r) for r in rows],
        "truncated": truncated,
        "chart": suggest_chart(columns, rows),
    }
