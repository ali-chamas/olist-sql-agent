from __future__ import annotations

from conftest import FakeLLM, asgi_request, call, parse_sse
from sqlagent.agent import SQLAgent
from sqlagent.app import AppConfig, clean_history, create_app
from sqlagent.db import ReadOnlyDatabase
from sqlagent.llm import LLMError

SQL = "SELECT COUNT(DISTINCT order_id) AS orders FROM sales"
ASK = {"question": "How many orders?"}


def make_app(db: ReadOnlyDatabase, *script, **config):
    llm = FakeLLM(list(script))
    return create_app(SQLAgent(llm, db), AppConfig(**config)), llm


def test_ask_returns_answer_sql_rows_chart_and_trace(db: ReadOnlyDatabase) -> None:
    answer = call("submit_answer", sql=SQL, answer="5 orders.", follow_ups=["And revenue?", "By year?"])
    app, _ = make_app(db, answer)
    status, body = asgi_request(app, "POST", "/api/ask", ASK)
    assert status == 200
    assert body["status"] == "answered" and body["answer"] == "5 orders."
    assert body["sql"] == SQL and body["columns"] == ["orders"] and body["rows"] == [[5]]
    assert body["chart"]["type"] == "kpi" and body["chart"]["items"][0]["value"] == 5
    assert body["follow_ups"] == ["And revenue?", "By year?"]
    assert body["steps"][0]["tool"] == "submit_answer" and body["cached"] is False


def test_ask_validates_input(db: ReadOnlyDatabase) -> None:
    app, _ = make_app(db)
    assert asgi_request(app, "POST", "/api/ask", {"question": "  "})[0] == 422
    assert asgi_request(app, "POST", "/api/ask", {"question": "x" * 501})[0] == 422
    assert asgi_request(app, "POST", "/api/ask", {"q": "missing"})[0] == 422
    assert asgi_request(app, "POST", "/api/ask", ["not", "an", "object"])[0] == 422
    assert asgi_request(app, "POST", "/api/ask", b"{not json")[0] == 400
    assert asgi_request(app, "POST", "/api/ask", b'{"question": "' + b"x" * 40_000 + b'"}')[0] == 413
    assert asgi_request(app, "GET", "/api/ask")[0] == 405


def test_provider_failure_maps_to_502_and_is_not_cached(db: ReadOnlyDatabase) -> None:
    app, llm = make_app(db, LLMError("HTTP 500"), call("cannot_answer", reason="no"))
    status, body = asgi_request(app, "POST", "/api/ask", ASK)
    assert status == 502 and body["status"] == "llm_error"
    assert asgi_request(app, "POST", "/api/ask", ASK)[1]["status"] == "cannot_answer"
    assert len(llm.calls) == 2


def test_repeated_question_is_served_from_the_cache_without_a_model_call(db: ReadOnlyDatabase) -> None:
    app, llm = make_app(db, call("submit_answer", sql=SQL, answer="5 orders."))
    first = asgi_request(app, "POST", "/api/ask", ASK)[1]
    again = asgi_request(app, "POST", "/api/ask", {"question": "  how many ORDERS  ?"})[1]
    assert first["cached"] is False and again["cached"] is True and again["rows"] == [[5]]
    assert len(llm.calls) == 1
    assert asgi_request(app, "GET", "/api/status")[1]["cache"] == {"hits": 1, "misses": 1}


def test_history_reaches_the_model_and_separates_cache_entries(db: ReadOnlyDatabase) -> None:
    answer = call("submit_answer", sql=SQL, answer="5.")
    app, llm = make_app(db, answer, answer)
    history = [{"question": "Revenue in 2017?", "sql": "SELECT 1", "ignored": "x"}]
    asgi_request(app, "POST", "/api/ask", {"question": "And in 2018?", "history": history})
    asgi_request(app, "POST", "/api/ask", {"question": "And in 2018?"})
    assert len(llm.calls) == 2  # same question, different conversation: not a cache hit
    first_prompt = llm.calls[0][0].content
    assert "Revenue in 2017?" in first_prompt and "SELECT 1" in first_prompt and first_prompt.endswith("And in 2018?")
    assert llm.calls[1][0].content == "And in 2018?"


def test_clean_history_keeps_the_last_three_valid_turns() -> None:
    raw = [{"question": f"q{i}", "sql": "s"} for i in range(5)] + [{"sql": "no question"}, "junk"]
    assert [t["question"] for t in clean_history(raw)] == ["q4"]
    assert [t["question"] for t in clean_history(raw[:5])] == ["q2", "q3", "q4"]
    assert clean_history("nope") == [] and clean_history([{"question": "q", "sql": 5}]) == [
        {"question": "q", "sql": ""}
    ]


