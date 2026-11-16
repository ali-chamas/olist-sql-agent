# Design notes

Why the project is built the way it is, and what each choice costs.

## 1. The schema goes in the prompt, not behind tools

A common agent design gives the model `list_tables` and `describe_table` tools. Here the whole schema (five tables, about 1,200 tokens) is in the system prompt.

- **Gain:** two or three fewer model calls per question. On a free tier with a tokens-per-minute cap, that is the difference between two questions a minute and one.
- **Cost:** it does not scale. Past a few dozen tables the prompt gets expensive and accuracy drops. The next step would be retrieving only the relevant tables per question.

## 2. Business definitions are part of the prompt

Column names do not say that `sales` has one row per order item, or that `review_score` is an order-level value repeated on every item row. Without that, a model computes an average review of 4.02 (per item) when the per-order answer is 4.09. The prompt states these rules, and the `order-level` eval category exists to catch regressions on them.

- **Cost:** the rules live in a prompt string. In a real warehouse they belong in a semantic layer or in views.

## 3. SQLite, and guardrails enforced by the database

SQLite needs no server, so anyone can clone and run the project in a minute. It also has two features that make strong guardrails cheap:

- `mode=ro` opens the file read-only.
- An authorizer callback is consulted while each statement is compiled, and can deny any table, function or statement type.

The text check ("starts with SELECT") only exists to give the model a readable error. Security does not depend on parsing SQL with regular expressions, which is where most text-to-SQL demos go wrong.

- **Cost:** SQLite's dialect differs from production warehouses. On PostgreSQL the same idea would be a read-only role, `statement_timeout`, and a row limit applied by the server.

## 4. Three tools, and `submit_answer` re-runs the SQL

The model could answer in free text after seeing query results. Instead it must call `submit_answer(sql, answer)`, and the agent executes that SQL again.

- **Gain:** the rows shown to the user always come from the SQL shown to the user. The model cannot display one query and describe another.
- **Gain:** the eval has an unambiguous final query to score.
- **Cost:** one extra query execution per question (milliseconds here).

`cannot_answer` gives the model an explicit way out. Without it, a model asked for "profit margin" tends to return revenue and call it profit.

## 5. No vendor SDK

`llm.py` defines a neutral message format and two adapters of about 80 lines each over `urllib`.

- **Gain:** the same agent runs on Groq, Gemini, OpenRouter, Ollama or Claude by changing two environment variables. That is what makes the project free to run.
- **Gain:** the adapters are tested against recorded request and response shapes with a fake transport, including retries on HTTP 429.
- **Cost:** no streaming, and provider-specific features (prompt caching, structured outputs) are not used.

## 6. Evaluation by execution accuracy

Comparing SQL text would fail every correct query that is written differently from the gold one. The eval runs both queries and compares results.

Choices inside the comparison:

- **Extra columns are allowed.** A model that returns `category, revenue, items` for a question about revenue by category is right. Each gold column must map to a distinct predicted column.
- **Row pairing is checked.** Matching each column as a set is not enough: `("SP", 5), ("RJ", 10)` must not pass for `("SP", 10), ("RJ", 5)`.
- **Numeric tolerance of 0.005.** The gold SQL never rounds, so "rounded to 2 decimals" answers match. The cost is that percentages must be on a 0 to 100 scale, which the prompt states; a ratio such as 0.068 is correctly marked wrong.

Known weakness: a wrong query can return the right rows by chance, mostly on single-number answers.

## 7. The eval runner assumes it will be interrupted

Free tiers cap tokens per minute and per day. The runner therefore:

- paces itself with a sliding one-minute token window (`--tpm`),
- relies on the HTTP layer to retry 429 and 5xx responses with the server's `Retry-After`,
- appends each finished case to a JSON-lines file and skips finished cases on the next run. Cases that failed with a provider error are retried.

## 8. An oracle run tests the harness

`python -m sqlagent eval --oracle` uses a fake model that submits the gold SQL. It must score 50/50. This checks that every gold query passes the guardrails and that the comparison accepts identical results, with no API key. CI runs the same check on a small fixture database.

## 9. The server chooses the chart, with rules

The model could be asked for a chart specification. Instead `presentation.py` looks at the shape of the result: one row of numbers becomes stat tiles, a value over four or more periods becomes a line, a value across 2 to 25 categories becomes ranked bars, anything else stays a table.

- **Gain:** no extra tokens, no chart that names a column the result does not have, and the rules are unit-tested.
- **Gain:** the same function serves the agent's answers, the SQL editor and the dashboards.
- **Cost:** the rules only know a few forms. A question that deserves a stacked bar or a scatter plot gets a table.

Series with different units never share an axis: `revenue` and `orders` in one result plot revenue only, and both stay in the table.

## 10. Dashboards do not call the model

Three kinds of figure exist, and only one costs tokens:

