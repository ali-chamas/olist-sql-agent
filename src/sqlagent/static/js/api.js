import { createSSEParser } from "./lib.js";

const OFFLINE = "Cannot reach the Ask Olist server. Check that it is running, then try again.";

/** An Error carrying the HTTP status and the server's message. */
async function responseError(response) {
  let message = `Request failed (HTTP ${response.status}).`;
  try {
    const body = await response.json();
    if (body.error) message = body.error;
  } catch {
    // Keep the generic message.
  }
  return Object.assign(new Error(message), { status: response.status });
}

/** fetch, with network failures turned into a sentence a person can act on. Aborts pass through. */
async function request(path, options) {
  try {
    return await fetch(path, options);
  } catch (err) {
    if (err.name === "AbortError") throw err;
    throw Object.assign(new Error(OFFLINE), { status: 0 });
  }
}

async function getJSON(path) {
  const response = await request(path);
  if (!response.ok) throw await responseError(response);
  return response.json();
}

function post(path, body, signal) {
  return request(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal });
}

/** Any JSON call. Resolves to the parsed body, or null for 204. */
async function send(method, path, body) {
  const options = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) options.body = JSON.stringify(body);
  const response = await request(path, options);
  if (!response.ok) throw await responseError(response);
  return response.status === 204 ? null : response.json();
}

/** POST, then read a server-sent event stream. onEvent gets the progress events; resolves with the "result" event. */
async function stream(path, body, onEvent, signal) {
  const response = await post(path, body, signal);
  if (!response.ok) throw await responseError(response);
  const parse = createSSEParser();
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let result = null;
  for (;;) {
    let chunk;
    try {
      chunk = await reader.read();
    } catch (err) {
      if (err.name === "AbortError") throw err;
      throw Object.assign(new Error("The connection to the server was lost before the answer arrived."), { status: 0 });
    }
    const { value, done } = chunk;
    if (done) break;
    for (const item of parse(decoder.decode(value, { stream: true }))) {
      if (item.event === "result") result = item.data;
      else onEvent(item);
    }
  }
  if (!result) throw new Error("The connection closed before an answer arrived.");
  return result;
}

const e = encodeURIComponent;

export const getStatus = () => getJSON("/api/status");
export const getSchema = () => getJSON("/api/schema");
export const getOverview = (year) => getJSON(`/api/dashboard?year=${e(year)}`);

/** Run read-only SQL without a model call. Throws an Error carrying the guardrail message. */
export async function runQuery(sql) {
  const response = await post("/api/query", { sql });
  if (!response.ok) throw await responseError(response);
  return response.json();
}

/** Ask a question; progress events go to onEvent; aborting the signal rejects with an AbortError. */
export const askStream = (question, history, onEvent, signal) => stream("/api/ask/stream", { question, history }, onEvent, signal);

// ------------------------------------------------------- saved dashboards

const board = (id) => `/api/dashboards/${e(id)}`;
const tile = (id, tileId) => `${board(id)}/tiles/${e(tileId)}`;

export const listDashboards = () => getJSON("/api/dashboards").then((r) => r.dashboards);
export const getDashboard = (id) => getJSON(board(id));
export const createDashboard = (title, description = "") => send("POST", "/api/dashboards", { title, description });
export const updateDashboard = (id, fields) => send("PATCH", board(id), fields);
export const deleteDashboard = (id) => send("DELETE", board(id));
export const getRevisions = (id) => getJSON(`${board(id)}/revisions`).then((r) => r.revisions);
export const restoreVersion = (id, version) => send("POST", `${board(id)}/restore`, { version });

export const addTile = (id, fields) => send("POST", `${board(id)}/tiles`, fields);
export const updateTile = (id, tileId, fields) => send("PATCH", tile(id, tileId), fields);
export const removeTile = (id, tileId, version) => send("DELETE", `${tile(id, tileId)}?version=${e(version)}`);
export const moveTile = (id, tileId, position, version) => send("POST", `${tile(id, tileId)}/move`, { position, version });
export const getTileData = (id, tileId) => getJSON(`${tile(id, tileId)}/data`);

/** Ask the AI to change a dashboard. Resolves with {status, message, summary, changes, dashboard, saved}. */
export const editWithAI = (id, prompt, { tileId, version } = {}, onEvent, signal) =>
  stream(`${board(id)}/agent`, { prompt, tile_id: tileId, version }, onEvent, signal);

/** Ask the AI to build a new dashboard from a description. */
export const buildWithAI = (prompt, onEvent, signal) => stream("/api/dashboards/build", { prompt }, onEvent, signal);
