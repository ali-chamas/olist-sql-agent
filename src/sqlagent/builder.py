"""The dashboard agent: builds and edits a dashboard from a request in plain language.

It works on a copy of the dashboard. Each tool call is validated the same way a
person's edit is (the SQL runs through the guardrails, the chart type must fit the
result), and refusals go back to the model as tool errors so it can correct itself.
When the run ends, all accepted changes are saved together as one version, so one
request is one step in the history and one Undo.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from .agent import EMPTY_TURN, Step, format_result
from .catalog import TABLES
from .db import QueryError, ReadOnlyDatabase
from .filters import FilterError, FilterOptions, Filters, describe, load_options, validate
from .llm import INVALID_CALL_TEXT, LLMClient, LLMError, Message, ToolCall, ToolSpec, Usage, report_waits
from .prompts import schema_text
from .workspace import (
    TEXT,
    Change,
    Dashboard,
    EditError,
    add_text_tile,
    add_tile,
    move_tile,
    remove_tile,
    set_filters,
    summarize,
    update_details,
    update_tile,
)

EventHandler = Callable[[dict[str, Any]], None]

RULES = """\
You build and edit dashboards for the Olist e-commerce dataset (a Brazilian marketplace,
orders from September 2016 to September 2018). People describe what they want in plain
language; you change the dashboard with tools. They cannot write SQL, so you do.

How to work:
- Read the current dashboard in the request. Refer to tiles by their id.
- Change only what was asked. For a new dashboard, create 4 to 7 tiles that together
  answer the request, starting with a kpi tile of 2 to 5 headline numbers.
- add_tile and update_tile run the SQL and check the chart type before accepting.
  If a call is refused, read the error, fix the query or the chart type, and try again.
- Use run_sql only when you need to look at the data before deciding.
- To limit the WHOLE dashboard to a period, a customer state or a product category,
  call set_filters instead of editing every query. Filters apply to every tile.
- Use add_text for a short section heading or context (for example "Delivery" with one
  sentence of explanation). Do not use it to restate numbers.
- Finish with finish, giving one sentence that says what you changed.
- If the request cannot be done with this data, call cannot_do with the reason.

Tiles:
- Each tile answers one question. Titles are short and in sentence case,
  for example "Revenue by month" or "Top 10 categories by revenue".
- chart: kpi = one row with 1 to 6 numbers. line = a time column (YYYY-MM or year) and
  1 to 3 numeric columns in the same unit. bar = one label column and one number,
  2 to 25 rows, ranked and with a LIMIT. table = anything else (at most 50 rows).
  auto lets the server choose.
- size: full for line charts, kpi rows and wide tables; half otherwise. Two half tiles
  share a row, so place half tiles in pairs.
- Aggregate in SQL. Never return raw rows without a LIMIT.
- For monthly trends use strftime('%Y-%m', order_date) AS month, and only complete
  months: order_date >= '2017-01-01' AND order_date < '2018-09-01', unless asked otherwise.
- Name numeric columns after their unit: revenue, orders, customers, late_rate_pct.

SQL rules (SQLite, read-only, one SELECT, WITH allowed):
- sales has one row per order ITEM. Orders = COUNT(DISTINCT order_id). Items = COUNT(*).
- Revenue = SUM(revenue), which includes freight. Average order value = SUM(revenue) / COUNT(DISTINCT order_id).
- review_score, delivery_days, delay_days, is_late and payment_type repeat on every item row of an
  order: reduce to one row per order before averaging, for example
  SELECT AVG(score) FROM (SELECT order_id, MAX(review_score) AS score FROM sales GROUP BY order_id).
