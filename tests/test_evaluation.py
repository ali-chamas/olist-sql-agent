from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from conftest import FakeLLM, call
from sqlagent.agent import SQLAgent
from sqlagent.db import ReadOnlyDatabase
from sqlagent.evaluation import (
    EvalCase,
    OracleLLM,
    TokenPacer,
    load_cases,
    load_outcomes,
    render_report,
    results_match,
    run_eval,
    score_case,
    summarize,
)
from sqlagent.llm import LLMError

QUESTIONS = Path(__file__).resolve().parents[1] / "evals" / "questions.json"

# ---------------------------------------------------------------- comparison


def test_identical_results_match() -> None:
    assert results_match([("SP", 10), ("RJ", 5)], [("SP", 10), ("RJ", 5)])


def test_row_order_is_ignored_unless_the_case_is_ordered() -> None:
    gold, pred = [("SP", 10), ("RJ", 5)], [("RJ", 5), ("SP", 10)]
    assert results_match(gold, pred)
    assert not results_match(gold, pred, ordered=True)


def test_column_order_and_extra_columns_are_accepted() -> None:
    gold = [("SP", 10.0), ("RJ", 5.0)]
    assert results_match(gold, [(10.0, "SP"), (5.0, "RJ")])
    assert results_match(gold, [("SP", "Sao Paulo", 10.0), ("RJ", "Rio", 5.0)])


def test_missing_column_or_row_fails() -> None:
    assert not results_match([("SP", 10)], [("SP",)])
    assert not results_match([("SP", 10), ("RJ", 5)], [("SP", 10)])
    assert not results_match([("SP", 10)], [("SP", 10), ("RJ", 5)])


def test_values_must_belong_to_the_same_row() -> None:
    # Each column matches as a set, but the pairing is wrong.
    assert not results_match([("SP", 10), ("RJ", 5)], [("SP", 5), ("RJ", 10)])


def test_rounded_numbers_match_unrounded_gold_but_real_differences_fail() -> None:
    assert results_match([(160.57763809,)], [(160.58,)])
    assert results_match([(4.0893,)], [(4.09,)])
    assert not results_match([(4.0893,)], [(4.02,)])  # item-level average instead of per order
    assert not results_match([(6.77,)], [(0.0677,)])  # ratio instead of percentage


def test_text_ignores_case_and_numbers_may_arrive_as_text() -> None:
    assert results_match([("Health Beauty",)], [(" health beauty ",)])
    assert results_match([(2017, 44579)], [("2017", 44579)])
    assert not results_match([("Toys",)], [("Books",)])


def test_nulls_only_match_nulls_and_empty_results_match() -> None:
    assert results_match([(None, 1)], [(None, 1)])
    assert not results_match([(None,)], [(0,)])
    assert results_match([], [])


# ------------------------------------------------------------------- scoring

CASE = EvalCase("c1", "aggregates", "How many orders?", "SELECT COUNT(DISTINCT order_id) FROM sales")
REFUSE = EvalCase("c2", "unanswerable", "Customer age?", None)


def run_one(db: ReadOnlyDatabase, case: EvalCase, *script) -> str:
    agent = SQLAgent(FakeLLM(list(script)), db, max_steps=2)
    return score_case(case, agent.ask(case.question), db).verdict


def test_verdicts(db: ReadOnlyDatabase) -> None:
    right = call("submit_answer", sql="SELECT COUNT(*) FROM (SELECT DISTINCT order_id FROM sales)", answer="5")
    wrong = call("submit_answer", sql="SELECT COUNT(*) FROM sales", answer="6")
    refuse = call("cannot_answer", reason="no")
    stuck = call("run_sql", sql="SELECT nope")

    assert run_one(db, CASE, right) == "correct"
    assert run_one(db, CASE, wrong) == "wrong_result"
    assert run_one(db, CASE, refuse) == "wrongly_refused"
    assert run_one(db, CASE, stuck, stuck) == "no_answer"
    assert run_one(db, CASE, LLMError("down")) == "llm_error"
    assert run_one(db, REFUSE, refuse) == "correct"
    assert run_one(db, REFUSE, wrong) == "should_refuse"


# -------------------------------------------------------------------- runner


def test_run_writes_results_and_resumes_without_repeating_finished_cases(db: ReadOnlyDatabase, tmp_path) -> None:
    cases = [CASE, REFUSE]
    path = tmp_path / "out" / "fake.jsonl"
    good = call("submit_answer", sql=CASE.gold_sql, answer="5")

    first = FakeLLM([good, LLMError("rate limited")])
    outcomes = run_eval(SQLAgent(first, db), cases, path)
    assert [o.verdict for o in outcomes] == ["correct", "llm_error"]
    assert set(load_outcomes(path)) == {"c1", "c2"}

    # Second run: c1 is kept, only the failed c2 is retried.
    second = FakeLLM([call("cannot_answer", reason="no")])
    outcomes = run_eval(SQLAgent(second, db), cases, path)
    assert [o.verdict for o in outcomes] == ["correct", "correct"]
    assert len(second.calls) == 1
    assert len(path.read_text().splitlines()) == 2

    # --fresh starts over.
    third = FakeLLM([good, call("cannot_answer", reason="no")])
    run_eval(SQLAgent(third, db), cases, path, resume=False)
    assert len(third.calls) == 2


def test_summary_and_report(db: ReadOnlyDatabase) -> None:
    llm = FakeLLM(
        [call("submit_answer", sql="SELECT COUNT(*) FROM sales", answer="6"), call("cannot_answer", reason="no")]
    )
    outcomes = run_eval(SQLAgent(llm, db), [CASE, REFUSE])
    summary = summarize(outcomes)
    assert summary["correct"] == 1 and summary["total"] == 2 and summary["accuracy"] == 50.0
    assert summary["by_category"]["aggregates"] == {"correct": 0, "total": 1}
    assert summary["verdicts"]["wrong_result"] == 1

    report = render_report(outcomes, "fake-model", dt.date(2026, 1, 2))
    assert "**Execution accuracy: 1/2 (50.0%)**" in report
    assert "### c1 (wrong_result)" in report and "SELECT COUNT(*) FROM sales" in report


def test_token_pacer_waits_for_the_minute_window() -> None:
    now = [0.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    pacer = TokenPacer(8000, clock=lambda: now[0], sleep=sleep)
    assert pacer.wait(3000) == 0.0  # nothing used yet
    pacer.record(4000)
    now[0] = 10.0
    pacer.record(3000)
    assert pacer.wait(500) == 0.0  # 7,500 of 8,000 still fits
    waited = pacer.wait(3000)  # must wait until the first 4,000 leave the window
    assert waited == pytest.approx(50.05)
    assert slept == [pytest.approx(50.05)]
    assert TokenPacer(0).wait(10**9) == 0.0  # disabled


# ---------------------------------------------------------- the question set


def test_question_file_has_50_cases_with_unique_ids() -> None:
    cases = load_cases(QUESTIONS)
    assert len(cases) == 50
    assert sum(not c.answerable for c in cases) == 4
    assert all(c.question.strip() and c.category for c in cases)


def test_every_gold_query_passes_the_guardrails_and_the_oracle_scores_100(db: ReadOnlyDatabase) -> None:
    """Runs the whole harness on the fixture database with a model that submits the gold SQL."""
    cases = load_cases(QUESTIONS)
    outcomes = run_eval(SQLAgent(OracleLLM(cases), db), cases)
    failed = [(o.id, o.verdict, o.detail) for o in outcomes if not o.passed]
    assert failed == []