| Figure | Where its SQL comes from | Model calls on load |
| --- | --- | --- |
| An answer | written by the agent | 2 to 3, or 0 from the cache |
| A pinned tile | the SQL saved when it was pinned | 0 |
| An overview tile | fixed in `dashboards.py` | 0 |

Pinned tiles rerun through `/api/query`, which applies the same four guardrail layers as the agent. That endpoint accepts SQL from the browser, which is acceptable here because the database is public and read-only; with private data it would need authentication and per-user row limits.

The overview's year filter shows the safe way to take user input into SQL when a bound parameter is not enough: the value is checked against a whitelist and mapped to a constant clause.

## 11. Surviving as a public demo on a free key

A hosted demo with a free API key has a fixed daily token allowance and anonymous visitors. Three in-memory mechanisms in `limits.py` protect it, checked in this order:

1. **Answer cache** keyed by model, normalised question and conversation. A repeated question costs nothing and does not count against the rate limit.
2. **Rate limit** per client address, separate for questions and for plain queries.
3. **Daily token budget.** When it is spent, questions get a clear message and the dashboards keep working.

- **Cost:** all three reset on restart and assume one process. With several instances they would move to Redis.

## 12. Steps are streamed with server-sent events

`/api/ask/stream` sends one event per agent step and a final result event. The agent runs in a worker thread and hands events to the response through an asyncio queue.

- **Gain:** a three-call answer takes several seconds on a free tier; seeing "query failed, retrying" is better than a spinner, and it makes the self-correction visible.
- **Cost:** the stream cannot be cached by a proxy, and a disconnected client does not stop the worker thread, so the question still spends its tokens.

A plain `/api/ask` endpoint returns the same payload in one response for scripts and tests.

## 13. The frontend has no build step

The web app is hand-written ES modules, SVG and CSS, served as static files. There is no framework and no bundler.

- **Gain:** one Python service to deploy, a strict Content-Security-Policy with no inline code, and nothing to install to change the UI.
- **Gain:** pure helpers (number formatting, axis ticks, the event-stream parser, CSV export) are tested with `node --test`.
- **Cost:** no component model and no type checking. Past roughly this size, a React and TypeScript rewrite would pay for itself.

All text from the model or the database reaches the page through `textContent`, never `innerHTML`.

## 14. Data assumptions are checked, not trusted

The prompt tells the model that order-level values are identical on every item row of an order. `python -m sqlagent check-data` turns that and eleven other assumptions into queries. The Docker build runs it, so an image with a broken database does not ship.

## 15. Malformed tool calls are repaired or retried, not fatal

Found in the first live run: `gpt-oss-120b` on Groq sometimes emits the right arguments under the wrong tool name (`commentary`, its own output channel). Groq validates tool calls and answers HTTP 400 with code `tool_use_failed` and the raw output in `failed_generation`.

- The three tools have non-overlapping arguments, so the intended tool can be identified from the arguments alone: `{sql, answer}` can only be `submit_answer`. A wrong name is corrected, in rejected calls and in normal responses.
- If the output cannot be repaired, the adapter returns an empty turn. The agent then reminds the model of its tools and tries again, within the same step limit.
- Every other HTTP 400 stays fatal.

Two more failure modes found by a simulator that validates requests the way the real API does:

- **Raw "harmony" output.** gpt-oss sometimes leaks its internal format (`<|channel|>commentary to=functions.run_sql ... {"sql": ...}`). The tool name and the JSON arguments are extracted from that text.
- **Empty turns.** When a reasoning model spends its whole completion budget thinking, it returns no text and no tool call. Sending that turn back as-is is a 400 on OpenAI-style APIs, so it is replaced by a placeholder and the model is reminded of its tools. The completion budget is also 4,096 tokens, not 1,024, because hidden reasoning counts against it.

The repaired SQL still goes through the guardrails like any other query. The cost is a rule tied to one provider's error format; the test suite pins that format.

## 16. Saved dashboards are data the AI edits through tools

The dashboard agent never writes to a database. It gets the dashboard as JSON and a small set of tools (`add_tile`, `update_tile`, `move_tile`, `remove_tile`, `update_dashboard`, `run_sql` to explore, `finish`, `cannot_do`) that change a draft in memory.

- **Every tile is checked when it is added.** Its SQL runs through the read-only guardrails, and the chart it asks for must fit the result (a line needs a time column, a stat tile needs one row). A failure goes back to the model as the tool result, so it fixes the query instead of saving a broken tile.
- **Every tool result ends with the current tile list**, so the model works from the real state, not its memory of it.
- **One request, one version.** The draft is saved once, at the end, with a summary built from the changes ("Added 2 tiles, Removed “Payment mix”"). Undo restores the previous version. A request that changes nothing saves nothing.
- **Separate databases.** Dashboards live in `app.db`, which is writable; the business data stays read-only. The agent's SQL can never reach `app.db`.
- **Optimistic concurrency.** Each save carries the version it started from. If someone else saved first, the server answers 409 and the page reloads the dashboard rather than overwriting their change.
- **Full snapshots in history.** Each version stores the whole dashboard as JSON. Dashboards are small (at most 24 tiles), so restoring is a copy, not a replay.