def test_rate_limit_is_per_client_and_cached_answers_do_not_count(db: ReadOnlyDatabase) -> None:
    refuse = call("cannot_answer", reason="no")
    app, _ = make_app(db, refuse, refuse, refuse, ask_per_minute=2)
    assert asgi_request(app, "POST", "/api/ask", {"question": "one?"}, client="1.1.1.1")[0] == 200
    assert asgi_request(app, "POST", "/api/ask", {"question": "two?"}, client="1.1.1.1")[0] == 200
    assert asgi_request(app, "POST", "/api/ask", {"question": "three?"}, client="1.1.1.1")[0] == 429
    assert asgi_request(app, "POST", "/api/ask", {"question": "one?"}, client="1.1.1.1")[0] == 200  # cached
    assert asgi_request(app, "POST", "/api/ask", {"question": "three?"}, client="2.2.2.2")[0] == 200


def test_forwarded_address_is_used_only_when_the_proxy_is_trusted(db: ReadOnlyDatabase) -> None:
    refuse = call("cannot_answer", reason="no")
    forwarded = {"x-forwarded-for": "9.9.9.9, 10.0.0.1"}
    trusted, _ = make_app(db, refuse, refuse, ask_per_minute=1, trust_proxy=True)
    assert asgi_request(trusted, "POST", "/api/ask", {"question": "one?"}, headers=forwarded)[0] == 200
    assert (
        asgi_request(trusted, "POST", "/api/ask", {"question": "two?"}, client="5.5.5.5", headers=forwarded)[0] == 429
    )
    untrusted, _ = make_app(db, refuse, refuse, ask_per_minute=1)
    assert asgi_request(untrusted, "POST", "/api/ask", {"question": "one?"}, headers=forwarded)[0] == 200
    assert (
        asgi_request(untrusted, "POST", "/api/ask", {"question": "two?"}, client="5.5.5.5", headers=forwarded)[0] == 200
    )


def test_daily_token_budget_stops_new_questions_but_not_cached_ones(db: ReadOnlyDatabase) -> None:
    answer = call("submit_answer", sql=SQL, answer="5.")  # each scripted call reports 120 tokens
    app, llm = make_app(db, answer, answer, daily_token_budget=100)
    assert asgi_request(app, "POST", "/api/ask", ASK)[0] == 200
    status, body = asgi_request(app, "POST", "/api/ask", {"question": "Something new?"})
    assert status == 429 and "quota" in body["error"]
    assert asgi_request(app, "POST", "/api/ask", ASK)[1]["cached"] is True
    assert asgi_request(app, "GET", "/api/status")[1]["budget"] == {"limit": 100, "used": 100}
    assert len(llm.calls) == 1


def test_stream_sends_each_step_then_the_result(db: ReadOnlyDatabase) -> None:
    app, _ = make_app(
        db,
        call("run_sql", sql="SELECT nope FROM sales"),
        call("run_sql", sql=SQL),
        call("submit_answer", sql=SQL, answer="5 orders."),
    )
    status, headers, payload = asgi_request(app, "POST", "/api/ask/stream", ASK, raw=True)
    assert status == 200 and headers["content-type"].startswith("text/event-stream")
    events = parse_sse(payload)
    assert [name for name, _ in events] == ["thinking", "step", "thinking", "step", "thinking", "step", "result"]
    assert events[1][1]["step"]["ok"] is False and events[3][1]["step"]["row_count"] == 1
    result = events[-1][1]
    assert result["status"] == "answered" and result["rows"] == [[5]] and result["chart"]["type"] == "kpi"

    # A second identical request is answered from the cache as a single event.
    cached = parse_sse(asgi_request(app, "POST", "/api/ask/stream", ASK, raw=True)[2])
    assert [name for name, _ in cached] == ["result"] and cached[0][1]["cached"] is True


def test_stream_rejects_bad_input_with_plain_json_errors(db: ReadOnlyDatabase) -> None:
    app, _ = make_app(db, ask_per_minute=0)
    assert asgi_request(app, "POST", "/api/ask/stream", {"question": ""})[0] == 422
    assert asgi_request(app, "POST", "/api/ask/stream", ASK)[0] == 429


