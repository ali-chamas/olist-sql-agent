from __future__ import annotations

from conftest import FakeLLM, call
from sqlagent.agent import SQLAgent, format_result
from sqlagent.db import ReadOnlyDatabase
from sqlagent.llm import LLMError, LLMResponse, ToolCall, Usage

GOOD_SQL = "SELECT COUNT(DISTINCT order_id) AS orders FROM sales"


def test_happy_path_runs_then_submits(db: ReadOnlyDatabase) -> None:
    llm = FakeLLM([call("run_sql", sql=GOOD_SQL), call("submit_answer", sql=GOOD_SQL, answer="There are 5 orders.")])
    result = SQLAgent(llm, db).ask("How many orders?")

    assert result.status == "answered"
    assert result.answer == "There are 5 orders."
    assert result.rows == [(5,)]
    assert result.columns == ["orders"]
    assert [s.tool for s in result.steps] == ["run_sql", "submit_answer"]
    assert result.llm_calls == 2
    assert (result.input_tokens, result.output_tokens) == (200, 40)
    # The second model call saw the query result as a tool message.
    tool_message = llm.calls[1][-1]
    assert tool_message.role == "tool" and '"rows": [[5]]' in tool_message.content


def test_sql_error_is_returned_to_the_model_which_then_corrects_itself(db: ReadOnlyDatabase) -> None:
    llm = FakeLLM(
        [
            call("run_sql", sql="SELECT COUNT(DISTINCT orderid) FROM sales"),
            call("submit_answer", sql=GOOD_SQL, answer="5 orders."),
        ]
    )
    result = SQLAgent(llm, db).ask("How many orders?")

    assert result.status == "answered"
    assert result.steps[0].ok is False and "orderid" in result.steps[0].error
    error_message = llm.calls[1][-1]
    assert error_message.is_error and "orderid" in error_message.content


def test_guardrail_rejection_never_reaches_the_user_as_an_answer(db: ReadOnlyDatabase) -> None:
    llm = FakeLLM(
        [
            call("submit_answer", sql="DELETE FROM sales", answer="Done."),
            call("cannot_answer", reason="I can only read data."),
        ]
    )
    result = SQLAgent(llm, db).ask("Delete everything")

    assert result.status == "cannot_answer"
    assert result.sql == ""
    assert result.steps[0].ok is False
    assert db.execute("SELECT COUNT(*) FROM sales").rows == [(6,)]


def test_submitted_sql_is_re_executed_so_the_answer_matches_the_query(db: ReadOnlyDatabase) -> None:
    llm = FakeLLM([call("submit_answer", sql="SELECT 1 AS one;", answer="One.")])
    result = SQLAgent(llm, db).ask("Give me one")
    assert result.status == "answered" and result.rows == [(1,)] and result.sql == "SELECT 1 AS one"


def test_cannot_answer_ends_the_run(db: ReadOnlyDatabase) -> None:
    result = SQLAgent(FakeLLM([call("cannot_answer", reason="No cost data.")]), db).ask("Profit margin?")
    assert result.status == "cannot_answer" and result.answer == "No cost data." and result.sql == ""


def test_text_only_reply_gets_a_nudge_then_continues(db: ReadOnlyDatabase) -> None:
    llm = FakeLLM(
        [
            LLMResponse(text="The answer is probably 5.", tool_calls=[], usage=Usage()),
            call("submit_answer", sql=GOOD_SQL, answer="5 orders."),
        ]
    )
    result = SQLAgent(llm, db).ask("How many orders?")
    assert result.status == "answered"
    assert llm.calls[1][-1].role == "user" and "submit_answer" in llm.calls[1][-1].content


def test_stops_after_max_steps(db: ReadOnlyDatabase) -> None:
    llm = FakeLLM([call("run_sql", sql="SELECT nope FROM sales") for _ in range(10)])
    result = SQLAgent(llm, db, max_steps=3).ask("Loop forever")
    assert result.status == "max_steps"
    assert result.llm_calls == 3 and len(result.steps) == 3
    assert "3 model calls" in result.error


