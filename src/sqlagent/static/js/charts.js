// Figures drawn from the payload the server suggests: stat tiles, ranked bars, line chart, table.
// Marks carry the series colour; every label and number stays in text colours.

import { clear, el, svg } from "./dom.js";
import { chartTitle, describeDelta, formatValue, humanize, niceTicks, pickIndexes, shorten } from "./lib.js";

const ENTRANCE_MS = 900; // how long the first-draw animation class stays on

// ----------------------------------------------------------------- tooltip

let tooltip = null;

function showTooltip(event, rows, heading) {
  if (!tooltip) {
    tooltip = el("div", { class: "tooltip", role: "status" });
    document.body.append(tooltip);
  }
  clear(tooltip);
  if (heading) tooltip.append(el("div", { class: "tooltip-heading", text: heading }));
  for (const row of rows) {
    tooltip.append(
      el("div", { class: "tooltip-row" }, [
        row.series ? el("span", { class: `tooltip-key series-${row.series}` }) : null,
        el("strong", { text: row.value }),
        el("span", { class: "tooltip-label", text: row.label }),
      ]),
    );
  }
  tooltip.hidden = false;
  const box = tooltip.getBoundingClientRect();
  const x = Math.min(event.clientX + 14, window.innerWidth - box.width - 8);
  const y = event.clientY - box.height - 12 < 8 ? event.clientY + 16 : event.clientY - box.height - 12;
  tooltip.style.left = `${Math.max(8, x) + window.scrollX}px`;
  tooltip.style.top = `${y + window.scrollY}px`;
}

function hideTooltip() {
  if (tooltip) tooltip.hidden = true;
}

function settle(node) {
  node.classList.add("enter");
  setTimeout(() => node.classList.remove("enter"), ENTRANCE_MS);
}

// --------------------------------------------------------------- stat tiles

function renderDelta(item) {
  const delta = item.delta && describeDelta(item.value, item.delta.previous, item.format, item.delta.higher_is_better);
  if (!delta) return null;
  const arrow = { up: "▲", down: "▼", flat: "■" }[delta.direction];
  return el("div", { class: `stat-delta tone-${delta.tone}`, title: `Previous: ${formatValue(item.delta.previous, item.format, true)}` }, [
    el("span", { class: "delta-arrow", "aria-hidden": "true", text: arrow }),
    el("span", { class: "delta-value", text: delta.text }),
    el("span", { class: "delta-label", text: item.delta.label }),
  ]);
}

function renderKpi(chart) {
  const single = chart.items.length === 1;
  const tiles = chart.items.map((item) =>
    el("div", { class: single ? "stat stat-hero" : "stat" }, [
      el("div", { class: "stat-value", text: formatValue(item.value, item.format), title: formatValue(item.value, item.format, true) }),
      el("div", { class: "stat-label", text: humanize(item.label) }),
      renderDelta(item),
    ]),
  );
  return el("div", { class: "stats" }, [
    chart.context ? el("div", { class: "stats-context", text: chart.context }) : null,
    el("div", { class: "stats-row" }, tiles),
  ]);
}

// --------------------------------------------------------------------- bars

function renderBars(chart) {
  const max = Math.max(...chart.values);
  if (!(max > 0) || Math.min(...chart.values) < 0) return null; // negative values read better as a table
  const list = el("ul", { class: "bars", "aria-label": chartTitle(chart) });
  chart.labels.forEach((label, i) => {
    const value = chart.values[i];
    const exact = formatValue(value, chart.format, true);
    const fill = el("span", { class: "bar-fill" });
    fill.style.setProperty("--share", Math.max(0.004, value / max).toFixed(4));
    fill.style.setProperty("--i", i);
    const row = el("li", { class: "bar-row", tabIndex: 0, "aria-label": `${label}: ${exact}` }, [
      el("span", { class: "bar-label", text: shorten(label, 34), title: label }),
      el("span", { class: "bar-track" }, [fill, el("span", { class: "bar-value", text: formatValue(value, chart.format) })]),
    ]);
    const show = (event) => showTooltip(event, [{ value: exact, label: humanize(chart.y) }], label);
    row.addEventListener("pointermove", show);
    row.addEventListener("pointerleave", hideTooltip);
    row.addEventListener("focus", () => {
      const box = row.getBoundingClientRect();
      show({ clientX: box.left + box.width / 2, clientY: box.top });
    });
    row.addEventListener("blur", hideTooltip);
    list.append(row);
  });
  settle(list);
  return list;
}

// --------------------------------------------------------------------- line

const LINE = { height: 240, top: 14, bottom: 30, right: 18 };
const CHAR = 6.6; // approximate width of a 12px tabular digit

