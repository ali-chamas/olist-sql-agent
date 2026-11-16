"""Evaluation harness: execution accuracy on a set of reference questions.

Each case has a question and a hand-written gold SQL query. The agent's final
SQL and the gold SQL are both executed, and the case passes when the two
results contain the same data (see ``results_match``). Comparing results
rather than SQL text accepts any correct query, however it is written.

Cases without gold SQL are questions the dataset cannot answer; they pass when
the agent declines instead of inventing a number.
"""

from __future__ import annotations

import datetime as dt
import json
import statistics
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .agent import AgentResult, SQLAgent
from .db import QueryError, ReadOnlyDatabase
from .llm import LLMResponse, Message, ToolCall, ToolSpec, Usage

EVAL_MAX_ROWS = 2000
NUMERIC_TOLERANCE = 0.005  # absolute; lets "rounded to 2 decimals" match the unrounded value
MAX_COLUMN_SEARCH = 5000  # cap on column assignments tried per case

VERDICTS = ("correct", "wrong_result", "no_answer", "wrongly_refused", "should_refuse", "llm_error")


# --------------------------------------------------------------------------
# Cases
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EvalCase:
    id: str
    category: str
    question: str
    gold_sql: str | None  # None: the question cannot be answered from the data
    ordered: bool = False  # True when row order is part of the answer

    @property
    def answerable(self) -> bool:
        return self.gold_sql is not None


def load_cases(path: str | Path) -> list[EvalCase]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = [
        EvalCase(
            id=item["id"],
            category=item["category"],
            question=item["question"],
            gold_sql=item.get("gold_sql"),
            ordered=bool(item.get("ordered", False)),
        )
        for item in raw
    ]
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate case ids in the question file.")
    return cases


# --------------------------------------------------------------------------
# Result comparison
# --------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def values_equal(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if _is_number(a) and _is_number(b):
        return abs(float(a) - float(b)) <= NUMERIC_TOLERANCE + 1e-9
    if _is_number(a) != _is_number(b):
        # A number returned as text ("2017") still counts.
        try:
            return abs(float(a) - float(b)) <= NUMERIC_TOLERANCE + 1e-9
        except (TypeError, ValueError):
            return False
    return str(a).strip().casefold() == str(b).strip().casefold()


def _sort_key(value: Any) -> tuple[int, Any]:
    if value is None:
        return (0, 0)
    if _is_number(value):
        return (1, float(value))
    return (2, str(value).strip().casefold())


def _columns_equal(a: Sequence[Any], b: Sequence[Any], ordered: bool) -> bool:
    if not ordered:
        a, b = sorted(a, key=_sort_key), sorted(b, key=_sort_key)
    return all(values_equal(x, y) for x, y in zip(a, b, strict=True))


def _rows_equal(gold: list[tuple], pred: list[tuple], ordered: bool) -> bool:
    if ordered:
        return all(all(values_equal(x, y) for x, y in zip(g, p, strict=True)) for g, p in zip(gold, pred, strict=True))
    remaining = list(pred)
    for g in gold:
        for i, p in enumerate(remaining):
            if all(values_equal(x, y) for x, y in zip(g, p, strict=True)):
                del remaining[i]
                break
        else:
            return False
    return True


def results_match(
    gold_rows: Sequence[Sequence[Any]], pred_rows: Sequence[Sequence[Any]], ordered: bool = False
) -> bool:
    """True when the prediction contains the gold data.

    * Same number of rows.
    * Every gold column maps to a distinct predicted column. Extra predicted
      columns and a different column order are accepted; column names are ignored.
    * Numbers match within NUMERIC_TOLERANCE; text ignores case and outer spaces.
    * Row order matters only when ``ordered`` is set.
    """
    if len(gold_rows) != len(pred_rows):
        return False
    if not gold_rows:
        return True
    n_gold, n_pred = len(gold_rows[0]), len(pred_rows[0])
    if n_gold > n_pred:
        return False

    gold_cols = [[r[j] for r in gold_rows] for j in range(n_gold)]
    pred_cols = [[r[j] for r in pred_rows] for j in range(n_pred)]
    candidates = [
        [k for k in range(n_pred) if _columns_equal(gold_cols[j], pred_cols[k], ordered)] for j in range(n_gold)
    ]
    if any(not c for c in candidates):
        return False

    tried = 0

    def assign(j: int, used: tuple[int, ...]) -> bool:
        nonlocal tried
        if j == n_gold:
            tried += 1
            projected = [tuple(r[k] for k in used) for r in pred_rows]
            return _rows_equal([tuple(r) for r in gold_rows], projected, ordered)
        for k in candidates[j]:
            if k in used:
                continue
            if tried >= MAX_COLUMN_SEARCH:
                return False
            if assign(j + 1, (*used, k)):
                return True
        return False

    return assign(0, ())


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


