"""HTTP API and web UI (Starlette).

    GET  /                    the app
    GET  /api/status          model, daily quota and cache counters
    GET  /api/schema          tables, columns and known data quirks
    GET  /api/dashboard       curated overview tiles; ?year=2017 filters them
    POST /api/ask             {"question", "history"?} -> answer, SQL, rows, chart, trace
    POST /api/ask/stream      same, as server-sent events: one per agent step, then the result
    POST /api/query           {"sql"} -> rows and chart; runs through the same guardrails, no model call

Only /api/ask spends model tokens. It sits behind a per-client rate limit, an
answer cache and a daily token budget, so a public demo cannot drain the key.
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .agent import AgentResult, SQLAgent
from .builder import DashboardAgent
from .catalog import DATA_NOTES, TABLES
from .dashboards import build_dashboard, dataset_facts
from .db import QueryError
from .limits import AnswerCache, DailyTokenBudget, RateLimiter
from .presentation import result_payload, suggest_chart
from .workspace import Workspace
from .workspace_api import workspace_routes

STATIC = Path(__file__).parent / "static"
MIN_QUESTION, MAX_QUESTION = 3, 500
MAX_SQL = 4000
MAX_HISTORY = 3
MAX_BODY_BYTES = 32_768

SECURITY_HEADERS = {
    b"content-security-policy": (
        b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        b"connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    b"x-content-type-options": b"nosniff",
    b"referrer-policy": b"no-referrer",
}


class SecurityHeaders:
    """Pure ASGI middleware, so it does not buffer streamed responses.

    Also marks the page and its modules ``no-cache``: the browser keeps them but checks
    with the server (a cheap 304) before each use. The modules are not versioned, so
    without this an update could load a new main.js next to a cached old ask.js.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        revalidate = path == "/" or path.startswith("/static/")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                present = {name.lower() for name, _ in message.get("headers", [])}
                extra = [(k, v) for k, v in SECURITY_HEADERS.items() if k not in present]
                if revalidate and b"cache-control" not in present:
                    extra.append((b"cache-control", b"no-cache"))
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        await self.app(scope, receive, send_with_headers)


@dataclass
class AppConfig:
    ask_per_minute: int = 10
    query_per_minute: int = 60
    daily_token_budget: int = 0  # 0 disables the cap
    cache_ttl_s: float = 86_400
    cache_entries: int = 500
    trust_proxy: bool = False  # read the client address from X-Forwarded-For
    warm_up: bool = False  # pre-build the dashboards in a background thread at start-up


def error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def clean_history(raw: Any) -> list[dict[str, str]]:
    """Keep the last few well-formed turns; drop everything else."""
    if not isinstance(raw, list):
        return []
    turns = []
    for item in raw[-MAX_HISTORY:]:
        if isinstance(item, dict) and isinstance(item.get("question"), str) and item["question"].strip():
            sql = item.get("sql") if isinstance(item.get("sql"), str) else ""
            turns.append({"question": item["question"].strip()[:MAX_QUESTION], "sql": sql[:MAX_SQL]})
    return turns


