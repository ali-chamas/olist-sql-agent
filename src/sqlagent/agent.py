"""The agent loop: question in, checked SQL and an answer out.

The model has three tools: run_sql (try a query), submit_answer (finish) and
cannot_answer (decline). Guardrail and SQL errors are returned to the model as
tool errors so it can correct itself; the loop is bounded by ``max_steps``.

Two optional extras: ``history`` gives the model the previous turns of a
conversation so follow-up questions work, and ``on_event`` receives each step
as it happens so a UI can show the agent working.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from .db import QueryError, QueryResult, ReadOnlyDatabase
from .llm import LLMClient, LLMError, Message, ToolCall, Usage, report_waits
from .prompts import TOOLS, system_prompt

PREVIEW_ROWS = 20
PREVIEW_CHARS = 2500
MAX_HISTORY_TURNS = 3
MAX_FOLLOW_UPS = 3

EventHandler = Callable[[dict[str, Any]], None]

EMPTY_TURN = "(no response)"
NUDGE = "Use the tools: call run_sql to test a query, then submit_answer, or cannot_answer if the data cannot answer."


@dataclass
class Step:
    """One tool call made by the model, for the trace shown in the UI and evals."""

    tool: str
    sql: str = ""
    ok: bool = True
    error: str = ""
    row_count: int = 0
    elapsed_ms: float = 0.0
    detail: str = ""  # a sentence for the UI, used by the dashboard agent's edits


@dataclass
class AgentResult:
    question: str
    status: str  # answered | cannot_answer | max_steps | llm_error
    answer: str = ""
    sql: str = ""
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    truncated: bool = False
    steps: list[Step] = field(default_factory=list)
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    error: str = ""
    follow_ups: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["rows"] = [list(r) for r in self.rows]
        return data


def format_result(result: QueryResult) -> str:
    """Compact JSON preview of a result for the model."""
    preview = [list(r) for r in result.rows[:PREVIEW_ROWS]]
    payload = {
        "columns": result.columns,
        "rows": preview,
        "row_count": result.row_count,
        "more_rows": result.truncated or result.row_count > len(preview),
    }
    text = json.dumps(payload, default=str, ensure_ascii=False)
    if len(text) > PREVIEW_CHARS:
        payload["rows"] = preview[:5]
        payload["more_rows"] = True
        text = json.dumps(payload, default=str, ensure_ascii=False)[:PREVIEW_CHARS]
    return text


def build_user_message(question: str, history: list[dict[str, str]] | None) -> str:
    """The question, preceded by the last turns when this is a follow-up."""
    turns = [t for t in (history or []) if t.get("question")][-MAX_HISTORY_TURNS:]
    if not turns:
        return question
    lines = ["Earlier in this conversation:"]
    for turn in turns:
        lines.append(f"Q: {turn['question'][:300]}")
        if turn.get("sql"):
            lines.append(f"SQL: {turn['sql'][:1200]}")
    lines += ["", "The new question may refer to the ones above. Answer only the new question.", "", question]
    return "\n".join(lines)


def clean_follow_ups(raw: Any) -> list[str]:
    if isinstance(raw, str):  # some models send one question as a plain string
        raw = [raw]
    if not isinstance(raw, list):
        return []
    seen: list[str] = []
    for item in raw:
        text = str(item).strip()[:160] if isinstance(item, str) else ""
        if text and text not in seen:
            seen.append(text)
    return seen[:MAX_FOLLOW_UPS]


class SQLAgent:
    def __init__(self, llm: LLMClient, db: ReadOnlyDatabase, max_steps: int = 6) -> None:
        self.llm = llm
        self.db = db
        self.max_steps = max_steps
        self.system = system_prompt()

    def ask(
        self,
        question: str,
        history: list[dict[str, str]] | None = None,
        on_event: EventHandler | None = None,
    ) -> AgentResult:
        started = time.monotonic()
        result = AgentResult(question=question, status="max_steps")
        usage = Usage()
        messages: list[Message] = [Message(role="user", content=build_user_message(question, history))]

        def emit(event: dict[str, Any]) -> None:
            if on_event:
                on_event(event)

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
            # An assistant turn with neither text nor tool calls is rejected by OpenAI-style APIs,
            # and reasoning models produce exactly that when they run out of tokens.
            content = response.text or ("" if response.tool_calls else EMPTY_TURN)
            messages.append(Message(role="assistant", content=content, tool_calls=response.tool_calls))

            if not response.tool_calls:
                emit({"type": "retry", "call": call_number})
                messages.append(Message(role="user", content=NUDGE))
                continue

            finished = False
            for call in response.tool_calls:
                if finished:
                    # Every tool call needs a result, even after the run is decided.
                    messages.append(self._tool_message(call, "Ignored: the answer was already submitted.", True))
                    continue
                content, is_error, finished = self._handle(call, result)
                emit({"type": "step", "step": asdict(result.steps[-1])})
                messages.append(self._tool_message(call, content, is_error))
            if finished:
                break

        if result.status == "max_steps":
            result.error = f"No answer after {self.max_steps} model calls."
        result.input_tokens, result.output_tokens = usage.input_tokens, usage.output_tokens
        result.latency_ms = round((time.monotonic() - started) * 1000, 1)
        return result

    @staticmethod
    def _tool_message(call: ToolCall, content: str, is_error: bool) -> Message:
        return Message(role="tool", content=content, tool_call_id=call.id, name=call.name, is_error=is_error)

    def _handle(self, call: ToolCall, result: AgentResult) -> tuple[str, bool, bool]:
        """Run one tool call. Returns (content for the model, is_error, finished)."""
        if call.parse_error:
            result.steps.append(Step(tool=call.name, ok=False, error=call.parse_error))
            return call.parse_error, True, False

        if call.name == "cannot_answer":
            reason = str(call.arguments.get("reason") or "").strip() or "The dataset cannot answer this question."
            result.steps.append(Step(tool="cannot_answer"))
            result.status, result.answer = "cannot_answer", reason
            return "Recorded.", False, True

        if call.name not in ("run_sql", "submit_answer"):
            message = f"Unknown tool {call.name!r}. Available: run_sql, submit_answer, cannot_answer."
            result.steps.append(Step(tool=call.name, ok=False, error=message))
            return message, True, False

        sql = call.arguments.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            message = "Missing required argument: sql."
            result.steps.append(Step(tool=call.name, ok=False, error=message))
            return message, True, False

        try:
            query = self.db.execute(sql)
        except QueryError as exc:
            result.steps.append(Step(tool=call.name, sql=sql, ok=False, error=exc.message))
            return exc.message, True, False

        result.steps.append(Step(tool=call.name, sql=query.sql, row_count=query.row_count, elapsed_ms=query.elapsed_ms))
        if call.name == "run_sql":
            return format_result(query), False, False

        result.status = "answered"
        result.answer = str(call.arguments.get("answer") or "").strip()
        result.follow_ups = clean_follow_ups(call.arguments.get("follow_ups"))
        result.sql = query.sql
        result.columns, result.rows, result.truncated = query.columns, query.rows, query.truncated
        return "Recorded.", False, True