@dataclass
class CaseOutcome:
    id: str
    category: str
    question: str
    verdict: str
    detail: str = ""
    predicted_sql: str = ""
    llm_calls: int = 0
    tokens: int = 0
    latency_ms: float = 0.0
    model: str = ""

    @property
    def passed(self) -> bool:
        return self.verdict == "correct"


def score_case(case: EvalCase, result: AgentResult, db: ReadOnlyDatabase, model: str = "") -> CaseOutcome:
    outcome = CaseOutcome(
        id=case.id,
        category=case.category,
        question=case.question,
        verdict="wrong_result",
        predicted_sql=result.sql,
        llm_calls=result.llm_calls,
        tokens=result.input_tokens + result.output_tokens,
        latency_ms=result.latency_ms,
        model=model,
    )
    if result.status == "llm_error":
        outcome.verdict, outcome.detail = "llm_error", result.error
        return outcome
    if not case.answerable:
        outcome.verdict = "correct" if result.status == "cannot_answer" else "should_refuse"
        return outcome
    if result.status == "cannot_answer":
        outcome.verdict, outcome.detail = "wrongly_refused", result.answer
        return outcome
    if result.status != "answered":
        outcome.verdict, outcome.detail = "no_answer", result.error
        return outcome

    gold = db.execute(case.gold_sql or "", max_rows=EVAL_MAX_ROWS)
    try:
        predicted = db.execute(result.sql, max_rows=EVAL_MAX_ROWS)
    except QueryError as exc:  # cannot happen for a submitted query, but stay safe
        outcome.detail = exc.message
        return outcome
    if results_match(gold.rows, predicted.rows, case.ordered):
        outcome.verdict = "correct"
    else:
        outcome.detail = (
            f"gold returned {gold.row_count} rows x {len(gold.columns)} cols, "
            f"prediction {predicted.row_count} rows x {len(predicted.columns)} cols"
        )
    return outcome


# --------------------------------------------------------------------------
# Pacing for rate-limited (free) providers
# --------------------------------------------------------------------------