def create_app(agent: SQLAgent, config: AppConfig | None = None, workspace: Workspace | None = None) -> Starlette:
    config = config or AppConfig()
    ask_limiter = RateLimiter(config.ask_per_minute)
    query_limiter = RateLimiter(config.query_per_minute)
    cache = AnswerCache(config.cache_entries, config.cache_ttl_s)
    budget = DailyTokenBudget(config.daily_token_budget)
    dashboards: dict[str, dict[str, Any]] = {}
    facts: dict[str, Any] = {}

    def warm_up() -> None:
        """Build every dashboard once at start-up, so the first visit to Overview is instant."""
        try:
            facts.update(dataset_facts(agent.db))
            for year in ("all", "2016", "2017", "2018"):
                dashboards.setdefault(year, build_dashboard(agent.db, year))
        except Exception:  # noqa: BLE001 - warming is an optimisation; requests build on demand
            pass

    if config.warm_up:
        threading.Thread(target=warm_up, name="dashboard-warm-up", daemon=True).start()

    def client_of(request: Request) -> str:
        if config.trust_proxy:
            forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            if forwarded:
                return forwarded
        return request.client.host if request.client else "unknown"

    async def read_json(request: Request) -> dict[str, Any] | JSONResponse:
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return error("Request body too large.", 413)
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return error("Body must be JSON.", 400)
        if not isinstance(payload, dict):
            return error("Body must be a JSON object.", 422)
        return payload

    def to_payload(result: AgentResult, cached: bool = False) -> dict[str, Any]:
        data = json.loads(json.dumps(result.to_dict(), default=str))
        data["chart"] = suggest_chart(result.columns, result.rows)
        data["cached"] = cached
        return data

    async def prepare_ask(request: Request) -> JSONResponse | tuple[str, list[dict[str, str]], str]:
        """Validate an ask request. Returns an error response, or (question, history, cache key)."""
        payload = await read_json(request)
        if isinstance(payload, JSONResponse):
            return payload
        question = payload.get("question")
        if not isinstance(question, str) or not (MIN_QUESTION <= len(question.strip()) <= MAX_QUESTION):
            return error(f"'question' must be a string of {MIN_QUESTION} to {MAX_QUESTION} characters.", 422)
        question = question.strip()
        history = clean_history(payload.get("history"))
        return question, history, cache.key(agent.llm.model, question, history)

    def gate(request: Request, key: str) -> JSONResponse | dict[str, Any] | None:
        """Cache first, then the limits. Returns a cached payload, an error response, or None to proceed."""
        cached = cache.get(key)
        if cached is not None:
            return {**cached, "cached": True}
        if not ask_limiter.allow(client_of(request)):
            return error("Too many questions. Try again in a minute.", 429)
        if not budget.available():
            return error("The demo has used its model quota for today. Dashboards and saved queries still work.", 429)
        return None

    def finish(key: str, result: AgentResult) -> dict[str, Any]:
        budget.spend(result.input_tokens + result.output_tokens)
        payload = to_payload(result)
        if result.status in ("answered", "cannot_answer"):
            cache.put(key, payload)
        return payload

    # ------------------------------------------------------------------ routes

    def admit_model_call(request: Request) -> JSONResponse | None:
        """Rate limit and daily budget for anything that calls the model (no cache: edits are not repeatable)."""
        if not ask_limiter.allow(client_of(request)):
            return error("Too many requests to the AI. Try again in a minute.", 429)
        if not budget.available():
            return error("The model quota for today is used up. Dashboards still load and can be edited by hand.", 429)
        return None

    async def index(request: Request) -> FileResponse:
        return FileResponse(STATIC / "index.html")

    async def status(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "model": agent.llm.model,
                "budget": budget.snapshot(),
                "cache": {"hits": cache.hits, "misses": cache.misses},
            }
        )

    async def schema(request: Request) -> JSONResponse:
        tables = [
            {
                "name": t.name,
                "description": t.description,
                "columns": [{"name": c.name, "type": c.type, "description": c.description} for c in t.columns],
            }
            for t in TABLES
        ]
        if not facts:
            facts.update(await run_in_threadpool(dataset_facts, agent.db))
        return JSONResponse({"tables": tables, "notes": list(DATA_NOTES), "facts": facts})

    async def dashboard(request: Request) -> JSONResponse:
        year = request.query_params.get("year") or "all"
        if year not in dashboards:
            try:
                dashboards[year] = await run_in_threadpool(build_dashboard, agent.db, year)
            except ValueError as exc:
                return error(str(exc), 422)
        return JSONResponse(json.loads(json.dumps(dashboards[year], default=str)))

    async def query(request: Request) -> JSONResponse:
        if not query_limiter.allow(client_of(request)):
            return error("Too many requests. Try again in a minute.", 429)
        payload = await read_json(request)
        if isinstance(payload, JSONResponse):
            return payload
        sql = payload.get("sql")
        if not isinstance(sql, str) or not sql.strip() or len(sql) > MAX_SQL:
            return error(f"'sql' must be a non-empty string of at most {MAX_SQL} characters.", 422)
        try:
            result = await run_in_threadpool(agent.db.execute, sql)
        except QueryError as exc:
            return JSONResponse({"error": exc.message, "kind": exc.kind}, status_code=400)
        body = result_payload(result.columns, result.rows, result.truncated)
        body.update(sql=result.sql, elapsed_ms=result.elapsed_ms)
        return JSONResponse(json.loads(json.dumps(body, default=str)))

    async def ask(request: Request) -> JSONResponse:
        prepared = await prepare_ask(request)
        if isinstance(prepared, JSONResponse):
            return prepared
        question, history, key = prepared
        gated = gate(request, key)
        if isinstance(gated, JSONResponse):
            return gated
        if gated is not None:
            return JSONResponse(gated)
        result = await run_in_threadpool(agent.ask, question, history)
        return JSONResponse(finish(key, result), status_code=502 if result.status == "llm_error" else 200)

    async def ask_stream(request: Request) -> Response:
        prepared = await prepare_ask(request)
        if isinstance(prepared, JSONResponse):
            return prepared
        question, history, key = prepared
        gated = gate(request, key)
        if isinstance(gated, JSONResponse):
            return gated

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()

        def on_event(event: dict[str, Any]) -> None:  # called from the worker thread
            loop.call_soon_threadsafe(queue.put_nowait, (event["type"], event))

        async def work() -> None:
            try:
                result = await run_in_threadpool(agent.ask, question, history, on_event)
                payload = finish(key, result)
            except Exception as exc:  # noqa: BLE001 - the stream must always end with a result
                payload = to_payload(AgentResult(question=question, status="llm_error", error=f"Internal error: {exc}"))
            queue.put_nowait(("result", payload))

        async def events():
            if gated is not None:
                yield sse("result", gated)
                return
            task = asyncio.create_task(work())
            # Retrieve the outcome so a cancelled or failed task is never reported as unhandled.
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            while True:
                name, data = await queue.get()
                yield sse(name, data)
                if name == "result":
                    return

        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        return StreamingResponse(events(), media_type="text/event-stream", headers=headers)

    extra = []
    if workspace is not None:
        builder = DashboardAgent(agent.llm, agent.db)
        extra = workspace_routes(workspace, agent.db, builder, read_json, admit_model_call, budget.spend)

    app = Starlette(
        routes=[
            *extra,
            Route("/", index),
            Route("/api/status", status),
            Route("/api/health", status),
            Route("/api/schema", schema),
            Route("/api/dashboard", dashboard),
            Route("/api/query", query, methods=["POST"]),
            Route("/api/ask", ask, methods=["POST"]),
            Route("/api/ask/stream", ask_stream, methods=["POST"]),
            Mount("/static", StaticFiles(directory=STATIC), name="static"),
        ]
    )
    app.add_middleware(SecurityHeaders)
    return app


def sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def create_app_from_env() -> Starlette:
    """Factory for ``uvicorn --factory sqlagent.app:create_app_from_env``."""
    from .config import Settings, app_config, build_agent, build_workspace

    settings = Settings.from_env()
    return create_app(build_agent(settings), app_config(), build_workspace(settings))
