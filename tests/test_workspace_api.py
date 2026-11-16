from __future__ import annotations

import pytest

from conftest import FakeLLM, asgi_request, call, parse_sse
from sqlagent.agent import SQLAgent
from sqlagent.app import AppConfig, create_app
from sqlagent.db import ReadOnlyDatabase
from sqlagent.llm import LLMError
from sqlagent.workspace import Workspace

BY_STATE = (
    "SELECT c.state, COUNT(DISTINCT s.order_id) AS orders FROM sales s JOIN customers c USING (customer_key) GROUP BY 1"
)
TOTALS = "SELECT SUM(revenue) AS revenue, COUNT(DISTINCT order_id) AS orders FROM sales"


@pytest.fixture()
def ws(tmp_path) -> Workspace:
    workspace = Workspace(tmp_path / "app.db")
    yield workspace
    workspace.close()


def make(db, ws, *script, **config):
    llm = FakeLLM(list(script))
    return create_app(SQLAgent(llm, db), AppConfig(**config), ws), llm


def test_create_list_get_rename_delete(db: ReadOnlyDatabase, ws: Workspace) -> None:
    app, _ = make(db, ws)
    status, created = asgi_request(app, "POST", "/api/dashboards", {"title": "Marketing"})
    assert status == 201 and created["version"] == 1
    did = created["id"]
    assert [d["title"] for d in asgi_request(app, "GET", "/api/dashboards")[1]["dashboards"]] == ["Marketing"]
    status, renamed = asgi_request(app, "PATCH", f"/api/dashboards/{did}", {"title": "Growth", "version": 1})
    assert status == 200 and renamed["title"] == "Growth" and renamed["version"] == 2
    assert asgi_request(app, "PATCH", f"/api/dashboards/{did}", {"title": "Stale", "version": 1})[0] == 409
    assert asgi_request(app, "GET", f"/api/dashboards/{did}")[1]["title"] == "Growth"
    assert asgi_request(app, "DELETE", f"/api/dashboards/{did}")[0] == 204
    assert asgi_request(app, "GET", f"/api/dashboards/{did}")[0] == 404


def test_tiles_by_hand_and_tile_data(db: ReadOnlyDatabase, ws: Workspace) -> None:
    app, _ = make(db, ws)
    did = asgi_request(app, "POST", "/api/dashboards", {"title": "Marketing"})[1]["id"]
    status, board = asgi_request(app, "POST", f"/api/dashboards/{did}/tiles", {"title": "By state", "sql": BY_STATE})
    assert status == 200 and board["tiles"][0]["chart"] == "auto"
    tid = board["tiles"][0]["id"]

    status, data = asgi_request(app, "GET", f"/api/dashboards/{did}/tiles/{tid}/data")
    assert status == 200 and data["chart"]["type"] == "bar" and data["rows"]

    status, body = asgi_request(
        app, "POST", f"/api/dashboards/{did}/tiles", {"title": "Bad", "sql": "DROP TABLE sales"}
    )
    assert status == 422 and "SELECT" in body["error"]
    assert asgi_request(app, "PATCH", f"/api/dashboards/{did}/tiles/{tid}", {"chart": "line"})[0] == 422
    status, board = asgi_request(app, "PATCH", f"/api/dashboards/{did}/tiles/{tid}", {"chart": "table", "size": "full"})
    assert status == 200 and board["tiles"][0]["chart"] == "table"

    asgi_request(app, "POST", f"/api/dashboards/{did}/tiles", {"title": "Totals", "sql": TOTALS})
    status, board = asgi_request(app, "POST", f"/api/dashboards/{did}/tiles/{tid}/move", {"position": 1})
    assert [t["title"] for t in board["tiles"]] == ["Totals", "By state"]
    status, board = asgi_request(app, "DELETE", f"/api/dashboards/{did}/tiles/{tid}")
    assert status == 200 and [t["title"] for t in board["tiles"]] == ["Totals"]
    assert asgi_request(app, "GET", f"/api/dashboards/{did}/tiles/{tid}/data")[0] == 404


def test_history_and_restore(db: ReadOnlyDatabase, ws: Workspace) -> None:
    app, _ = make(db, ws)
    did = asgi_request(app, "POST", "/api/dashboards", {"title": "Marketing"})[1]["id"]
    asgi_request(app, "POST", f"/api/dashboards/{did}/tiles", {"title": "Totals", "sql": TOTALS})
    history = asgi_request(app, "GET", f"/api/dashboards/{did}/revisions")[1]["revisions"]
    assert [r["version"] for r in history] == [2, 1] and history[0]["summary"] == "Added “Totals”"
    status, board = asgi_request(app, "POST", f"/api/dashboards/{did}/restore", {"version": 1})
    assert status == 200 and board["tiles"] == [] and board["version"] == 3
    assert asgi_request(app, "POST", f"/api/dashboards/{did}/restore", {"version": "1"})[0] == 422
    assert asgi_request(app, "POST", f"/api/dashboards/{did}/restore", {"version": 99})[0] == 404


