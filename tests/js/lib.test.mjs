// Run with: node --test tests/js
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  createSSEParser, describeStep, formatValue, humanize, niceTicks, pickIndexes, shorten, toCSV,
} from "../../src/sqlagent/static/js/lib.js";

test("formatValue abbreviates large numbers and keeps small ones exact", () => {
  assert.equal(formatValue(98666), "98.7K");
  assert.equal(formatValue(9999), "9,999");
  assert.equal(formatValue(160.5776), "160.58");
  assert.equal(formatValue(15843553.24, "currency"), "R$ 15.84M");
  assert.equal(formatValue(1441248.07, "currency"), "R$ 1.44M");
  assert.equal(formatValue(2000000), "2M");
  assert.equal(formatValue(160.5776, "currency"), "R$ 160.58");
  assert.equal(formatValue(6.7737, "percent"), "6.77%");
  assert.equal(formatValue(50, "percent"), "50%");
  assert.equal(formatValue(-12500), "-12.5K");
});

test("formatValue exact mode never abbreviates, and passes through non-numbers", () => {
  assert.equal(formatValue(15843553.24, "currency", true), "R$ 15,843,553.24");
  assert.equal(formatValue(98666, "number", true), "98,666");
  assert.equal(formatValue(null), "n/a");
  assert.equal(formatValue("SP"), "SP");
});

test("niceTicks starts at zero, covers the maximum and uses round steps", () => {
  assert.deepEqual(niceTicks(1179143), [0, 500000, 1000000, 1500000]);
  assert.deepEqual(niceTicks(4.6), [0, 2, 4, 6]);
  assert.deepEqual(niceTicks(100), [0, 25, 50, 75, 100]);
  assert.deepEqual(niceTicks(0.9), [0, 0.25, 0.5, 0.75, 1]);
  assert.deepEqual(niceTicks(0), [0, 1]);
  for (const max of [3, 17, 99, 1234, 98765.4]) {
    const ticks = niceTicks(max);
    assert.equal(ticks[0], 0);
    assert.ok(ticks.at(-1) >= max, `${ticks} covers ${max}`);
    assert.ok(ticks.length <= 7);
  }
});

test("pickIndexes keeps both ends and spreads the rest", () => {
  assert.deepEqual(pickIndexes(4, 6), [0, 1, 2, 3]);
  assert.deepEqual(pickIndexes(12, 4), [0, 4, 11]);
  assert.deepEqual(pickIndexes(12, 6), [0, 3, 6, 11]);
  assert.deepEqual(pickIndexes(24, 6), [0, 5, 10, 15, 23]);
  assert.deepEqual(pickIndexes(13, 4), [0, 4, 8, 12]);
  assert.deepEqual(pickIndexes(12, 7), [0, 2, 4, 6, 8, 11]); // 10 would crowd the last label
  assert.deepEqual(pickIndexes(25, 2), [0, 24]);
  assert.deepEqual(pickIndexes(25, 1), [0, 24]);
});

test("humanize and shorten", () => {
  assert.equal(humanize("average_order_value"), "Average order value");
  assert.equal(humanize(""), "");
  assert.equal(shorten("4869f7a5dfa277a7dca6462dcf3b52b2", 12), "4869f7a5dfa…");
  assert.equal(shorten("short", 12), "short");
});

test("toCSV quotes commas, quotes and newlines", () => {
  const csv = toCSV(["city", "note", "n"], [["Sao Paulo, SP", 'say "hi"', 3], [null, "a\nb", 1.5]]);
  assert.equal(csv, 'city,note,n\r\n"Sao Paulo, SP","say ""hi""",3\r\n,"a\nb",1.5\r\n');
});

test("SSE parser handles events split across chunks", () => {
  const push = createSSEParser();
  assert.deepEqual(push('event: thinking\ndata: {"call"'), []);
  assert.deepEqual(push(': 1}\n\nevent: step\ndata: {"ok": true}\n\nevent: res'), [
    { event: "thinking", data: { call: 1 } },
    { event: "step", data: { ok: true } },
  ]);
  assert.deepEqual(push('ult\r\ndata: {"status": "answered"}\r\n\r\n'), [{ event: "result", data: { status: "answered" } }]);
});

test("SSE parser skips malformed events and keeps going", () => {
  const push = createSSEParser();
  assert.deepEqual(push("event: step\ndata: {oops\n\nevent: step\ndata: {}\n\n"), [{ event: "step", data: {} }]);
});

