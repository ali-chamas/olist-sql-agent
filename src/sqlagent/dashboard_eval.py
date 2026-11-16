"""Evaluation of the dashboard agent.

A question has one right answer; a dashboard does not. So a case states what any good
dashboard for the request must have, and the checks are mechanical:

- the run ends with ``finish`` (or ``cannot_do`` when the case expects a refusal);
- the number of tiles is within a range;
- every chart tile's SQL runs and returns rows (the agent checked it when adding it; this
  re-runs it as saved, so a later edit cannot have broken an earlier tile);
- the chart types the request implies are present (a trend needs a line, "headline numbers" a kpi);
- each required topic appears in some tile's title or SQL ("revenue|sales" means either word);
- expected dashboard filters are set, and for edit cases, expected tiles were kept or removed.

Cases may start from an existing dashboard (``start``), which is how edits are tested.
The score is the share of cases that pass every check; the report lists each failed check.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .builder import BuildResult, DashboardAgent
from .db import QueryError, ReadOnlyDatabase
from .evaluation import TokenPacer
from .workspace import TEXT, Dashboard, Tile

EXPECTED_TOKENS = 12_000  # a typical build, for pacing


@dataclass
class DashboardCase:
    id: str
    request: str
    category: str = "build"
    start: list[dict[str, Any]] = field(default_factory=list)
    focus: str | None = None  # title of the tile the request is about, for "Edit with AI" cases
    min_tiles: int = 1
    max_tiles: int = 24
    charts: list[str] = field(default_factory=list)  # chart types that must appear
    topics: list[str] = field(default_factory=list)  # "a|b" means a or b, in a title or the SQL
    filters: dict[str, str] = field(default_factory=dict)
    kept: list[str] = field(default_factory=list)  # titles that must still be there
    removed: list[str] = field(default_factory=list)  # titles that must be gone
    expect_cannot_do: bool = False


def load_dashboard_cases(path: str | Path) -> list[DashboardCase]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [DashboardCase(**item) for item in raw["cases"]]


def starting_dashboard(case: DashboardCase) -> tuple[Dashboard, str | None]:
    tiles = [
        Tile(
            id=f"t_start{i}",
            title=t["title"],
            sql=t.get("sql", ""),
            chart=t.get("chart", "auto"),
            size=t.get("size", "half"),
        )
        for i, t in enumerate(case.start)
    ]
    dashboard = Dashboard(id="d_eval", title="Eval dashboard" if tiles else "Untitled dashboard", tiles=tiles)
    focus = next((t.id for t in tiles if t.title == case.focus), None)
    return dashboard, focus


@dataclass
class DashboardOutcome:
    id: str
    category: str
    status: str
    failures: list[str]
    tiles: list[dict[str, str]]
    filters: dict[str, str]
    llm_calls: int
    tokens: int
    latency_ms: float
    message: str = ""

    @property
    def passed(self) -> bool:
        return not self.failures


def _mentions(tiles: Sequence[Tile], topic: str) -> bool:
    words = [w.strip().lower() for w in topic.split("|") if w.strip()]
    text = " ".join(f"{t.title} {t.sql} {t.note}" for t in tiles).lower()
    return any(re.search(rf"\b{re.escape(w)}", text) for w in words)


def check_result(case: DashboardCase, result: BuildResult, db: ReadOnlyDatabase) -> list[str]:
    """Every check the dashboard fails, in plain words. An empty list is a pass."""
    if case.expect_cannot_do:
        return [] if result.status == "cannot_do" else [f"expected a refusal, got {result.status}"]
    if result.status != "done":
        return [f"run ended with {result.status}: {result.error or result.message}".strip()]
    board = result.dashboard
    tiles = board.tiles if board else []
    failures = []
    if not case.min_tiles <= len(tiles) <= case.max_tiles:
        failures.append(f"{len(tiles)} tiles, expected {case.min_tiles} to {case.max_tiles}")
    for tile in tiles:
        if tile.chart == TEXT:
            continue
        try:
            if not db.execute(tile.sql).rows:
                failures.append(f"“{tile.title}” returns no rows")
        except QueryError as exc:
            failures.append(f"“{tile.title}” fails: {exc.message}")
    present = {t.chart for t in tiles}
    for chart in case.charts:
        if chart not in present:
            failures.append(f"no {chart} tile")
    for topic in case.topics:
        if not _mentions(tiles, topic):
            failures.append(f"nothing about {topic.replace('|', ' or ')}")
    for key, value in case.filters.items():
        if (board.filters if board else {}).get(key) != value:
            failures.append(f"filter {key} is {(board.filters if board else {}).get(key)!r}, expected {value!r}")
    titles = {t.title for t in tiles}
    for title in case.kept:
        if title not in titles:
            failures.append(f"“{title}” was removed")
    for title in case.removed:
        if title in titles:
            failures.append(f"“{title}” is still there")
    return failures


def run_dashboard_case(agent: DashboardAgent, case: DashboardCase) -> DashboardOutcome:
    dashboard, focus = starting_dashboard(case)
    result = agent.run(dashboard, case.request, focus)
    board = result.dashboard
    return DashboardOutcome(
        id=case.id,
        category=case.category,
        status=result.status,
        failures=check_result(case, result, agent.db),
        tiles=[{"title": t.title, "chart": t.chart, "size": t.size} for t in (board.tiles if board else [])],
        filters=dict(board.filters) if board else {},
        llm_calls=result.llm_calls,
        tokens=result.input_tokens + result.output_tokens,
        latency_ms=result.latency_ms,
        message=result.message or result.error,
    )


def run_dashboard_eval(
    agent: DashboardAgent,
    cases: Sequence[DashboardCase],
    results_path: Path | None = None,
    pacer: TokenPacer | None = None,
    resume: bool = True,
    on_case: Callable[[DashboardOutcome], None] | None = None,
) -> list[DashboardOutcome]:
    """Run the cases, saving each outcome as it finishes so an interrupted run can resume."""
    done: dict[str, DashboardOutcome] = {}
    if results_path and resume and results_path.exists():
        for line in results_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                done[item["id"]] = DashboardOutcome(**item)
    outcomes = []
    for case in cases:
        if case.id in done:
            outcomes.append(done[case.id])
            continue
        if pacer:
            pacer.wait(EXPECTED_TOKENS)
        outcome = run_dashboard_case(agent, case)
        if pacer:
            pacer.record(outcome.tokens)
        outcomes.append(outcome)
        if results_path:
            results_path.parent.mkdir(parents=True, exist_ok=True)
            with results_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(outcome), ensure_ascii=False) + "\n")
        if on_case:
            on_case(outcome)
    return outcomes


def render_dashboard_report(outcomes: Sequence[DashboardOutcome], model: str, date: dt.date | None = None) -> str:
    passed = sum(o.passed for o in outcomes)
    total = len(outcomes) or 1
    calls = sum(o.llm_calls for o in outcomes) / total
    tokens = sum(o.tokens for o in outcomes) / total
    seconds = sum(o.latency_ms for o in outcomes) / total / 1000
    lines = [
        f"# Dashboard agent eval: {model}",
        "",
        f"Run on {(date or dt.date.today()).isoformat()}. {passed}/{len(outcomes)} cases pass every check "
        f"({100 * passed / total:.0f}%).",
        f"Per case on average: {calls:.1f} model calls, {tokens:,.0f} tokens, {seconds:.1f} s.",
        "",
        "| Case | Category | Result | Tiles | Calls | Tokens | Failed checks |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for o in outcomes:
        charts = ", ".join(t["chart"] for t in o.tiles) or "none"
        failures = "; ".join(o.failures) or ""
        verdict = "pass" if o.passed else "fail"
        lines.append(f"| {o.id} | {o.category} | {verdict} | {charts} | {o.llm_calls} | {o.tokens:,} | {failures} |")
    return "\n".join(lines) + "\n"
