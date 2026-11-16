"""Provider-agnostic chat-with-tools clients.

The agent talks to one small interface (``LLMClient.complete``). Two adapters
implement it over plain HTTPS, with no vendor SDK:

* ``OpenAICompatClient``: the /chat/completions format used by Groq, Google
  Gemini's OpenAI endpoint, OpenRouter, Ollama and OpenAI itself.
* ``AnthropicClient``: Claude's Messages API.

Conversation history is kept in a neutral format (see ``Message``) and each
adapter converts it to its provider's wire format on every call.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Protocol

# --------------------------------------------------------------------------
# Neutral types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema of the arguments


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    parse_error: str | None = None  # set when the model sent malformed arguments


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class Message:
    """One turn. role is 'user', 'assistant' or 'tool'."""

    role: str
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)  # assistant only
    tool_call_id: str = ""  # tool only
    name: str = ""  # tool only
    is_error: bool = False  # tool only


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    usage: Usage


class LLMError(Exception):
    """The provider could not be reached or returned an unusable response."""


class LLMHTTPError(LLMError):
    """A non-retryable HTTP error. Keeps the status and body so an adapter can inspect them."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Provider returned HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


class LLMClient(Protocol):
    model: str

    def complete(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse: ...


# --------------------------------------------------------------------------
# HTTP transport with retries
# --------------------------------------------------------------------------

Transport = Callable[[str, dict[str, str], dict[str, Any], float], tuple[int, dict[str, str], bytes]]


def urllib_transport(
    url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(  # noqa: S310 (URL comes from configuration)
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LLMError(f"Network error calling {url}: {exc}") from exc


RETRYABLE = frozenset({408, 409, 429, 500, 502, 503, 504, 529})


# Told about each backoff wait (seconds, HTTP status), so a live UI can say why nothing is happening.
WaitListener = Callable[[float, int], None]
_wait_listener: ContextVar[WaitListener | None] = ContextVar("wait_listener", default=None)


@contextmanager
def report_waits(listener: WaitListener) -> Iterator[None]:
    """Call ``listener`` before every retry wait made by code running inside this block."""
    token = _wait_listener.set(listener)
    try:
        yield
    finally:
        _wait_listener.reset(token)


@dataclass
class HttpJson:
    """POST JSON, retry rate limits and server errors with backoff."""

    transport: Transport = urllib_transport
    timeout: float = 60.0
    max_retries: int = 4
    max_wait: float = 65.0  # free tiers reset per minute; never wait much longer
    sleep: Callable[[float], None] = time.sleep

    def post(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        attempt = 0
        while True:
            status, response_headers, body = self.transport(url, headers, payload, self.timeout)
            if 200 <= status < 300:
                try:
                    return json.loads(body)
                except json.JSONDecodeError as exc:
                    raise LLMError(f"Provider returned non-JSON body (HTTP {status}).") from exc
            if status in RETRYABLE and attempt < self.max_retries:
                wait = self._wait_seconds(response_headers, attempt)
                listener = _wait_listener.get()
                if listener is not None:
                    listener(wait, status)
                self.sleep(wait)
                attempt += 1
                continue
            raise LLMHTTPError(status, body.decode("utf-8", errors="replace")[:20_000])

    def _wait_seconds(self, headers: dict[str, str], attempt: int) -> float:
        retry_after = headers.get("retry-after", "")
        try:
            wait = float(retry_after)
        except ValueError:
            wait = 2.0 * (2**attempt)
        return max(0.5, min(wait, self.max_wait))


# --------------------------------------------------------------------------
# OpenAI-compatible adapter
# --------------------------------------------------------------------------


def _parse_arguments(raw: Any) -> tuple[dict[str, Any], str | None]:
    if isinstance(raw, dict):
        return raw, None
    if raw is None or raw == "":
        return {}, None
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        return {}, f"Arguments were not valid JSON: {exc}"
    if not isinstance(parsed, dict):
        return {}, "Arguments must be a JSON object."
    return parsed, None


def match_tool_by_arguments(arguments: dict[str, Any], tools: list[ToolSpec]) -> str | None:
    """The one tool whose required arguments are all present and that accepts every argument given."""
    keys = set(arguments)
    matches = [
        t.name
        for t in tools
        if set(t.parameters.get("required", [])) <= keys <= set(t.parameters.get("properties", {}))
    ]
    return matches[0] if len(matches) == 1 else None


# Argument names models sometimes use instead of the schema's.
ARGUMENT_ALIASES = {"query": "sql", "sql_query": "sql", "final_answer": "answer", "followups": "follow_ups"}


def normalize_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    fixed = dict(arguments)
    for alias, name in ARGUMENT_ALIASES.items():
        if alias in fixed and name not in fixed:
            fixed[name] = fixed.pop(alias)
    return fixed


def known_tool_in(name: str, tools: list[ToolSpec]) -> str | None:
    """'functions.run_sql', 'run_sql<|channel|>commentary' and similar spellings of a real tool name."""
    names = {t.name for t in tools}
    if name in names:
        return name
    found = [n for n in names if re.search(rf"(?<![A-Za-z0-9_]){re.escape(n)}(?![A-Za-z0-9_])", name)]
    return found[0] if len(found) == 1 else None


def repair_tool_name(call: ToolCall, tools: list[ToolSpec]) -> ToolCall:
    """Fix a call to a tool that does not exist when its arguments identify the tool it meant.

    Some open models occasionally emit the right arguments under a wrong name (gpt-oss
    writes its "commentary" channel name instead of the function). The arguments of
    the three tools here do not overlap, so the intended tool is unambiguous.
    """
    if not tools or call.parse_error:
        return call
    arguments = normalize_arguments(call.arguments)
    intended = known_tool_in(call.name, tools) or match_tool_by_arguments(arguments, tools)
    if intended == call.name and arguments == call.arguments:
        return call
    return ToolCall(id=call.id, name=intended or call.name, arguments=arguments)


INVALID_CALL_TEXT = "(My previous tool call was invalid.)"


def recover_rejected_tool_call(exc: LLMHTTPError, tools: list[ToolSpec]) -> LLMResponse | None:
    """Turn a provider-side "tool call validation failed" error into something the agent can continue from.

    Groq validates tool calls and answers HTTP 400 with code ``tool_use_failed`` and the
    model's raw output in ``failed_generation``. If that output names the wrong tool but
    carries usable arguments, the call is repaired. Otherwise an empty turn is returned,
    which makes the agent remind the model of the tools and try again. Any other error
    returns None and stays fatal.
    """
    if exc.status != 400:
        return None
    try:
        error = json.loads(exc.body).get("error") or {}
    except (json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(error, dict) or error.get("code") != "tool_use_failed":
        return None

    call = parse_failed_generation(error.get("failed_generation"), tools)
    if call is not None:
        return LLMResponse(text="", tool_calls=[call], usage=Usage())
    return LLMResponse(text=INVALID_CALL_TEXT, tool_calls=[], usage=Usage())


def _first_json_object(text: str) -> dict[str, Any] | None:
    """The first complete JSON object in free text, if any."""
    decoder = json.JSONDecoder()
    for start in (i for i, ch in enumerate(text) if ch == "{"):
        try:
            value, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def parse_failed_generation(raw: Any, tools: list[ToolSpec]) -> ToolCall | None:
    """Recover a usable tool call from the raw text of a rejected generation.

    Seen in practice:
      {"name": "commentary", "arguments": {...}}              wrong name, right arguments
      {"name": "submit_answer", "arguments": {...}}           arguments that failed the schema
      <|channel|>commentary to=functions.run_sql ... {"sql": ...}   raw gpt-oss "harmony" output
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    obj = _first_json_object(raw)
    if obj is None:
        return None
    if "arguments" in obj or "parameters" in obj:
        arguments, problem = _parse_arguments(obj.get("arguments", obj.get("parameters")))
        name = str(obj.get("name") or "")
    else:
        arguments, problem = obj, None  # the object is the arguments themselves
        name = raw[: raw.find("{")]
    if problem or not arguments:
        return None
    call_id = f"recovered_{time.monotonic_ns()}"  # unique: one conversation can recover several times
    call = repair_tool_name(ToolCall(id=call_id, name=name, arguments=arguments), tools)
    return call if call.name in {t.name for t in tools} else None


class OpenAICompatClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        http: HttpJson | None = None,
        max_tokens: int = 4096,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.http = http or HttpJson()
        self.max_tokens = max_tokens

    def _wire_messages(self, system: str, messages: list[Message]) -> list[dict[str, Any]]:
        wire: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            if m.role == "assistant":
                item: dict[str, Any] = {"role": "assistant", "content": m.content or None}
                if m.tool_calls:
                    item["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                        }
                        for tc in m.tool_calls
                    ]
                wire.append(item)
            elif m.role == "tool":
                wire.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
            else:
                wire.append({"role": "user", "content": m.content})
        return wire

    def complete(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._wire_messages(system, messages),
            "temperature": 0,
            "max_tokens": self.max_tokens,
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
                }
                for t in tools
            ]
            payload["tool_choice"] = "auto"
        headers = {"Content-Type": "application/json", "User-Agent": "olist-sql-agent/0.1"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            data = self.http.post(f"{self.base_url}/chat/completions", headers, payload)
        except LLMHTTPError as exc:
            recovered = recover_rejected_tool_call(exc, tools)
            if recovered is None:
                raise
            return recovered

        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unexpected response shape: {str(data)[:200]}") from exc
        calls = []
        for i, raw in enumerate(message.get("tool_calls") or []):
            function = raw.get("function") or {}
            arguments, error = _parse_arguments(function.get("arguments"))
            call = ToolCall(
                id=raw.get("id") or f"call_{i}",
                name=function.get("name") or "",
                arguments=arguments,
                parse_error=error,
            )
            calls.append(repair_tool_name(call, tools))
        usage = data.get("usage") or {}
        return LLMResponse(
            text=message.get("content") or "",
            tool_calls=calls,
            usage=Usage(int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)),
        )


# --------------------------------------------------------------------------
# Anthropic (Claude) adapter
# --------------------------------------------------------------------------


class AnthropicClient:
    API_VERSION = "2023-06-01"

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "https://api.anthropic.com",
        http: HttpJson | None = None,
        max_tokens: int = 4096,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.http = http or HttpJson()
        self.max_tokens = max_tokens

    def _wire_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        wire: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "assistant":
                blocks: list[dict[str, Any]] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                blocks.extend(
                    {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments} for tc in m.tool_calls
                )
                wire.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "(no content)"}]})
            elif m.role == "tool":
                block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
                if m.is_error:
                    block["is_error"] = True
                # Claude expects all results of one assistant turn in a single user message.
                if wire and wire[-1]["role"] == "user" and isinstance(wire[-1]["content"], list):
                    wire[-1]["content"].append(block)
                else:
                    wire.append({"role": "user", "content": [block]})
            else:
                wire.append({"role": "user", "content": m.content})
        return wire

    def complete(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": self._wire_messages(messages),
        }
        if tools:
            payload["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools
            ]
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": self.API_VERSION,
            "User-Agent": "olist-sql-agent/0.1",
        }
        data = self.http.post(f"{self.base_url}/v1/messages", headers, payload)

        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise LLMError(f"Unexpected response shape: {str(data)[:200]}")
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in blocks:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                arguments, error = _parse_arguments(block.get("input"))
                call = ToolCall(
                    id=block.get("id", ""), name=block.get("name", ""), arguments=arguments, parse_error=error
                )
                calls.append(repair_tool_name(call, tools))
        usage = data.get("usage") or {}
        return LLMResponse(
            text="".join(text_parts),
            tool_calls=calls,
            usage=Usage(int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)),
        )
