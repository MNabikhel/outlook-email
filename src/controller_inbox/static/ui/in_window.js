/* Inside the CloseDesk window (the app, app_window.py) rather than a browser tab: what leaves CloseDesk opens where
   it would from a browser. A mail address (mailto:, "Open in mail app") opens the mail app; another website, or a
   CloseDesk page meant for a new tab (the printable digest, a file's original, the chat in a window of its own), opens
   in the default browser. The window does that through its bridge (window.pywebview.api.open_outside), which opens
   nothing but web and mail addresses. A download stays with the window, which saves it where the person chooses,
   Downloads first: every download link carries "download", so both engines treat it as one.

   In a browser tab there is no bridge, and nothing here does anything. Loaded by both UIs: the workspace (main.js)
   and the classic pages (classic.js), which say through ``pageLoaded`` when their page is up. */

const bridge = () => (window.pywebview && window.pywebview.api) || null;

/** Whether this page is shown in the CloseDesk window. */
export function inWindow() {
  const api = bridge();
  return Boolean(api && typeof api.open_outside === "function");
}

/** What a click on ``link`` should do in the window: "outside" (the computer opens it: mail app, default browser) or
    "" (as in a browser: in the window, or a download the window saves). */
export function routeOf(link, here = window.location) {
  let url;
  try {
    url = new URL(link.getAttribute("href") || "", here.href);
  } catch (error) {
    return "";
  }
  if (url.protocol === "javascript:") return "";
  if (url.protocol !== "http:" && url.protocol !== "https:") return "outside";
  if (url.origin !== here.origin) return "outside";
  if (link.hasAttribute("download")) return "";
  const target = (link.getAttribute("target") || "").toLowerCase();
  return target && !["_self", "_top", "_parent"].includes(target) ? "outside" : "";
}

function outside(href) {
  try {
    const asked = bridge().open_outside(href);
    if (asked && typeof asked.catch === "function") asked.catch(() => {});
  } catch (error) {
    /* the window has gone */
  }
}

function onClick(event) {
  // A modified click (a new tab, a new window) is the window's own: it opens those in the browser too.
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || !inWindow()) return;
  const link = event.target instanceof Element ? event.target.closest("a[href]") : null;
  if (!link || routeOf(link) !== "outside") return;
  event.preventDefault();
  outside(link.href);
}

// What window.open gives back in the window: the page opened in the browser, out of this page's reach.
const OPENED = Object.freeze({ closed: false, focus() {}, blur() {}, close() {} });

function setUp() {
  if (window.closeDeskInWindow) return; // loaded once a page
  window.closeDeskInWindow = true;
  // Before the pages' own handlers (the router, the preview): a link that leaves goes before they look at it.
  document.addEventListener("click", onClick, true);
  const own = window.open;
  window.open = function open(url, target, features) {
    if (!inWindow()) return own.call(window, url, target, features);
    let href;
    try {
      href = new URL(String(url ?? ""), window.location.href).href;
    } catch (error) {
      return null;
    }
    if (!url || href === "about:blank") return null;
    outside(href);
    return OPENED;
  };
}

/** CloseDesk's page is up in the window: a classic page once its scripts ran, the workspace once it has started
    (CI's check of the real window waits for this, in both UIs). Nothing in a browser tab. */
export function pageLoaded() {
  const ready = () => {
    const api = bridge();
    if (api && typeof api.page_ready === "function") {
      try {
        // With the port it came from: CI's check of a window onto a CloseDesk already open sees it showed that one.
        const said = api.page_ready(window.location.pathname, window.location.port);
        if (said && typeof said.catch === "function") said.catch(() => {});
      } catch (error) {
        /* not the window after all */
      }
    }
  };
  if (inWindow()) ready();
  else window.addEventListener("pywebviewready", ready, { once: true });
}

setUp();
