// A small popover menu anchored to a button. One open at a time; closes on outside click,
// Escape, or after an item is chosen. Arrow keys move between items.

import { el } from "./dom.js";

let open = null;

function close(restoreFocus = false) {
  if (!open) return;
  const { node, anchor, onDocument, onKey } = open;
  node.remove();
  anchor.setAttribute("aria-expanded", "false");
  document.removeEventListener("pointerdown", onDocument, true);
  document.removeEventListener("keydown", onKey, true);
  open = null;
  if (restoreFocus) anchor.focus();
}

/**
 * items: [{ label, onSelect, danger?, disabled? }, "separator", or a DOM node for custom content]
 */
export function openMenu(anchor, items, { label = "Menu" } = {}) {
  const wasOpen = open?.anchor === anchor;
  close();
  if (wasOpen) return;

  const node = el("div", { class: "menu", role: "menu", "aria-label": label });
  for (const item of items) {
    if (item === "separator") node.append(el("div", { class: "menu-separator", role: "separator" }));
    else if (item instanceof Node) node.append(item);
    else {
      node.append(el("button", {
        type: "button", role: "menuitem", class: item.danger ? "menu-item menu-danger" : "menu-item",
        text: item.label, disabled: Boolean(item.disabled),
        onclick: () => { close(); item.onSelect(); },
      }));
    }
  }
  document.body.append(node);

  const box = anchor.getBoundingClientRect();
  const width = node.offsetWidth;
  const left = Math.min(Math.max(8, box.right - width), window.innerWidth - width - 8);
  const below = box.bottom + 6 + node.offsetHeight < window.innerHeight;
  node.style.left = `${left + window.scrollX}px`;
  node.style.top = `${(below ? box.bottom + 6 : box.top - node.offsetHeight - 6) + window.scrollY}px`;
  anchor.setAttribute("aria-expanded", "true");

  const focusables = () => [...node.querySelectorAll("button:not([disabled]), input")];
  const onDocument = (event) => {
    if (!node.contains(event.target) && !anchor.contains(event.target)) close();
  };
  const onKey = (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      close(true);
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      const list = focusables();
      const index = list.indexOf(document.activeElement);
      const next = event.key === "ArrowDown" ? index + 1 : index - 1;
      list[(next + list.length) % list.length]?.focus();
      event.preventDefault();
    }
  };
  document.addEventListener("pointerdown", onDocument, true);
  document.addEventListener("keydown", onKey, true);
  open = { node, anchor, onDocument, onKey };
  focusables()[0]?.focus();
  return node;
}

export function closeMenu() {
  close();
}
