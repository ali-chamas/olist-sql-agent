// Small DOM helpers. All text reaches the page through textContent, never innerHTML.

export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null) continue;
    if (value === false && !(key in node)) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key in node && key !== "list") node[key] = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of [].concat(children)) {
    if (child !== null && child !== undefined && child !== false) node.append(child);
  }
  return node;
}

const SVG_NS = "http://www.w3.org/2000/svg";

export function svg(tag, attrs = {}, children = []) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null) continue;
    if (key === "text") node.textContent = value;
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) if (child) node.append(child);
  return node;
}

export function clear(node) {
  node.replaceChildren();
  return node;
}

export function download(filename, text, type = "text/csv") {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const link = el("a", { href: url, download: filename });
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

// 16px line icons, drawn with the current text colour.
const ICONS = {
  bookmark: "M4.5 2.5h7v11L8 10.75 4.5 13.5z",
  download: "M8 2.5v7.5M4.75 7 8 10.25 11.25 7M3 13.5h10",
  copy: "M6 6h7.5v7.5H6zM3.5 10.5V3.5a1 1 0 0 1 1-1h6",
  chat: "M3 3.5h10v7H7.5L4.5 13v-2.5H3z",
  close: "M4.5 4.5l7 7M11.5 4.5l-7 7",
  retry: "M12.5 8a4.5 4.5 0 1 1-1.3-3.2M12.5 2.75V5.5H9.75",
  plus: "M8 3v10M3 8h10",
  more: "M3.5 8h.01M8 8h.01M12.5 8h.01",
  sparkle: "M8 2.5l1.3 3.2 3.2 1.3-3.2 1.3L8 11.5 6.7 8.3 3.5 7l3.2-1.3zM12.5 11v3M11 12.5h3",
  grid: "M2.75 2.75h4.5v4.5h-4.5zM8.75 2.75h4.5v4.5h-4.5zM2.75 8.75h4.5v4.5h-4.5zM8.75 8.75h4.5v4.5h-4.5z",
  question: "M3 3.5h10v7H8l-3 2.5v-2.5H3zM8 5.25v2.5M8 9.25h.01",
  table: "M2.5 3h11v10h-11zM2.5 6.5h11M2.5 10h11M6.5 3v10",
  filter: "M2.5 3.5h11L9.25 8.5v4L6.75 13.5v-5z",
  play: "M5 3.5v9l7-4.5z",
  print: "M4.5 6V2.5h7V6M4.5 11H3V6h10v5h-1.5M4.5 9h7v4.5h-7z",
  duplicate: "M5.5 5.5h8v8h-8zM10.5 5.5v-3h-8v8h3",
  text: "M3 3.5h10M8 3.5v9M5.5 12.5h5",
  search: "M7 12a5 5 0 1 0 0-10 5 5 0 0 0 0 10zM10.75 10.75 13.5 13.5",
  drag: "M6 3.5h.01M10 3.5h.01M6 8h.01M10 8h.01M6 12.5h.01M10 12.5h.01",
  sun: "M8 10.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5zM8 1.5v1.5M8 13v1.5M1.5 8H3M13 8h1.5M3.4 3.4l1 1M11.6 11.6l1 1M3.4 12.6l1-1M11.6 4.4l1-1",
  moon: "M13 9.5A5.5 5.5 0 1 1 6.5 3a4.5 4.5 0 0 0 6.5 6.5z",
  database: "M3 4c0-1 2.2-1.75 5-1.75S13 3 13 4v8c0 1-2.2 1.75-5 1.75S3 13 3 12zM3 4c0 1 2.2 1.75 5 1.75S13 5 13 4M3 8c0 1 2.2 1.75 5 1.75S13 9 13 8",
};

export function icon(name) {
  return svg("svg", { class: "icon", width: 16, height: 16, viewBox: "0 0 16 16", "aria-hidden": "true", focusable: "false" }, [
    svg("path", { d: ICONS[name], fill: "none", stroke: "currentColor", "stroke-width": 1.5, "stroke-linecap": "round", "stroke-linejoin": "round" }),
  ]);
}

/** A small button with an icon and a visible label. */
export function toolButton(iconName, label, onclick, props = {}) {
  const text = el("span", { class: "tool-label", text: label });
  const button = el("button", { type: "button", class: "tool", onclick, ...props }, [icon(iconName), text]);
  button.setLabel = (value) => { text.textContent = value; };
  return button;
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = el("textarea", { value: text, readOnly: true, class: "visually-hidden" });
    document.body.append(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  }
}
