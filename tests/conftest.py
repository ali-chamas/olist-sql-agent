"""Shared fixtures: a tiny database with the real schema, and a scripted model."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from sqlagent.catalog import TABLES, create_table_sql
from sqlagent.db import ReadOnlyDatabase
from sqlagent.llm import LLMError, LLMResponse, Message, ToolCall, ToolSpec, Usage

# fmt: off
# order_id, item, order_date, customer, product, seller, status, price, freight, revenue,
# order_rank, is_repeat, cohort_month, months_since_first, delivered, estimated,
# delivery_days, delay_days, is_late, review_score, payment_type, installments
SALES = [
    ("o1", 1, "2017-01-10", "c1", "p1", "s1", "delivered", 100.0, 10.0, 110.0,
     1, 0, "2017-01-01", 0, "2017-01-15", "2017-01-20", 5, -5, 0, 5, "credit_card", 2),
    ("o1", 2, "2017-01-10", "c1", "p2", "s1", "delivered", 50.0, 5.0, 55.0,
     1, 0, "2017-01-01", 0, "2017-01-15", "2017-01-20", 5, -5, 0, 5, "credit_card", 2),
    ("o2", 1, "2017-02-03", "c2", "p1", "s2", "delivered", 100.0, 20.0, 120.0,
     1, 0, "2017-02-01", 0, "2017-02-20", "2017-02-10", 17, 10, 1, 1, "boleto", 1),
    ("o3", 1, "2017-03-05", "c1", "p3", "s2", "delivered", 30.0, 5.0, 35.0,
     2, 1, "2017-01-01", 2, "2017-03-09", "2017-03-15", 4, -6, 0, 4, "credit_card", 1),
    ("o4", 1, "2018-01-20", "c3", "p2", "s1", "canceled", 50.0, 5.0, 55.0,
     1, 0, "2018-01-01", 0, None, "2018-02-01", None, None, 0, 2, "voucher", 1),
    ("o5", 1, "2018-02-11", "c2", "p3", "s1", "delivered", 30.0, 8.0, 38.0,
     2, 1, "2017-02-01", 12, "2018-02-16", "2018-02-14", 5, 2, 1, 3, "credit_card", 3),
]
CUSTOMERS = [
    ("c1", "Sao Paulo", "SP", "2017-01-10", "2017-01-01", 2, 1),
    ("c2", "Rio De Janeiro", "RJ", "2017-02-03", "2017-02-01", 2, 1),
    ("c3", "Campinas", "SP", "2018-01-20", "2018-01-01", 1, 0),
]
PRODUCTS = [
    ("p1", "Health Beauty", 500.0, 20.0, 10.0, 15.0, 2),
    ("p2", "Toys", 12000.0, 60.0, 40.0, 40.0, 1),
    ("p3", "Toys", 300.0, 10.0, 10.0, 10.0, 3),
]
SELLERS = [("s1", "Sao Paulo", "SP"), ("s2", "Curitiba", "PR")]
DATES = [
    ("2017-01-10", 2017, 1, "Jan", "2017-01", "Q1", "Tuesday", "2017-01-01"),
    ("2017-02-03", 2017, 2, "Feb", "2017-02", "Q1", "Friday", "2017-02-01"),
    ("2017-03-05", 2017, 3, "Mar", "2017-03", "Q1", "Sunday", "2017-03-01"),
    ("2018-01-20", 2018, 1, "Jan", "2018-01", "Q1", "Saturday", "2018-01-01"),
    ("2018-02-11", 2018, 2, "Feb", "2018-02", "Q1", "Sunday", "2018-02-01"),
]
# fmt: on
ROWS = {"sales": SALES, "customers": CUSTOMERS, "products": PRODUCTS, "sellers": SELLERS, "dates": DATES}


def build_fixture_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    with conn:
        for table in TABLES:
            conn.execute(create_table_sql(table))
            marks = ", ".join("?" for _ in table.columns)
            conn.executemany(f"INSERT INTO {table.name} VALUES ({marks})", ROWS[table.name])
    conn.close()
    return path


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return build_fixture_db(tmp_path / "test.db")


@pytest.fixture()
def db(db_path: Path) -> ReadOnlyDatabase:
    return ReadOnlyDatabase(db_path, max_rows=50, timeout_s=1.0)


class FakeLLM:
    """Returns scripted responses in order and records what it was sent."""

    model = "fake"

    def __init__(self, script: list[LLMResponse | Exception]) -> None:
        self.script = list(script)
        self.calls: list[list[Message]] = []

    def complete(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        self.calls.append(list(messages))
        if not self.script:
            raise LLMError("script exhausted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def call(name: str, call_id: str = "t1", **arguments: Any) -> LLMResponse:
    """A model turn consisting of one tool call."""
    return LLMResponse(text="", tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)], usage=Usage(100, 20))


def asgi_request(
    app,
    method: str,
    path: str,
    body: Any = None,
    client: str = "1.2.3.4",
    headers: dict[str, str] | None = None,
    raw: bool = False,
) -> tuple[int, Any]:
    """Drive an ASGI app directly, so the tests need no HTTP client library.

    Returns (status, parsed JSON) for JSON responses and (status, bytes) otherwise.
    With raw=True it returns (status, headers dict, bytes).
    """
    path, _, query = path.partition("?")
    data = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    request_headers = {"content-type": "application/json", "content-length": str(len(data)), **(headers or {})}
    # fmt: off
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
        "path": path, "raw_path": path.encode(), "query_string": query.encode(), "root_path": "", "scheme": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in request_headers.items()],
        "client": (client, 1234), "server": ("testserver", 80),
    }
    # fmt: on
    sent: list[dict] = []
    delivered = False

    async def receive() -> dict:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": data, "more_body": False}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    def finished() -> bool:
        return any(m["type"] == "http.response.body" and not m.get("more_body") for m in sent)

    async def run() -> None:
        task = asyncio.ensure_future(app(scope, receive, send))
        while not finished() and not task.done():
            await asyncio.sleep(0.005)
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(run())
    start = next(m for m in sent if m["type"] == "http.response.start")
    status = start["status"]
    response_headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    if raw:
        return status, response_headers, payload
    if response_headers.get("content-type", "").startswith("application/json"):
        return status, json.loads(payload)
    return status, payload


def parse_sse(payload: bytes) -> list[tuple[str, dict]]:
    """Split a text/event-stream body into (event name, data) pairs."""
    events = []
    for block in payload.decode().strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        events.append((lines["event"], json.loads(lines["data"])))
    return events
