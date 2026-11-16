// The Ask view: a conversation of questions, each answered with a sentence, a figure and the
// agent's visible work. Answers come first; how they were found is one click away.

import { addTile, askStream, createDashboard, getSchema, listDashboards, runQuery } from "./api.js";
import { renderFigure } from "./charts.js";
import { clear, copyText, download, el, icon, toolButton } from "./dom.js";
import { chartTitle, describeStep, formatElapsed, formatPeriod, formatValue, shorten, splitHighlights, toCSV, waitingText } from "./lib.js";
import { closeMenu, openMenu } from "./menu.js";
import { addHistory } from "./store.js";
import { toast } from "./toast.js";

const EXAMPLE_GROUPS = [
  ["Revenue", ["Which 5 product categories have the highest revenue?", "What was the revenue for each month of 2017?"]],
  ["Customers", ["What percentage of customers ordered more than once?", "Which 5 customer cities generate the most revenue?"]],
  ["Delivery and reviews", ["What is the average review score per order for late deliveries?", "What percentage of delivered orders arrived late?"]],
];

const PLACEHOLDER = "Ask about orders, revenue, customers, delivery or reviews";
const HISTORY_TURNS = 3;

const FAILURES = {
  max_steps: "The agent could not find a working query within its attempts.",
  llm_error: "The model provider returned an error.",
};

// ---------------------------------------------------------------- pieces

function highlighted(text) {
  return splitHighlights(text).map((part) => (part.mark ? el("mark", { class: "figure-mark", text: part.text }) : part.text));
}

function stepItem(step) {
  return el("li", { class: step.ok ? "step" : "step step-failed" }, [
    el("span", { class: "step-mark", "aria-hidden": "true" }),
    el("div", { class: "step-body" }, [
      el("div", { class: "step-title", text: describeStep(step) }),
      step.error ? el("div", { class: "step-error", text: step.error }) : null,
      step.sql ? el("pre", { class: "sql", text: step.sql }) : null,
    ]),
  ]);
}

function pendingItem(call) {
  return el("li", { class: "step step-pending" }, [
    el("span", { class: "step-mark", "aria-hidden": "true" }),
    el("div", { class: "step-body" }, el("div", { class: "step-title", text: call === 1 ? "Writing a query" : "Reading the result" })),
  ]);
}

/** A sentence a person can act on, instead of the provider's raw error. */
function explainFailure(result) {
  const error = result.error || "";
  if (result.status !== "llm_error") return FAILURES[result.status] || "Something went wrong.";
  if (/HTTP 40[13]/.test(error)) return "The model provider rejected the API key. Check LLM_API_KEY in the server settings.";
  if (/HTTP 429/.test(error)) return "The model provider is rate limiting this key. Wait a minute and try again.";
  if (/Network error/.test(error)) return "The server could not reach the model provider. Check the connection and try again.";
  return FAILURES.llm_error;
}

function notice(text, tone, actions = []) {
  return el("div", { class: `notice notice-${tone}`, role: tone === "error" ? "alert" : null }, [
    el("p", { class: "notice-text", text }),
    actions.length ? el("div", { class: "notice-actions" }, actions) : null,
  ]);
}

function workSummary(result) {
  if (result.cached) return "Answered from the cache, no model call";
  const tokens = (result.input_tokens + result.output_tokens).toLocaleString("en-US");
  const failed = (result.steps || []).filter((s) => !s.ok).length;
  const parts = [
    `${result.llm_calls} model ${result.llm_calls === 1 ? "call" : "calls"}`,
    `${tokens} tokens`,
    formatElapsed(result.latency_ms),
  ];
  if (failed) parts.push(`${failed} failed ${failed === 1 ? "query" : "queries"} corrected`);
  return parts.join(", ");
}

// ------------------------------------------------------------------ view

