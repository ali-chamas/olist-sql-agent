// Pure helpers with no DOM access, so they run under `node --test` as well as in the browser.

const COMPACT_FROM = 10_000;

function trimZeros(text) {
  return text.includes(".") ? text.replace(/\.?0+$/, "") : text;
}

function compact(value) {
  const abs = Math.abs(value);
  if (abs >= 1e9) return trimZeros((value / 1e9).toFixed(2)) + "B";
  if (abs >= 1e6) return trimZeros((value / 1e6).toFixed(2)) + "M";
  return trimZeros((value / 1e3).toFixed(1)) + "K";
}

/**
 * Format a number for display.
 * format: "currency" (Brazilian reais), "percent" or "number".
 * exact: never abbreviate (used in tables and tooltips).
 */
export function formatValue(value, format = "number", exact = false) {
  if (value === null || value === undefined) return "n/a";
  if (typeof value !== "number" || !Number.isFinite(value)) return String(value);
  if (format === "percent") return trimZeros(value.toFixed(2)) + "%";
  const prefix = format === "currency" ? "R$ " : "";
  if (!exact && Math.abs(value) >= COMPACT_FROM) return prefix + compact(value);
  const digits = Number.isInteger(value) ? 0 : 2;
  return prefix + value.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

/** Round axis ticks from zero up to at least `max`: [0, 500, 1000, 1500]. */
export function niceTicks(max, count = 4) {
  if (!(max > 0)) return [0, 1];
  const rough = max / count;
  const power = 10 ** Math.floor(Math.log10(rough));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * power).find((s) => s >= rough);
  const ticks = [];
  for (let v = 0; v < max + step * 0.999; v += step) ticks.push(Number(v.toPrecision(12)));
  return ticks;
}

const WORDS = { pct: "%", percent: "%", percentage: "%", avg: "average", aov: "average order value", qty: "quantity", id: "ID" };

/** "average_order_value" -> "Average order value", "repeat_customer_pct" -> "Repeat customer (%)" */
export function humanize(name) {
  const words = String(name).toLowerCase().split(/[_\s]+/).filter(Boolean).map((w) => WORDS[w] ?? w);
  const rest = words.filter((w) => w !== "%");
  let text = rest.join(" ");
  if (rest.length < words.length) text = text ? `${text} (%)` : "%";
  return text ? text[0].toUpperCase() + text.slice(1) : "";
}

/** A title for a suggested chart: "Revenue by category". Stat tiles need none. */
export function chartTitle(chart) {
  if (!chart) return "";
  if (chart.type === "bar") return `${humanize(chart.y)} by ${humanize(chart.x).toLowerCase()}`;
  if (chart.type === "line") {
    const names = chart.series.map((s, i) => (i === 0 ? humanize(s.name) : humanize(s.name).toLowerCase()));
    return `${names.join(" and ")} by ${humanize(chart.x).toLowerCase()}`;
  }
  return "";
}

const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2016-09-04", "2018-09-03" -> "Sep 2016 to Sep 2018" */
export function formatPeriod(first, last) {
  const month = (iso) => {
    const [year, m] = String(iso).split("-");
    return `${MONTH_NAMES[Number(m) - 1] || ""} ${year}`.trim();
  };
  return `${month(first)} to ${month(last)}`;
}

export function formatElapsed(ms) {
  return `${(Math.max(0, ms) / 1000).toFixed(1)} s`;
}

/**
 * Compare a value with the previous period.
 * Percentages change in points; everything else in relative percent.
 * Returns { text, direction: "up" | "down" | "flat", tone: "good" | "bad" | "flat" } or null.
 */
export function describeDelta(value, previous, format = "number", higherIsBetter = true) {
  if (typeof value !== "number" || typeof previous !== "number") return null;
  let change;
  let text;
  if (format === "percent") {
    change = value - previous;
    text = `${Math.abs(change).toFixed(1)} pts`;
  } else {
    if (previous === 0) return null;
    change = ((value - previous) / Math.abs(previous)) * 100;
    text = `${Math.abs(change).toFixed(1)}%`;
  }
  if (Math.abs(change) < 0.05) return { text: "No change", direction: "flat", tone: "flat" };
  const up = change > 0;
  return { text: `${up ? "+" : "\u2212"}${text}`, direction: up ? "up" : "down", tone: up === higherIsBetter ? "good" : "bad" };
}