test("describeStep reads like a sentence", () => {
  assert.equal(describeStep({ tool: "run_sql", ok: true, row_count: 5, elapsed_ms: 189.6 }), "Tested a query: 5 rows in 190 ms");
  assert.equal(describeStep({ tool: "submit_answer", ok: true, row_count: 1, elapsed_ms: 3 }), "Submitted the final query: 1 row in 3 ms");
  assert.equal(describeStep({ tool: "run_sql", ok: false }), "Query failed");
  assert.equal(describeStep({ tool: "cannot_answer", ok: true }), "Decided the data cannot do this");
});

import { chartTitle, describeDelta, formatElapsed, formatPeriod, splitHighlights } from "../../src/sqlagent/static/js/lib.js";

test("humanize spells out abbreviations", () => {
  assert.equal(humanize("repeat_customer_pct"), "Repeat customer (%)");
  assert.equal(humanize("avg_freight"), "Average freight");
  assert.equal(humanize("seller_id"), "Seller ID");
  assert.equal(humanize("pct"), "%");
});

test("chartTitle names what is plotted", () => {
  assert.equal(chartTitle({ type: "bar", x: "category", y: "revenue" }), "Revenue by category");
  assert.equal(chartTitle({ type: "line", x: "month", series: [{ name: "revenue" }, { name: "freight" }] }), "Revenue and freight by month");
  assert.equal(chartTitle({ type: "kpi", items: [] }), "");
  assert.equal(chartTitle(null), "");
});

test("formatPeriod and formatElapsed", () => {
  assert.equal(formatPeriod("2016-09-04", "2018-09-03"), "Sep 2016 to Sep 2018");
  assert.equal(formatElapsed(2140), "2.1 s");
});

test("describeDelta uses points for rates and colours by whether up is good", () => {
  assert.deepEqual(describeDelta(8643697.6, 3610270.15, "currency"), { text: "+139.4%", direction: "up", tone: "good" });
  assert.deepEqual(describeDelta(4.08, 4.15), { text: "−1.7%", direction: "down", tone: "bad" });
  assert.deepEqual(describeDelta(7.73, 3.5, "percent", false), { text: "+4.2 pts", direction: "up", tone: "bad" });
  assert.deepEqual(describeDelta(5, 5), { text: "No change", direction: "flat", tone: "flat" });
  assert.equal(describeDelta(5, 0), null);
  assert.equal(describeDelta(null, 3), null);
});

test("splitHighlights marks amounts, percentages and formatted numbers only", () => {
  const marked = (text) => splitHighlights(text).filter((p) => p.mark).map((p) => p.text);
  assert.deepEqual(marked("Health Beauty leads with R$ 1.44M, then R$ 137K in 2017."), ["R$ 1.44M", "R$ 137K"]);
  assert.deepEqual(marked("3.12% of customers, 98,666 orders, average 4.09, top 5 in 2018."), ["3.12%", "98,666", "4.09"]);
  assert.deepEqual(marked("Revenue was R$ 15,843,553.24."), ["R$ 15,843,553.24"]);
  assert.equal(splitHighlights("No figures here").length, 1);
  assert.equal(splitHighlights("a 1.5 b").map((p) => p.text).join(""), "a 1.5 b");
});

import { parseRoute, timeAgo } from "../../src/sqlagent/static/js/lib.js";

test("describeStep covers dashboard edits", () => {
  assert.equal(describeStep({ tool: "add_tile", ok: true, detail: "Added “Revenue”" }), "Added “Revenue”");
  assert.equal(describeStep({ tool: "add_tile", ok: false, detail: "Revenue" }), "Could not add “Revenue”");
  assert.equal(describeStep({ tool: "update_dashboard", ok: false }), "Could not rename the dashboard");
  assert.equal(describeStep({ tool: "remove_tile", ok: false, detail: "" }), "Could not remove a tile");
  assert.equal(describeStep({ tool: "finish", ok: true }), "Finished");
  assert.equal(describeStep({ tool: "cannot_do", ok: true }), "Decided the data cannot do this");
});

test("timeAgo reads naturally", () => {
  const now = Date.parse("2026-11-16T18:00:00Z");
  assert.equal(timeAgo("2026-11-16T17:59:50+00:00", now), "just now");
  assert.equal(timeAgo("2026-11-16T17:59:00+00:00", now), "1 minute ago");
  assert.equal(timeAgo("2026-11-16T17:25:00+00:00", now), "35 minutes ago");
  assert.equal(timeAgo("2026-11-16T15:00:00+00:00", now), "3 hours ago");
  assert.equal(timeAgo("2026-11-15T16:00:00+00:00", now), "yesterday");
  assert.equal(timeAgo("2026-11-12T18:00:00+00:00", now), "4 days ago");
  assert.equal(timeAgo("2026-10-01T18:00:00+00:00", now), "1 Oct 2026");
  assert.equal(timeAgo("nonsense", now), "");
});