export function createAskView({ onAsked, onOpen }) {
  // Home
  const facts = el("dl", { class: "facts", hidden: true });
  const intro = el("section", { class: "intro" }, [
    el("h1", { class: "intro-title", text: "Ask a question about 100,000 marketplace orders." }),
    el("p", {
      class: "intro-lead",
      text: "An agent writes the SQL, runs it on a read-only copy of the data, and shows each step so you can check its work.",
    }),
    facts,
    el("div", { class: "example-groups" }, EXAMPLE_GROUPS.map(([name, questions]) =>
      el("section", { class: "example-group" }, [
        el("h2", { class: "example-group-title", text: name }),
        el("ul", { class: "examples" }, questions.map((q) =>
          el("li", {}, el("button", { type: "button", class: "example", text: q, onclick: () => ask(q) })))),
      ]))),
  ]);

  getSchema()
    .then(({ facts: f }) => {
      if (!f) return;
      const items = [
        [formatValue(f.orders, "number", true), "orders"],
        [formatPeriod(f.first_date, f.last_date), "of purchases"],
        [String(f.categories), "product categories"],
        [formatValue(f.sellers, "number", true), "sellers"],
        [String(f.states), "Brazilian states"],
      ];
      facts.replaceChildren(...items.map(([value, label]) => el("div", { class: "fact" }, [el("dt", { text: label }), el("dd", { text: value })])));
      facts.hidden = false;
    })
    .catch(() => {});

  // Composer
  const thread = el("div", { class: "thread" });
  const input = el("input", {
    type: "text", class: "composer-input", maxLength: 500, autocomplete: "off", enterKeyHint: "send",
    placeholder: PLACEHOLDER, "aria-label": "Your question",
  });
  const submit = el("button", { type: "submit", class: "button button-primary", text: "Ask" });
  const stop = el("button", { type: "button", class: "button button-secondary", text: "Stop", hidden: true, onclick: () => controller?.abort() });
  const contextText = el("span", { class: "context-text" });
  const context = el("div", { class: "context", hidden: true }, [
    contextText,
    el("button", { type: "button", class: "tool", onclick: () => reset() }, [icon("close"), el("span", { class: "tool-label", text: "New conversation" })]),
  ]);
  const form = el("form", { class: "composer", onsubmit: (event) => { event.preventDefault(); ask(input.value); } }, [input, submit, stop]);
  const hint = el("p", { class: "composer-hint", text: "Press / to start typing. Press the up arrow to bring back your last question." });

  const node = el("div", { class: "view view-ask" }, [intro, thread, el("div", { class: "composer-bar" }, [context, form, hint])]);

  let turns = [];
  let busy = false;
  let controller = null;
  let lastQuestion = "";

  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowUp" && !input.value && lastQuestion) {
      event.preventDefault();
      input.value = lastQuestion;
    } else if (event.key === "Escape") {
      input.blur();
    }
  });

  function updateContext() {
    const count = Math.min(turns.length, HISTORY_TURNS);
    context.hidden = count === 0;
    contextText.textContent = count === 1
      ? `Follow-ups build on “${shorten(turns[0].question, 64)}”`
      : `Follow-ups build on the last ${count} questions`;
    input.placeholder = count ? "Ask a follow-up, such as “and for 2018?”" : PLACEHOLDER;
  }

  function reset() {
    if (busy) controller?.abort();
    turns = [];
    clear(thread);
    intro.hidden = false;
    updateContext();
    input.focus();
  }

  function setBusy(value) {
    busy = value;
    submit.hidden = value;
    stop.hidden = !value;
  }

  // -------------------------------------------------------------- result

  function workPanel(result, state, redraw) {
    const editor = el("textarea", { class: "sql-editor", rows: 4, spellcheck: false, value: state.sql, "aria-label": "Final SQL query" });
    const editNote = el("p", { class: "edit-note", hidden: true });
    const run = el("button", {
      type: "button", class: "button button-secondary", text: "Run edited query",
      onclick: async () => {
        run.disabled = true;
        try {
          const payload = await runQuery(editor.value);
          state.sql = payload.sql;
          state.payload = payload;
          redraw();
          editNote.className = "edit-note";
          editNote.textContent = `The figure now shows your edited query: ${payload.rows.length} ${payload.rows.length === 1 ? "row" : "rows"} in ${Math.round(payload.elapsed_ms)} ms. The answer sentence still describes the original.`;
        } catch (err) {
          editNote.className = "edit-note edit-error";
          editNote.textContent = err.message;
        }
        editNote.hidden = false;
        run.disabled = false;
      },
    });
    const copy = toolButton("copy", "Copy SQL", async () => {
      toast((await copyText(editor.value)) ? "SQL copied" : "Copy failed. Select the query and copy it by hand.");
    });

    return el("details", { class: "work" }, [
      el("summary", {}, [el("span", { class: "work-label", text: "How this was answered" }), el("span", { class: "work-meta", text: workSummary(result) })]),
      el("div", { class: "work-body" }, [
        result.cached ? null : el("ol", { class: "steps" }, (result.steps || []).map(stepItem)),
        el("h4", { class: "work-title", text: "Final query" }),
        editor,
        el("div", { class: "edit-actions" }, [run, copy, el("span", { class: "hint", text: "Only read-only SELECT statements run." })]),
        editNote,
      ]),
    ]);
  }

  function renderAnswered(body, question, result) {
    const state = { sql: result.sql, payload: result, addedTo: new Set() };

    async function addTo(dashboard) {
      const tileTitle = chartTitle(state.payload.chart) || shorten(question, 110);
      try {
        await addTile(dashboard.id, { title: tileTitle, sql: state.sql });
        state.addedTo.add(`${dashboard.id}\u0000${state.sql}`);
        syncPin();
        toast(`Added to \u201C${dashboard.title}\u201D`, { label: "Open", run: () => onOpen(`d/${dashboard.id}`) });
      } catch (err) {
        toast(err.message);
      }
    }

    async function chooseDashboard(anchor) {
      let dashboards;
      try {
        dashboards = await listDashboards();
      } catch (err) {
        toast(err.message);
        return;
      }
      const name = el("input", { class: "menu-input", maxLength: 120, placeholder: "New dashboard name", "aria-label": "New dashboard name" });
      const create = el("form", { class: "menu-form" }, [name, el("button", { type: "submit", class: "button button-primary", text: "Create" })]);
      create.addEventListener("submit", async (event) => {
        event.preventDefault();
        closeMenu();
        try {
          await addTo(await createDashboard(name.value.trim() || "Untitled dashboard"));
        } catch (err) {
          toast(err.message);
        }
      });
      const items = dashboards.map((d) => ({ label: d.title, onSelect: () => addTo(d) }));
      openMenu(anchor, [
        ...(items.length ? [el("p", { class: "menu-label", text: "Add to" }), ...items, "separator"] : []),
        el("p", { class: "menu-label", text: "Or create a dashboard" }),
        create,
      ], { label: "Add to dashboard" });
    }

    const pin = toolButton("bookmark", "Add to dashboard", () => chooseDashboard(pin), { "aria-haspopup": "menu", "aria-expanded": "false" });
    const syncPin = () => {
      const added = [...state.addedTo].some((key) => key.endsWith(`\u0000${state.sql}`));
      pin.setLabel(added ? "Added" : "Add to dashboard");
      pin.setAttribute("aria-pressed", added ? "true" : "false");
    };
    const csv = toolButton("download", "CSV", () => download("ask-olist.csv", toCSV(state.payload.columns, state.payload.rows)), {
      "aria-label": "Download the result as CSV",
    });
    const slot = el("div", { class: "figure-slot" });
    const redraw = () => {
      slot.replaceChildren(renderFigure(state.payload, { tools: [pin, csv] }));
      syncPin();
    };
    redraw();

    body.append(el("p", { class: "answer" }, highlighted(result.answer || "Done.")), slot, workPanel(result, state, redraw));
    if (result.follow_ups?.length) {
      body.append(
        el("div", { class: "followups" }, [
          el("p", { class: "followups-label", text: "Ask next" }),
          el("ul", { class: "followup-list" }, result.follow_ups.map((q) =>
            el("li", {}, el("button", { type: "button", class: "example example-small", text: q, onclick: () => ask(q) })))),
        ]),
      );
    }
  }

  function renderResult(entry, body, question, result) {
    clear(body);
    if (result.status === "answered") {
      renderAnswered(body, question, result);
      return;
    }
    if (result.status === "cannot_answer") {
      body.append(notice(result.answer || "The data cannot answer this question.", "info", [
        el("button", { type: "button", class: "tool", text: "See what the data covers", onclick: () => onOpen("data") }),
      ]));
      return;
    }
    body.append(notice(explainFailure(result), "error", [retryButton(entry, question)]));
    if (result.error) {
      body.append(el("details", { class: "work" }, [
        el("summary", {}, el("span", { class: "work-label", text: "Technical details" })),
        el("div", { class: "work-body" }, el("pre", { class: "sql", text: result.error })),
      ]));
    }
    if (result.steps?.length) {
      body.append(el("details", { class: "work" }, [
        el("summary", {}, [el("span", { class: "work-label", text: "What the agent tried" }), el("span", { class: "work-meta", text: workSummary(result) })]),
        el("div", { class: "work-body" }, el("ol", { class: "steps" }, result.steps.map(stepItem))),
      ]));
    }
  }

  function retryButton(entry, question) {
    return el("button", { type: "button", class: "tool", onclick: () => { entry.remove(); ask(question); } }, [
      icon("retry"), el("span", { class: "tool-label", text: "Try again" }),
    ]);
  }

  // ----------------------------------------------------------------- ask

  async function ask(raw) {
    const question = String(raw || "").trim();
    if (!question || busy) return;
    setBusy(true);
    intro.hidden = true;
    input.value = "";
    lastQuestion = question;

    const live = el("ol", { class: "steps" });
    const clock = el("p", { class: "progress-clock", text: "Starting" });
    const progress = el("div", { class: "progress", "aria-live": "polite" }, [live, clock]);
    const body = el("div", { class: "entry-body" });
    const entry = el("article", { class: "entry", "aria-busy": "true" }, [el("h2", { class: "question", text: question }), progress, body]);
    thread.append(entry);
    entry.scrollIntoView({ behavior: "smooth", block: "start" });

    const started = performance.now();
    const timer = setInterval(() => { clock.textContent = `Working for ${formatElapsed(performance.now() - started)}`; }, 100);
    controller = new AbortController();
    let pending = null;

    try {
      const result = await askStream(question, turns.slice(-HISTORY_TURNS), ({ event, data }) => {
        if (event === "thinking") {
          // Reuse the pending row so retries never stack spinners.
          if (!pending) {
            pending = pendingItem(data.call);
            live.append(pending);
          }
        } else if (event === "waiting") {
          pending?.querySelector(".step-title")?.replaceChildren(waitingText(data));
        } else if (event === "retry") {
          pending?.querySelector(".step-title")?.replaceChildren("The model's reply was not a valid step; asking again");
        } else if (event === "step") {
          const item = stepItem(data.step);
          if (pending) pending.replaceWith(item);
          else live.append(item);
          pending = null;
        }
      }, controller.signal);
      progress.remove();
      renderResult(entry, body, question, result);
      if (result.status === "answered") {
        turns.push({ question, sql: result.sql });
        updateContext();
      }
      addHistory(question);
      onAsked();
    } catch (err) {
      progress.remove();
      if (err.name === "AbortError") {
        body.replaceChildren(notice("Stopped. This question was not answered.", "info", [retryButton(entry, question)]));
      } else {
        const actions = [retryButton(entry, question)];
        if (err.status === 429) actions.push(el("button", { type: "button", class: "tool", text: "Open the overview", onclick: () => onOpen("overview") }));
        body.replaceChildren(notice(err.message, "error", actions));
      }
    } finally {
      clearInterval(timer);
      entry.removeAttribute("aria-busy");
      controller = null;
      setBusy(false);
      input.focus({ preventScroll: true });
    }
  }

  /** Start a new conversation from a dashboard tile: the tile becomes the first turn. */
  function seed({ title, sql, payload }) {
    if (busy) controller?.abort();
    clear(thread);
    intro.hidden = true;
    const entry = el("article", { class: "entry" }, [
      el("p", { class: "entry-source", text: "From your dashboards" }),
      el("h2", { class: "question", text: title }),
      el("div", { class: "entry-body" }, renderFigure(payload, { title: "" })),
    ]);
    thread.append(entry);
    turns = [{ question: title, sql }];
    updateContext();
    requestAnimationFrame(() => {
      entry.scrollIntoView({ block: "start" });
      input.focus({ preventScroll: true });
    });
  }

  /** Ask a question on its own, outside the current conversation (used by Recent questions). */
  function askFresh(question) {
    if (busy) return;
    if (turns.length || thread.childElementCount) reset();
    ask(question);
  }

  return { node, ask, askFresh, seed, reset, focus: () => input.focus() };
}
