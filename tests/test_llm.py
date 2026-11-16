"""Adapter tests against the documented wire formats, using a fake transport."""

from __future__ import annotations

import json
from typing import Any

import pytest

from sqlagent.llm import AnthropicClient, HttpJson, LLMError, Message, OpenAICompatClient, ToolCall, ToolSpec

TOOL = ToolSpec(
    "run_sql", "Run SQL.", {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}
)

HISTORY = [
    Message(role="user", content="How many orders?"),
    Message(role="assistant", content="", tool_calls=[ToolCall("c1", "run_sql", {"sql": "SELECT 1"})]),
    Message(role="tool", content='{"rows": [[1]]}', tool_call_id="c1", name="run_sql"),
]


class Recorder:
    """Fake transport: replies from a queue and keeps the requests."""

    def __init__(self, *replies: tuple[int, dict[str, str], Any]) -> None:
        self.replies = list(replies)
        self.requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    def __call__(self, url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float):
        self.requests.append((url, headers, payload))
        status, reply_headers, body = self.replies.pop(0)
        return status, reply_headers, body if isinstance(body, bytes) else json.dumps(body).encode()


def http(recorder: Recorder, sleeps: list[float] | None = None) -> HttpJson:
    return HttpJson(transport=recorder, sleep=(sleeps if sleeps is not None else []).append)


OPENAI_REPLY = {
    "choices": [
        {
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_9",
                        "type": "function",
                        "function": {"name": "run_sql", "arguments": '{"sql": "SELECT 2"}'},
                    }
                ],
            }
        }
    ],
    "usage": {"prompt_tokens": 321, "completion_tokens": 12},
}


