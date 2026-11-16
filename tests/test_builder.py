from __future__ import annotations

from conftest import FakeLLM, call
from sqlagent.builder import DashboardAgent, build_request
from sqlagent.db import ReadOnlyDatabase
from sqlagent.llm import LLMError, LLMResponse, ToolCall, Usage
from sqlagent.workspace import Dashboard, Tile

BY_STATE = (
    "SELECT c.state, COUNT(DISTINCT s.order_id) AS orders FROM sales s JOIN customers c USING (customer_key) GROUP BY 1"
)
TOTALS = "SELECT SUM(revenue) AS revenue, COUNT(DISTINCT order_id) AS orders FROM sales"


def board() -> Dashboard:
    return Dashboard(
        id="d_1", title="Marketing", tiles=[Tile(id="t_a", title="Totals", sql=TOTALS, chart="kpi", size="full")]
    )


def turn(*calls: tuple[str, dict]) -> LLMResponse:
    tool_calls = [ToolCall(id=f"c{i}", name=name, arguments=args) for i, (name, args) in enumerate(calls)]
    return LLMResponse(text="", tool_calls=tool_calls, usage=Usage(100, 20))


def test_build_adds_tiles_renames_and_finishes(db: ReadOnlyDatabase) -> None:
    llm = FakeLLM(
        [
            turn(
                ("add_tile", {"title": "Orders by state", "sql": BY_STATE, "chart": "bar"}),
                ("update_dashboard", {"title": "Customer geography"}),
            ),
            call("finish", summary="Built a geography dashboard."),
        ]
    )
    original = board()
    result = DashboardAgent(llm, db).run(original, "Show where customers are")
    assert result.status == "done" and result.message == "Built a geography dashboard."
    assert [t.title for t in result.dashboard.tiles] == ["Totals", "Orders by state"]
    assert result.dashboard.title == "Customer geography"
    assert [c.kind for c in result.changes] == ["added", "renamed"]
    assert original.title == "Marketing" and len(original.tiles) == 1  # works on a copy
    tool_result = llm.calls[1][-2]
    assert "Added" in tool_result.content and "Tiles now:" in tool_result.content


def test_refused_edits_go_back_to_the_model_which_corrects_them(db: ReadOnlyDatabase) -> None:
    llm = FakeLLM(
        [
            call("add_tile", title="Orders by state", sql=BY_STATE, chart="line"),
            call("add_tile", title="Orders by state", sql=BY_STATE, chart="bar"),
            call("finish", summary="Added it."),
        ]
    )
    result = DashboardAgent(llm, db).run(board(), "add orders by state")
    assert result.status == "done" and len(result.dashboard.tiles) == 2
    assert result.steps[0].ok is False and "time column" in result.steps[0].error
    assert llm.calls[1][-1].is_error


def test_edit_remove_and_move_by_id(db: ReadOnlyDatabase) -> None:
    start = board()
    start.tiles.append(Tile(id="t_b", title="Orders by state", sql=BY_STATE, chart="bar"))
    llm = FakeLLM(
        [
            turn(("update_tile", {"tile_id": "t_b", "size": "full"}), ("move_tile", {"tile_id": "t_b", "position": 0})),
            turn(("remove_tile", {"tile_id": "t_a"}), ("remove_tile", {"tile_id": "t_missing"})),
            call("finish", summary="Done."),
        ]
    )
    result = DashboardAgent(llm, db).run(start, "make states full width and first, drop totals")
    assert [(t.id, t.size) for t in result.dashboard.tiles] == [("t_b", "full")]
    assert [s.ok for s in result.steps] == [True, True, True, False, True]
    assert "No tile" in result.steps[3].error


def test_cannot_do_discards_changes(db: ReadOnlyDatabase) -> None:
    llm = FakeLLM([call("add_tile", title="x", sql=TOTALS), call("cannot_do", reason="No marketing spend data.")])
    result = DashboardAgent(llm, db).run(board(), "show return on ad spend")
    assert result.status == "cannot_do" and result.changes == [] and "spend" in result.message


def test_provider_error_and_step_limit(db: ReadOnlyDatabase) -> None:
    assert DashboardAgent(FakeLLM([LLMError("HTTP 500")]), db).run(board(), "x").status == "llm_error"
    looping = FakeLLM([call("run_sql", sql="SELECT 1") for _ in range(5)])
    result = DashboardAgent(looping, db, max_steps=3).run(board(), "x")
    assert result.status == "max_steps" and result.llm_calls == 3


