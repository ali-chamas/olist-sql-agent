from __future__ import annotations

import pytest

from sqlagent.db import ReadOnlyDatabase
from sqlagent.presentation import ChartMismatch, chart_for
from sqlagent.workspace import (
    Conflict,
    EditError,
    NotFound,
    Workspace,
    add_tile,
    move_tile,
    remove_tile,
    summarize,
    update_details,
    update_tile,
)

BY_STATE = (
    "SELECT c.state, COUNT(DISTINCT s.order_id) AS orders FROM sales s JOIN customers c USING (customer_key) GROUP BY 1"
)
BY_MONTH = "SELECT strftime('%Y-%m', order_date) AS month, SUM(revenue) AS revenue FROM sales GROUP BY 1"
TOTALS = "SELECT SUM(revenue) AS revenue, COUNT(DISTINCT order_id) AS orders FROM sales"


@pytest.fixture()
def ws(tmp_path) -> Workspace:
    workspace = Workspace(tmp_path / "app.db")
    yield workspace
    workspace.close()


# ---------------------------------------------------------- chart types


def test_requested_chart_types_are_checked_against_the_result(db: ReadOnlyDatabase) -> None:
    by_state = db.execute(BY_STATE)
    assert chart_for(by_state.columns, by_state.rows, "bar")["type"] == "bar"
    assert chart_for(by_state.columns, by_state.rows, "table") == {"type": "table"}
    with pytest.raises(ChartMismatch, match="time column"):
        chart_for(by_state.columns, by_state.rows, "line")
    with pytest.raises(ChartMismatch, match="one row"):
        chart_for(by_state.columns, by_state.rows, "kpi")
    months = db.execute(BY_MONTH)
    assert chart_for(months.columns, months.rows, "line")["type"] == "line"  # 4 months; forced lines accept 2+
    totals = db.execute(TOTALS)
    assert chart_for(totals.columns, totals.rows, "kpi")["type"] == "kpi"
    with pytest.raises(ChartMismatch, match="Unknown"):
        chart_for(totals.columns, totals.rows, "pie")


# -------------------------------------------------------------- editing


def test_add_tile_validates_sql_chart_and_picks_a_size(db: ReadOnlyDatabase, ws: Workspace) -> None:
    board = ws.create("Marketing")
    change = add_tile(board, db, "Orders by state", BY_STATE, "bar")
    assert change.kind == "added" and board.tiles[0].size == "half" and board.tiles[0].chart == "bar"
    add_tile(board, db, "Revenue by month", BY_MONTH, "line")
    assert board.tiles[1].size == "full"
    add_tile(board, db, "Totals", TOTALS, position=0)
    assert [t.title for t in board.tiles] == ["Totals", "Orders by state", "Revenue by month"]

    with pytest.raises(EditError, match="Only a single SELECT"):
        add_tile(board, db, "Bad", "DELETE FROM sales")
    with pytest.raises(EditError, match="time column"):
        add_tile(board, db, "Wrong chart", BY_STATE, "line")
    with pytest.raises(EditError, match="title"):
        add_tile(board, db, "  ", BY_STATE)
    with pytest.raises(EditError, match="size"):
        add_tile(board, db, "Huge", BY_STATE, size="huge")
    assert len(board.tiles) == 3  # refused edits change nothing


def test_update_move_remove_and_rename(db: ReadOnlyDatabase, ws: Workspace) -> None:
    board = ws.create("Marketing")
    add_tile(board, db, "Orders by state", BY_STATE)
    add_tile(board, db, "Totals", TOTALS)
    first, second = board.tiles

    update_tile(board, db, first.id, title="Orders per state", size="full")
    assert board.tiles[0].title == "Orders per state" and board.tiles[0].size == "full"
    update_tile(board, db, first.id, chart="table")
    assert board.tiles[0].chart == "table" and board.tiles[0].sql == first.sql
    with pytest.raises(EditError, match="time column"):
        update_tile(board, db, first.id, chart="line")
    with pytest.raises(EditError, match="Nothing to change"):
        update_tile(board, db, first.id, title="Orders per state")
    with pytest.raises(EditError, match="No tile"):
        update_tile(board, db, "t_missing", title="x")

    move_tile(board, second.id, 0)
    assert board.tiles[0].id == second.id
    move_tile(board, second.id, 99)
    assert board.tiles[-1].id == second.id
    remove_tile(board, second.id)
    assert [t.id for t in board.tiles] == [first.id]

    update_details(board, title="Growth", description="Weekly review")
    assert (board.title, board.description) == ("Growth", "Weekly review")
    with pytest.raises(EditError):
        update_details(board)


