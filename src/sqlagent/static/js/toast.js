// Short confirmations ("SQL copied", "Pinned"), announced politely to screen readers.

import { el } from "./dom.js";

const DURATION_MS = 4000;
const MAX_VISIBLE = 2;

export function toast(message, action = null) {
  const region = document.getElementById("toasts");
  if (!region) return;
  const node = el("div", { class: "toast" }, [
    el("span", { text: message }),
    action
      ? el("button", {
          type: "button", class: "toast-action", text: action.label,
          onclick: () => { action.run(); node.remove(); },
        })
      : null,
  ]);
  region.append(node);
  // Keep at most two on screen: the newest message is the one that matters.
  while (region.children.length > MAX_VISIBLE) region.firstElementChild.remove();
  // A toast with an action (Undo) stays longer, and does not leave while the pointer is on it.
  let timer = setTimeout(() => node.remove(), action ? DURATION_MS * 1.75 : DURATION_MS);
  node.addEventListener("pointerenter", () => clearTimeout(timer));
  node.addEventListener("pointerleave", () => { timer = setTimeout(() => node.remove(), 1500); });
}