- Late-delivery rate = late orders / delivered orders (delivered_date IS NOT NULL), as a percentage 0 to 100.
"""

log = logging.getLogger(__name__)

# Invalid replies in a row (no tool call, or one the provider rejected) before the run stops.
MAX_INVALID_TURNS = 3
FINISH_NUDGE = (
    "Use the tools to change the dashboard, then call finish with a one-line summary. "
    "Call cannot_do if it cannot be done."
)

_SQL = {"type": "string", "description": "One SQLite SELECT statement."}
_CHART = {"type": "string", "enum": ["auto", "kpi", "bar", "line", "table"]}
_SIZE = {"type": "string", "enum": ["half", "full"]}

TOOLS: list[ToolSpec] = [
    ToolSpec(
        "run_sql",
        "Run a read-only query to look at the data. Returns the first rows.",
        {"type": "object", "properties": {"sql": _SQL}, "required": ["sql"]},
    ),
    ToolSpec(
        "add_tile",
        "Add a tile. The SQL is run and the chart type checked before the tile is accepted.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "sql": _SQL,
                "chart": _CHART,
                "size": _SIZE,
                "note": {"type": "string", "description": "Optional one-line caveat shown under the title."},
                "position": {"type": "integer", "description": "0 puts it first. Omit to add it at the end."},
            },
            "required": ["title", "sql"],
        },
    ),
    ToolSpec(
        "update_tile",
        "Change a tile. Give only the fields to change.",
        {
            "type": "object",
            "properties": {
                "tile_id": {"type": "string"},
                "title": {"type": "string"},
                "sql": _SQL,
                "chart": _CHART,
                "size": _SIZE,
                "note": {"type": "string"},
            },
            "required": ["tile_id"],
        },
    ),
    ToolSpec(
        "remove_tile",
        "Remove a tile.",
        {"type": "object", "properties": {"tile_id": {"type": "string"}}, "required": ["tile_id"]},
    ),
    ToolSpec(
        "move_tile",
        "Move a tile to a new position. 0 is first.",
        {
            "type": "object",
            "properties": {"tile_id": {"type": "string"}, "position": {"type": "integer"}},
            "required": ["tile_id", "position"],
        },
    ),
    ToolSpec(
        "add_text",
        "Add a text tile: a short heading and up to a few sentences. No query.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "text": {"type": "string"},
                "size": _SIZE,
                "position": {"type": "integer"},
            },
            "required": ["title"],
        },
    ),
    ToolSpec(
        "set_filters",
        "Set the dashboard filters, which apply to every tile. Empty or omitted fields clear that filter.",
        {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "First month, YYYY-MM, inclusive."},
                "date_to": {"type": "string", "description": "Last month, YYYY-MM, inclusive."},
                "state": {"type": "string", "description": "Customer state code, for example SP."},
                "category": {"type": "string", "description": "Product category, for example Health Beauty."},
            },
        },
    ),
    ToolSpec(
        "update_dashboard",
        "Change the dashboard's title or description.",
        {"type": "object", "properties": {"title": {"type": "string"}, "description": {"type": "string"}}},
    ),
    ToolSpec(
        "finish",
        "Finish. Say in one sentence what you changed.",
        {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]},
    ),
    ToolSpec(
        "cannot_do",
        "Stop without changing anything, because the data or the tools cannot do this.",
        {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
    ),
]
TOOL_NAMES = {t.name for t in TOOLS}


def system_prompt() -> str:
    return f"{RULES}\nSchema:\n{schema_text()}\n"


def describe_dashboard(dashboard: Dashboard) -> str:
    tiles = [
        {
            "id": t.id,
            "title": t.title,
            "chart": t.chart,
            "size": t.size,
            "sql": t.sql,
            **({"note": t.note} if t.note else {}),
        }
        for t in dashboard.tiles
    ]
    return json.dumps(
        {
            "title": dashboard.title,
            "description": dashboard.description,
            "filters": describe(Filters.from_dict(dashboard.filters)),
            "tiles": tiles,
        },
        indent=1,
    )


def build_request(dashboard: Dashboard, request: str, focus_tile: str | None) -> str:
    lines = ["Current dashboard:", describe_dashboard(dashboard), ""]
    if not dashboard.tiles:
        lines.append("The dashboard is empty: build it, and give it a fitting title and description.")
    if focus_tile:
        lines.append(f"The person is talking about tile {focus_tile}. Change only that tile unless they say otherwise.")
    lines += ["", f"Request: {request}"]
    return "\n".join(lines)


@dataclass
class BuildResult:
    status: str  # done | cannot_do | max_steps | llm_error
    message: str = ""
    dashboard: Dashboard | None = None
    changes: list[Change] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    error: str = ""

    @property
    def summary(self) -> str:
        return summarize(self.changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "summary": self.summary,
            "changes": [asdict(c) for c in self.changes],
            "steps": [asdict(s) for s in self.steps],
            "llm_calls": self.llm_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }


def describe_change(change: Change) -> str:
    if change.kind == "filtered":
        return f"Set the filters to {change.title}"
    verbs = {"added": "Added", "updated": "Updated", "removed": "Removed", "moved": "Moved", "renamed": "Renamed to"}
    return f"{verbs[change.kind]} “{change.title}”"


class DashboardAgent:
    def __init__(self, llm: LLMClient, db: ReadOnlyDatabase, max_steps: int = 14) -> None:
        self.llm = llm
        self.db = db
        self.max_steps = max_steps
        self.system = system_prompt()
        self._options: FilterOptions | None = None
        assert TABLES  # the schema is part of the prompt

    @property
    def options(self) -> FilterOptions:
        if self._options is None:
            self._options = load_options(self.db)
        return self._options

    def run(
        self,
        dashboard: Dashboard,
        request: str,
        focus_tile: str | None = None,
        on_event: EventHandler | None = None,
    ) -> BuildResult:
        started = time.monotonic()
        draft = copy.deepcopy(dashboard)
        result = BuildResult(status="max_steps", dashboard=draft)
        usage = Usage()
        messages = [Message(role="user", content=build_request(draft, request, focus_tile))]

        def emit(event: dict[str, Any]) -> None:
            if on_event:
                on_event(event)

        invalid_turns = 0
        prose_turns = 0
        for call_number in range(1, self.max_steps + 1):
            emit({"type": "thinking", "call": call_number})
            try:
                with report_waits(
                    lambda seconds, status: emit({"type": "waiting", "seconds": seconds, "status": status})
                ):
                    response = self.llm.complete(self.system, messages, TOOLS)
            except LLMError as exc:
                result.status, result.error = "llm_error", str(exc)
                break
            result.llm_calls += 1
            usage.add(response.usage)
            content = response.text or ("" if response.tool_calls else EMPTY_TURN)
            messages.append(Message(role="assistant", content=content, tool_calls=response.tool_calls))
            if not response.tool_calls:
                text = (response.text or "").strip()
                if text and text != INVALID_CALL_TEXT:
                    # A plain-text reply. After changes, it is the model's closing summary.
                    # Before any change, give it one more chance to act, then take the text as its answer.
                    prose_turns += 1
                    if result.changes or prose_turns >= 2:
                        result.status, result.message = "done", text[:600]
                        result.steps.append(Step(tool="finish"))
                        emit({"type": "step", "step": asdict(result.steps[-1])})
                        break
                else:
                    invalid_turns += 1
                    log.warning(
                        "dashboard agent: invalid model turn %d: %r", invalid_turns, (response.text or "")[:200]
                    )
                    if invalid_turns >= MAX_INVALID_TURNS:
                        if result.changes:
                            result.status = "done"
                            result.message = "Saved the changes made before the model stopped responding properly."
                        else:
                            result.status = "llm_error"
                            result.error = f"The model returned {MAX_INVALID_TURNS} invalid replies in a row."
                        break
                emit({"type": "retry", "call": call_number})
                messages.append(Message(role="user", content=FINISH_NUDGE))
                continue
            invalid_turns = 0

            finished = False
            for call in response.tool_calls:
                if finished:
                    messages.append(self._tool_message(call, "Ignored: the run already finished.", True))
                    continue
                text, is_error, finished = self._handle(call, draft, result)
                emit({"type": "step", "step": asdict(result.steps[-1])})
                messages.append(self._tool_message(call, text, is_error))
            if finished:
                break

        if result.status == "max_steps":
            result.error = f"The agent stopped after {self.max_steps} model calls."
        result.input_tokens, result.output_tokens = usage.input_tokens, usage.output_tokens
        result.latency_ms = round((time.monotonic() - started) * 1000, 1)
        return result

    @staticmethod
    def _tool_message(call: ToolCall, content: str, is_error: bool) -> Message:
        return Message(role="tool", content=content, tool_call_id=call.id, name=call.name, is_error=is_error)

    def _handle(self, call: ToolCall, draft: Dashboard, result: BuildResult) -> tuple[str, bool, bool]:
        args = call.arguments
        if call.parse_error:
            result.steps.append(Step(tool=call.name, ok=False, error=call.parse_error))
            return call.parse_error, True, False
        if call.name not in TOOL_NAMES:
            message = f"Unknown tool {call.name!r}. Available: {', '.join(sorted(TOOL_NAMES))}."
            result.steps.append(Step(tool=call.name, ok=False, error=message))
            return message, True, False

        if call.name == "finish":
            result.status = "done"
            result.message = str(args.get("summary") or "").strip()
            result.steps.append(Step(tool="finish", sql=""))
            return "Done.", False, True
        if call.name == "cannot_do":
            result.status = "cannot_do"
            result.message = str(args.get("reason") or "").strip() or "This cannot be done with this data."
            result.changes.clear()
            result.steps.append(Step(tool="cannot_do"))
            return "Recorded.", False, True

        if call.name == "run_sql":
            sql = args.get("sql")
            try:
                query = self.db.execute(sql if isinstance(sql, str) else "")
            except QueryError as exc:
                result.steps.append(Step(tool="run_sql", sql=str(sql or ""), ok=False, error=exc.message))
                return exc.message, True, False
            result.steps.append(
                Step(tool="run_sql", sql=query.sql, row_count=query.row_count, elapsed_ms=query.elapsed_ms)
            )
            return format_result(query), False, False

        try:
            change = self._apply(call.name, args, draft)
        except EditError as exc:
            title = args.get("title") or args.get("tile_id") or ""
            result.steps.append(
                Step(tool=call.name, sql=str(args.get("sql") or ""), ok=False, error=str(exc), detail=str(title))
            )
            return str(exc), True, False
        result.changes.append(change)
        tile = next((t for t in draft.tiles if t.id == change.tile_id), None)
        detail = describe_change(change)
        sql = tile.sql if tile and call.name in ("add_tile", "update_tile") and tile.chart != TEXT else ""
        result.steps.append(Step(tool=call.name, sql=sql, detail=detail))
        if tile and change.kind in ("added", "updated"):
            detail += f" as tile {tile.id} ({tile.chart} chart, {tile.size} width)"
        return detail + ". " + _layout(draft), False, False

    def _apply(self, name: str, args: dict[str, Any], draft: Dashboard) -> Change:
        if name == "add_tile":
            return add_tile(
                draft,
                self.db,
                args.get("title"),
                args.get("sql"),
                args.get("chart") or "auto",
                args.get("size"),
                args.get("note") or "",
                args.get("position"),
            )
        if name == "update_tile":
            fields = {k: args.get(k) for k in ("title", "sql", "chart", "size", "note")}
            return update_tile(draft, self.db, args.get("tile_id"), **fields)
        if name == "remove_tile":
            return remove_tile(draft, args.get("tile_id"))
        if name == "add_text":
            return add_text_tile(
                draft, args.get("title"), args.get("text") or "", args.get("size"), args.get("position")
            )
        if name == "set_filters":
            try:
                chosen = validate(Filters.from_dict(args), self.options)
            except FilterError as exc:
                raise EditError(f"{exc} Allowed states: {', '.join(self.options.states)}.") from exc
            return set_filters(draft, chosen.to_dict(), describe(chosen))
        if name == "move_tile":
            return move_tile(draft, args.get("tile_id"), args.get("position"))
        return update_details(draft, args.get("title"), args.get("description"))


def _layout(draft: Dashboard) -> str:
    order = ", ".join(f"{t.id} {t.title!r}" for t in draft.tiles) or "none"
    return f"Tiles now: {order}."
