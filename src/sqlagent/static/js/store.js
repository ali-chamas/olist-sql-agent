// Per-browser state in localStorage: recent questions, pinned tiles and the theme.
// Storage can be unavailable or full, so every access is guarded and the app works without it.

const KEYS = { history: "askolist.history", pins: "askolist.pins", theme: "askolist.theme" };
const MAX_HISTORY = 30;
const MAX_PINS = 24;

function read(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch {
    return fallback;
  }
}

function write(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Ignore: the state simply does not persist.
  }
}

export function getHistory() {
  const items = read(KEYS.history, []);
  return Array.isArray(items) ? items : [];
}

export function addHistory(question) {
  const items = getHistory().filter((q) => q !== question);
  items.unshift(question);
  write(KEYS.history, items.slice(0, MAX_HISTORY));
}

export function clearHistory() {
  write(KEYS.history, []);
}

/** Pins from before dashboards were saved on the server. Read once, migrated, then cleared. */
export function clearPins() {
  write(KEYS.pins, []);
}

export function getPins() {
  const items = read(KEYS.pins, []);
  return Array.isArray(items) ? items.filter((p) => p && p.sql && p.title) : [];
}

export function isPinned(sql) {
  return getPins().some((p) => p.sql === sql);
}

function pinsChanged() {
  window.dispatchEvent(new CustomEvent("pins-changed"));
}

export function addPin(title, sql) {
  if (isPinned(sql)) return;
  write(KEYS.pins, [{ id: `${Date.now()}`, title, sql }, ...getPins()].slice(0, MAX_PINS));
  pinsChanged();
}

export function removePin(sql) {
  write(KEYS.pins, getPins().filter((p) => p.sql !== sql));
  pinsChanged();
}

export function getTheme() {
  const value = read(KEYS.theme, null);
  return value === "light" || value === "dark" ? value : null;
}

export function setTheme(value) {
  write(KEYS.theme, value);
}