test("parseRoute splits hashes", () => {
  assert.deepEqual(parseRoute("#/d/d_12ab"), ["d", "d_12ab"]);
  assert.deepEqual(parseRoute("#/ask"), ["ask"]);
  assert.deepEqual(parseRoute(""), []);
  assert.deepEqual(parseRoute("#/d/a%20b"), ["d", "a b"]);
});

test("layout stretches a half-width tile that would sit alone", async () => {
  // workspace.js imports browser-only modules, so stub the DOM globals it touches at import time.
  globalThis.document ??= { getElementById: () => null };
  const { layout } = await import("../../src/sqlagent/static/js/workspace.js");
  const t = (size) => ({ size });
  assert.deepEqual(layout([t("full"), t("half"), t("half")]), ["full", "half", "half"]);
  assert.deepEqual(layout([t("half"), t("full")]), ["full", "full"]);
  assert.deepEqual(layout([t("half"), t("half"), t("half")]), ["half", "half", "full"]);
  assert.deepEqual(layout([t("half"), t("half"), t("half"), t("full"), t("half")]), ["half", "half", "full", "full", "full"]);
  assert.deepEqual(layout([]), []);
});

test("waitingText explains rate-limit waits", async () => {
  const { waitingText } = await import("../../src/sqlagent/static/js/lib.js");
  assert.match(waitingText({ seconds: 11.6, status: 429 }), /per-minute limit; waiting 12 s/);
  assert.match(waitingText({ seconds: 0.2, status: 503 }), /trying again in 1 s/);
});

test("filter helpers", async () => {
  const lib = await import("../../src/sqlagent/static/js/lib.js");
  assert.equal(lib.filterQuery({ state: "SP", date_from: "2018-01", category: "" }), "date_from=2018-01&state=SP");
  assert.ok(lib.sameFilters({ state: "SP", category: "" }, { state: "SP" }));
  assert.equal(lib.formatMonth("2017-03"), "Mar 2017");
  assert.deepEqual(lib.monthRange("2017-11", "2018-02"), ["2017-11", "2017-12", "2018-01", "2018-02"]);
  const presets = lib.periodPresets("2016-09", "2018-09");
  assert.deepEqual(presets.map((p) => p.label), ["All time", "2016", "2017", "2018", "Last 6 months", "Last 3 months"]);
  assert.deepEqual([presets[2].date_from, presets[2].date_to], ["2017-01", "2017-12"]);
  assert.deepEqual([presets[4].date_from, presets[4].date_to], ["2018-03", "2018-08"]);
  assert.equal(lib.describePeriod({ date_from: "2018-01", date_to: "2018-06" }), "Jan 2018 to Jun 2018");
  assert.equal(lib.describePeriod({}), "All time");
});

test("axisLabels shows the year only where it changes", async () => {
  const { axisLabels } = await import("../../src/sqlagent/static/js/lib.js");
  const labels = ["2017-11", "2017-12", "2018-01", "2018-02", "2018-03"];
  assert.deepEqual(axisLabels(labels, [0, 2, 4]), ["Nov 2017", "Jan 2018", "Mar"]);
  assert.deepEqual(axisLabels(["2016", "2017"], [0, 1]), ["2016", "2017"]);
});

test("routes carry filters and bars know which filter they set", async () => {
  const lib = await import("../../src/sqlagent/static/js/lib.js");
  assert.deepEqual(lib.parseRoute("#/d/d_1?state=SP"), ["d", "d_1"]);
  assert.deepEqual(lib.routeFilters("#/d/d_1?state=SP&date_from=2018-01&junk=1"), { date_from: "2018-01", state: "SP" });
  const options = { states: ["RJ", "SP"], categories: ["Toys"] };
  assert.equal(lib.filterFor({ type: "bar", x: "state", labels: ["SP", "RJ"] }, options), "state");
  assert.equal(lib.filterFor({ type: "bar", x: "category", labels: ["Toys"] }, options), "category");
  assert.equal(lib.filterFor({ type: "bar", x: "city and state", labels: ["Sao Paulo, SP"] }, options), null);
  assert.equal(lib.filterFor({ type: "line", x: "state", labels: ["SP"] }, options), null);
});