def test_summaries_read_naturally() -> None:
    from sqlagent.workspace import Change

    assert summarize([]) == "No changes"
    assert summarize([Change("added", "Revenue")]) == "Added “Revenue”"
    changes = [Change("added", "A"), Change("added", "B"), Change("removed", "C"), Change("renamed", "Growth")]
    assert summarize(changes) == "Added 2 tiles, Removed “C”, Renamed the dashboard"


# -------------------------------------------------------------- storage


def test_save_versions_history_and_restore(db: ReadOnlyDatabase, ws: Workspace) -> None:
    board = ws.create("Marketing", "For the weekly meeting")
    assert board.version == 1 and board.tiles == []
    add_tile(board, db, "Orders by state", BY_STATE)
    board = ws.save(board, author="ai", summary="Added a tile", expected_version=1)
    assert board.version == 2 and len(board.tiles) == 1

    remove_tile(board, board.tiles[0].id)
    board = ws.save(board, author="you", summary="Removed it", expected_version=2)
    history = ws.revisions(board.id)
    assert [(r["version"], r["author"], r["tile_count"]) for r in history] == [
        (3, "you", 0),
        (2, "ai", 1),
        (1, "you", 0),
    ]

    restored = ws.restore(board.id, 2)
    assert restored.version == 4 and [t.title for t in restored.tiles] == ["Orders by state"]
    assert ws.revisions(board.id)[0]["summary"] == "Restored version 2"


def test_stale_edits_are_refused(ws: Workspace) -> None:
    board = ws.create("Marketing")
    ws.save(board, author="you", summary="first", expected_version=1)
    with pytest.raises(Conflict, match="changed"):
        ws.save(board, author="you", summary="second", expected_version=1)


def test_list_get_delete(ws: Workspace) -> None:
    a = ws.create("A")
    ws.create("B")
    assert {d["title"] for d in ws.list()} == {"A", "B"}
    assert ws.list()[0]["tile_count"] == 0
    ws.delete(a.id)
    with pytest.raises(NotFound):
        ws.get(a.id)
    with pytest.raises(NotFound):
        ws.delete(a.id)
    with pytest.raises(NotFound):
        ws.restore("d_missing", 1)


def test_data_survives_reopening(db: ReadOnlyDatabase, tmp_path) -> None:
    path = tmp_path / "app.db"
    first = Workspace(path)
    board = first.create("Kept")
    add_tile(board, db, "Totals", TOTALS)
    first.save(board, author="you", summary="x")
    first.close()
    again = Workspace(path)
    assert [t.title for t in again.get(board.id).tiles] == ["Totals"]
    again.close()


def test_titles_are_cleaned_and_bounded(ws: Workspace) -> None:
    assert ws.create("  Spaced   out  ").title == "Spaced out"
    assert ws.create("").title == "Untitled dashboard"
    with pytest.raises(EditError, match="longer"):
        ws.create("x" * 121)


def test_text_tiles_keep_line_breaks_and_refuse_queries(ws) -> None:
    from sqlagent.workspace import EditError, add_text_tile, update_tile

    board = ws.create("Notes")
    add_text_tile(board, "Delivery", "Late orders get worse reviews.\n\n\n\nSee below.  ")
    tile = board.tiles[0]
    assert (tile.chart, tile.sql, tile.size, tile.note) == (
        "text",
        "",
        "full",
        "Late orders get worse reviews.\n\nSee below.",
    )
    with pytest.raises(EditError):
        update_tile(board, None, tile.id, sql="SELECT 1")
    update_tile(board, None, tile.id, note="Updated.")
    saved = ws.save(board, "you", "text")
    assert saved.tiles[0].note == "Updated."
    with pytest.raises(EditError):
        add_text_tile(board, "", "  ")


def test_duplicate_copies_tiles_and_filters_with_new_ids(ws) -> None:
    from sqlagent.workspace import add_text_tile

    board = ws.create("Growth")
    add_text_tile(board, "Intro", "Hello")
    board.filters = {"state": "SP"}
    board = ws.save(board, "you", "setup")
    copy = ws.duplicate(board.id)
    assert copy.id != board.id and copy.title == "Copy of Growth"
    assert copy.filters == {"state": "SP"} and copy.tiles[0].note == "Hello"
    assert copy.tiles[0].id != board.tiles[0].id
    assert ws.revisions(copy.id)[0]["summary"] == "Copied 1 tile"


def test_old_app_databases_gain_the_filters_column(tmp_path) -> None:
    import sqlite3

    from sqlagent.workspace import Workspace

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE dashboards (id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', "
        "version INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO dashboards VALUES ('d_old', 'Old', '', 1, 'x', 'x')")
    conn.commit()
    conn.close()
    workspace = Workspace(path)
    assert workspace.get("d_old").filters == {}
    workspace.close()
