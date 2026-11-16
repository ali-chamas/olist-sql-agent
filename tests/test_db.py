"""The guardrails are the security boundary, so each one is tested on its own."""

from __future__ import annotations

import sqlite3

import pytest

from sqlagent.db import QueryError, ReadOnlyDatabase, check_statement, strip_sql


def test_select_returns_columns_and_rows(db: ReadOnlyDatabase) -> None:
    result = db.execute("SELECT order_id, revenue FROM sales WHERE order_id = 'o2'")
    assert result.columns == ["order_id", "revenue"]
    assert result.rows == [("o2", 120.0)]
    assert not result.truncated


def test_cte_and_subquery_are_allowed(db: ReadOnlyDatabase) -> None:
    sql = "WITH t AS (SELECT order_id, SUM(revenue) r FROM sales GROUP BY 1) SELECT COUNT(*) FROM t"
    assert db.execute(sql).rows == [(5,)]
    assert db.execute("SELECT n FROM (SELECT COUNT(*) n FROM customers)").rows == [(3,)]


def test_recursive_cte_is_allowed(db: ReadOnlyDatabase) -> None:
    sql = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 3) SELECT SUM(x) FROM c"
    assert db.execute(sql).rows == [(6,)]


def test_comments_and_trailing_semicolon_are_stripped(db: ReadOnlyDatabase) -> None:
    assert strip_sql("/* note */ SELECT 1; -- done") == "SELECT 1"
    assert db.execute("-- how many\nSELECT COUNT(*) FROM sellers;").rows == [(2,)]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO sales (order_id) VALUES ('x')",
        "UPDATE sales SET price = 0",
        "DELETE FROM sales",
        "DROP TABLE sales",
        "CREATE TABLE t (a)",
        "PRAGMA table_info(sales)",
        "ATTACH DATABASE 'other.db' AS other",
        "VACUUM",
        "",
        "   ",
    ],
)
def test_non_select_statements_are_rejected_by_the_text_check(db: ReadOnlyDatabase, sql: str) -> None:
    with pytest.raises(QueryError) as info:
        db.execute(sql)
    assert info.value.kind == "rejected"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM sqlite_master",
        "SELECT COUNT(*) FROM sqlite_schema",
        "SELECT * FROM pragma_table_info('sales')",
        "SELECT COUNT(*) FROM temp.sqlite_temp_master",
        "SELECT load_extension('evil')",
        "WITH x AS (SELECT 1) SELECT * FROM sqlite_master",
    ],
)
def test_selects_outside_the_catalogue_are_rejected_by_the_authorizer(db: ReadOnlyDatabase, sql: str) -> None:
    # These pass the text check, so only the SQLite authorizer stands in the way.
    check_statement(sql)
    with pytest.raises(QueryError) as info:
        db.execute(sql)
    assert info.value.kind == "rejected"


def test_multiple_statements_are_rejected(db: ReadOnlyDatabase) -> None:
    with pytest.raises(QueryError) as info:
        db.execute("SELECT 1; DELETE FROM sales")
    assert info.value.kind == "rejected"


def test_write_hidden_in_a_cte_is_rejected_and_data_is_unchanged(db: ReadOnlyDatabase) -> None:
    with pytest.raises(QueryError):
        db.execute("WITH t AS (SELECT 1) DELETE FROM sales")
    assert db.execute("SELECT COUNT(*) FROM sales").rows == [(6,)]


def test_connection_is_read_only_even_without_the_authorizer(db: ReadOnlyDatabase) -> None:
    conn = db._connect()
    conn.set_authorizer(None)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM sales")
    conn.close()


def test_sql_errors_are_reported_with_the_sqlite_message(db: ReadOnlyDatabase) -> None:
    with pytest.raises(QueryError) as info:
        db.execute("SELECT no_such_column FROM sales")
    assert info.value.kind == "sql_error"
    assert "no_such_column" in info.value.message


def test_long_running_query_times_out(db_path) -> None:
    fast_fail = ReadOnlyDatabase(db_path, timeout_s=0.2)
    endless = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT COUNT(*) FROM c"
    with pytest.raises(QueryError) as info:
        fast_fail.execute(endless)
    assert info.value.kind == "timeout"


def test_rows_are_capped_and_flagged(db_path) -> None:
    small = ReadOnlyDatabase(db_path, max_rows=4)
    result = small.execute("SELECT order_id FROM sales")
    assert result.row_count == 4
    assert result.truncated
    assert small.execute("SELECT order_id FROM sales", max_rows=100).row_count == 6


def test_missing_database_file_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        ReadOnlyDatabase(tmp_path / "missing.db")
