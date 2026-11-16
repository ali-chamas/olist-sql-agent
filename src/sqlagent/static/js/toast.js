// Short confirmations ("SQL copied", "Pinned"), announced politely to screen readers.

import { el } from "./dom.js";

const DURATION_MS = 4000;

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
  setTimeout(() => node.remove(), DURATION_MS);
}
