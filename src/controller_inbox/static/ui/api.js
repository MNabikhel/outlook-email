/* Talking to the CloseDesk server. Every request carries the X-CloseDesk header the server requires. */

const PAGE = { "X-CloseDesk": "1" };

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

// What a request says when CloseDesk doesn't answer at all, instead of the browser's own words ("Failed to fetch").
const NOT_ANSWERING = "CloseDesk isn't answering. If it has closed, double-click CloseDesk to start it again.";

/** ``error`` said in a sentence of the caller's, ``sentence(why)``: "Couldn't load this list (the server said 500).".
    When CloseDesk isn't answering, its own words alone: they already say what to do, and in brackets they read
    "Something went wrong (CloseDesk isn't answering. … start it again.). Try again." */
export function inSentence(error, sentence) {
  return error && error.status === 0 ? error.message : sentence(error ? error.message : "");
}

/** fetch, with plain words when CloseDesk can't be reached (it was stopped, say). A request cancelled on purpose
    still ends as an AbortError, which callers leave unsaid. */
async function send(path, options) {
  try {
    return await fetch(path, options);
  } catch (error) {
    if ((error && error.name === "AbortError") || (options.signal && options.signal.aborted)) throw error;
    throw new ApiError(NOT_ANSWERING, 0);
  }
}

async function handle(response) {
  let data = null;
  try {
    data = await response.json();
  } catch (error) {
    data = null;
  }
  if (!response.ok) {
    // FastAPI's errors carry "detail"; CloseDesk's own refusals (a vision read while busy) carry "message".
    const said = data && (typeof data.detail === "string" ? data.detail : typeof data.message === "string" ? data.message : "");
    const detail = said || `the server said ${response.status}`;
    throw new ApiError(detail, response.status);
  }
  return data;
}

export async function getJSON(path, { signal } = {}) {
  return handle(await send(path, { headers: PAGE, signal, credentials: "same-origin", cache: "no-store" }));
}

/** A picture (a file's page), fetched with the header the server requires, so an <img> can't load it itself. */
export async function getBlob(path, { signal } = {}) {
  const response = await send(path, { headers: PAGE, signal, credentials: "same-origin", cache: "no-store" });
  if (!response.ok) await handle(response);
  return response.blob();
}

export async function postJSON(path, body = {}, { signal } = {}) {
  return handle(
    await send(path, {
      method: "POST",
      headers: { ...PAGE, "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
      credentials: "same-origin",
    })
  );
}

/** A streamed POST: calls onEvent for each line of newline-delimited JSON. */
export async function postStream(path, body, onEvent, { signal } = {}) {
  const response = await send(path, {
    method: "POST",
    headers: { ...PAGE, "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
    credentials: "same-origin",
  });
  if (!response.ok || !response.body) {
    let detail = `the server said ${response.status}`;
    try {
      const data = await response.json();
      if (typeof data.detail === "string") detail = data.detail;
    } catch (error) {
      /* not JSON */
    }
    throw new ApiError(detail, response.status);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, cut).trim();
      buffer = buffer.slice(cut + 1);
      if (!line) continue;
      let event = null;
      try {
        event = JSON.parse(line);
      } catch (error) {
        continue;
      }
      onEvent(event);
    }
  }
  const rest = buffer.trim();
  if (rest) {
    try {
      onEvent(JSON.parse(rest));
    } catch (error) {
      /* a cut-off last line */
    }
  }
}

export const mailPath = (id) => `/inbox/${encodeURIComponent(id)}`;
export const filePath = (id, n) => `/inbox/${encodeURIComponent(id)}/files/${Number(n)}`;