function drawLine(host, chart, width, animate) {
  clear(host);
  const format = chart.series[0].format;
  const count = chart.labels.length;
  const values = chart.series.flatMap((s) => s.values.filter((v) => v !== null));
  const ticks = niceTicks(Math.max(...values, 0));
  const top = ticks[ticks.length - 1];
  const single = chart.series.length === 1;
  const endLabel = single ? formatValue(chart.series[0].values[count - 1], format) : "";
  const left = Math.ceil(Math.max(...ticks.map((t) => formatValue(t, format).length)) * CHAR) + 14;
  const right = LINE.right + (single ? endLabel.length * 7 + 10 : 0);
  const innerW = Math.max(80, width - left - right);
  const innerH = LINE.height - LINE.top - LINE.bottom;
  const x = (i) => left + (count === 1 ? innerW / 2 : (i * innerW) / (count - 1));
  const y = (v) => LINE.top + innerH - (v / top) * innerH;

  const root = svg("svg", {
    class: animate ? "line-chart enter" : "line-chart", width, height: LINE.height, viewBox: `0 0 ${width} ${LINE.height}`,
    role: "img", tabindex: 0, "aria-label": `${chartTitle(chart)}. Use the arrow keys to read each value.`,
  });

  for (const tick of ticks) {
    root.append(svg("line", { class: tick === 0 ? "axis" : "grid", x1: left, x2: left + innerW, y1: y(tick), y2: y(tick) }));
    root.append(svg("text", { class: "tick", x: left - 8, y: y(tick) + 4, "text-anchor": "end", text: formatValue(tick, format) }));
  }
  const labelWidth = Math.max(...chart.labels.map((l) => l.length)) * CHAR + 28;
  for (const i of pickIndexes(count, Math.max(2, Math.floor(innerW / labelWidth)))) {
    const anchor = i === 0 ? "start" : i === count - 1 ? "end" : "middle";
    root.append(svg("text", { class: "tick", x: x(i), y: LINE.height - 8, "text-anchor": anchor, text: chart.labels[i] }));
  }

  chart.series.forEach((series, s) => {
    const points = series.values.map((v, i) => (v === null ? null : [x(i), y(v)])).filter(Boolean);
    const path = points.map(([px, py], i) => `${i ? "L" : "M"}${px.toFixed(1)},${py.toFixed(1)}`).join(" ");
    if (single && points.length > 1) {
      const base = y(0);
      root.append(svg("path", { class: "area series-1", d: `${path} L${points[points.length - 1][0].toFixed(1)},${base} L${points[0][0].toFixed(1)},${base} Z` }));
    }
    root.append(svg("path", { class: `line series-${s + 1}`, d: path, pathLength: 1 }));
    const last = points[points.length - 1];
    if (last) root.append(svg("circle", { class: `dot end-dot series-${s + 1}`, cx: last[0], cy: last[1], r: 4 }));
    if (single && last) root.append(svg("text", { class: "end-label", x: last[0] + 9, y: last[1] + 4, text: endLabel }));
  });

  // Hover layer: a crosshair that snaps to the nearest period and lists every series.
  const cross = svg("line", { class: "crosshair", y1: LINE.top, y2: LINE.top + innerH, visibility: "hidden" });
  const marks = chart.series.map((_, s) => svg("circle", { class: `dot series-${s + 1}`, r: 4, visibility: "hidden" }));
  root.append(cross, ...marks);
  let active = -1;

  function focusIndex(i, event) {
    active = Math.max(0, Math.min(count - 1, i));
    cross.setAttribute("x1", x(active));
    cross.setAttribute("x2", x(active));
    cross.setAttribute("visibility", "visible");
    chart.series.forEach((series, s) => {
      const v = series.values[active];
      marks[s].setAttribute("visibility", v === null ? "hidden" : "visible");
      if (v !== null) {
        marks[s].setAttribute("cx", x(active));
        marks[s].setAttribute("cy", y(v));
      }
    });
    const rows = chart.series.map((series, s) => ({
      series: chart.series.length > 1 ? s + 1 : 0,
      value: formatValue(series.values[active], series.format, true),
      label: humanize(series.name),
    }));
    const box = root.getBoundingClientRect();
    showTooltip(event || { clientX: box.left + x(active), clientY: box.top + LINE.top + 24 }, rows, chart.labels[active]);
  }

  function leave() {
    active = -1;
    cross.setAttribute("visibility", "hidden");
    marks.forEach((m) => m.setAttribute("visibility", "hidden"));
    hideTooltip();
  }

  root.addEventListener("pointermove", (event) => {
    const box = root.getBoundingClientRect();
    const ratio = (event.clientX - box.left - left) / innerW;
    focusIndex(Math.round(ratio * (count - 1)), event);
  });
  root.addEventListener("pointerleave", leave);
  root.addEventListener("blur", leave);
  root.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    focusIndex(active === -1 ? count - 1 : active + (event.key === "ArrowRight" ? 1 : -1));
  });
  host.append(root);
}

