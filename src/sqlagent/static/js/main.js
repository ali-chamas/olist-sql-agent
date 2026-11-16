// App shell: navigation, recent questions, quota meter, theme, shortcuts, and the four views.

import { addTile, createDashboard, getSchema, getStatus } from "./api.js";
import { createAskView } from "./ask.js";
import { createOverviewView } from "./boards.js";
import { clear, el } from "./dom.js";
import { parseRoute, shorten } from "./lib.js";
import { clearHistory, clearPins, getHistory, getPins, getTheme, setTheme } from "./store.js";
import { toast } from "./toast.js";
import { createDashboardsView, createDashboardView } from "./workspace.js";

const main = document.getElementById("main");
const nav = document.getElementById("nav");
const recent = document.getElementById("recent");
const recentSection = document.getElementById("recent-section");
const footer = document.getElementById("rail-footer");

// ------------------------------------------------------------------- schema

function createSchemaView() {
  const body = el("div", {});
  const node = el("div", { class: "view" }, [
    el("header", { class: "view-head" }, [
      el("h1", { class: "view-title", text: "Data" }),
      el("p", { class: "view-lead", text: "The five tables the agent can read, with the same descriptions the model is given." }),
    ]),
    body,
  ]);
  let loaded = false;
  async function show() {
    if (loaded) return;
    try {
      const schema = await getSchema();
      clear(body).append(
        el("section", { class: "notes" }, [
          el("h2", { class: "section-title", text: "Things to know about this data" }),
          el("ul", {}, schema.notes.map((note) => el("li", { text: note }))),
        ]),
        el("nav", { class: "toc", "aria-label": "Tables" }, schema.tables.map((t) => el("a", { href: `#table-${t.name}`, class: "toc-link", text: t.name, onclick: (event) => {
          event.preventDefault();
          document.getElementById(`table-${t.name}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
        } }))),
        ...schema.tables.map((table) =>
          el("section", { class: "schema-table", id: `table-${table.name}` }, [
            el("h2", { class: "section-title schema-name", text: table.name }),
            el("p", { class: "view-lead", text: table.description }),
            el("div", { class: "table-wrap" }, el("table", {}, [
              el("thead", {}, el("tr", {}, ["Column", "Type", "Meaning"].map((h) => el("th", { text: h, scope: "col" })))),
              el("tbody", {}, table.columns.map((c) =>
                el("tr", {}, [el("td", { class: "mono", text: c.name }), el("td", { class: "muted", text: c.type.toLowerCase() }), el("td", { text: c.description })]))),
            ])),
          ])),
      );
      loaded = true;
    } catch (err) {
      clear(body).append(el("p", { class: "edit-error", text: err.message }));
    }
  }
  return { node, show };
}

// -------------------------------------------------------------------- shell

const NAV = [
  ["ask", "Ask"],
  ["dashboards", "Dashboards"],
  ["data", "Data"],
];
const TITLES = { ask: "Ask Olist", dashboards: "Dashboards", overview: "Marketplace overview", data: "Data" };

/** Go to a route such as "ask" or "d/d_123". Renders at once, so callers can act on the new view. */
function go(path) {
  if (location.hash !== `#/${path}`) history.pushState(null, "", `#/${path}`);
  render();
}

function explore(tileInfo) {
  go("ask");
  views.ask.seed(tileInfo);
}

let boardOptions = null; // highlight or focus requests for the next dashboard render

const views = {
  ask: createAskView({ onAsked: () => { renderRecent(); refreshStatus(); }, onOpen: go }),
  dashboards: createDashboardsView({ onOpen: (id, options) => { boardOptions = options; go(`d/${encodeURIComponent(id)}`); } }),
  board: createDashboardView({ onAskAbout: explore, onMissing: () => go("dashboards") }),
  overview: createOverviewView({ onExplore: explore }),
  data: createSchemaView(),
};

function currentRoute() {
  const [name, id] = parseRoute(location.hash);
  if (name === "d" && id) return { view: "board", section: "dashboards", id };
  if (name === "pinned") return { view: "dashboards", section: "dashboards" }; // old link
  if (name === "overview") return { view: "overview", section: "dashboards" };
  if (name && views[name] && name !== "board") return { view: name, section: name };
  return { view: "ask", section: "ask" };
}

function render() {
  const route = currentRoute();
  for (const link of nav.querySelectorAll("a")) {
    if (link.dataset.route === route.section) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  const view = views[route.view];
  if (main.firstElementChild !== view.node) {
    main.replaceChildren(view.node);
    window.scrollTo(0, 0);
  }
  if (route.view === "board") {
    view.show(route.id, boardOptions || {});
    boardOptions = null;
  }
  else {
    view.show?.();
    document.title = route.view === "ask" ? "Ask Olist" : `${TITLES[route.view]} | Ask Olist`;
  }
}

/** Pins saved in this browser by earlier versions become a server-side dashboard, once. */
async function migratePins() {
  const pins = getPins();
  if (!pins.length) return;
  try {
    const board = await createDashboard("Pinned answers", "Answers pinned before dashboards were saved on the server.");
    for (const pin of pins.slice().reverse()) {
      try {
        await addTile(board.id, { title: pin.title.slice(0, 120), sql: pin.sql });
      } catch {
        // A pin whose query no longer runs is dropped.
      }
    }
    clearPins();
    toast("Your pinned answers are now a dashboard", { label: "Open", run: () => go(`d/${board.id}`) });
  } catch {
    // Server unreachable: try again on the next visit.
  }
}

function renderRecent() {
  const items = getHistory();
  recentSection.hidden = items.length === 0;
  clear(recent).append(
    ...items.slice(0, 12).map((q) =>
      el("li", {}, el("button", { type: "button", class: "recent-item", text: shorten(q, 70), title: q, onclick: () => { go("ask"); views.ask.askFresh(q); } }))),
  );
}

async function refreshStatus() {
  try {
    const status = await getStatus();
    const parts = [el("div", { class: "model", text: `Model: ${status.model}` })];
    if (status.budget.limit > 0) {
      const share = Math.min(100, Math.round((100 * status.budget.used) / status.budget.limit));
      const fill = el("span", { class: "meter-fill" });
      fill.style.width = `${share}%`;
      parts.push(
        el("div", { class: "quota" }, [
          el("div", { class: "quota-label", text: `Today's model quota: ${share}% used` }),
          el("div", { class: "meter", role: "meter", "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": share, "aria-label": "Model quota used today" }, fill),
        ]),
      );
    }
    document.getElementById("status").replaceChildren(...parts);
  } catch {
    // The status panel is optional.
  }
}

function applyTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
}

function setupTheme() {
  applyTheme(getTheme());
  const button = el("button", { type: "button", class: "tool theme-toggle" });
  const isDark = () => (document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")) === "dark";
  const label = () => { button.textContent = isDark() ? "Use light theme" : "Use dark theme"; };
  button.addEventListener("click", () => {
    const next = isDark() ? "light" : "dark";
    setTheme(next);
    applyTheme(next);
    label();
  });
  label();
  footer.append(button);
}

function typingInField(target) {
  return target instanceof HTMLElement && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName));
}

nav.append(...NAV.map(([route, label]) =>
  el("a", { href: `#/${route}`, class: "nav-link", "data-route": route }, el("span", { text: label }))));

document.getElementById("clear-recent").addEventListener("click", () => { clearHistory(); renderRecent(); });
window.addEventListener("hashchange", render);
window.addEventListener("storage", renderRecent);
document.addEventListener("keydown", (event) => {
  if (event.key === "/" && !event.metaKey && !event.ctrlKey && !event.altKey && !typingInField(event.target)) {
    event.preventDefault();
    go("ask");
    views.ask.focus();
  }
});

setupTheme();
renderRecent();
refreshStatus();
render();
migratePins();