// Figures worth highlighting in an answer: amounts in reais, percentages, and numbers
// with separators or decimals. Plain integers such as years or "top 5" stay unmarked.
const FIGURE = /R\$\s?\d[\d,]*(?:\.\d+)?(?:\s?[KMB]\b)?|\d[\d,]*(?:\.\d+)?\s?%|\d{1,3}(?:,\d{3})+(?:\.\d+)?(?:\s?[KMB]\b)?|\d+\.\d+(?:\s?[KMB]\b)?/g;

/** Split text into runs, marking the figures: [{ text, mark }]. */
export function splitHighlights(text) {
  const source = String(text ?? "");
  const parts = [];
  let last = 0;
  for (const match of source.matchAll(FIGURE)) {
    if (match.index > last) parts.push({ text: source.slice(last, match.index), mark: false });
    parts.push({ text: match[0], mark: true });
    last = match.index + match[0].length;
  }
  if (last < source.length) parts.push({ text: source.slice(last), mark: false });
  return parts;
}

export function shorten(text, max = 28) {
  const value = String(text);
  return value.length > max ? value.slice(0, max - 1) + "…" : value;
}

/**
 * Pick at most `max` indexes at a regular stride, always including the first and last.
 * The stride stays constant so axis labels are evenly spaced; a label that would
 * crowd the last one is dropped.
 */
export function pickIndexes(length, max = 6) {
  if (length <= Math.max(2, max)) return Array.from({ length }, (_, i) => i);
  const last = length - 1;
  const stride = Math.ceil(last / (Math.max(2, max) - 1));
  const picked = [];
  for (let i = 0; i < last; i += stride) picked.push(i);
  if (last - picked[picked.length - 1] < stride * 0.9 && picked.length > 1) picked.pop();
  picked.push(last);
  return picked;
}

function csvCell(value) {
  if (value === null || value === undefined) return "";
  const text = String(value);
  return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

export function toCSV(columns, rows) {
  return [columns, ...rows].map((row) => row.map(csvCell).join(",")).join("\r\n") + "\r\n";
}

/**
 * Incremental parser for a text/event-stream body.
 * Feed it text chunks; it returns the complete events and keeps the unfinished tail.
 */
export function createSSEParser() {
  let buffer = "";
  return function push(chunk) {
    buffer += chunk.replace(/\r\n/g, "\n");
    const events = [];
    let end = buffer.indexOf("\n\n");
    while (end !== -1) {
      const block = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      let name = "message";
      const data = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) name = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
      }
      if (data.length) {
        try {
          events.push({ event: name, data: JSON.parse(data.join("\n")) });
        } catch {
          // A malformed event is skipped; the stream goes on.
        }
      }
      end = buffer.indexOf("\n\n");
    }
    return events;
  };
}

const EDIT_VERBS = { add_tile: "add", update_tile: "update", remove_tile: "remove", move_tile: "move", update_dashboard: "rename" };

/** One sentence describing an agent step, for the trace. */
export function describeStep(step) {
  if (step.tool === "cannot_answer" || step.tool === "cannot_do") return "Decided the data cannot do this";
  if (step.tool === "finish") return "Finished";
  if (step.tool in EDIT_VERBS) {
    if (step.ok) return step.detail || "Changed the dashboard";
    const what = step.tool === "update_dashboard" ? "the dashboard" : step.detail ? `\u201C${step.detail}\u201D` : "a tile";
    return `Could not ${EDIT_VERBS[step.tool]} ${what}`;
  }
  if (!step.ok) return step.tool === "submit_answer" ? "Final query was refused" : "Query failed";
  const rows = `${step.row_count} ${step.row_count === 1 ? "row" : "rows"}`;
  const time = `${Math.round(step.elapsed_ms)} ms`;
  return step.tool === "submit_answer" ? `Submitted the final query: ${rows} in ${time}` : `Tested a query: ${rows} in ${time}`;
}