class TokenPacer:
    """Keeps token use under a tokens-per-minute budget by sleeping between cases."""

    def __init__(
        self,
        tokens_per_minute: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.tpm = tokens_per_minute
        self.clock, self.sleep = clock, sleep
        self._events: deque[tuple[float, int]] = deque()

    def record(self, tokens: int) -> None:
        self._events.append((self.clock(), tokens))

    def wait(self, expected_tokens: int) -> float:
        """Sleep until ``expected_tokens`` more fit in the current minute. Returns seconds slept."""
        if self.tpm <= 0:
            return 0.0
        slept = 0.0
        while True:
            now = self.clock()
            while self._events and now - self._events[0][0] >= 60:
                self._events.popleft()
            used = sum(t for _, t in self._events)
            if not self._events or used + expected_tokens <= self.tpm:
                return slept
            pause = 60 - (now - self._events[0][0]) + 0.05
            self.sleep(pause)
            slept += pause


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


def load_outcomes(path: Path) -> dict[str, CaseOutcome]:
    outcomes: dict[str, CaseOutcome] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                outcome = CaseOutcome(**json.loads(line))
                outcomes[outcome.id] = outcome
    return outcomes


def run_eval(
    agent: SQLAgent,
    cases: Iterable[EvalCase],
    results_path: Path | None = None,
    pacer: TokenPacer | None = None,
    resume: bool = True,
    on_case: Callable[[CaseOutcome], None] | None = None,
) -> list[CaseOutcome]:
    """Run every case. Finished cases are appended to ``results_path`` (JSON lines),
    so an interrupted or rate-limited run continues where it stopped."""
    model = agent.llm.model
    done: dict[str, CaseOutcome] = {}
    if results_path and resume:
        done = {k: v for k, v in load_outcomes(results_path).items() if v.verdict != "llm_error" and v.model == model}
    elif results_path:
        results_path.unlink(missing_ok=True)
    if results_path:
        results_path.parent.mkdir(parents=True, exist_ok=True)
        results_path.write_text("".join(json.dumps(asdict(o)) + "\n" for o in done.values()), encoding="utf-8")

    outcomes: list[CaseOutcome] = []
    typical_tokens = 3000
    for case in cases:
        if case.id in done:
            outcomes.append(done[case.id])
            continue
        if pacer:
            pacer.wait(typical_tokens)
        result = agent.ask(case.question)
        outcome = score_case(case, result, agent.db, model)
        if pacer:
            pacer.record(outcome.tokens)
        if outcome.tokens:
            typical_tokens = max(500, int(0.7 * typical_tokens + 0.3 * outcome.tokens))
        outcomes.append(outcome)
        if results_path:
            with results_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(outcome)) + "\n")
        if on_case:
            on_case(outcome)
    return outcomes


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def summarize(outcomes: Sequence[CaseOutcome]) -> dict[str, Any]:
    total = len(outcomes)
    correct = sum(o.passed for o in outcomes)
    by_category: dict[str, list[int]] = {}
    for o in outcomes:
        bucket = by_category.setdefault(o.category, [0, 0])
        bucket[0] += int(o.passed)
        bucket[1] += 1
    verdicts = {v: sum(o.verdict == v for o in outcomes) for v in VERDICTS}
    ran = [o for o in outcomes if o.llm_calls]
    return {
        "total": total,
        "correct": correct,
        "accuracy": round(100 * correct / total, 1) if total else 0.0,
        "by_category": {k: {"correct": v[0], "total": v[1]} for k, v in sorted(by_category.items())},
        "verdicts": verdicts,
        "avg_llm_calls": round(statistics.mean(o.llm_calls for o in ran), 2) if ran else 0,
        "avg_tokens": round(statistics.mean(o.tokens for o in ran)) if ran else 0,
        "median_latency_s": round(statistics.median(o.latency_ms for o in ran) / 1000, 2) if ran else 0,
    }


def render_report(outcomes: Sequence[CaseOutcome], model: str, date: dt.date | None = None) -> str:
    s = summarize(outcomes)
    date = date or dt.date.today()
    lines = [
        f"# Evaluation report: {model}",
        "",
        f"Run date: {date.isoformat()}",
        "",
        f"**Execution accuracy: {s['correct']}/{s['total']} ({s['accuracy']}%)**",
        "",
        f"- Average model calls per question: {s['avg_llm_calls']}",
        f"- Average tokens per question: {s['avg_tokens']}",
        f"- Median latency: {s['median_latency_s']} s",
        "",
        "| Category | Correct | Total |",
        "| --- | --- | --- |",
    ]
    lines += [f"| {name} | {v['correct']} | {v['total']} |" for name, v in s["by_category"].items()]
    lines += ["", "| Verdict | Count |", "| --- | --- |"]
    lines += [f"| {name} | {count} |" for name, count in s["verdicts"].items() if count]
    failures = [o for o in outcomes if not o.passed]
    if failures:
        lines += ["", "## Failures", ""]
        for o in failures:
            lines.append(f"### {o.id} ({o.verdict})")
            lines.append("")
            lines.append(o.question)
            if o.detail:
                lines += ["", f"Detail: {o.detail}"]
            if o.predicted_sql:
                lines += ["", "```sql", o.predicted_sql, "```"]
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------
# Oracle model: checks the harness itself, needs no API key
# --------------------------------------------------------------------------


class OracleLLM:
    """A fake model that always submits the gold SQL.

    Running the eval with it must score 100%. That proves the gold queries pass
    the guardrails and that the comparison logic accepts identical results.
    """

    model = "oracle"

    def __init__(self, cases: Iterable[EvalCase]) -> None:
        self._gold = {c.question: c.gold_sql for c in cases}

    def complete(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        question = messages[0].content
        gold = self._gold.get(question)
        if gold is None:
            call = ToolCall(id="oracle", name="cannot_answer", arguments={"reason": "Not in the data."})
        else:
            call = ToolCall(id="oracle", name="submit_answer", arguments={"sql": gold, "answer": "Gold answer."})
        return LLMResponse(text="", tool_calls=[call], usage=Usage())
