// Saved dashboards: the list (with "describe a dashboard" to build one) and the editor,
// where a prompt bar changes the dashboard through the AI and every tile has manual controls.

import * as api from "./api.js";
import { renderFigure } from "./charts.js";
import { clear, el, icon, toolButton } from "./dom.js";
import { describeStep, formatElapsed, shorten, timeAgo, waitingText } from "./lib.js";
import { openMenu } from "./menu.js";
import { toast } from "./toast.js";

const BUILD_EXAMPLES = [
  "Marketing overview: revenue trend, top categories, repeat customers and payment mix",
  "Delivery performance: late-delivery rate by state and its effect on review scores",
  "Category deep dive for 2018: revenue, orders and average order value by category",
];
const EDIT_EXAMPLES = ["Add revenue by customer state", "Make the monthly chart full width", "Add a table of the top 10 sellers"];
const CHART_LABELS = { auto: "Automatic", kpi: "Headline numbers", bar: "Bar chart", line: "Line chart", table: "Table" };
const HIGHLIGHT_MS = 6000;

function explainFailure(result) {
  const error = result.error || "";
  if (result.status === "conflict") return "Someone changed this dashboard while the AI was working. It has been reloaded; try your request again.";
  if (result.status === "max_steps") return "The AI could not finish within its attempts. Nothing was saved. Try a more specific request.";
  if (/HTTP 40[13]/.test(error)) return "The model provider rejected the API key. Check LLM_API_KEY in the server settings.";
  if (/HTTP 429/.test(error)) return "The model provider is rate limiting this key. Wait a minute and try again.";
  return "The model provider returned an error. Nothing was saved.";
}

/** A live list of the agent's steps with a running clock. */
function createProgress() {
  const steps = el("ol", { class: "steps" });
  const clock = el("p", { class: "progress-clock" });
  const node = el("div", { class: "progress", "aria-live": "polite" }, [steps, clock]);
  let pending = null;
  let timer = null;
  return {
    node,
    start() {
      clear(steps);
      pending = null;
      const started = performance.now();
      clock.textContent = "Starting";
      timer = setInterval(() => { clock.textContent = `Working for ${formatElapsed(performance.now() - started)}`; }, 100);
    },
    event({ event, data }) {
      if (event === "thinking") {
        // One pending row at a time: a new model call reuses it instead of stacking spinners.
        if (!pending) {
          pending = el("li", { class: "step step-pending" }, [
            el("span", { class: "step-mark", "aria-hidden": "true" }),
            el("div", { class: "step-body" }, el("div", { class: "step-title" })),
          ]);
          steps.append(pending);
        }
        if (!pending.dataset.retry) pending.querySelector(".step-title").textContent = data.call === 1 ? "Reading the request" : "Working";
      } else if (event === "waiting") {
        pending?.querySelector(".step-title")?.replaceChildren(waitingText(data));
      } else if (event === "retry") {
        if (pending) {
          pending.dataset.retry = "1";
          pending.querySelector(".step-title").textContent = "The model's reply was not a valid change; asking again";
        }
      } else if (event === "step") {
        const step = data.step;
        const item = el("li", { class: step.ok ? "step" : "step step-failed" }, [
          el("span", { class: "step-mark", "aria-hidden": "true" }),
          el("div", { class: "step-body" }, [
            el("div", { class: "step-title", text: describeStep(step) }),
            step.error ? el("div", { class: "step-error", text: step.error }) : null,
          ]),
        ]);
        if (pending) pending.replaceWith(item);
        else steps.append(item);
        pending = null;
      }
    },
    stop() {
      clearInterval(timer);
      pending?.remove();
      clock.textContent = "";
    },
  };
}

// =================================================================== list

