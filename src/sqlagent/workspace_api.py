"""HTTP routes for saved dashboards.

    GET    /api/dashboards                                  list
    POST   /api/dashboards                                  create a blank dashboard {title?, description?}
    POST   /api/dashboards/build                            build a new one from a prompt (server-sent events)
    GET    /api/dashboards/{id}                             dashboard with its tiles
    PATCH  /api/dashboards/{id}                             {title?, description?, filters?, version?}
    DELETE /api/dashboards/{id}
    POST   /api/dashboards/{id}/duplicate                   a copy, titled "Copy of ..."
    POST   /api/dashboards/from-overview                    an editable copy of the built-in overview
    POST   /api/dashboards/{id}/agent                       edit with a prompt {prompt, tile_id?, version?} (events)
    GET    /api/dashboards/{id}/revisions                   history
    POST   /api/dashboards/{id}/restore                     {version}
    POST   /api/dashboards/{id}/tiles                       {title, sql, chart?, size?, note?, position?, version?}
                                                            or {kind: "text", title?, text, size?, position?}
    PATCH  /api/dashboards/{id}/tiles/{tile}                {title?, sql?, chart?, size?, note?, version?}
    DELETE /api/dashboards/{id}/tiles/{tile}                ?version=
    POST   /api/dashboards/{id}/tiles/{tile}/move           {position, version?}
    GET    /api/dashboards/{id}/tiles/{tile}/data           the tile's rows and chart
                                                            ?date_from=YYYY-MM&date_to=&state=&category=
    GET    /api/filters                                     the values the filters can take

Tile data is fetched by tile id: the browser never sends the SQL that a dashboard runs.
`version` is optional on edits; when given, an edit based on an old version is refused (409).
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .builder import BuildResult, DashboardAgent
from .dashboards import OVERVIEW_DESCRIPTION, OVERVIEW_TITLE, editable_tiles
from .db import QueryError, ReadOnlyDatabase
from .filters import FilterError, FilterOptions, Filters, apply_filters, describe, load_options, validate
from .presentation import ChartMismatch, chart_for, suggest_chart
from .workspace import (
    TEXT,
    Conflict,
    Dashboard,
    EditError,
    MissingTile,
    NotFound,
    Tile,
    Workspace,
    add_text_tile,
    add_tile,
    move_tile,
    remove_tile,
    set_filters,
    summarize,
    update_details,
    update_tile,
)

MAX_PROMPT = 1000
TILE_CACHE_ENTRIES = 512


class ResultCache:
    """Tile results by (SQL, chart, filters). The data is read-only, so entries never go stale."""

    def __init__(self, size: int = TILE_CACHE_ENTRIES) -> None:
        self.size = size
        self._items: OrderedDict[tuple, Any] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple) -> Any:
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                return self._items[key]
        return None

    def put(self, key: tuple, value: Any) -> None:
        with self._lock:
            self._items[key] = value
            self._items.move_to_end(key)
            while len(self._items) > self.size:
                self._items.popitem(last=False)


Json = dict[str, Any]


def error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def as_json(data: Any) -> JSONResponse:
    return JSONResponse(json.loads(json.dumps(data, default=str)))


def sse(event: str, data: Json) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def event_stream(job: Callable[[Callable[[Json], None]], Json], first: Json | None = None) -> StreamingResponse:
    """Run ``job(on_event)`` in a worker thread and stream its events, then one 'result' event."""

    async def events():
        if first is not None:
            yield sse("result", first)
            return
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, Json]] = asyncio.Queue()

        def on_event(event: Json) -> None:  # called from the worker thread
            loop.call_soon_threadsafe(queue.put_nowait, (event["type"], event))

        async def work() -> None:
            try:
                payload = await run_in_threadpool(job, on_event)
            except Exception as exc:  # noqa: BLE001 - the stream must always end with a result
                payload = {"status": "llm_error", "error": f"Internal error: {exc}", "steps": []}
            queue.put_nowait(("result", payload))

        task = asyncio.create_task(work())
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        while True:
            name, data = await queue.get()
            yield sse(name, data)
            if name == "result":
                return

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


def workspace_routes(
    workspace: Workspace,
    db: ReadOnlyDatabase,
    builder: DashboardAgent,
    read_json: Callable[[Request], Awaitable[Json | JSONResponse]],
    admit_model_call: Callable[[Request], JSONResponse | None],
    spend: Callable[[int], None],
) -> list[Route]:
    async def guarded(fn: Callable[[], Any]) -> Any:
        """Run a workspace call in a thread and turn its errors into HTTP responses."""
        try:
            return await run_in_threadpool(fn)
        except (NotFound, MissingTile) as exc:
            return error(str(exc), 404)
        except Conflict as exc:
            return error(str(exc), 409)
        except (EditError, FilterError) as exc:
            return error(str(exc), 422)

    options_cache: list[FilterOptions] = []
    results = ResultCache()

    def options() -> FilterOptions:
        if not options_cache:
            options_cache.append(load_options(db))
        return options_cache[0]

    def filters_from(source: Any) -> Filters:
        return validate(Filters.from_dict(dict(source)), options())

    def version_of(payload: Json) -> int | None:
        value = payload.get("version")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    async def mutate(request: Request, edit: Callable[[Dashboard, Json], Any]) -> JSONResponse:
        payload = await read_json(request) if request.method != "DELETE" else {}
        if isinstance(payload, JSONResponse):
            return payload
        if request.method == "DELETE" and request.query_params.get("version", "").isdigit():
            payload = {"version": int(request.query_params["version"])}
        dashboard_id = request.path_params["dashboard_id"]

        def work() -> Dashboard:
            dashboard = workspace.get(dashboard_id)
            change = edit(dashboard, payload)
            return workspace.save(
                dashboard, author="you", summary=summarize([change]), expected_version=version_of(payload)
            )

        result = await guarded(work)
        return result if isinstance(result, JSONResponse) else as_json(result.to_dict())

    # ------------------------------------------------------------ dashboards

    async def list_dashboards(request: Request) -> JSONResponse:
        return as_json({"dashboards": await run_in_threadpool(workspace.list)})

    async def create_dashboard(request: Request) -> JSONResponse:
        payload = await read_json(request)
        if isinstance(payload, JSONResponse):
            return payload
        result = await guarded(lambda: workspace.create(payload.get("title") or "", payload.get("description") or ""))
        if isinstance(result, JSONResponse):
            return result
        return JSONResponse(json.loads(json.dumps(result.to_dict(), default=str)), status_code=201)

    async def get_dashboard(request: Request) -> JSONResponse:
        result = await guarded(lambda: workspace.get(request.path_params["dashboard_id"]))
        return result if isinstance(result, JSONResponse) else as_json(result.to_dict())

    async def patch_dashboard(request: Request) -> JSONResponse:
        def edit(dashboard: Dashboard, payload: Json) -> Any:
            if "filters" in payload:
                if not isinstance(payload["filters"], dict):
                    raise EditError("filters must be an object.")
                chosen = filters_from(payload["filters"])
                return set_filters(dashboard, chosen.to_dict(), describe(chosen))
            return update_details(dashboard, payload.get("title"), payload.get("description"))

        return await mutate(request, edit)

    async def duplicate(request: Request) -> JSONResponse:
        result = await guarded(lambda: workspace.duplicate(request.path_params["dashboard_id"]))
        if isinstance(result, JSONResponse):
            return result
        return JSONResponse(json.loads(json.dumps(result.to_dict(), default=str)), status_code=201)

    async def from_overview(request: Request) -> JSONResponse:
        def work() -> Dashboard:
            tiles = [Tile(id="", **t) for t in editable_tiles()]
            return workspace.create_from(OVERVIEW_TITLE, OVERVIEW_DESCRIPTION, tiles)

        result = await guarded(work)
        if isinstance(result, JSONResponse):
            return result
        return JSONResponse(json.loads(json.dumps(result.to_dict(), default=str)), status_code=201)

    async def filter_options(request: Request) -> JSONResponse:
        return as_json((await run_in_threadpool(options)).to_dict())

    async def delete_dashboard(request: Request) -> Response:
        result = await guarded(lambda: workspace.delete(request.path_params["dashboard_id"]))
        return result if isinstance(result, JSONResponse) else Response(status_code=204)

    async def revisions(request: Request) -> JSONResponse:
        result = await guarded(lambda: workspace.revisions(request.path_params["dashboard_id"]))
        return result if isinstance(result, JSONResponse) else as_json({"revisions": result})

    async def restore(request: Request) -> JSONResponse:
        payload = await read_json(request)
        if isinstance(payload, JSONResponse):
            return payload
        version = version_of(payload)
        if version is None:
            return error("'version' must be a whole number.", 422)
        result = await guarded(lambda: workspace.restore(request.path_params["dashboard_id"], version))
        return result if isinstance(result, JSONResponse) else as_json(result.to_dict())

    # ----------------------------------------------------------------- tiles

    async def create_tile(request: Request) -> JSONResponse:
        return await mutate(
            request,
            lambda d, p: (
                add_text_tile(d, p.get("title"), p.get("text"), p.get("size"), p.get("position"))
                if p.get("kind") == TEXT
                else add_tile(
                    d,
                    db,
                    p.get("title"),
                    p.get("sql"),
                    p.get("chart") or "auto",
                    p.get("size"),
                    p.get("note") or "",
                    p.get("position"),
                )
            ),
        )

    async def patch_tile(request: Request) -> JSONResponse:
        tile_id = request.path_params["tile_id"]
        fields = ("title", "sql", "chart", "size", "note")
        return await mutate(request, lambda d, p: update_tile(d, db, tile_id, **{k: p.get(k) for k in fields}))

    async def delete_tile(request: Request) -> JSONResponse:
        return await mutate(request, lambda d, p: remove_tile(d, request.path_params["tile_id"]))

    async def move(request: Request) -> JSONResponse:
        return await mutate(request, lambda d, p: move_tile(d, request.path_params["tile_id"], p.get("position")))

    async def tile_data(request: Request) -> JSONResponse:
        def work() -> Json:
            dashboard = workspace.get(request.path_params["dashboard_id"])
            tile = dashboard.tile(request.path_params["tile_id"])
            if tile.chart == TEXT:
                return {"text": tile.note, "chart": {"type": TEXT}, "columns": [], "rows": []}
            chosen = filters_from(request.query_params)
            key = (tile.sql, tile.chart, tuple(sorted(chosen.to_dict().items())))
            cached = results.get(key)
            if cached is not None:
                return {**cached, "cached": True}
            try:
                result = db.execute(apply_filters(tile.sql, chosen))
            except QueryError as exc:
                return {"error": exc.message}
            if not result.rows:
                chart, warning = {"type": "empty"}, ""
            else:
                try:
                    chart = chart_for(result.columns, result.rows, tile.chart)
                    warning = ""
                except ChartMismatch:
                    chart = suggest_chart(result.columns, result.rows)
                    reason = "With these filters, the result" if chosen.active else "This result"
                    warning = f"{reason} does not fit a {tile.chart} chart, so it is shown automatically."
            payload = {
                "columns": result.columns,
                "rows": [list(r) for r in result.rows],
                "truncated": result.truncated,
                "chart": chart,
                "warning": warning,
                "elapsed_ms": result.elapsed_ms,
            }
            results.put(key, payload)
            return payload

        result = await guarded(work)
        return result if isinstance(result, JSONResponse) else as_json(result)

    # ----------------------------------------------------------------- agent

    async def read_prompt(request: Request) -> Json | JSONResponse:
        payload = await read_json(request)
        if isinstance(payload, JSONResponse):
            return payload
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not (3 <= len(prompt.strip()) <= MAX_PROMPT):
            return error(f"'prompt' must be a string of 3 to {MAX_PROMPT} characters.", 422)
        payload["prompt"] = prompt.strip()
        return payload

    def finish_payload(result: BuildResult, dashboard: Dashboard | None, saved: bool) -> Json:
        data = result.to_dict()
        data["saved"] = saved
        data["dashboard"] = dashboard.to_dict() if dashboard else None
        return data

    async def edit_with_agent(request: Request) -> Response:
        payload = await read_prompt(request)
        if isinstance(payload, JSONResponse):
            return payload
        dashboard_id = request.path_params["dashboard_id"]
        loaded = await guarded(lambda: workspace.get(dashboard_id))
        if isinstance(loaded, JSONResponse):
            return loaded
        tile_id = payload.get("tile_id") if isinstance(payload.get("tile_id"), str) else None
        if tile_id and tile_id not in {t.id for t in loaded.tiles}:
            return error(f"No tile with id {tile_id!r}.", 404)
        refused = admit_model_call(request)
        if refused is not None:
            return refused
        expected = version_of(payload) if version_of(payload) is not None else loaded.version

        def job(on_event: Callable[[Json], None]) -> Json:
            result = builder.run(loaded, payload["prompt"], tile_id, on_event)
            spend(result.input_tokens + result.output_tokens)
            if not result.changes or result.status in ("cannot_do", "llm_error"):
                return finish_payload(result, loaded, saved=False)
            try:
                saved = workspace.save(
                    result.dashboard, author="ai", summary=result.message or result.summary, expected_version=expected
                )
            except Conflict as exc:
                result.status, result.error = "conflict", str(exc)
                return finish_payload(result, workspace.get(dashboard_id), saved=False)
            return finish_payload(result, saved, saved=True)

        return event_stream(job)

    async def build_new(request: Request) -> Response:
        payload = await read_prompt(request)
        if isinstance(payload, JSONResponse):
            return payload
        refused = admit_model_call(request)
        if refused is not None:
            return refused

        def job(on_event: Callable[[Json], None]) -> Json:
            draft = Dashboard(id="", title="Untitled dashboard")
            result = builder.run(draft, payload["prompt"], None, on_event)
            spend(result.input_tokens + result.output_tokens)
            built = result.dashboard
            if result.status in ("cannot_do", "llm_error") or built is None or not built.tiles:
                if result.status == "done":
                    result.status, result.error = "max_steps", "The agent finished without adding any tiles."
                return finish_payload(result, None, saved=False)
            created = workspace.create(built.title, built.description, author="ai", summary="Created the dashboard")
            created.tiles = built.tiles
            saved = workspace.save(
                created, author="ai", summary=result.message or result.summary, expected_version=created.version
            )
            return finish_payload(result, saved, saved=True)

        return event_stream(job)

    base = "/api/dashboards"
    one = base + "/{dashboard_id}"
    tile = one + "/tiles/{tile_id}"
    return [
        Route(base, list_dashboards, methods=["GET"]),
        Route(base, create_dashboard, methods=["POST"]),
        Route(base + "/build", build_new, methods=["POST"]),
        Route(base + "/from-overview", from_overview, methods=["POST"]),
        Route(one, get_dashboard, methods=["GET"]),
        Route(one, patch_dashboard, methods=["PATCH"]),
        Route(one, delete_dashboard, methods=["DELETE"]),
        Route(one + "/duplicate", duplicate, methods=["POST"]),
        Route(one + "/agent", edit_with_agent, methods=["POST"]),
        Route(one + "/revisions", revisions, methods=["GET"]),
        Route(one + "/restore", restore, methods=["POST"]),
        Route(one + "/tiles", create_tile, methods=["POST"]),
        Route(tile, patch_tile, methods=["PATCH"]),
        Route(tile, delete_tile, methods=["DELETE"]),
        Route(tile + "/move", move, methods=["POST"]),
        Route(tile + "/data", tile_data, methods=["GET"]),
        Route("/api/filters", filter_options, methods=["GET"]),
    ]
