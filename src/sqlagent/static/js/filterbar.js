// Dashboard filters: period, customer state and product category.
// Changing a filter redraws every tile at once; "Save as default" stores them on the dashboard.

import { el } from "./dom.js";
import { cleanFilters, formatMonth, monthRange, periodPresets, sameFilters } from "./lib.js";

const CUSTOM = "custom";

/**
 * @param {{ onChange: (filters: object) => void, onSave: (filters: object) => void }} handlers
 */
export function createFilterBar({ onChange, onSave }) {
  const period = el("select", { class: "filter-select", "aria-label": "Period" });
  const from = el("select", { class: "filter-select filter-month", "aria-label": "First month" });
  const to = el("select", { class: "filter-select filter-month", "aria-label": "Last month" });
  const custom = el("span", { class: "filter-custom", hidden: true }, [from, el("span", { class: "filter-dash", text: "to" }), to]);
  const state = el("select", { class: "filter-select", "aria-label": "Customer state" });
  const category = el("select", { class: "filter-select", "aria-label": "Product category" });
  const reset = el("button", { type: "button", class: "tool filter-reset", text: "Clear filters", hidden: true });
  const save = el("button", { type: "button", class: "tool filter-save", text: "Save as default", hidden: true });
  const field = (label, control) => el("label", { class: "filter" }, [el("span", { class: "filter-label", text: label }), control]);
  const node = el("div", { class: "filter-bar", role: "group", "aria-label": "Dashboard filters" }, [
    field("Period", period),
    custom,
    field("State", state),
    field("Category", category),
    el("span", { class: "filter-actions" }, [reset, save]),
  ]);

  let presets = [];
  let active = {};
  let saved = {};
  let ready = false;

  function setOptions(options) {
    presets = periodPresets(options.first_month, options.last_month);
    period.replaceChildren(
      ...presets.map((p) => el("option", { value: p.id, text: p.label })),
      el("option", { value: CUSTOM, text: "Custom range" }),
    );
    const months = monthRange(options.first_month, options.last_month);
    for (const select of [from, to]) {
      select.replaceChildren(...months.map((m) => el("option", { value: m, text: formatMonth(m) })));
    }
    state.replaceChildren(el("option", { value: "", text: "All states" }), ...options.states.map((s) => el("option", { value: s, text: s })));
    category.replaceChildren(el("option", { value: "", text: "All categories" }), ...options.categories.map((c) => el("option", { value: c, text: c })));
    ready = true;
    paint();
  }

  /** Show these filters; ``savedFilters`` are the dashboard's defaults. */
  function set(filters, savedFilters) {
    active = cleanFilters(filters);
    saved = cleanFilters(savedFilters);
    paint();
  }

  function paint() {
    if (!ready) return;
    const preset = presets.find((p) => (p.date_from || "") === (active.date_from || "") && (p.date_to || "") === (active.date_to || ""));
    const isCustom = !preset;
    period.value = isCustom ? CUSTOM : preset.id;
    custom.hidden = !isCustom;
    if (isCustom) {
      from.value = active.date_from || from.options[0]?.value;
      to.value = active.date_to || to.options[to.options.length - 1]?.value;
    }
    state.value = active.state || "";
    category.value = active.category || "";
    const any = Object.keys(active).length > 0;
    reset.hidden = !any;
    save.hidden = sameFilters(active, saved);
    node.classList.toggle("filter-bar-active", any);
    for (const [select, on] of [[period, Boolean(active.date_from || active.date_to)], [state, Boolean(active.state)], [category, Boolean(active.category)]]) {
      select.closest(".filter")?.classList.toggle("filter-on", on);
    }
  }

  function change(next) {
    active = cleanFilters(next);
    paint();
    onChange(active);
  }

  period.addEventListener("change", () => {
    if (period.value === CUSTOM) {
      custom.hidden = false;
      change({ ...active, date_from: from.value || from.options[0]?.value, date_to: to.value || to.options[to.options.length - 1]?.value });
      return;
    }
    const preset = presets.find((p) => p.id === period.value);
    change({ ...active, date_from: preset.date_from, date_to: preset.date_to });
  });
  from.addEventListener("change", () => {
    if (to.value && from.value > to.value) to.value = from.value;
    change({ ...active, date_from: from.value, date_to: to.value });
  });
  to.addEventListener("change", () => {
    if (from.value && to.value < from.value) from.value = to.value;
    change({ ...active, date_from: from.value, date_to: to.value });
  });
  state.addEventListener("change", () => change({ ...active, state: state.value }));
  category.addEventListener("change", () => change({ ...active, category: category.value }));
  reset.addEventListener("click", () => change({}));
  save.addEventListener("click", () => onSave(active));

  return { node, setOptions, set, get: () => ({ ...active }) };
}