def test_events_stream_each_step(db: ReadOnlyDatabase) -> None:
    events = []
    llm = FakeLLM([call("add_tile", title="Orders by state", sql=BY_STATE), call("finish", summary="ok")])
    DashboardAgent(llm, db).run(board(), "x", on_event=events.append)
    assert [e["type"] for e in events] == ["thinking", "step", "thinking", "step"]
    assert events[1]["step"]["detail"] == "Added “Orders by state”"


def test_request_describes_the_dashboard_and_focus() -> None:
    text = build_request(board(), "make it a table", "t_a")
    assert '"id": "t_a"' in text and "tile t_a" in text and text.endswith("Request: make it a table")
    assert "empty" in build_request(Dashboard(id="", title="New"), "build", None)


def text_turn(text: str) -> LLMResponse:
    return LLMResponse(text=text, tool_calls=[], usage=Usage(100, 20))


def test_a_closing_message_in_prose_after_changes_finishes_the_run(db: ReadOnlyDatabase) -> None:
    events: list[dict] = []
    llm = FakeLLM([call("remove_tile", tile_id="t_a"), text_turn("Removed the totals tile.")])
    result = DashboardAgent(llm, db).run(board(), "remove totals", on_event=events.append)
    assert result.status == "done" and result.message == "Removed the totals tile."
    assert result.dashboard.tiles == [] and len(llm.calls) == 2
    assert [e["type"] for e in events] == ["thinking", "step", "thinking", "step"]


def test_prose_before_any_change_gets_one_more_chance(db: ReadOnlyDatabase) -> None:
    llm = FakeLLM(
        [
            text_turn("I will add it now."),
            call("add_tile", title="By state", sql=BY_STATE),
            call("finish", summary="ok"),
        ]
    )
    result = DashboardAgent(llm, db).run(board(), "add orders by state")
    assert result.status == "done" and len(result.dashboard.tiles) == 2


def test_repeated_invalid_turns_stop_the_run_and_report_retries(db: ReadOnlyDatabase) -> None:
    events: list[dict] = []
    llm = FakeLLM([text_turn(""), text_turn(""), text_turn(""), call("finish", summary="never reached")])
    result = DashboardAgent(llm, db).run(board(), "do something", on_event=events.append)
    assert result.status == "llm_error" and "3 invalid replies" in result.error
    assert [e["type"] for e in events].count("retry") == 2 and len(llm.calls) == 3


def test_invalid_turns_after_changes_keep_the_changes(db: ReadOnlyDatabase) -> None:
    llm = FakeLLM([call("add_tile", title="By state", sql=BY_STATE), text_turn(""), text_turn(""), text_turn("")])
    result = DashboardAgent(llm, db).run(board(), "add orders by state")
    assert result.status == "done" and len(result.dashboard.tiles) == 2


def test_agent_sets_filters_and_adds_text(db: ReadOnlyDatabase) -> None:
    llm = FakeLLM(
        [
            call("set_filters", state="ZZ"),
            turn(
                ("set_filters", {"date_from": "2018-01", "state": "SP"}),
                ("add_text", {"title": "Sao Paulo", "text": "2018 only."}),
            ),
            call("finish", summary="Filtered to SP in 2018."),
        ]
    )
    result = DashboardAgent(llm, db).run(board(), "only Sao Paulo in 2018, with a heading")
    assert result.status == "done"
    assert result.steps[0].ok is False and "Allowed states: RJ, SP" in result.steps[0].error
    assert result.dashboard.filters == {"date_from": "2018-01", "state": "SP"}
    assert result.dashboard.tiles[-1].chart == "text" and result.dashboard.tiles[-1].note == "2018 only."
    assert result.summary == "Added “Sao Paulo”, Set the filters to 2018-01 to end, customer state SP"
    assert '"filters": "2018-01 to end' not in build_request(board(), "x", None)


def test_steps_carry_the_draft_and_a_preview_of_the_changed_tile(db: ReadOnlyDatabase) -> None:
    events: list[dict] = []
    llm = FakeLLM(
        [
            turn(
                ("add_tile", {"title": "Orders by state", "sql": BY_STATE, "chart": "bar"}),
                ("remove_tile", {"tile_id": "t_a"}),
            ),
            call("finish", summary="done"),
        ]
    )
    DashboardAgent(llm, db).run(board(), "swap", on_event=events.append)
    steps = [e for e in events if e["type"] == "step"]
    added, removed = steps[0]["draft"], steps[1]["draft"]
    assert [t["title"] for t in added["tiles"]] == ["Totals", "Orders by state"]
    assert added["preview"]["payload"]["chart"]["type"] == "bar"
    assert added["preview"]["tile_id"] == added["tiles"][1]["id"]
    assert [t["title"] for t in removed["tiles"]] == ["Orders by state"] and "preview" not in removed
    assert "draft" not in steps[2]  # finish changes nothing