Opening a dashboard reruns each tile's SQL through the guardrails and costs no model tokens. The trade-off: no accounts, so anyone with the URL can edit; history makes every change reversible.

## 17. Filters shadow the tables instead of rewriting each query

A dashboard filter has to apply to SQL the model wrote, in any shape: joins, subqueries, its own `WITH` clauses. Parsing and rewriting that SQL would be fragile. Instead the filter puts common table expressions with the same names as the tables in front of the query:

```sql
WITH sales AS (SELECT * FROM main.sales WHERE order_date >= '2018-01-01' AND order_date < '2018-09-01'
                 AND customer_key IN (SELECT customer_key FROM main.customers WHERE state = 'SP')),
     customers AS (SELECT * FROM main.customers WHERE state = 'SP')
SELECT ... the tile's SQL, unchanged ...
```

SQLite resolves an unqualified name to a CTE before a table, and `main.sales` always means the real table, so every reference in the tile reads the filtered rows. A tile that already has its own `WITH` gets the filter CTEs merged into the same clause.

- **Values never arrive as text.** A month must match `YYYY-MM`; a state or category must be one of the values read from the data at start-up. Only then is it written as a quoted literal. The executor takes no parameters, so this check is what makes the literal safe, and the tests try the usual injections.
- **The guardrails still apply**: the shadowing CTEs are part of one read-only `SELECT`, compiled under the same authorizer.
- **The tile's own conditions still apply.** A tile fixed to 2017 shows nothing when the dashboard is filtered to 2018, which is the correct answer and is shown as "No data for these filters", not as an error.
- The trade-off: the filters know this schema's three dimensions. A different database needs its own definitions in `filters.py`.

Filters live in the address (`#/d/<id>?state=SP`), so a filtered view can be shared. Saved filters are the dashboard's default and are versioned like any other change. Clicking a bar of states or categories sets that filter, which gives cross-filtering without any per-chart configuration.

## 18. A model that answers in prose, or not at all, is a normal case

Free-tier models fail in ways that are not errors: an empty turn after spending the whole completion budget on reasoning, a tool call the provider rejects, or a closing message written as prose instead of a `finish` call. Each one used to cost a model call and leave a spinner on the page.

- A **prose reply after changes** is taken as the closing summary. Before any change it gets one reminder, then it is taken as the answer (often an explanation of why the request cannot be done).
- An **empty or rejected turn** gets a reminder. Three in a row stop the run: changes already made are kept; otherwise the person is told the model failed.
- The page shows **one** pending step at a time, and relabels it ("The model's reply was not a valid change; asking again") instead of adding another spinner.
- **Rate-limit waits are visible.** The HTTP client reports each backoff through a context variable (`report_waits`), so the agent can stream "waiting 12 s" without the client knowing about agents or pages.

## 19. The dashboard is drawn while the agent works

The agent already runs every tile's SQL to validate it, so it costs nothing to send the result along. Each successful change emits the draft's layout and, for a new or changed tile, the rows and chart. The page draws the draft live: on the dashboards page while building, and in place while editing. Unchanged tiles are left in the DOM, not re-inserted, so their charts do not replay their entrance animation; the newest tile is marked with the highlighter. If the run fails or is stopped, the saved dashboard is drawn again.

This changes how the wait feels: a 20-second run on a free model becomes a dashboard assembling itself, instead of a spinner.

## 20. Tile results are cached; the workspace can be locked

The business data is read-only, so a tile's result depends only on its SQL, its chart type and the filters. Those three are the cache key, and entries never go stale. Opening a dashboard twice, or switching back to a filter already seen, does not touch the database.

`APP_PASSWORD` turns on HTTP Basic authentication for every page and API call, compared in constant time, with the health check left open for the hosting platform. It is deliberately the smallest thing that makes a private deployment private; accounts and per-dashboard permissions are a different product.

## 21. The frontend is a product shell, still without a build step

The sidebar lists the saved dashboards; Ctrl+K (Cmd+K on a Mac) opens a command palette that jumps to any dashboard or page, or turns what was typed into a question or a new dashboard. Dashboard cards draw a thumbnail of their layout from the tiles' chart types. All of it is plain ES modules, so the page still loads with no bundler, and the Content-Security-Policy still allows only same-origin scripts.

## Next steps

1. Record the score for two or three models and track it per commit.
2. Add multi-turn cases to the eval; follow-up questions are not scored yet.
3. Add retrieval of similar solved questions as few-shot examples, and measure the gain.
4. Move to PostgreSQL with a read-only role, and compare dialect errors; this is also the path to connecting a team's own database, with filter definitions read from a config file.
5. Record the dashboard eval for two or three models, and add cases written by marketing people.
6. Add questions written by other people to reduce author bias in the eval.
