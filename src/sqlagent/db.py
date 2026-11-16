"""Read-only query execution with layered guardrails.

The model writes SQL, so every query is treated as untrusted input. Four
independent layers have to agree before a row comes back:

1. A text check rejects anything that is not a single SELECT/WITH statement.
   It exists to give the model a clear error message, not for security.
2. The connection is opened read-only (SQLite URI ``mode=ro``).
3. A SQLite authorizer allows reads of the catalogued tables (and of CTEs the
   query defines itself) and nothing else: no writes, no PRAGMA, no ATTACH,
   no schema tables, no extension loading.
4. A progress handler aborts queries that exceed the time budget, and the
   result is capped at ``max_rows``.

Layers 2 and 3 are enforced by SQLite itself while it compiles the statement,
so they hold even if the text check is fooled.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from .catalog import TABLE_NAMES

_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_ALLOWED_START = ("select", "with")
_BLOCKED_TABLE_PREFIXES = ("sqlite_", "pragma_")
_BLOCKED_FUNCTIONS = frozenset({"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer"})


class QueryError(Exception):
    """A query that was refused or failed. ``kind`` says which."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind  # "rejected" | "sql_error" | "timeout"
        self.message = message


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    truncated: bool
    elapsed_ms: float
    sql: str = field(default="", repr=False)

    @property
    def row_count(self) -> int:
        return len(self.rows)


def strip_sql(sql: str) -> str:
    """Remove comments, surrounding whitespace and one trailing semicolon."""
    cleaned = _COMMENT.sub(" ", sql or "").strip()
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].rstrip()
    return cleaned


def check_statement(sql: str) -> str:
    """Return the cleaned statement or raise QueryError('rejected')."""
    cleaned = strip_sql(sql)
    if not cleaned:
        raise QueryError("rejected", "Empty query.")
    first = cleaned.split(None, 1)[0].lower()
    if first not in _ALLOWED_START:
        raise QueryError("rejected", "Only a single SELECT statement (optionally starting with WITH) is allowed.")
    return cleaned


def _authorizer(action: int, arg1: str | None, arg2: str | None, db_name: str | None, source: str | None) -> int:
    if action == sqlite3.SQLITE_SELECT or action == sqlite3.SQLITE_RECURSIVE:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:
        table = (arg1 or "").lower()
        if not table or table in TABLE_NAMES:
            return sqlite3.SQLITE_OK
        # Recursive CTEs are reported under their own name with no database.
        # Real objects outside the catalogue are schema tables (sqlite_*) or
        # table-valued pragmas (pragma_*), or carry a database name.
        if db_name is None and not table.startswith(_BLOCKED_TABLE_PREFIXES):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION:
        if arg2 and arg2.lower() in _BLOCKED_FUNCTIONS:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


class ReadOnlyDatabase:
    """Executes guarded SELECT statements against one SQLite file.

    A new connection is opened per query. SQLite connections are cheap, and
    this keeps the class safe to call from a thread pool.
    """

    def __init__(self, path: str | Path, max_rows: int = 200, timeout_s: float = 5.0) -> None:
        self.path = Path(path)
        self.max_rows = max_rows
        self.timeout_s = timeout_s
        if not self.path.exists():
            raise FileNotFoundError(f"Database not found: {self.path}. Run scripts/build_db.py first.")

    def _connect(self) -> sqlite3.Connection:
        uri = f"file:{self.path.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.set_authorizer(_authorizer)
        return conn

    def execute(self, sql: str, max_rows: int | None = None) -> QueryResult:
        cleaned = check_statement(sql)
        limit = self.max_rows if max_rows is None else max_rows
        deadline = time.monotonic() + self.timeout_s
        started = time.monotonic()
        conn = self._connect()
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        try:
            cursor = conn.execute(cleaned)
            if cursor.description is None:
                raise QueryError("rejected", "The statement did not return rows.")
            columns = [d[0] for d in cursor.description]
            rows = cursor.fetchmany(limit + 1)
        except QueryError:
            raise
        except sqlite3.DatabaseError as exc:
            raise self._translate(exc, deadline) from exc
        except sqlite3.Warning as exc:  # raised for multiple statements on older Pythons
            raise QueryError("rejected", "Only one statement per query is allowed.") from exc
        finally:
            conn.close()
        truncated = len(rows) > limit
        return QueryResult(
            columns=columns,
            rows=rows[:limit],
            truncated=truncated,
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
            sql=cleaned,
        )

    def _translate(self, exc: sqlite3.DatabaseError, deadline: float) -> QueryError:
        message = str(exc)
        lowered = message.lower()
        if "interrupted" in lowered and time.monotonic() > deadline:
            return QueryError("timeout", f"Query exceeded the {self.timeout_s:g}s time limit. Simplify it.")
        if "not authorized" in lowered or "prohibited" in lowered or "readonly" in lowered:
            return QueryError(
                "rejected",
                "Query refused: only SELECT on the tables sales, customers, products, sellers and dates is allowed.",
            )
        if "one statement at a time" in lowered:
            return QueryError("rejected", "Only one statement per query is allowed.")
        return QueryError("sql_error", f"SQL error: {message}")
