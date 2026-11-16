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