def test_provider_failure_is_reported_not_raised(db: ReadOnlyDatabase) -> None:
    result = SQLAgent(FakeLLM([LLMError("HTTP 401")]), db).ask("How many orders?")
    assert result.status == "llm_error" and "401" in result.error


def test_malformed_and_unknown_tool_calls_are_sent_back_as_errors(db: ReadOnlyDatabase) -> None:
    bad_json = LLMResponse(
        text="",
        usage=Usage(),
        tool_calls=[ToolCall(id="a", name="run_sql", arguments={}, parse_error="Arguments were not valid JSON")],
    )
    llm = FakeLLM(
        [
            bad_json,
            call("drop_database"),
            call("run_sql"),  # missing sql
            call("submit_answer", sql=GOOD_SQL, answer="5."),
        ]
    )
    result = SQLAgent(llm, db).ask("How many orders?")
    assert result.status == "answered"
    assert [s.ok for s in result.steps] == [False, False, False, True]


def test_every_tool_call_gets_a_result_when_several_arrive_together(db: ReadOnlyDatabase) -> None:
    both = LLMResponse(
        text="",
        usage=Usage(),
        tool_calls=[
            ToolCall(id="a", name="submit_answer", arguments={"sql": GOOD_SQL, "answer": "5."}),
            ToolCall(id="b", name="run_sql", arguments={"sql": "SELECT 2"}),
        ],
    )
    agent = SQLAgent(FakeLLM([both]), db)
    result = agent.ask("How many orders?")
    assert result.status == "answered" and len(result.steps) == 1


def test_result_preview_is_bounded(db: ReadOnlyDatabase) -> None:
    wide = db.execute("SELECT * FROM sales s1, sales s2", max_rows=36)
    text = format_result(wide)
    assert len(text) <= 2500
    assert '"more_rows": true' in text


def test_agent_recovers_when_the_provider_rejects_a_malformed_tool_call(db: ReadOnlyDatabase) -> None:
    """End to end through the real adapter: a rejected call, then a nudge, then a valid answer."""
    import json

    from sqlagent.llm import HttpJson, OpenAICompatClient

    def reply(name: str, **arguments) -> bytes:
        call = {"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
        return json.dumps({"choices": [{"message": {"content": None, "tool_calls": [call]}}], "usage": {}}).encode()

    unrepairable = {
        "error": {"code": "tool_use_failed", "message": "bad", "failed_generation": "<|channel|>commentary"}
    }
    replies = [
        (400, {}, json.dumps(unrepairable).encode()),
        (200, {}, reply("submit_answer", sql=GOOD_SQL, answer="5.")),
    ]
    sent: list[dict] = []

    def transport(url, headers, payload, timeout):
        sent.append(payload)
        return replies.pop(0)

    llm = OpenAICompatClient("https://x/v1", "K", "m", http=HttpJson(transport=transport))
    result = SQLAgent(llm, db).ask("How many orders?")
    assert result.status == "answered" and result.rows == [(5,)]
    # The retry carried a reminder of the available tools.
    assert "submit_answer" in sent[1]["messages"][-1]["content"] and sent[1]["messages"][-1]["role"] == "user"


def test_an_empty_model_turn_is_sent_back_as_valid_text(db: ReadOnlyDatabase) -> None:
    """Reasoning models can return nothing when they hit the token limit; the next request must stay valid."""
    llm = FakeLLM(
        [LLMResponse(text="", tool_calls=[], usage=Usage()), call("submit_answer", sql=GOOD_SQL, answer="5.")]
    )
    result = SQLAgent(llm, db).ask("How many orders?")
    assert result.status == "answered"
    assistant = [m for m in llm.calls[1] if m.role == "assistant"][0]
    assert assistant.content and not assistant.tool_calls
