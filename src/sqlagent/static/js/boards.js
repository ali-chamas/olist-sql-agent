// The built-in overview: fixed queries, no model calls, a year filter with comparisons.
// Any tile can start a conversation in Ask, with its query as the context.

import { copyOverview, getOverview } from "./api.js";
import { renderFigure } from "./charts.js";
import { clear, el, toolButton } from "./dom.js";
import { toast } from "./toast.js";

function tile(title, payload, { wide = false, note = "", sql = "", onExplore = null, onRemove = null } = {}) {
  if (payload.error) {
    return el("section", { class: "figure tile" }, [
      el("header", { class: "figure-head" }, el("h3", { class: "figure-title", text: title })),
      el("p", { class: "edit-error", text: payload.error }),
      onRemove ? el("div", { class: "figure-tools" }, toolButton("close", "Remove", onRemove)) : null,
    ]);
  }
  const tools = [];
  if (onExplore) tools.push(toolButton("chat", "Ask", () => onExplore({ title, sql, payload }), { "aria-label": `Ask a follow-up about ${title}` }));
  if (onRemove) tools.push(toolButton("close", "Remove", onRemove, { "aria-label": `Remove ${title}` }));
  return renderFigure(payload, { title, note, tools, className: wide ? "tile tile-wide" : "tile" });
}

export function createOverviewView({ onExplore, onOpen }) {
  const copy = toolButton("duplicate", "Make an editable copy", async () => {
    copy.disabled = true;
    try {
      const board = await copyOverview();
      window.dispatchEvent(new Event("boards:changed"));
      toast("Copied the overview. Change it in words or by hand, and add filters.");
      onOpen(board.id);
    } catch (err) {
      toast(err.message);
    } finally {
      copy.disabled = false;
    }
  });
  const grid = el("div", { class: "tiles" });
  const filter = el("div", { class: "segmented", role: "group", "aria-label": "Year" });
  const node = el("div", { class: "view view-wide" }, [
    el("header", { class: "view-head" }, [
      el("div", { class: "view-head-row" }, [
        el("a", { class: "back-link", href: "#/dashboards", text: "All dashboards" }),
        copy,
      ]),
      el("h1", { class: "view-title", text: "Marketplace overview" }),
      el("p", { class: "view-lead", text: "The marketplace at a glance. These are fixed queries, so this page uses no model tokens. Choose Ask on any chart to dig into it." }),
    ]),
    el("div", { class: "filters" }, [el("span", { class: "filters-label", text: "Year" }), filter]),
    grid,
  ]);
  let current = "all";
  let loaded = false;
  let latest = 0;

  async function load(year) {
    current = year;
    const ticket = ++latest; // only the most recent request may render
    for (const button of filter.querySelectorAll(".seg")) {
      button.setAttribute("aria-pressed", button.dataset.year === year ? "true" : "false");
    }
    grid.classList.add("is-loading"); // keep the previous render while the new one loads
    grid.setAttribute("aria-busy", "true");
    try {
      const board = await getOverview(year);
      if (ticket !== latest) return;
      clear(filter).append(
        ...board.years.map((y) =>
          el("button", {
            type: "button", class: "seg", text: y === "all" ? "All years" : y, "data-year": y,
            "aria-pressed": y === board.year ? "true" : "false", onclick: () => load(y),
          })),
      );
      clear(grid).append(...board.tiles.map((t) => tile(t.title, t, { wide: t.wide, note: t.note, sql: t.sql, onExplore })));
      loaded = true;
    } catch (err) {
      if (ticket !== latest) return;
      clear(grid).append(el("p", { class: "edit-error", text: err.message }));
    }
    grid.classList.remove("is-loading");
    grid.removeAttribute("aria-busy");
  }

  return { node, show: () => { if (!loaded) load(current); } };
}
