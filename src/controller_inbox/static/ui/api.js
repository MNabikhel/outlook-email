/* Talking to the CloseDesk server. Every request carries the X-CloseDesk header the server requires. */

const PAGE = { "X-CloseDesk": "1" };

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
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
  return handle(await fetch(path, { headers: PAGE, signal, credentials: "same-origin", cache: "no-store" }));
}

/** A picture (a file's page), fetched with the header the server requires, so an <img> can't load it itself. */
export async function getBlob(path, { signal } = {}) {
  const response = await fetch(path, { headers: PAGE, signal, credentials: "same-origin", cache: "no-store" });
  if (!response.ok) await handle(response);
  return response.blob();
}

export async function postJSON(path, body = {}, { signal } = {}) {
  return handle(
    await fetch(path, {
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
  const response = await fetch(path, {
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
