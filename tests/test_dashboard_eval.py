from __future__ import annotations

from pathlib import Path

from conftest import FakeLLM, call
from sqlagent.builder import DashboardAgent
from sqlagent.dashboard_eval import (
    DashboardCase,
    load_dashboard_cases,
    render_dashboard_report,
    run_dashboard_eval,
    starting_dashboard,
)
from sqlagent.db import ReadOnlyDatabase

ROOT = Path(__file__).resolve().parents[1]
TOTALS = "SELECT SUM(revenue) AS revenue, COUNT(DISTINCT order_id) AS orders FROM sales"
MONTHLY = "SELECT strftime('%Y-%m', order_date) AS month, SUM(revenue) AS revenue FROM sales GROUP BY 1 ORDER BY 1"
BY_STATE = "SELECT c.state, SUM(s.revenue) AS revenue FROM sales s JOIN customers c USING (customer_key) GROUP BY 1"


def test_reference_cases_load_and_their_starting_tiles_run(db: ReadOnlyDatabase) -> None:
    cases = load_dashboard_cases(ROOT / "evals" / "dashboards.json")
    assert len(cases) >= 12 and len({c.id for c in cases}) == len(cases)
    assert {c.category for c in cases} == {"build", "filters", "edit", "refusal"}
    for case in cases:
        board, focus = starting_dashboard(case)
        assert (focus is not None) == (case.focus is not None)


def test_a_good_build_passes_and_a_thin_one_fails_with_reasons(db: ReadOnlyDatabase, tmp_path) -> None:
    case = DashboardCase(
        id="c1", request="Revenue overview", min_tiles=2, charts=["kpi", "line"], topics=["revenue", "state|region"]
    )
    good = FakeLLM(
        [
            call("add_tile", title="Totals", sql=TOTALS, chart="kpi"),
            call("add_tile", title="Revenue by month", sql=MONTHLY, chart="line"),
            call("add_tile", title="Revenue by state", sql=BY_STATE, chart="bar"),
            call("finish", summary="Built it."),
        ]
    )
    thin = FakeLLM([call("add_tile", title="Totals", sql=TOTALS, chart="kpi"), call("finish", summary="Done.")])
    path = tmp_path / "results.jsonl"
    [passed] = run_dashboard_eval(DashboardAgent(good, db), [case], path)
    assert passed.passed and [t["chart"] for t in passed.tiles] == ["kpi", "line", "bar"]

    case.id = "c2"
    [failed] = run_dashboard_eval(DashboardAgent(thin, db), [case], path)
    assert failed.failures == ["1 tiles, expected 2 to 24", "no line tile", "nothing about state or region"]

    # Saved outcomes are reused on a second run, so an interrupted run resumes.
    again = run_dashboard_eval(DashboardAgent(FakeLLM([]), db), [case], path)
    assert again[0].failures == failed.failures
    report = render_dashboard_report([passed, failed], "fake-model")
    assert "1/2 cases pass every check" in report and "no line tile" in report


def test_edit_and_refusal_checks(db: ReadOnlyDatabase) -> None:
    start = [{"title": "Totals", "sql": TOTALS, "chart": "kpi"}, {"title": "By state", "sql": BY_STATE, "chart": "bar"}]
    edit = DashboardCase(id="e", request="remove by state", start=start, removed=["By state"], kept=["Totals"])
    llm = FakeLLM([call("remove_tile", tile_id="t_start1"), call("finish", summary="Removed it.")])
    [outcome] = run_dashboard_eval(DashboardAgent(llm, db), [edit])
    assert outcome.passed

    refusal = DashboardCase(id="r", request="add ad spend", start=start, expect_cannot_do=True)
    complied = FakeLLM([call("add_tile", title="Spend", sql=TOTALS), call("finish", summary="ok")])
    [outcome] = run_dashboard_eval(DashboardAgent(complied, db), [refusal])
    assert outcome.failures == ["expected a refusal, got done"]
