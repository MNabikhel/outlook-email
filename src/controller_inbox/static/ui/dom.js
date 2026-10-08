/* Building the page safely: every piece of text from the server goes in as a text node, never as HTML. */

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const SAFE_URL = /^(\/(?!\/)|#|mailto:)/i;

/** An element: h("a", { href: "/app", class: "x", onclick: fn }, "text", child, [more]). */
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") el.className = value;
      else if (key === "dataset") Object.assign(el.dataset, value);
      else if (key === "style" && typeof value === "object") Object.assign(el.style, value);
      else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
      else if (key === "text") el.textContent = value;
      else if ((key === "href" || key === "src" || key === "action") && !SAFE_URL.test(String(value))) continue;
      else el.setAttribute(key, value === true ? "" : String(value));
    }
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const child of [children].flat(Infinity)) {
    if (child === null || child === undefined || child === false || child === "") continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function replace(el, ...children) {
  el.replaceChildren();
  return append(el, children);
}

export const escapeHtml = (text) =>
  String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

export const plural = (n, word, many = word + "s") => `${n} ${n === 1 ? word : many}`;

export function isTyping(target) {
  return Boolean(target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)));
}

export const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* Icons: stroke paths on a 24px grid, drawn with SVG elements (no markup strings). */

const ring = (cx, cy, r) => `M${cx + r} ${cy}a${r} ${r} 0 1 1 ${-2 * r} 0a${r} ${r} 0 1 1 ${2 * r} 0`;

const PATHS = {
  today: "M8 2v4M16 2v4M3 10h18M5 4h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2zM9 16l2 2 4-4",
  important: "M5 21V4M5 4h12l-2.5 4L17 12H5",
  informational: `${ring(12, 12, 9)}M12 16v-4M12 8h.01`,
  reference: "M3 4h18v4H3zM5 8v12h14V8M10 12h4",
  all: "M22 12h-6l-2 3h-4l-2-3H2M5.5 5h13l3.5 7v6a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-6z",
  tasks: "M9 11l3 3 8-8M20 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11",
  fraud: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10zM12 8v4M12 16h.01",
  coding: "M4 9h16M4 15h16M10 3L8 21M16 3l-2 18",
  digest: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M8 13h8M8 17h8M8 9h2",
  settings: "M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6",
  chat: "M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z",
  search: `${ring(11, 11, 7)}M21 21l-4.3-4.3`,
  help: `${ring(12, 12, 9)}M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3M12 17h.01`,
  sidebar: "M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2zM9 3v18",
  back: "M19 12H5M12 19l-7-7 7-7",
  check: "M20 6L9 17l-5-5",
  undo: "M3 7v6h6M21 17a9 9 0 0 0-15-6.7L3 13",
  clip: "M21.4 11.1l-9.2 9.2a6 6 0 0 1-8.5-8.5l9.2-9.2a4 4 0 0 1 5.7 5.7l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5",
  file: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6",
  table: "M3 3h18v18H3zM3 9h18M3 15h18M9 3v18",
  text: "M17 10H3M21 6H3M21 14H3M17 18H3",
  external: "M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6M15 3h6v6M10 14L21 3",
  download: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3",
  x: "M18 6L6 18M6 6l12 12",
  plus: "M12 5v14M5 12h14",
  history: `${ring(12, 12, 9)}M12 7v5l3 2`,
  stop: "M7 7h10v10H7z",
  send: "M12 19V5M5 12l7-7 7 7",
  up: "M18 15l-6-6-6 6",
  down: "M6 9l6 6 6-6",
  right: "M9 18l6-6-6-6",
  lock: "M5 11h14v10H5zM8 11V7a4 4 0 0 1 8 0v4",
  alert: "M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0zM12 9v4M12 17h.01",
  mail: "M4 4h16a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2zM22 6l-10 7L2 6",
  pen: "M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z",
  refresh: "M21 12a9 9 0 1 1-3-6.7L21 8M21 3v5h-5",
  copy: "M9 9h11v11H9zM5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1",
  classic: "M3 3h18v18H3zM3 9h18M9 21V9",
  sun: `${ring(12, 12, 4)}M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4`,
  moon: "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z",
  play: "M6 4l14 8-14 8z",
  ok: `${ring(12, 12, 9)}M8 12l3 3 5-6`,
  eye: `M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z${ring(12, 12, 3)}`,
  circle: ring(12, 12, 8),
};

const SVG = "http://www.w3.org/2000/svg";

export function icon(name, size = 16) {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", "ic");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.8");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  const path = document.createElementNS(SVG, "path");
  path.setAttribute("d", PATHS[name] || PATHS.circle);
  svg.append(path);
  return svg;
}

/* Toasts: one line, an optional action (Undo), gone after a few seconds. */

export function toast(message, { action = "", onAction = null, ms = 4000, tone = "" } = {}) {
  const box = document.getElementById("toasts");
  if (!box) return () => {};
  const item = h("div", { class: `toast ${tone}`, role: "status" }, h("span", { class: "toast-text" }, message));
  let timer = null;
  const close = () => {
    clearTimeout(timer);
    item.classList.add("leaving");
    setTimeout(() => item.remove(), reducedMotion() ? 0 : 180);
  };
  if (action && onAction) {
    item.append(
      h("button", { type: "button", class: "toast-action", onclick: () => { close(); onAction(); } }, action)
    );
  }
  item.append(h("button", { type: "button", class: "toast-close", "aria-label": "Dismiss", onclick: close }, icon("x", 14)));
  box.append(item);
  while (box.children.length > 3) box.firstElementChild.remove();
  timer = setTimeout(close, ms);
  return close;
}

/* Skeletons shown while a pane loads. */

export function skeletonList(rows = 8) {
  return h(
    "div",
    { class: "sk-list", "aria-hidden": "true" },
    Array.from({ length: rows }, (_, i) =>
      h(
        "div",
        { class: "sk-row" },
        h("span", { class: "sk sk-line", style: { width: `${40 + ((i * 17) % 30)}%` } }),
        h("span", { class: "sk sk-line", style: { width: `${70 + ((i * 11) % 25)}%` } }),
        h("span", { class: "sk sk-line sk-faint", style: { width: `${55 + ((i * 7) % 35)}%` } })
      )
    )
  );
}

export function skeletonReader() {
  return h(
    "div",
    { class: "sk-reader", "aria-hidden": "true" },
    h("span", { class: "sk sk-line", style: { width: "22%" } }),
    h("span", { class: "sk sk-title", style: { width: "64%" } }),
    h("span", { class: "sk sk-line", style: { width: "38%" } }),
    h("div", { class: "sk-block" }, h("span", { class: "sk sk-box" }), h("span", { class: "sk sk-box" }), h("span", { class: "sk sk-box" })),
    ...Array.from({ length: 6 }, (_, i) => h("span", { class: "sk sk-line sk-faint", style: { width: `${60 + ((i * 13) % 35)}%` } }))
  );
}
