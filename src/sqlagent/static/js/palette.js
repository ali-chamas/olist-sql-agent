// Command palette (Ctrl+K or Cmd+K): jump to any dashboard or page, or start a question or a
// dashboard from what you typed. Everything it does is also reachable without it.

import { listDashboards } from "./api.js";
import { el, icon } from "./dom.js";
import { shorten } from "./lib.js";

/**
 * @param {{ go: (path: string) => void, ask: (q: string) => void, build: (prompt: string) => void, toggleTheme: () => void }} actions
 */
export function createPalette(actions) {
  const input = el("input", {
    type: "text", class: "palette-input", autocomplete: "off", spellcheck: false, role: "combobox",
    "aria-expanded": "true", "aria-controls": "palette-list", "aria-label": "Search dashboards and pages, or type a question",
    placeholder: "Jump to a dashboard, or type a question",
  });
  const list = el("ul", { class: "palette-list", id: "palette-list", role: "listbox" });
  const panel = el("div", { class: "palette", role: "dialog", "aria-modal": "true", "aria-label": "Command palette" }, [
    el("div", { class: "palette-search" }, [icon("search"), input, el("kbd", { class: "palette-kbd", text: "Esc" })]),
    list,
    el("p", { class: "palette-foot", text: "Enter to open. Arrow keys to move." }),
  ]);
  const backdrop = el("div", { class: "palette-backdrop", hidden: true }, panel);
  document.body.append(backdrop);

  let boards = [];
  let items = [];
  let index = 0;
  let returnFocus = null;

  const pages = [
    { label: "Ask a question", hint: "Page", icon: "question", run: () => actions.go("ask") },
    { label: "Dashboards", hint: "Page", icon: "grid", run: () => actions.go("dashboards") },
    { label: "Marketplace overview", hint: "Built-in dashboard", icon: "grid", run: () => actions.go("overview") },
    { label: "Data: tables and columns", hint: "Page", icon: "database", run: () => actions.go("data") },
    { label: "Switch light or dark theme", hint: "Action", icon: "sun", run: () => actions.toggleTheme() },
  ];

  function matches(text, query) {
    const hay = text.toLowerCase();
    return query.split(/\s+/).filter(Boolean).every((word) => hay.includes(word));
  }

  function compute() {
    const query = input.value.trim().toLowerCase();
    const boardItems = boards.map((b) => ({
      label: b.title, hint: `${b.tile_count} tile${b.tile_count === 1 ? "" : "s"}`, icon: "grid", run: () => actions.go(`d/${encodeURIComponent(b.id)}`),
    }));
    // Exact titles first, then titles that start with the query, then any match.
    const rank = (item) => {
      const label = item.label.toLowerCase();
      return label === query ? 0 : label.startsWith(query) ? 1 : 2;
    };
    const found = [...boardItems, ...pages]
      .filter((item) => !query || matches(item.label, query))
      .map((item, i) => ({ item, i }))
      .sort((a, b) => (query ? rank(a.item) - rank(b.item) : 0) || a.i - b.i)
      .map(({ item }) => item);
    const typed = input.value.trim();
    const free = typed.length >= 3
      ? [
          { label: `Ask: ${shorten(typed, 70)}`, hint: "Question", icon: "question", run: () => actions.ask(typed) },
          { label: `Build a dashboard: ${shorten(typed, 60)}`, hint: "AI", icon: "sparkle", run: () => actions.build(typed) },
        ]
      : [];
    // A typed question goes first only when nothing else matches well.
    items = found.length && typed.length < 25 ? [...found.slice(0, 8), ...free] : [...free, ...found.slice(0, 8)];
    index = Math.min(index, Math.max(0, items.length - 1));
    paint();
  }

  function paint() {
    list.replaceChildren(...items.map((item, i) => {
      const option = el("li", {
        class: "palette-item", role: "option", id: `palette-${i}`, "aria-selected": i === index ? "true" : "false",
        onclick: () => choose(i),
      }, [icon(item.icon), el("span", { class: "palette-label", text: item.label }), el("span", { class: "palette-hint", text: item.hint })]);
      option.addEventListener("pointermove", () => {
        if (index !== i) {
          index = i;
          paint();
        }
      });
      return option;
    }));
    if (!items.length) list.append(el("li", { class: "palette-empty", text: "Nothing matches. Type a few words to ask a question instead." }));
    input.setAttribute("aria-activedescendant", items.length ? `palette-${index}` : "");
    list.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
  }

  function choose(i) {
    const item = items[i];
    if (!item) return;
    close();
    item.run();
  }

  async function open() {
    returnFocus = document.activeElement;
    backdrop.hidden = false;
    input.value = "";
    index = 0;
    compute();
    input.focus();
    try {
      boards = await listDashboards();
      compute();
    } catch {
      // Pages still work without the list.
    }
  }

  function close() {
    backdrop.hidden = true;
    if (returnFocus instanceof HTMLElement) returnFocus.focus({ preventScroll: true });
  }

  input.addEventListener("input", () => { index = 0; compute(); });
  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!items.length) return;
      index = (index + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      paint();
    } else if (event.key === "Enter") {
      event.preventDefault();
      choose(index);
    } else if (event.key === "Escape") {
      event.preventDefault();
      close();
    }
  });
  backdrop.addEventListener("pointerdown", (event) => { if (event.target === backdrop) close(); });
  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      if (backdrop.hidden) open();
      else close();
    }
  });

  return { open, close };
}