export function createDashboardsView({ onOpen }) {
  const grid = el("div", { class: "board-cards" });
  const prompt = el("textarea", {
    class: "builder-input", rows: 3, maxLength: 1000, "aria-label": "Describe the dashboard you need",
    placeholder: "Describe the dashboard you need, for example: revenue trend, top categories and repeat customers for the marketing team",
  });
  const build = el("button", { type: "submit", class: "button button-primary", text: "Build dashboard" });
  const stop = el("button", { type: "button", class: "button button-secondary", text: "Stop", hidden: true });
  const blank = el("button", { type: "button", class: "tool", text: "Start a blank dashboard" });
  const progress = createProgress();
  const outcome = el("div", { class: "builder-outcome" });
  progress.node.hidden = true;

  const form = el("form", { class: "builder" }, [
    prompt,
    el("div", { class: "builder-row" }, [
      el("ul", { class: "chip-list" }, BUILD_EXAMPLES.map((text) =>
        el("li", {}, el("button", { type: "button", class: "chip", text: shorten(text, 60), title: text, onclick: () => { prompt.value = text; prompt.focus(); } })))),
      el("div", { class: "builder-actions" }, [blank, stop, build]),
    ]),
    progress.node,
    outcome,
  ]);

  const node = el("div", { class: "view view-wide" }, [
    el("header", { class: "view-head" }, [
      el("h1", { class: "view-title", text: "Dashboards" }),
      el("p", { class: "view-lead", text: "Describe a dashboard and the AI builds it from read-only queries. Open any dashboard to change it in plain language." }),
    ]),
    form,
    el("h2", { class: "section-title cards-title", text: "Your dashboards" }),
    grid,
  ]);

  let controller = null;

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = prompt.value.trim();
    if (text.length < 3 || controller) return;
    controller = new AbortController();
    build.hidden = true;
    blank.disabled = true;
    stop.hidden = false;
    prompt.disabled = true;
    clear(outcome);
    progress.node.hidden = false;
    progress.start();
    try {
      const result = await api.buildWithAI(text, (e) => progress.event(e), controller.signal);
      if (result.saved && result.dashboard) {
        prompt.value = "";
        toast(`Built “${result.dashboard.title}”`);
        onOpen(result.dashboard.id, { highlight: result.dashboard.tiles.map((t) => t.id) });
      } else {
        const text = result.status === "cannot_do" ? result.message : explainFailure(result);
        outcome.replaceChildren(el("div", { class: result.status === "cannot_do" ? "notice notice-info" : "notice notice-error" }, el("p", { class: "notice-text", text })));
      }
    } catch (err) {
      const text = err.name === "AbortError" ? "Stopped. No dashboard was created." : err.message;
      outcome.replaceChildren(el("div", { class: "notice notice-info" }, el("p", { class: "notice-text", text })));
    } finally {
      progress.stop();
      progress.node.hidden = true;
      controller = null;
      build.hidden = false;
      blank.disabled = false;
      stop.hidden = true;
      prompt.disabled = false;
    }
  });
  prompt.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) form.requestSubmit();
  });
  stop.addEventListener("click", () => controller?.abort());
  blank.addEventListener("click", async () => {
    try {
      const created = await api.createDashboard("Untitled dashboard");
      onOpen(created.id, { focusTitle: true });
    } catch (err) {
      toast(err.message);
    }
  });

  function card(item) {
    const remove = el("button", { type: "button", class: "tool card-delete", text: "Delete" });
    let armed = null;
    remove.addEventListener("click", async (event) => {
      event.stopPropagation();
      if (!armed) {
        remove.textContent = "Confirm delete";
        remove.classList.add("is-armed");
        armed = setTimeout(() => { armed = null; remove.textContent = "Delete"; remove.classList.remove("is-armed"); }, 4000);
        return;
      }
      clearTimeout(armed);
      try {
        await api.deleteDashboard(item.id);
        toast(`Deleted “${item.title}”`);
        show();
      } catch (err) {
        toast(err.message);
      }
    });
    const charts = item.tile_count === 1 ? "1 chart" : `${item.tile_count} charts`;
    return el("article", { class: "board-card" }, [
      el("a", { class: "board-card-link", href: `#/d/${encodeURIComponent(item.id)}` }, [
        el("h3", { class: "board-card-title", text: item.title }),
        item.description ? el("p", { class: "board-card-text", text: item.description }) : null,
      ]),
      el("div", { class: "board-card-meta" }, [el("span", { text: `${charts}, updated ${timeAgo(item.updated_at)}` }), remove]),
    ]);
  }

  const builtIn = el("article", { class: "board-card board-card-builtin" }, [
    el("a", { class: "board-card-link", href: "#/overview" }, [
      el("h3", { class: "board-card-title", text: "Marketplace overview" }),
      el("p", { class: "board-card-text", text: "Headline numbers, revenue trend, categories, states and delivery, with year-over-year comparisons." }),
    ]),
    el("div", { class: "board-card-meta" }, el("span", { text: "Built in, read-only" })),
  ]);

  async function show() {
    try {
      const items = await api.listDashboards();
      clear(grid).append(builtIn, ...items.map(card));
    } catch (err) {
      clear(grid).append(el("p", { class: "edit-error", text: err.message }));
    }
  }

  return { node, show };
}