def test_agent_edit_streams_steps_and_saves_one_version(db: ReadOnlyDatabase, ws: Workspace) -> None:
    app, llm = make(
        db,
        ws,
        call("add_tile", title="Orders by state", sql=BY_STATE, chart="bar"),
        call("add_tile", title="Totals", sql=TOTALS, chart="kpi", position=0),
        call("finish", summary="Added two tiles."),
    )
    did = asgi_request(app, "POST", "/api/dashboards", {"title": "Marketing"})[1]["id"]
    status, headers, payload = asgi_request(
        app, "POST", f"/api/dashboards/{did}/agent", {"prompt": "add basics"}, raw=True
    )
    assert status == 200 and headers["content-type"].startswith("text/event-stream")
    events = parse_sse(payload)
    assert [n for n, _ in events].count("step") == 3
    result = events[-1][1]
    assert result["status"] == "done" and result["saved"] is True
    assert [t["title"] for t in result["dashboard"]["tiles"]] == ["Totals", "Orders by state"]
    assert result["dashboard"]["version"] == 2  # three tool calls, one version
    assert asgi_request(app, "GET", f"/api/dashboards/{did}/revisions")[1]["revisions"][0]["author"] == "ai"
    assert "Current dashboard" in llm.calls[0][0].content


def test_agent_focus_tile_and_refusals(db: ReadOnlyDatabase, ws: Workspace) -> None:
    app, llm = make(db, ws, call("cannot_do", reason="No ad spend data."), ask_per_minute=2)
    did = asgi_request(app, "POST", "/api/dashboards", {"title": "Marketing"})[1]["id"]
    assert asgi_request(app, "POST", f"/api/dashboards/{did}/agent", {"prompt": "x"})[0] == 422
    assert asgi_request(app, "POST", f"/api/dashboards/{did}/agent", {"prompt": "edit it", "tile_id": "t_zz"})[0] == 404
    events = parse_sse(asgi_request(app, "POST", f"/api/dashboards/{did}/agent", {"prompt": "show ROAS"}, raw=True)[2])
    assert events[-1][1]["status"] == "cannot_do" and events[-1][1]["saved"] is False
    assert asgi_request(app, "GET", f"/api/dashboards/{did}")[1]["version"] == 1
    asgi_request(app, "POST", f"/api/dashboards/{did}/agent", {"prompt": "again"})
    assert asgi_request(app, "POST", f"/api/dashboards/{did}/agent", {"prompt": "third"})[0] == 429


def test_agent_edit_conflict_when_someone_saved_meanwhile(db: ReadOnlyDatabase, ws: Workspace) -> None:
    app, _ = make(db, ws, call("add_tile", title="Totals", sql=TOTALS), call("finish", summary="ok"))
    did = asgi_request(app, "POST", "/api/dashboards", {"title": "Marketing"})[1]["id"]
    asgi_request(app, "PATCH", f"/api/dashboards/{did}", {"title": "Renamed"})
    events = parse_sse(
        asgi_request(app, "POST", f"/api/dashboards/{did}/agent", {"prompt": "add", "version": 1}, raw=True)[2]
    )
    assert events[-1][1]["status"] == "conflict" and events[-1][1]["saved"] is False


def test_build_creates_a_dashboard_only_when_tiles_were_added(db: ReadOnlyDatabase, ws: Workspace) -> None:
    app, _ = make(
        db,
        ws,
        call("update_dashboard", title="Geography", description="Where customers are"),
        call("add_tile", title="Orders by state", sql=BY_STATE),
        call("finish", summary="Built it."),
        LLMError("HTTP 500"),
    )
    events = parse_sse(
        asgi_request(app, "POST", "/api/dashboards/build", {"prompt": "where are customers"}, raw=True)[2]
    )
    result = events[-1][1]
    assert result["saved"] is True and result["dashboard"]["title"] == "Geography"
    assert len(ws.list()) == 1
    failed = parse_sse(asgi_request(app, "POST", "/api/dashboards/build", {"prompt": "another one"}, raw=True)[2])
    assert failed[-1][1]["status"] == "llm_error" and failed[-1][1]["saved"] is False
    assert len(ws.list()) == 1


def test_routes_are_absent_without_a_workspace(db: ReadOnlyDatabase) -> None:
    app = create_app(SQLAgent(FakeLLM([]), db))
    assert asgi_request(app, "GET", "/api/dashboards")[0] == 404


def test_editable_copy_of_the_overview(db: ReadOnlyDatabase, ws: Workspace) -> None:
    app, _ = make(db, ws)
    status, board = asgi_request(app, "POST", "/api/dashboards/from-overview", {})
    assert status == 201 and board["title"] == "Marketplace overview" and len(board["tiles"]) == 6
    assert [t["chart"] for t in board["tiles"]][:2] == ["kpi", "line"]
    for tile in board["tiles"]:
        status, data = asgi_request(app, "GET", f"/api/dashboards/{board['id']}/tiles/{tile['id']}/data")
        assert status == 200 and "error" not in data, (tile["title"], data)