function renderLine(chart) {
  const host = el("div", { class: "line-host" });
  const wrap = el("div", { class: "line-wrap" }, [
    chart.series.length > 1
      ? el("div", { class: "legend" }, chart.series.map((s, i) =>
          el("span", { class: "legend-item" }, [el("span", { class: `legend-key series-${i + 1}` }), humanize(s.name)])))
      : null,
    host,
  ]);
  let lastWidth = 0;
  let drawn = false;
  new ResizeObserver(([entry]) => {
    const width = Math.floor(entry.contentRect.width);
    if (width > 0 && width !== lastWidth) {
      lastWidth = width;
      drawLine(host, chart, width, !drawn);
      drawn = true;
    }
  }).observe(host);
  return wrap;
}

// -------------------------------------------------------------------- table

const MAX_TABLE_ROWS = 100;

export function renderTable(columns, rows, truncated = false) {
  if (!columns.length) return el("p", { class: "table-note", text: "The query returned no columns." });
  if (!rows.length) return el("p", { class: "table-note", text: "The query returned no rows." });
  const numeric = columns.map((_, c) => rows.every((r) => r[c] === null || typeof r[c] === "number"));
  const head = el("tr", {}, columns.map((name, c) => el("th", { text: name, class: numeric[c] ? "num" : "", scope: "col" })));
  const body = rows.slice(0, MAX_TABLE_ROWS).map((row) =>
    el("tr", {}, row.map((value, c) =>
      el("td", {
        class: numeric[c] ? "num" : "",
        text: value === null ? "NULL" : typeof value === "number" ? formatValue(value, "number", true) : String(value),
      }))),
  );
  const shown = Math.min(rows.length, MAX_TABLE_ROWS);
  const note = truncated || rows.length > shown
    ? el("p", { class: "table-note", text: `Showing the first ${shown} rows${truncated ? "; the query returned more" : ""}.` })
    : null;
  return el("div", {}, [
    el("div", { class: "table-wrap" }, el("table", {}, [el("thead", {}, head), el("tbody", {}, body)])),
    note,
  ]);
}

// ------------------------------------------------------------------- figure

function renderChart(chart) {
  if (!chart) return null;
  if (chart.type === "kpi") return renderKpi(chart);
  if (chart.type === "bar") return renderBars(chart);
  if (chart.type === "line" && chart.series.length) return renderLine(chart);
  return null;
}

/**
 * A result as a card: a header with its title, note and tools, then the chart.
 * Charts come with their table twin behind a Chart/Table switch, so a chart never
 * gates data. Stat tiles skip the switch: they already are text.
 *
 * title: undefined derives one from the chart, "" shows none.
 */
export function renderFigure(payload, { title, note = "", tools = [], className = "", level = "h3" } = {}) {
  const chartNode = renderChart(payload.chart);
  const tableNode = renderTable(payload.columns, payload.rows, payload.truncated);
  const heading = title === undefined ? chartTitle(payload.chart) : title;
  const body = el("div", { class: "figure-body" }, chartNode || tableNode);

  let toggle = null;
  if (chartNode && payload.chart.type !== "kpi") {
    const buttons = ["Chart", "Table"].map((name, i) =>
      el("button", {
        type: "button", class: "seg", text: name, "aria-pressed": i === 0 ? "true" : "false",
        onclick: () => {
          buttons.forEach((b, j) => b.setAttribute("aria-pressed", j === i ? "true" : "false"));
          body.replaceChildren(i === 0 ? chartNode : tableNode);
        },
      }),
    );
    toggle = el("div", { class: "segmented", role: "group", "aria-label": "Show as" }, buttons);
  }

  const toolNodes = [toggle, ...tools].filter(Boolean);
  const head = heading || note || toolNodes.length
    ? el("header", { class: "figure-head" }, [
        el("div", { class: "figure-heading" }, [
          heading ? el(level, { class: "figure-title", text: heading }) : null,
          note ? el("p", { class: "figure-note", text: note }) : null,
        ]),
        toolNodes.length ? el("div", { class: "figure-tools" }, toolNodes) : null,
      ])
    : null;
  return el("section", { class: `figure ${className}`.trim() }, [head, body]);
}