/**
 * The width each tile is drawn at. A half-width tile that would sit alone in its row
 * (followed by a full-width tile, or last) is drawn full width, so the grid has no holes.
 * The saved size is unchanged.
 */
export function layout(tiles) {
  const widths = [];
  let column = 0;
  tiles.forEach((tile, i) => {
    if (tile.size === "full") {
      widths.push("full");
      column = 0;
      return;
    }
    const next = tiles[i + 1];
    if (column === 0 && (!next || next.size === "full")) {
      widths.push("full");
      return;
    }
    widths.push("half");
    column = (column + 1) % 2;
  });
  return widths;
}

// ================================================================= editor

export function createDashboardView({ onAskAbout, onMissing }) {
  const title = el("input", { class: "board-title", "aria-label": "Dashboard title", maxLength: 120 });
  const description = el("textarea", { class: "board-description", rows: 1, "aria-label": "Dashboard description", maxLength: 400, placeholder: "Add a description" });
  const fitDescription = () => {
    description.style.height = "auto";
    description.style.height = `${description.scrollHeight}px`;
  };
  description.addEventListener("input", fitDescription);
  const meta = el("p", { class: "board-meta" });
  const historyButton = toolButton("retry", "History", () => toggleHistory());
  const more = el("button", { type: "button", class: "tool", "aria-haspopup": "menu", "aria-expanded": "false" }, [el("span", { class: "tool-label", text: "More" })]);
  const history = el("section", { class: "history", hidden: true, "aria-label": "Version history" });
  const grid = el("div", { class: "tiles board-tiles" });
  const empty = el("div", { class: "empty board-empty", hidden: true });

  // AI prompt bar
  const input = el("input", {
    type: "text", class: "composer-input", maxLength: 1000, autocomplete: "off", enterKeyHint: "send",
    "aria-label": "Describe a change to this dashboard",
  });
  const submit = el("button", { type: "submit", class: "button button-primary", text: "Apply" });
  const stop = el("button", { type: "button", class: "button button-secondary", text: "Stop", hidden: true });
  const scopeText = el("span", { class: "context-text" });
  const scope = el("div", { class: "context", hidden: true }, [
    scopeText,
    el("button", { type: "button", class: "tool", onclick: () => setScope(null) }, [icon("close"), el("span", { class: "tool-label", text: "Whole dashboard" })]),
  ]);
  const progress = createProgress();
  progress.node.hidden = true;
  const outcome = el("div", { class: "board-outcome", hidden: true });
  const form = el("form", { class: "composer" }, [input, submit, stop]);
  const bar = el("div", { class: "composer-bar board-bar" }, [progress.node, outcome, scope, form]);

  const node = el("div", { class: "view view-wide view-board" }, [
    el("header", { class: "board-head" }, [
      el("div", { class: "board-heading" }, [title, description, meta]),
      el("div", { class: "board-tools" }, [historyButton, more]),
    ]),
    history,
    empty,
    grid,
    bar,
  ]);

  let board = null;
  let dataCache = new Map(); // tile id -> { key, payload } so unchanged tiles are not refetched
  let scopeTile = null;
  let controller = null;

  // ------------------------------------------------------------- loading

  async function show(id, options = {}) {
    if (board?.id !== id) {
      board = null;
      dataCache = new Map();
      clear(grid);
      setScope(null);
      history.hidden = true;
      outcome.hidden = true;
      title.value = "";
      description.value = "";
      meta.textContent = "Loading";
    }
    try {
      render(await api.getDashboard(id), options.highlight || []);
      if (options.focusTitle) {
        title.focus();
        title.select();
      }
    } catch (err) {
      if (err.status === 404) onMissing();
      else meta.textContent = err.message;
    }
  }

  function render(next, changed = []) {
    board = next;
    if (document.activeElement !== title) title.value = board.title;
    if (document.activeElement !== description) description.value = board.description;
    requestAnimationFrame(fitDescription);
    meta.textContent = `Version ${board.version}, updated ${timeAgo(board.updated_at)}`;
    document.title = `${board.title} | Ask Olist`;
    empty.hidden = board.tiles.length > 0;
    if (!board.tiles.length) renderEmpty();
    input.placeholder = board.tiles.length
      ? "Describe a change: add a chart, remove one, make it a table, change the period"
      : "Describe what this dashboard should show";
    const keep = new Set(board.tiles.map((t) => t.id));
    for (const id of dataCache.keys()) if (!keep.has(id)) dataCache.delete(id);
    const widths = layout(board.tiles);
    clear(grid).append(...board.tiles.map((t, i) => tileNode(t, i, changed.includes(t.id), widths[i])));
    if (!history.hidden) loadHistory();
    if (scopeTile && !keep.has(scopeTile.id)) setScope(null);
  }

  function renderEmpty() {
    clear(empty).append(
      el("h2", { class: "empty-title", text: "This dashboard is empty" }),
      el("p", { text: "Describe what it should show in the box at the bottom, or add answers from Ask with Add to dashboard." }),
      el("ul", { class: "chip-list" }, BUILD_EXAMPLES.map((text) =>
        el("li", {}, el("button", { type: "button", class: "chip", text: shorten(text, 60), title: text, onclick: () => { input.value = text; input.focus(); } })))),
    );
  }

  // --------------------------------------------------------------- tiles

  function tileNode(tile, index, changed, width) {
    const slot = el("div", { class: width === "full" ? "tile-slot tile-wide" : "tile-slot", "data-tile": tile.id });
    const key = `${tile.sql}\u0000${tile.chart}`;
    const cached = dataCache.get(tile.id);
    const draw = (payload) => {
      const moreButton = el("button", {
        type: "button", class: "tool tile-more", "aria-haspopup": "menu", "aria-expanded": "false",
        "aria-label": `Actions for ${tile.title}`,
      }, [el("span", { class: "tool-label", text: "More" })]);
      moreButton.addEventListener("click", () => openTileMenu(moreButton, tile, index, payload));
      const ai = toolButton("chat", "Edit with AI", () => setScope(tile), { "aria-label": `Edit ${tile.title} with AI` });
      const figure = payload.error
        ? el("section", { class: "figure tile" }, [
            el("header", { class: "figure-head" }, [
              el("div", { class: "figure-heading" }, el("h3", { class: "figure-title", text: tile.title })),
              el("div", { class: "figure-tools" }, [ai, moreButton]),
            ]),
            el("p", { class: "edit-error", text: `This query failed: ${payload.error}` }),
          ])
        : renderFigure(payload, { title: tile.title, note: payload.warning || tile.note, tools: [ai, moreButton], className: "tile" });
      if (changed) {
        figure.classList.add("tile-changed");
        figure.prepend(el("span", { class: "tile-badge", text: "Changed by AI" }));
        setTimeout(() => figure.classList.remove("tile-changed"), HIGHLIGHT_MS);
      }
      slot.replaceChildren(figure);
    };
    if (cached && cached.key === key) {
      draw(cached.payload);
    } else {
      slot.append(el("section", { class: "figure tile tile-loading" }, [
        el("header", { class: "figure-head" }, el("h3", { class: "figure-title", text: tile.title })),
        el("p", { class: "table-note", text: "Running the query" }),
      ]));
      api.getTileData(board.id, tile.id)
        .then((payload) => {
          dataCache.set(tile.id, { key, payload });
          if (slot.isConnected) draw(payload);
        })
        .catch((err) => { if (slot.isConnected) draw({ error: err.message }); });
    }
    return slot;
  }

  async function apply(promise, message) {
    try {
      const next = await promise;
      render(next);
      if (message) toast(message, { label: "Undo", run: () => undo(next.version - 1) });
      return true;
    } catch (err) {
      if (err.status === 409) {
        toast("The dashboard changed elsewhere. Reloaded the latest version.");
        await show(board.id);
      } else {
        toast(err.message);
      }
      return false;
    }
  }

  async function undo(version) {
    if (!board || version < 1) return;
    const restored = await apply(api.restoreVersion(board.id, version), null);
    if (restored) toast(`Restored version ${version}`);
  }

  function openTileMenu(anchor, tile, index, payload) {
    const last = board.tiles.length - 1;
    openMenu(anchor, [
      { label: "Edit query and chart", onSelect: () => openEditor(tile) },
      { label: tile.size === "full" ? "Make half width" : "Make full width",
        onSelect: () => apply(api.updateTile(board.id, tile.id, { size: tile.size === "full" ? "half" : "full", version: board.version }), "Resized the chart") },
      { label: "Move earlier", disabled: index === 0, onSelect: () => apply(api.moveTile(board.id, tile.id, index - 1, board.version), "Moved the chart") },
      { label: "Move later", disabled: index === last, onSelect: () => apply(api.moveTile(board.id, tile.id, index + 1, board.version), "Moved the chart") },
      { label: "Ask a question about it", disabled: Boolean(payload.error), onSelect: () => onAskAbout({ title: tile.title, sql: tile.sql, payload }) },
      "separator",
      { label: "Remove", danger: true, onSelect: () => apply(api.removeTile(board.id, tile.id, board.version), `Removed “${tile.title}”`) },
    ], { label: `Actions for ${tile.title}` });
  }

  function openEditor(tile) {
    const slot = grid.querySelector(`[data-tile="${CSS.escape(tile.id)}"]`);
    if (!slot) return;
    const fieldTitle = el("input", { class: "field-input", value: tile.title, maxLength: 120 });
    const fieldChart = el("select", { class: "field-input" }, Object.entries(CHART_LABELS).map(([value, label]) =>
      el("option", { value, text: label, selected: value === tile.chart })));
    const fieldSize = el("select", { class: "field-input" }, [["half", "Half width"], ["full", "Full width"]].map(([value, label]) =>
      el("option", { value, text: label, selected: value === tile.size })));
    const fieldNote = el("input", { class: "field-input", value: tile.note, maxLength: 240, placeholder: "Optional caveat shown under the title" });
    const fieldSql = el("textarea", { class: "sql-editor", rows: 6, spellcheck: false, value: tile.sql });
    const message = el("p", { class: "edit-error", role: "alert", hidden: true });
    const save = el("button", { type: "submit", class: "button button-primary", text: "Save" });
    const cancel = el("button", { type: "button", class: "button button-secondary", text: "Cancel", onclick: () => render(board) });
    const field = (label, control) => el("label", { class: "field" }, [el("span", { class: "field-label", text: label }), control]);
    const editor = el("form", { class: "figure tile tile-editor" }, [
      el("h3", { class: "figure-title", text: "Edit chart" }),
      field("Title", fieldTitle),
      el("div", { class: "field-row" }, [field("Show as", fieldChart), field("Width", fieldSize)]),
      field("Note", fieldNote),
      field("SQL (read-only)", fieldSql),
      message,
      el("div", { class: "edit-actions" }, [save, cancel]),
    ]);
    editor.addEventListener("submit", async (event) => {
      event.preventDefault();
      save.disabled = true;
      const changes = { version: board.version };
      if (fieldTitle.value.trim() !== tile.title) changes.title = fieldTitle.value;
      if (fieldChart.value !== tile.chart) changes.chart = fieldChart.value;
      if (fieldSize.value !== tile.size) changes.size = fieldSize.value;
      if (fieldNote.value.trim() !== tile.note) changes.note = fieldNote.value;
      if (fieldSql.value.trim() !== tile.sql) changes.sql = fieldSql.value;
      if (Object.keys(changes).length === 1) {
        render(board);
        return;
      }
      try {
        render(await api.updateTile(board.id, tile.id, changes), [tile.id]);
        toast("Saved the chart");
      } catch (err) {
        message.textContent = err.message;
        message.hidden = false;
        save.disabled = false;
      }
    });
    slot.classList.add("tile-wide");
    slot.replaceChildren(editor);
    fieldTitle.focus();
  }

  // ------------------------------------------------------------- details

  async function saveDetails() {
    if (!board) return;
    const fields = {};
    if (title.value.trim() && title.value.trim() !== board.title) fields.title = title.value;
    if (description.value.trim() !== board.description) fields.description = description.value;
    if (!Object.keys(fields).length) {
      title.value = board.title;
      return;
    }
    await apply(api.updateDashboard(board.id, { ...fields, version: board.version }), null);
  }
  for (const field of [title, description]) {
    field.addEventListener("change", saveDetails);
    field.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        field.blur();
      }
      if (event.key === "Escape") {
        field.value = field === title ? board.title : board.description;
        field.blur();
      }
    });
  }

  more.addEventListener("click", () => {
    const confirmDelete = el("button", { type: "button", role: "menuitem", class: "menu-item menu-danger", text: "Delete this dashboard" });
    confirmDelete.addEventListener("click", async () => {
      if (confirmDelete.dataset.armed !== "true") {
        confirmDelete.dataset.armed = "true";
        confirmDelete.textContent = "Click again to delete";
        return;
      }
      try {
        await api.deleteDashboard(board.id);
        toast(`Deleted “${board.title}”`);
        onMissing();
      } catch (err) {
        toast(err.message);
      }
    });
    openMenu(more, [confirmDelete], { label: "Dashboard actions" });
  });

  // ------------------------------------------------------------- history

  function toggleHistory() {
    history.hidden = !history.hidden;
    historyButton.setAttribute("aria-pressed", history.hidden ? "false" : "true");
    if (!history.hidden) loadHistory();
  }

  async function loadHistory() {
    try {
      const items = await api.getRevisions(board.id);
      clear(history).append(
        el("h2", { class: "section-title", text: "History" }),
        el("ol", { class: "history-list" }, items.map((r) =>
          el("li", { class: "history-item" }, [
            el("div", { class: "history-text" }, [
              el("span", { class: "history-summary", text: r.summary }),
              el("span", { class: "history-meta", text: `Version ${r.version}, ${r.author === "ai" ? "by the AI" : "by you"}, ${timeAgo(r.created_at)}` }),
            ]),
            r.version === board.version
              ? el("span", { class: "history-current", text: "Current" })
              : el("button", { type: "button", class: "tool", text: "Restore", onclick: () => undo(r.version) }),
          ]))),
      );
    } catch (err) {
      clear(history).append(el("p", { class: "edit-error", text: err.message }));
    }
  }

  // ------------------------------------------------------------------ AI

  function setScope(tile) {
    scopeTile = tile;
    scope.hidden = !tile;
    if (tile) {
      scopeText.textContent = `Editing “${shorten(tile.title, 60)}”`;
      input.placeholder = "Describe the change, such as “make it a line chart by month”";
      input.focus();
    } else if (board) {
      input.placeholder = board.tiles.length ? "Describe a change: add a chart, remove one, make it a table, change the period" : "Describe what this dashboard should show";
    }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (text.length < 3 || controller || !board) return;
    controller = new AbortController();
    submit.hidden = true;
    stop.hidden = false;
    outcome.hidden = true;
    progress.node.hidden = false;
    progress.start();
    const before = board.version;
    try {
      const result = await api.editWithAI(board.id, text, { tileId: scopeTile?.id, version: board.version }, (e) => progress.event(e), controller.signal);
      input.value = "";
      if (result.saved) {
        const changed = result.changes.filter((c) => c.tile_id && c.kind !== "removed").map((c) => c.tile_id);
        render(result.dashboard, changed);
        setScope(null);
        showOutcome(result.message || result.summary, "done");
        toast(result.summary, { label: "Undo", run: () => undo(before) });
      } else if (result.status === "cannot_do") {
        showOutcome(result.message, "info");
      } else if (result.status === "done" && !result.changes.length) {
        showOutcome(`Nothing was changed. ${result.message || "The dashboard already matches the request."}`, "info");
      } else {
        if (result.status === "conflict" && result.dashboard) render(result.dashboard);
        showOutcome(explainFailure(result), "error");
      }
    } catch (err) {
      showOutcome(err.name === "AbortError" ? "Stopped. Nothing was changed." : err.message, err.name === "AbortError" ? "info" : "error");
    } finally {
      progress.stop();
      progress.node.hidden = true;
      controller = null;
      submit.hidden = false;
      stop.hidden = true;
      input.focus({ preventScroll: true });
    }
  });
  stop.addEventListener("click", () => controller?.abort());

  function showOutcome(text, tone) {
    outcome.className = `board-outcome notice notice-${tone === "done" ? "info" : tone}`;
    outcome.replaceChildren(
      el("p", { class: "notice-text", text }),
      el("button", { type: "button", class: "tool", "aria-label": "Dismiss", onclick: () => { outcome.hidden = true; } }, icon("close")),
    );
    outcome.hidden = false;
  }

  return { node, show, focus: () => input.focus() };
}