/** "just now", "5 minutes ago", "yesterday", or a date, from an ISO timestamp. */
export function timeAgo(iso, now = Date.now()) {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const seconds = Math.max(0, Math.round((now - then) / 1000));
  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} ${minutes === 1 ? "minute" : "minutes"} ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} ${hours === 1 ? "hour" : "hours"} ago`;
  const days = Math.round(hours / 24);
  if (days === 1) return "yesterday";
  if (days < 7) return `${days} days ago`;
  return new Date(then).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

/** The parts of a route hash: "#/d/d_123" -> ["d", "d_123"]. */
export function parseRoute(hash) {
  return String(hash || "").replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
}

/** What to show while the HTTP client backs off from the provider. */
export function waitingText({ seconds, status }) {
  const s = Math.max(1, Math.round(seconds));
  return status === 429
    ? `The free model plan is at its per-minute limit; waiting ${s} s`
    : `The model provider is busy; trying again in ${s} s`;
}

// ------------------------------------------------------------------ filters

export const FILTER_KEYS = ["date_from", "date_to", "state", "category"];

/** Only the filters that are set, so two equal filter sets compare equal. */
export function cleanFilters(filters = {}) {
  const out = {};
  for (const key of FILTER_KEYS) if (filters[key]) out[key] = String(filters[key]);
  return out;
}

export function filterQuery(filters = {}) {
  const clean = cleanFilters(filters);
  return Object.keys(clean).map((k) => `${k}=${encodeURIComponent(clean[k])}`).join("&");
}

export function sameFilters(a, b) {
  return filterQuery(a) === filterQuery(b);
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2017-03" -> "Mar 2017"; anything else is returned unchanged. */
export function formatMonth(value) {
  const match = /^(\d{4})-(\d{2})$/.exec(String(value));
  return match ? `${MONTHS[Number(match[2]) - 1]} ${match[1]}` : String(value);
}

/** Every month from first to last, inclusive, as YYYY-MM. */
export function monthRange(first, last) {
  const out = [];
  let [y, m] = first.split("-").map(Number);
  const [ly, lm] = last.split("-").map(Number);
  while (y < ly || (y === ly && m <= lm)) {
    out.push(`${y}-${String(m).padStart(2, "0")}`);
    m += 1;
    if (m > 12) { m = 1; y += 1; }
  }
  return out;
}

/** Period shortcuts for the data's own range: all time, each year, and the last 3 and 6 complete months. */
export function periodPresets(first, last) {
  const months = monthRange(first, last);
  // The last month of the data is usually partial, so "last N months" ends one month earlier.
  const end = months.length > 1 ? months[months.length - 2] : last;
  const endIndex = months.indexOf(end);
  const years = [...new Set(months.map((m) => m.slice(0, 4)))];
  return [
    { id: "all", label: "All time", date_from: "", date_to: "" },
    ...years.map((y) => ({ id: y, label: y, date_from: months.find((m) => m.startsWith(y)), date_to: [...months].reverse().find((m) => m.startsWith(y)) })),
    { id: "l6", label: "Last 6 months", date_from: months[Math.max(0, endIndex - 5)], date_to: end },
    { id: "l3", label: "Last 3 months", date_from: months[Math.max(0, endIndex - 2)], date_to: end },
  ];
}

/** "Mar 2017 to Aug 2018", "From Mar 2017", "All time". */
export function describePeriod(filters = {}) {
  const { date_from: from, date_to: to } = filters;
  if (from && to) return from === to ? formatMonth(from) : `${formatMonth(from)} to ${formatMonth(to)}`;
  if (from) return `From ${formatMonth(from)}`;
  if (to) return `Up to ${formatMonth(to)}`;
  return "All time";
}

/** A file-name-safe version of a title: "Revenue by month" -> "revenue-by-month". */
export function slugify(text) {
  return String(text || "export").toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "").replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "export";
}

/**
 * Axis text for the chosen ticks of a time axis. Months read "Jan 2017", then "Mar", "May"...
 * with the year again only where it changes. Other labels are returned as they are.
 */
export function axisLabels(labels, indexes) {
  let lastYear = "";
  return indexes.map((i) => {
    const label = String(labels[i]);
    const match = /^(\d{4})-(\d{2})$/.exec(label);
    if (!match) return label;
    const month = formatMonth(label).slice(0, 3);
    if (match[1] === lastYear) return month;
    lastYear = match[1];
    return `${month} ${match[1]}`;
  });
}