def test_query_endpoint_runs_sql_through_the_guardrails(db: ReadOnlyDatabase) -> None:
    app, llm = make_app(db)
    status, body = asgi_request(
        app, "POST", "/api/query", {"sql": "SELECT state, COUNT(*) AS n FROM customers GROUP BY 1"}
    )
    assert status == 200 and body["columns"] == ["state", "n"]
    assert body["chart"] == {
        "type": "bar",
        "x": "state",
        "y": "n",
        "format": "number",
        "labels": ["SP", "RJ"],
        "values": [2, 1],
    }
    status, body = asgi_request(app, "POST", "/api/query", {"sql": "DELETE FROM sales"})
    assert status == 400 and body["kind"] == "rejected"
    assert asgi_request(app, "POST", "/api/query", {"sql": "SELECT * FROM sqlite_master"})[0] == 400
    assert asgi_request(app, "POST", "/api/query", {"sql": "SELECT nope"})[1]["kind"] == "sql_error"
    assert asgi_request(app, "POST", "/api/query", {"sql": ""})[0] == 422
    assert asgi_request(app, "POST", "/api/query", {"sql": "SELECT 1 -- " + "x" * 4000})[0] == 422
    assert llm.calls == []  # never touches the model


def test_query_endpoint_has_its_own_rate_limit(db: ReadOnlyDatabase) -> None:
    app, _ = make_app(db, query_per_minute=1)
    assert asgi_request(app, "POST", "/api/query", {"sql": "SELECT 1"})[0] == 200
    assert asgi_request(app, "POST", "/api/query", {"sql": "SELECT 1"})[0] == 429


def test_dashboard_endpoint_and_year_whitelist(db: ReadOnlyDatabase) -> None:
    app, llm = make_app(db)
    status, body = asgi_request(app, "GET", "/api/dashboard")
    assert status == 200 and body["year"] == "all" and body["years"] == ["all", "2016", "2017", "2018"]
    assert [t["id"] for t in body["tiles"]][:2] == ["kpis", "revenue_by_month"]
    status, filtered = asgi_request(app, "GET", "/api/dashboard?year=2017")
    assert status == 200 and filtered["year"] == "2017"
    assert asgi_request(app, "GET", "/api/dashboard?year=2017;DROP TABLE sales")[0] == 422
    assert asgi_request(app, "GET", "/api/dashboard?year=1999")[0] == 422
    assert llm.calls == []


def test_status_schema_static_files_and_security_headers(db: ReadOnlyDatabase) -> None:
    app, _ = make_app(db)
    status, body = asgi_request(app, "GET", "/api/status")
    assert status == 200 and body["model"] == "fake" and body["budget"] == {"limit": 0, "used": 0}
    status, schema = asgi_request(app, "GET", "/api/schema")
    assert [t["name"] for t in schema["tables"]] == ["sales", "customers", "products", "sellers", "dates"]
    assert len(schema["notes"]) >= 5
    assert schema["facts"]["orders"] == 5 and schema["facts"]["last_date"] == "2018-02-11"

    status, headers, html = asgi_request(app, "GET", "/", raw=True)
    assert status == 200 and b"Ask Olist" in html
    assert "script-src 'self'" in headers["content-security-policy"] and headers["x-content-type-options"] == "nosniff"
    assert b"<script>" not in html and b"style=" not in html  # the policy forbids inline code

    assert headers["cache-control"] == "no-cache"
    status, headers, _ = asgi_request(app, "GET", "/static/js/main.js", raw=True)
    assert status == 200 and "javascript" in headers["content-type"] and headers["cache-control"] == "no-cache"
    assert "cache-control" not in asgi_request(app, "GET", "/api/status", raw=True)[1]
    assert asgi_request(app, "GET", "/static/../config.py")[0] == 404


def test_password_gate_protects_everything_but_the_health_check(db) -> None:
    import base64

    from sqlagent.agent import SQLAgent
    from sqlagent.app import AppConfig, create_app

    app = create_app(SQLAgent(FakeLLM([]), db), AppConfig(password="s3cret"))
    status, _ = asgi_request(app, "GET", "/api/schema")
    assert status == 401
    assert asgi_request(app, "GET", "/api/health")[0] == 200
    good = {"authorization": "Basic " + base64.b64encode(b"marketing:s3cret").decode()}
    bad = {"authorization": "Basic " + base64.b64encode(b"marketing:nope").decode()}
    assert asgi_request(app, "GET", "/api/schema", headers=good)[0] == 200
    assert asgi_request(app, "GET", "/api/schema", headers=bad)[0] == 401
    assert asgi_request(app, "GET", "/api/schema", headers={"authorization": "Basic !!!"})[0] == 401