def test_openai_compat_request_and_response_shapes() -> None:
    recorder = Recorder((200, {}, OPENAI_REPLY))
    client = OpenAICompatClient("https://api.example.com/v1/", "KEY", "some-model", http=http(recorder))

    response = client.complete("SYSTEM", HISTORY, [TOOL])

    url, headers, payload = recorder.requests[0]
    assert url == "https://api.example.com/v1/chat/completions"
    assert headers["Authorization"] == "Bearer KEY"
    assert payload["model"] == "some-model" and payload["temperature"] == 0
    assert payload["tools"][0] == {
        "type": "function",
        "function": {"name": "run_sql", "description": "Run SQL.", "parameters": TOOL.parameters},
    }
    roles = [m["role"] for m in payload["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert payload["messages"][2]["tool_calls"][0]["function"]["arguments"] == '{"sql": "SELECT 1"}'
    assert payload["messages"][3] == {"role": "tool", "tool_call_id": "c1", "content": '{"rows": [[1]]}'}

    assert response.tool_calls == [ToolCall("call_9", "run_sql", {"sql": "SELECT 2"})]
    assert (response.usage.input_tokens, response.usage.output_tokens) == (321, 12)


def test_openai_compat_flags_malformed_arguments_instead_of_crashing() -> None:
    reply = json.loads(json.dumps(OPENAI_REPLY))
    reply["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = '{"sql": "SELECT'
    client = OpenAICompatClient("https://x/v1", "", "m", http=http(Recorder((200, {}, reply))))
    response = client.complete("S", HISTORY[:1], [TOOL])
    assert response.tool_calls[0].parse_error
    assert response.tool_calls[0].arguments == {}


def test_openai_compat_omits_auth_header_without_a_key() -> None:
    recorder = Recorder((200, {}, {"choices": [{"message": {"content": "hi"}}]}))
    response = OpenAICompatClient("http://localhost:11434/v1", "", "m", http=http(recorder)).complete(
        "S", HISTORY[:1], []
    )
    assert "Authorization" not in recorder.requests[0][1]
    assert "tools" not in recorder.requests[0][2]
    assert response.text == "hi" and response.tool_calls == []


ANTHROPIC_REPLY = {
    "content": [
        {"type": "text", "text": "Checking."},
        {"type": "tool_use", "id": "toolu_1", "name": "run_sql", "input": {"sql": "SELECT 2"}},
    ],
    "stop_reason": "tool_use",
    "usage": {"input_tokens": 400, "output_tokens": 30},
}


def test_anthropic_request_and_response_shapes() -> None:
    recorder = Recorder((200, {}, ANTHROPIC_REPLY))
    client = AnthropicClient("KEY", "claude-model", http=http(recorder))
    history = [
        *HISTORY,
        Message(role="tool", content="boom", tool_call_id="c2", name="run_sql", is_error=True),
    ]

    response = client.complete("SYSTEM", history, [TOOL])

    url, headers, payload = recorder.requests[0]
    assert url == "https://api.anthropic.com/v1/messages"
    assert headers["x-api-key"] == "KEY" and headers["anthropic-version"] == "2023-06-01"
    assert payload["system"] == "SYSTEM" and payload["max_tokens"] > 0
    assert payload["tools"] == [{"name": "run_sql", "description": "Run SQL.", "input_schema": TOOL.parameters}]
    assert [m["role"] for m in payload["messages"]] == ["user", "assistant", "user"]
    assert payload["messages"][1]["content"] == [
        {"type": "tool_use", "id": "c1", "name": "run_sql", "input": {"sql": "SELECT 1"}}
    ]
    # Both tool results travel in one user message; the failed one is flagged.
    assert payload["messages"][2]["content"] == [
        {"type": "tool_result", "tool_use_id": "c1", "content": '{"rows": [[1]]}'},
        {"type": "tool_result", "tool_use_id": "c2", "content": "boom", "is_error": True},
    ]

    assert response.text == "Checking."
    assert response.tool_calls == [ToolCall("toolu_1", "run_sql", {"sql": "SELECT 2"})]
    assert response.usage.total == 430


def test_rate_limit_is_retried_using_retry_after() -> None:
    sleeps: list[float] = []
    recorder = Recorder((429, {"retry-after": "7"}, {"error": "slow down"}), (200, {}, OPENAI_REPLY))
    client = OpenAICompatClient("https://x/v1", "K", "m", http=http(recorder, sleeps))
    assert client.complete("S", HISTORY[:1], [TOOL]).tool_calls
    assert sleeps == [7.0] and len(recorder.requests) == 2


def test_server_errors_back_off_exponentially_then_give_up() -> None:
    sleeps: list[float] = []
    recorder = Recorder(*[(503, {}, b"unavailable")] * 5)
    client = OpenAICompatClient("https://x/v1", "K", "m", http=http(recorder, sleeps))
    with pytest.raises(LLMError, match="503"):
        client.complete("S", HISTORY[:1], [TOOL])
    assert sleeps == [2.0, 4.0, 8.0, 16.0]


def test_auth_errors_are_not_retried() -> None:
    sleeps: list[float] = []
    recorder = Recorder((401, {}, {"error": "bad key"}))
    with pytest.raises(LLMError, match="401"):
        OpenAICompatClient("https://x/v1", "K", "m", http=http(recorder, sleeps)).complete("S", HISTORY[:1], [])
    assert sleeps == [] and len(recorder.requests) == 1


def test_unexpected_body_raises_llm_error() -> None:
    with pytest.raises(LLMError):
        OpenAICompatClient("https://x/v1", "K", "m", http=http(Recorder((200, {}, {"oops": 1})))).complete("S", [], [])
    with pytest.raises(LLMError):
        AnthropicClient("K", "m", http=http(Recorder((200, {}, b"<html>")))).complete("S", [], [])


# ------------------------------------------------- malformed tool calls from the model

from sqlagent.llm import match_tool_by_arguments, repair_tool_name  # noqa: E402
from sqlagent.prompts import TOOLS  # noqa: E402


def rejected(failed_generation: str, code: str = "tool_use_failed") -> tuple[int, dict, dict]:
    """The HTTP 400 Groq sends when the model's tool call fails validation."""
    message = "Tool call validation failed: attempted to call tool 'commentary' which was not in request.tools"
    error = {"message": message, "type": "invalid_request_error", "code": code, "failed_generation": failed_generation}
    return 400, {}, {"error": error}


def test_tool_is_identified_by_its_arguments() -> None:
    assert match_tool_by_arguments({"sql": "SELECT 1"}, TOOLS) == "run_sql"
    assert match_tool_by_arguments({"sql": "SELECT 1", "answer": "One."}, TOOLS) == "submit_answer"
    assert match_tool_by_arguments({"sql": "SELECT 1", "answer": "One.", "follow_ups": []}, TOOLS) == "submit_answer"
    assert match_tool_by_arguments({"reason": "No data."}, TOOLS) == "cannot_answer"
    assert match_tool_by_arguments({"answer": "No SQL given."}, TOOLS) is None
    assert match_tool_by_arguments({"sql": "SELECT 1", "extra": 1}, TOOLS) is None
    assert match_tool_by_arguments({}, TOOLS) is None


def test_valid_names_and_unparsed_calls_are_left_alone() -> None:
    good = ToolCall("a", "run_sql", {"sql": "SELECT 1", "answer": "looks like submit"})
    assert repair_tool_name(good, TOOLS) is good
    broken = ToolCall("b", "commentary", {}, parse_error="bad json")
    assert repair_tool_name(broken, TOOLS) is broken


def test_rejected_tool_call_with_a_wrong_name_is_repaired_from_its_arguments() -> None:
    generation = '{"name": "commentary", "arguments": {\n "answer": "Health Beauty leads.", "sql": "SELECT 1"}}'
    recorder = Recorder(rejected(generation))
    client = OpenAICompatClient("https://api.groq.com/openai/v1", "K", "openai/gpt-oss-120b", http=http(recorder))
    response = client.complete("S", HISTORY[:1], TOOLS)
    assert [(c.name, c.arguments["sql"]) for c in response.tool_calls] == [("submit_answer", "SELECT 1")]
    assert len(recorder.requests) == 1  # repaired locally, no second request


def test_rejected_tool_call_that_cannot_be_repaired_becomes_an_empty_turn() -> None:
    for generation in (
        '{"name": "commentary", "arguments": {"answer": "no sql"}}',
        "<|channel|>commentary not json",
        "",
    ):
        client = OpenAICompatClient("https://x/v1", "K", "m", http=http(Recorder(rejected(generation))))
        response = client.complete("S", HISTORY[:1], TOOLS)
        assert response.tool_calls == [] and response.text  # the agent will nudge and retry


def test_other_400_errors_stay_fatal() -> None:
    for reply in (rejected("{}", code="invalid_api_key"), (400, {}, b"not json"), (400, {}, {"error": "plain string"})):
        with pytest.raises(LLMError, match="400"):
            OpenAICompatClient("https://x/v1", "K", "m", http=http(Recorder(reply))).complete("S", HISTORY[:1], TOOLS)


def test_wrong_tool_name_in_a_normal_response_is_repaired_too() -> None:
    reply = json.loads(json.dumps(OPENAI_REPLY))
    reply["choices"][0]["message"]["tool_calls"][0]["function"] = {
        "name": "final",
        "arguments": '{"reason": "No cost data."}',
    }
    client = OpenAICompatClient("https://x/v1", "K", "m", http=http(Recorder((200, {}, reply))))
    assert client.complete("S", HISTORY[:1], TOOLS).tool_calls[0].name == "cannot_answer"


def test_raw_harmony_output_is_recovered() -> None:
    raw = '<|channel|>commentary to=functions.run_sql <|constrain|>json<|message|>{"sql": "SELECT 1"}<|call|>'
    client = OpenAICompatClient("https://x/v1", "K", "m", http=http(Recorder(rejected(raw))))
    assert [(c.name, c.arguments) for c in client.complete("S", HISTORY[:1], TOOLS).tool_calls] == [
        ("run_sql", {"sql": "SELECT 1"})
    ]


def test_schema_rejection_with_the_right_name_keeps_the_call() -> None:
    raw = '{"name": "submit_answer", "arguments": {"sql": "SELECT 1", "answer": "One.", "follow_ups": "And two?"}}'
    client = OpenAICompatClient("https://x/v1", "K", "m", http=http(Recorder(rejected(raw))))
    call = client.complete("S", HISTORY[:1], TOOLS).tool_calls[0]
    assert call.name == "submit_answer" and call.arguments["answer"] == "One."


def test_prefixed_names_and_argument_aliases_are_normalized() -> None:
    assert repair_tool_name(ToolCall("a", "functions.run_sql", {"query": "SELECT 1"}), TOOLS) == ToolCall(
        "a", "run_sql", {"sql": "SELECT 1"}
    )
    fixed = repair_tool_name(ToolCall("b", "submit_answer", {"sql_query": "SELECT 1", "final_answer": "x"}), TOOLS)
    assert fixed.arguments == {"sql": "SELECT 1", "answer": "x"}
    unchanged = ToolCall("c", "run_sql", {"sql": "SELECT 1"})
    assert repair_tool_name(unchanged, TOOLS) is unchanged


def test_retry_waits_are_reported_to_a_listener() -> None:
    from sqlagent.llm import HttpJson, report_waits

    replies = [(429, {"retry-after": "3"}, b"{}"), (200, {}, b'{"ok": true}')]
    http = HttpJson(transport=lambda *a: replies.pop(0), sleep=lambda s: None)
    heard: list[tuple[float, int]] = []
    with report_waits(lambda seconds, status: heard.append((seconds, status))):
        assert http.post("u", {}, {}) == {"ok": True}
    assert heard == [(3.0, 429)]
