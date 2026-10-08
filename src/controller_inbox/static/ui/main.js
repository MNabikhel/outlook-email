/* The CloseDesk workspace: a side bar, a list, a reading pane and Ask CloseDesk, rendered in the browser
   from /api. Moving around changes the address (so Back works and a link reopens the same email) without
   reloading the page. */

import { $, h, icon, replace, toast, plural, isTyping, reducedMotion, skeletonList, skeletonReader } from "./dom.js";
import { getJSON, postJSON } from "./api.js";
import { listSpec, loadList, rowEl } from "./lists.js";
import { emailView, fileView, errorView } from "./reader.js";
import { todayView, fraudView, codingView, digestView, settingsView, emptyReader } from "./pages.js";
import { createChat } from "./chat.js";
import { createOverlays } from "./palette.js";

const enc = encodeURIComponent;
const root = document.documentElement;
const ws = $("#ws");
const listPane = $("#list");
const reader = $("#reader");

const S = {
  meta: null,
  route: null,
  cache: new Map(), // list key -> { data, items }
  files: new Map(), // "id:n" -> file JSON
  list: null, // { spec, data, items, error }
  headerKey: "",
  filter: "all",
  query: "",
  sel: null, // selected row key
  email: null, // the open email's JSON
  shown: "", // what the reading pane shows, so a repeat visit doesn't redraw it
  listToken: 0,
  emailToken: 0,
  poll: 0,
  jobSeq: 0, // bumped when this page starts a job, so a status fetched before that doesn't undo it
  topJob: "", // what the job bar shows
};

/* ---------- Addresses ---------- */

function parse() {
  const parts = location.pathname.replace(/\/+$/, "").split("/").slice(2).map((part) => {
    try {
      return decodeURIComponent(part);
    } catch (error) {
      return part;
    }
  });
  const q = new URLSearchParams(location.search);
  const [first = "", second = "", third = "", fourth = ""] = parts;
  const route = { view: first || "today", list: "today", emailId: null, file: null, tab: q.get("tab") || "tables", date: q.get("date") || "" };
  if (first === "folder") route.list = `folder:${second}${q.get("done") === "1" ? ":done" : ""}`;
  else if (first === "all") route.list = q.get("q") ? `search:${q.get("q")}` : "all";
  else if (first === "tasks") route.list = `tasks:${q.get("status") || "open"}`;
  else if (first === "fraud") route.list = "fraud";
  else if (first === "coding") route.list = `coding:${q.get("status") || "review"}`;
  else if (first === "digest" || first === "settings") route.list = null;
  else if (first === "mail") {
    route.emailId = second;
    route.list = q.get("in") || null;
    if (third === "file" && Number(fourth) > 0) route.file = Number(fourth);
  } else if (first !== "today") route.view = "today";
  return route;
}

const currentKey = () => (S.list ? S.list.spec.key : null);
const mailUrl = (id, key = currentKey()) => `/app/mail/${enc(id)}${key ? `?in=${enc(key)}` : ""}`;
const fileUrl = (id, n, tab, key = currentKey()) => `/app/mail/${enc(id)}/file/${n}?${new URLSearchParams({ ...(key ? { in: key } : {}), tab })}`;

function navigate(url, { replace: swap = false } = {}) {
  if (url !== location.pathname + location.search) history[swap ? "replaceState" : "pushState"]({}, "", url);
  sync();
}

window.addEventListener("popstate", () => sync());

/* ---------- The side bar ---------- */

const NAV = [
  { id: "today", label: "Today", icon: "today", url: "/app", count: () => (S.cache.get("today") ? S.cache.get("today").items.length : "") },
  { id: "tasks", label: "Tasks", icon: "tasks", url: "/app/tasks", count: (m) => m.counts.open_actions },
  { heading: "Mail" },
  { id: "folder:important", label: "Important", icon: "important", url: "/app/folder/important", count: (m) => m.counts.important },
  { id: "folder:informational", label: "Informational", icon: "informational", url: "/app/folder/informational", count: (m) => m.counts.informational },
  { id: "folder:reference", label: "Reference", icon: "reference", url: "/app/folder/reference", count: (m) => m.counts.reference },
  { id: "all", label: "All mail", icon: "all", url: "/app/all", count: (m) => m.counts.emails },
  { heading: "Review" },
  { id: "fraud", label: "Fraud check", icon: "fraud", url: "/app/fraud", count: (m) => m.counts.fraud_alerts, tone: "danger" },
  { id: "coding", label: "AP coding", icon: "coding", url: "/app/coding", count: (m) => m.coding.review },
  { heading: "" },
  { id: "digest", label: "Daily digest", icon: "digest", url: "/app/digest" },
  { id: "settings", label: "Settings", icon: "settings", url: "/app/settings" },
];

function buildRail() {
  replace(
    $("#rail-nav"),
    NAV.map((item) =>
      item.heading !== undefined
        ? h("div", { class: "rail-heading" }, item.heading)
        : h(
            "a",
            { class: "rail-item", href: item.url, dataset: { nav: "", id: item.id }, title: item.label },
            icon(item.icon, 17),
            h("span", { class: "rail-label" }, item.label),
            item.count ? h("span", { class: `rail-count${item.tone ? ` ${item.tone}` : ""}`, dataset: { count: item.id } }) : null
          )
    )
  );
  replace($(".rail-toggle"), icon("sidebar", 17));
}

function updateRail(active) {
  const meta = S.meta;
  for (const item of NAV) {
    if (!item.id) continue;
    const link = $(`.rail-item[data-id="${item.id}"]`);
    if (!link) continue;
    const on = item.id === active;
    link.classList.toggle("on", on);
    if (on) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
    const badge = $(".rail-count", link);
    if (badge && meta) {
      const value = item.count(meta);
      badge.textContent = value === "" || value === undefined ? "" : String(value);
      badge.classList.toggle("zero", !value);
    }
  }
  if (!meta) return;
  replace(
    $("#rail-foot"),
    h("a", { class: `rail-chip model ${meta.model.active ? "on" : "off"}`, href: "/app/settings", dataset: { nav: "" }, title: meta.model.describe }, h("span", { class: "dot" }), h("span", { class: "rail-label" }, `Local model ${meta.model.label}`)),
    meta.finance ? h("div", { class: "rail-chip close", title: `Month-end ${meta.close_date}` }, h("b", null, String(meta.days_to_close)), h("span", { class: "rail-label" }, `day${meta.days_to_close === 1 ? "" : "s"} to month-end`)) : null,
    h("a", { class: "rail-item rail-classic", href: "/", title: "The classic CloseDesk pages" }, icon("classic", 17), h("span", { class: "rail-label" }, "Classic view"))
  );
}

function toggleRail() {
  const collapsed = root.dataset.rail !== "collapsed";
  if (collapsed) root.dataset.rail = "collapsed";
  else delete root.dataset.rail;
  try {
    localStorage.setItem("closedesk-rail", collapsed ? "collapsed" : "open");
  } catch (error) {
    /* not remembered */
  }
  if (S.route && S.route.view === "settings") showSettings();
}

/* ---------- The top bar ---------- */

function renderTop() {
  const meta = S.meta;
  if (!meta) return;
  replace(
    $("#top-date"),
    h("b", null, meta.today_long),
    h("span", { class: "muted" }, meta.finance ? ` · ${plural(meta.days_to_close, "day")} to month-end` : ` · ${plural(meta.counts.emails, "email")} sorted`),
    meta.is_sample ? h("span", { class: "chip faint sample" }, "sample mailbox") : null
  );
  const job = meta.job;
  const running = job.state === "running";
  const button = $("#process-btn");
  button.disabled = running;
  button.textContent = running ? "Processing…" : "Process new mail";
  // Redrawn only when it changes, so a click on Stop isn't lost to a redraw between press and release.
  const shown = JSON.stringify(running ? [job.stage_label, job.done, job.total, job.note, Boolean(job.about), job.stopping] : null);
  if (shown === S.topJob) return;
  S.topJob = shown;
  replace(
    $("#top-job"),
    running
      ? h(
          "div",
          { class: "job" },
          h("span", { class: "spin", "aria-hidden": "true" }),
          h("span", null, h("b", null, job.stage_label || "Working"), job.total > 1 ? ` · ${job.done} of ${job.total}` : "", job.note ? h("span", { class: "muted" }, ` · ${job.note}`) : null),
          h("span", { class: "job-bar" }, h("span", { style: { width: job.total ? `${Math.round((100 * job.done) / job.total)}%` : "30%" }, class: job.total ? "" : "indeterminate" })),
          job.stage === "vision" && !job.stopping
            ? h("button", { type: "button", class: "btn btn-sm btn-quiet", title: "Stop reading pages with the vision model after the page being read now", onclick: stopJob }, "Stop")
            : null
        )
      : null
  );
}

async function stopJob(event) {
  event.currentTarget.disabled = true;
  S.jobSeq += 1; // a status fetched before the click mustn't bring the button back
  try {
    const reply = await postJSON("/api/process/stop");
    if (reply && reply.stopping === false) return toast("Nothing is running now.");
    toast("Stopping after the page being read now.");
    if (S.meta) {
      S.meta.job = { ...S.meta.job, stopping: true };
      renderTop();
    }
  } catch (error) {
    toast(error.message, { tone: "error" });
  }
}

/* ---------- Theme ---------- */

const theme = () => {
  try {
    return localStorage.getItem("closedesk-theme") || "system";
  } catch (error) {
    return "system";
  }
};
const isDark = () => root.dataset.theme === "dark" || (!root.dataset.theme && window.matchMedia("(prefers-color-scheme: dark)").matches);

function setTheme(value) {
  if (value === "light" || value === "dark") root.dataset.theme = value;
  else delete root.dataset.theme;
  try {
    if (value === "light" || value === "dark") localStorage.setItem("closedesk-theme", value);
    else localStorage.removeItem("closedesk-theme");
  } catch (error) {
    /* not remembered */
  }
  themeButton();
  if (S.route && S.route.view === "settings") showSettings();
}

function themeButton() {
  const button = $("#theme-btn");
  const dark = isDark();
  replace(button, icon(dark ? "sun" : "moon", 17));
  button.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
  button.title = dark ? "Light theme" : "Dark theme";
}

window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", themeButton);

/* ---------- The list ---------- */

function visible() {
  if (!S.list || !S.list.items) return [];
  const filter = S.list.spec.filters.find((f) => f.id === S.filter);
  const words = S.query.toLowerCase().split(/\s+/).filter(Boolean);
  return S.list.items.filter((item) => (!filter || !filter.test || filter.test(item.data)) && words.every((word) => item.text.includes(word)));
}

function listHeader(spec) {
  const data = S.list.data;
  const search = h("input", {
    type: "search",
    class: "list-search",
    placeholder: spec.kind === "mail" ? "Filter, or Enter to search all mail" : "Filter this list",
    "aria-label": "Filter this list",
    value: spec.q || S.query, // a filter kept from before (a visit to Settings) shows in the box
    autocomplete: "off",
  });
  if (spec.q) S.query = "";
  search.addEventListener("input", () => {
    S.query = spec.q && search.value === spec.q ? "" : search.value;
    renderItems();
  });
  search.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      const q = search.value.trim();
      if (q && spec.kind === "mail") navigate(`/app/all?q=${enc(q)}`);
      else focusList();
    } else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      if (search.value) {
        search.value = "";
        S.query = "";
        renderItems();
      } else focusList();
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      focusList();
      move(1);
    }
  });
  const chips = h(
    "div",
    { class: "filters", role: "group", "aria-label": "Quick filters" },
    spec.filters.length > 1
      ? spec.filters.map((f) =>
          h("button", { type: "button", class: `fchip${S.filter === f.id ? " on" : ""}`, "aria-pressed": String(S.filter === f.id), dataset: { filter: f.id }, onclick: () => { S.filter = f.id; renderItems(); } }, f.label, h("span", { class: "fcount" }))
        )
      : null
  );
  let extra = null;
  if (spec.kind === "task") {
    extra = h("div", { class: "segs small" }, ["open", "done", "all"].map((status) => h("a", { class: `seg${spec.status === status ? " on" : ""}`, href: listSpec(`tasks:${status}`).url, dataset: { nav: "" } }, status.charAt(0).toUpperCase() + status.slice(1))));
  } else if (spec.kind === "coding") {
    const counts = (data && data.counts) || (S.meta && S.meta.coding) || {};
    const labels = { review: "To review", suggested: "Suggested", unmatched: "No code", confirmed: "Confirmed", all: "All" };
    extra = h(
      "div",
      { class: "filters status-row", role: "group", "aria-label": "Coding status" },
      Object.entries(labels).map(([status, label]) =>
        h("a", { class: `fchip${spec.status === status ? " on" : ""}`, href: listSpec(`coding:${status}`).url, dataset: { nav: "" }, "aria-current": spec.status === status ? "true" : null }, label, counts[status] !== undefined ? h("span", { class: "fcount" }, ` ${counts[status]}`) : null)
      )
    );
  } else if (spec.folder) {
    const doneCount = data ? data.done_count : 0;
    extra = spec.done
      ? h("a", { class: "link-sm", href: listSpec(`folder:${spec.folder}`).url, dataset: { nav: "" } }, "← Back to open mail")
      : doneCount
        ? h("a", { class: "link-sm", href: listSpec(`folder:${spec.folder}:done`).url, dataset: { nav: "" } }, `Done (${doneCount})`)
        : null;
  }
  const subtitle = spec.kind === "focus" && data ? data.headline : spec.q ? `Results for “${spec.q}”` : "";
  return h(
    "div",
    { class: "list-head" },
    h("div", { class: "list-title" }, h("h1", null, spec.title(data)), h("span", { class: "list-count", "aria-live": "polite" }), extra),
    subtitle ? h("p", { class: "list-sub" }, subtitle) : null,
    h("div", { class: "list-search-wrap" }, icon("search", 14), search, h("kbd", null, "/")),
    chips
  );
}

function renderList() {
  const spec = S.list.spec;
  if (S.headerKey !== spec.key || !$(".list-head", listPane)) {
    S.headerKey = spec.key;
    replace(listPane, listHeader(spec), h("div", { class: "list-body" }));
  }
  renderItems();
}

function refreshHeader() {
  // Counts or a headline that arrived after the header was drawn.
  const spec = S.list.spec;
  const old = $(".list-head", listPane);
  if (!old) return renderList();
  const typing = document.activeElement && document.activeElement.classList.contains("list-search");
  if (typing) return;
  const fresh = listHeader(spec);
  const search = $(".list-search", old);
  if (search && !spec.q) $(".list-search", fresh).value = search.value;
  old.replaceWith(fresh);
}

function renderItems() {
  const body = $(".list-body", listPane);
  if (!body) return;
  const { spec, items, error } = S.list;
  if (error) {
    replace(body, h("div", { class: "list-empty" }, icon("alert", 22), h("p", null, error), h("button", { type: "button", class: "btn btn-sm", onclick: () => ensureList(spec.key, { force: true }) }, "Try again")));
    return;
  }
  if (!items) {
    replace(body, skeletonList());
    return;
  }
  const shown = visible();
  // Only the quick-filter buttons: the coding status links carry their own counts from the server.
  for (const chip of listPane.querySelectorAll("button.fchip[data-filter]")) {
    const f = spec.filters.find((x) => x.id === chip.dataset.filter);
    if (!f) continue;
    const n = f && f.test ? items.filter((item) => f.test(item.data)).length : items.length;
    chip.classList.toggle("on", chip.dataset.filter === S.filter);
    chip.setAttribute("aria-pressed", String(chip.dataset.filter === S.filter));
    $(".fcount", chip).textContent = n ? ` ${n}` : "";
    chip.disabled = !n && f.id !== "all";
  }
  const count = $(".list-count", listPane);
  if (count) count.textContent = shown.length === items.length ? String(items.length) : `${shown.length} of ${items.length}`;
  if (!shown.length) {
    replace(body, h("div", { class: "list-empty" }, icon(spec.kind === "focus" ? "ok" : "all", 24), h("p", null, items.length ? "Nothing matches this filter." : spec.empty)));
    return;
  }
  const openId = S.email && S.route && S.route.emailId ? S.email.id : null;
  const ul = h(
    "ul",
    { class: "items", role: "listbox", "aria-label": spec.title(S.list.data), tabindex: "0" },
    shown.map((item) => rowEl(item, spec, { selected: item.key === S.sel, open: item.emailId === openId && (item.kind !== "task" || item.key === S.sel || !S.sel), onDone: doneItem }))
  );
  ul.addEventListener("focus", () => {
    if (!S.sel && shown.length) {
      S.sel = (shown.find((item) => item.emailId === openId) || shown[0]).key;
      updateSelection();
    }
  });
  const hadFocus = body.contains(document.activeElement);
  const scrolled = body.scrollTop;
  replace(body, ul);
  body.scrollTop = scrolled;
  if (hadFocus) ul.focus({ preventScroll: true });
  updateSelection(false);
}

function updateSelection(scroll = true) {
  const ul = $(".items", listPane);
  if (!ul) return;
  const openId = S.email && S.route && S.route.emailId ? S.email.id : null;
  let active = "";
  for (const li of ul.children) {
    const item = S.list.items.find((x) => x.key === li.dataset.key);
    const sel = li.dataset.key === S.sel;
    li.classList.toggle("is-sel", sel);
    li.setAttribute("aria-selected", String(sel));
    li.id = `row-${li.dataset.key}`.replace(/[^\w-]/g, "_");
    if (sel) active = li.id;
    li.classList.toggle("is-open", Boolean(item && openId && item.emailId === openId));
    if (sel && scroll) li.scrollIntoView({ block: "nearest", behavior: reducedMotion() ? "auto" : "smooth" });
  }
  if (active) ul.setAttribute("aria-activedescendant", active);
}

function focusList() {
  const ul = $(".items", listPane);
  if (ul) ul.focus({ preventScroll: true });
}

async function ensureList(key, { force = false, quiet = false } = {}) {
  const spec = listSpec(key);
  const same = S.list && S.list.spec.key === spec.key;
  if (same && !force && S.list.items) {
    renderList();
    return S.list;
  }
  const token = ++S.listToken;
  if (!same) {
    S.filter = "all";
    S.query = "";
    S.sel = null;
    const cached = S.cache.get(spec.key);
    S.list = cached ? { spec, ...cached } : { spec, data: null, items: null };
    renderList();
  } else if (!quiet) {
    S.list.error = "";
  }
  try {
    const data = await loadList(spec);
    if (token !== S.listToken) return S.list;
    const items = spec.items(data);
    S.cache.set(spec.key, { data, items });
    const hadData = Boolean(S.list.data);
    S.list = { spec, data, items };
    if (hadData) {
      refreshHeader();
      renderItems();
    } else renderList();
    updateRail(navId());
    if (S.route && !S.route.emailId && S.route.list) showOverview();
  } catch (error) {
    if (token !== S.listToken) return S.list;
    if (!S.list.items) {
      S.list.error = `Couldn't load this list (${error.message}).`;
      renderItems();
    }
  }
  return S.list;
}

const navId = () => (S.route && !S.route.list && (S.route.view === "digest" || S.route.view === "settings") ? S.route.view : S.list ? listSpec(S.list.spec.key).nav : "");

/* ---------- Moving and opening ---------- */

let stepTimer = 0;

function move(dir) {
  const shown = visible();
  if (!shown.length) return;
  let i = shown.findIndex((item) => item.key === S.sel);
  if (i < 0 && S.email) i = shown.findIndex((item) => item.emailId === S.email.id);
  i = i < 0 ? (dir > 0 ? 0 : shown.length - 1) : Math.max(0, Math.min(shown.length - 1, i + dir));
  S.sel = shown[i].key;
  updateSelection();
  // Keys act on the list from now on (Enter opens the row, not a link that had focus).
  if (!listPane.contains(document.activeElement)) focusList();
  if (S.route && S.route.emailId && !S.route.file) {
    // Reading: the next email opens as you move, like a mail app's reading pane.
    clearTimeout(stepTimer);
    const target = shown[i];
    stepTimer = setTimeout(() => navigate(mailUrl(target.emailId), { replace: true }), 90);
  }
}

function openSelected() {
  const item = visible().find((x) => x.key === S.sel);
  if (item) navigate(mailUrl(item.emailId));
}

function closeEmail() {
  if (!S.route) return;
  if (S.route.file && S.email) return navigate(mailUrl(S.email.id));
  if (S.route.emailId) {
    navigate(S.list ? S.list.spec.url : "/app");
    focusList();
  }
}

/* ---------- The reading pane ---------- */

function setReader(node, key = "", { animate = true } = {}) {
  S.shown = key;
  replace(reader, node);
  reader.scrollTop = 0;
  if (animate && !reducedMotion()) {
    node.classList.add("enter");
    node.addEventListener("animationend", () => node.classList.remove("enter"), { once: true });
  }
}

function showOverview() {
  if (!S.list) return;
  S.email = null;
  chat.setScope(null);
  const { spec, data } = S.list;
  document.title = `${spec.title(data)} · CloseDesk`;
  const key = `overview:${spec.key}:${data ? "data" : "none"}:${S.meta ? S.meta.version : ""}`;
  if (S.shown === key) return;
  if (!data) return setReader(skeletonReader(), key, { animate: false });
  if (spec.kind === "focus") setReader(todayView(data, S.meta), key);
  else if (spec.kind === "fraud") setReader(fraudView(data), key);
  else if (spec.kind === "coding") setReader(codingView(data), key);
  else setReader(emptyReader(spec.q ? `${plural(data.items.length, "email")} mention “${spec.q}”. Choose one to read it.` : "Choose an email to read it here."), key);
}

/* The open email's row: the selected one when it is this email's (an email can have several task rows). */
function openIndex(shown) {
  if (!S.email) return -1;
  const i = shown.findIndex((item) => item.key === S.sel && item.emailId === S.email.id);
  return i >= 0 ? i : shown.findIndex((item) => item.emailId === S.email.id);
}

const ctx = {
  position() {
    const shown = visible();
    return { index: openIndex(shown), total: shown.length };
  },
  step(dir) {
    const shown = visible();
    let i = openIndex(shown);
    if (i < 0) return;
    // Past the other rows of the same email, to the next email.
    do i += dir;
    while (shown[i] && shown[i].emailId === S.email.id);
    const next = shown[i];
    if (next) {
      S.sel = next.key;
      navigate(mailUrl(next.emailId), { replace: true });
    }
  },
  emailDone: (d, done) => setEmailDone(d.id, done),
  taskStatus: (task, status) => setTask(task.id, status),
  ask: (question, emailId) => chat.ask(question, emailId),
  prefill: (text) => chat.prefill(text),
  closeEmail,
  fileUrl: (n, tab) => fileUrl(S.email.id, n, tab),
  emailUrl: () => mailUrl(S.email.id),
  reload: () => reloadEmail(),
  categories: () => (S.meta ? S.meta.categories : []),
  visionRead,
  visionReading,
};

/* Reading a file's pages with the vision model: started from the file view or from an answer's offer, it runs as
   the background job; when it finishes the file view shows both readings and the chat offers to ask again. */
async function visionRead(emailId, n, again = false) {
  try {
    const result = await postJSON(`/api/mail/${enc(emailId)}/files/${Number(n)}/vision${again ? "?again=1" : ""}`);
    if (result.started) {
      toast(result.message, { ms: 8000 });
      S.jobSeq += 1;
      if (S.meta) {
        S.meta.job = {
          ...S.meta.job,
          state: "running", stage: "vision", stage_label: "Reading scans with the vision model", done: 0, total: result.pages.length, note: "",
          about: { kind: "vision", email_id: emailId, n: Number(n) }, stopping: false,
        };
        renderTop();
      }
      clearTimeout(S.poll);
      S.poll = setTimeout(poll, 600);
    } else toast(result.message);
    return result;
  } catch (error) {
    toast(error.message, { tone: "error", ms: 9000 });
    return null;
  }
}

/* Whether the vision model is reading this file now (the file view's button waits for it). */
function visionReading(emailId, n) {
  const job = S.meta && S.meta.job;
  return Boolean(job && job.state === "running" && job.about && job.about.email_id === emailId && Number(job.about.n) === Number(n));
}

function visionDone(result) {
  const key = `${result.email_id}:${result.n}`;
  S.files.delete(key);
  const here = S.route && S.route.emailId === result.email_id && S.route.file === result.n;
  if (here && S.email) {
    S.shown = "";
    showFile(S.email, result.n, S.route.tab, S.emailToken);
  }
  window.dispatchEvent(new CustomEvent("closedesk:vision", { detail: result }));
  // A file added to a conversation has no page here: it opens on its classic page.
  const fromChat = String(result.email_id).startsWith("chat-");
  const show = result.pages && !here;
  toast(result.message, {
    tone: result.pages ? "ok" : "error",
    ms: 10000,
    action: show ? "See the file" : "",
    onAction: show ? () => (fromChat ? window.open(result.href, "_blank", "noopener") : navigate(fileUrl(result.email_id, result.n, "text"))) : null,
  });
}

function drawEmail({ keepScroll = false } = {}) {
  const top = reader.scrollTop;
  // A reply being written survives a redraw of the same email (a task ticked, Done, Undo).
  const old = $(".rd", reader);
  const draft = old && old.dataset.email === S.email.id ? $(".draft", old) : null;
  const view = emailView(S.email, ctx);
  if (draft) $(".rd-head", view).after(draft);
  setReader(view, `email:${S.email.id}`, { animate: !keepScroll });
  if (keepScroll) reader.scrollTop = top;
}

async function showEmail(id, file, tab) {
  const token = ++S.emailToken;
  const have = S.email && S.email.id === id ? S.email : null;
  let skeleton = 0;
  if (!have) {
    S.email = null;
    skeleton = setTimeout(() => token === S.emailToken && setReader(skeletonReader(), "", { animate: false }), 120);
  }
  let d = have;
  if (!d) {
    try {
      d = await getJSON(`/api/mail/${enc(id)}`);
    } catch (error) {
      clearTimeout(skeleton);
      if (token === S.emailToken) {
        setReader(errorView(error.status === 404 ? "That email isn't here any more." : `Couldn't open it (${error.message}).`, () => showEmail(id, file, tab)), "error");
      }
      return null;
    }
    clearTimeout(skeleton);
    if (token !== S.emailToken) return null;
    S.email = d;
  }
  chat.setScope(d);
  updateSelection(false);
  const item = S.list && S.list.items ? S.list.items.find((x) => x.emailId === id) : null;
  if (item && (!S.sel || !S.list.items.some((x) => x.key === S.sel && x.emailId === id))) {
    S.sel = item.key;
    updateSelection();
  }
  if (file) {
    await showFile(d, file, tab, token);
  } else {
    document.title = `${d.subject} · CloseDesk`;
    if (S.shown !== `email:${d.id}`) drawEmail();
  }
  return d;
}

async function showFile(d, n, tab, token) {
  const key = `${d.id}:${n}`;
  let data = S.files.get(key);
  if (!data) {
    setReader(skeletonReader(), "", { animate: false });
    try {
      data = await getJSON(`/api/mail/${enc(d.id)}/files/${n}`);
    } catch (error) {
      if (token === S.emailToken) setReader(errorView(error.message, () => showFile(d, n, tab, token)), "error");
      return;
    }
    if (token !== S.emailToken) return;
    S.files.set(key, data);
  }
  const shownKey = `file:${key}:${tab}`;
  document.title = `${data.file.name} · CloseDesk`;
  if (S.shown !== shownKey) setReader(fileView(data, tab, ctx), shownKey, { animate: !S.shown.startsWith(`file:${key}`) });
}

async function reloadEmail() {
  if (!S.email) return;
  const id = S.email.id;
  S.files.clear();
  try {
    const d = await getJSON(`/api/mail/${enc(id)}`);
    if (!S.email || S.email.id !== id) return;
    S.email = d;
    if (S.shown === `email:${id}`) drawEmail({ keepScroll: true });
  } catch (error) {
    /* the next visit shows it */
  }
  poll();
}

function showDigest(date) {
  document.title = "Daily digest · CloseDesk";
  const key = `digest:${date}`;
  if (S.shown !== key) setReader(skeletonReader(), "", { animate: false });
  // An answer that comes back after you've moved on (another page, another date) is dropped.
  const wanted = () => S.route.view === "digest" && S.route.date === date;
  getJSON(`/api/digest${date ? `?date=${enc(date)}` : ""}`)
    .then((data) => {
      if (wanted()) setReader(digestView(data), key);
    })
    .catch((error) => {
      if (wanted()) setReader(errorView(error.message), "error");
    });
}

function showSettings() {
  document.title = "Settings · CloseDesk";
  setReader(settingsView(S.meta, { theme: theme(), setTheme, railCollapsed: root.dataset.rail === "collapsed", toggleRail }), "settings", { animate: S.shown !== "settings" });
}

/* ---------- Rendering what the address says ---------- */

async function sync() {
  const route = parse();
  const before = S.route;
  S.route = route;
  const doc = route.view === "digest" || route.view === "settings";
  ws.classList.toggle("doc", doc);
  ws.classList.toggle("wide", Boolean(route.file));
  ws.classList.toggle("reading", Boolean(route.emailId) || doc);
  $(".top-back").hidden = !(route.emailId || doc);
  if (doc) {
    S.email = null;
    chat.setScope(null);
    updateRail(route.view);
    return route.view === "digest" ? showDigest(route.date) : showSettings();
  }
  if (before && (before.view === "digest" || before.view === "settings") && S.list) {
    S.headerKey = "";
  }
  let key = route.list;
  if (route.emailId && !key) key = currentKey();
  if (key) {
    const pending = ensureList(key);
    updateRail(listSpec(key).nav);
    if (!route.emailId) {
      showOverview();
      return pending;
    }
  }
  if (route.emailId) {
    const d = await showEmail(route.emailId, route.file, route.tab);
    if (d && !key) {
      // Opened from a link: show the email's folder beside it.
      const folderKey = d.folder ? `folder:${d.folder}` : "all";
      await ensureList(folderKey);
      updateRail(listSpec(folderKey).nav);
      updateSelection();
    }
  }
}

/* ---------- Done, with Undo ---------- */

function afterChange(counts) {
  if (counts && S.meta) {
    S.meta.counts = counts;
    updateRail(navId());
  }
  // Other lists changed too; the current one is already right.
  for (const key of [...S.cache.keys()]) if (key !== currentKey()) S.cache.delete(key);
  clearTimeout(S.poll);
  S.poll = setTimeout(poll, 400);
}

function leave(item) {
  // Take a row out of the current list; returns how to put it back.
  const list = S.list;
  const at = list.items.indexOf(item);
  if (at < 0) return () => {};
  const shown = visible();
  const i = shown.indexOf(item);
  const next = shown[i + 1] || shown[i - 1] || null;
  const wasOpen = S.email && S.route.emailId === item.emailId && !S.route.file;
  const li = $(`.item[data-key="${CSS.escape(item.key)}"]`, listPane);
  const drop = () => {
    list.items.splice(list.items.indexOf(item), 1);
    if (S.sel === item.key) S.sel = next ? next.key : null;
    if (S.list === list) renderItems();
  };
  if (li && !reducedMotion()) {
    li.classList.add("leaving");
    setTimeout(drop, 170);
  } else drop();
  if (wasOpen && next && next.emailId !== item.emailId) navigate(mailUrl(next.emailId), { replace: true });
  return () => {
    if (!list.items.includes(item)) list.items.splice(Math.min(at, list.items.length), 0, item);
    S.sel = item.key;
    if (S.list === list) renderItems();
  };
}

async function optimistic({ apply, revert, request, undo, message }) {
  apply();
  let result;
  try {
    result = await request();
  } catch (error) {
    revert();
    toast(error.message, { tone: "error" });
    return;
  }
  afterChange(result && result.counts);
  toast(message, {
    action: "Undo",
    ms: 6500,
    onAction: async () => {
      revert();
      try {
        afterChange((await undo()).counts);
      } catch (error) {
        apply();
        toast(error.message, { tone: "error" });
      }
    },
  });
}

function setEmailDone(id, done, item = null) {
  const spec = S.list ? S.list.spec : null;
  item = item || (S.list && S.list.items ? S.list.items.find((x) => x.emailId === id && (x.kind === "mail" || (x.kind === "focus" && !x.data.action_id))) : null);
  const leaves = Boolean(item && spec && (item.kind === "focus" || (item.kind === "mail" && spec.folder)));
  let putBack = () => {};
  const setOpen = (value) => {
    if (S.email && S.email.id === id) {
      S.email.done = value;
      if (S.shown === `email:${id}`) drawEmail({ keepScroll: true });
    }
  };
  return optimistic({
    apply() {
      setOpen(done);
      if (!item) return;
      if (leaves) putBack = leave(item);
      else {
        item.done = item.data.done = done;
        renderItems();
      }
    },
    revert() {
      setOpen(!done);
      if (!item) return;
      if (leaves) putBack();
      else {
        item.done = item.data.done = !done;
        renderItems();
      }
    },
    request: () => postJSON(`/api/mail/${enc(id)}/done`, { done }),
    undo: () => postJSON(`/api/mail/${enc(id)}/done`, { done: !done }),
    message: done ? "Marked done." : "Back in its list.",
  });
}

function setTask(actionId, status, item = null) {
  const spec = S.list ? S.list.spec : null;
  item = item || (S.list && S.list.items ? S.list.items.find((x) => (x.kind === "task" && x.key === actionId) || (x.kind === "focus" && x.data.action_id === actionId)) : null);
  const leaves = Boolean(item && spec && (item.kind === "focus" ? status === "done" : spec.status !== "all"));
  let putBack = () => {};
  const setOpen = (value) => {
    const task = S.email && S.email.tasks.find((t) => t.id === actionId);
    if (task) {
      task.status = value;
      if (S.shown === `email:${S.email.id}`) drawEmail({ keepScroll: true });
    }
  };
  const back = status === "done" ? "open" : "done";
  return optimistic({
    apply() {
      setOpen(status);
      if (!item) return;
      if (leaves) putBack = leave(item);
      else if (item.kind === "task") {
        item.done = status === "done";
        item.data.status = status;
        renderItems();
      }
    },
    revert() {
      setOpen(back);
      if (!item) return;
      if (leaves) putBack();
      else if (item.kind === "task") {
        item.done = back === "done";
        item.data.status = back;
        renderItems();
      }
    },
    request: () => postJSON(`/api/tasks/${enc(actionId)}`, { status }),
    undo: () => postJSON(`/api/tasks/${enc(actionId)}`, { status: back }),
    message: status === "done" ? "Task done." : "Task reopened.",
  });
}

function doneItem(item) {
  const spec = S.list.spec;
  if (item.kind === "task") return setTask(item.data.id, item.done ? "open" : "done", item);
  if (item.kind === "focus") return item.data.action_id ? setTask(item.data.action_id, "done", item) : setEmailDone(item.emailId, true, item);
  if (item.kind === "mail") return setEmailDone(item.emailId, !(spec.done || item.done), item);
  toast("Nothing to mark done in this list. Open the email to act on it.");
}

function doneSelected() {
  const item = visible().find((x) => x.key === S.sel) || (S.email ? visible().find((x) => x.emailId === S.email.id) : null);
  if (item) return doneItem(item);
  if (S.email && S.email.folder) return setEmailDone(S.email.id, !S.email.done);
}

/* ---------- Live status ---------- */

async function poll() {
  clearTimeout(S.poll);
  const seq = S.jobSeq;
  try {
    const meta = await getJSON("/api/state");
    if (seq !== S.jobSeq) throw new Error("a job started while this was on its way: ask again");
    const before = S.meta;
    S.meta = meta;
    renderTop();
    updateRail(navId());
    chat.setModel(meta.model);
    $("#chat-btn").classList.toggle("live", meta.model.active);
    if (before && meta.job.state !== "running" && meta.job.finished_at && meta.job.finished_at !== before.job.finished_at) finished(meta.job);
    if (before && before.version !== meta.version) refreshData();
  } catch (error) {
    /* offline for a moment: try again later */
  }
  const running = S.meta && S.meta.job.state === "running";
  S.poll = setTimeout(poll, document.hidden ? 60000 : running ? 1500 : 15000);
}

function refreshData() {
  for (const key of [...S.cache.keys()]) if (key !== currentKey()) S.cache.delete(key);
  S.files.clear();
  if (S.list && S.route && S.route.list !== null) ensureList(S.list.spec.key, { force: true, quiet: true });
}

function finished(job) {
  if (job.state === "error" && job.about && job.about.kind === "vision") {
    return visionDone({ ...job.about, pages: 0, message: `The read stopped: ${job.error}` });
  }
  if (job.state === "error") return toast(`Processing stopped: ${job.error}`, { tone: "error", ms: 9000 });
  const result = job.result || {};
  if (result.kind === "index") return toast(`Indexed for search: ${result.indexed} emails and file sections.`, { tone: "ok" });
  if (result.kind === "vision") return visionDone(result);
  const read = typeof result.ingested === "number" ? `Read ${plural(result.ingested, "new file")}.` : "";
  toast(`Processed. ${read}`, { tone: "ok", ms: 6000 });
}

async function processMail() {
  try {
    const result = await postJSON("/api/process");
    const reading = !result.started && result.job && result.job.about && result.job.about.kind === "vision";
    toast(
      result.started
        ? "Processing new mail. The lists update as it goes."
        : reading
          ? "The vision model is reading pages of a scan. Stop it from the bar at the top, or try again when it's done."
          : "Already processing."
    );
    if (S.meta && result.started) {
      S.jobSeq += 1;
      S.meta.job = { ...result.job, stage_label: "Starting" };
      renderTop();
    }
    clearTimeout(S.poll);
    S.poll = setTimeout(poll, 600);
  } catch (error) {
    toast(error.message, { tone: "error" });
  }
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) poll();
});

/* ---------- Chat, palette, help ---------- */

const chat = createChat({
  root: $("#chat"),
  visionRead: (emailId, n) => visionRead(emailId, n),
  onToggle(open) {
    const button = $("#chat-btn");
    button.setAttribute("aria-expanded", String(open));
    button.classList.toggle("on", open);
  },
});

function localMail(words) {
  if (!words.length) return [];
  const out = [];
  for (const { items } of S.cache.values()) {
    for (const item of items || []) {
      if (!words.every((word) => item.text.includes(word))) continue;
      out.push({ id: item.emailId, subject: item.data.subject || item.data.title, sender: item.data.sender, locked: item.data.locked });
    }
  }
  return out;
}

const overlays = createOverlays({
  overlay: $("#overlay"),
  localMail,
  ask: (question) => chat.ask(question),
  openMail: (id) => navigate(mailUrl(id)),
  commands: () => [
    ...NAV.filter((item) => item.id).map((item) => ({ label: item.label, icon: item.icon, keywords: item.id === "today" ? "focus home" : "go", run: () => navigate(item.url) })),
    { label: "Ask CloseDesk", icon: "chat", keywords: "chat question", run: () => chat.open() },
    { label: "Process new mail", icon: "refresh", keywords: "import run", run: processMail },
    { label: isDark() ? "Switch to light theme" : "Switch to dark theme", icon: isDark() ? "sun" : "moon", keywords: "theme dark light", run: () => setTheme(isDark() ? "light" : "dark") },
    { label: root.dataset.rail === "collapsed" ? "Show the side bar labels" : "Collapse the side bar", icon: "sidebar", keywords: "rail sidebar", run: toggleRail },
    { label: "Keyboard shortcuts", icon: "help", keywords: "keys help", run: () => overlays.help() },
    { label: "Classic view", icon: "classic", keywords: "old dashboard", run: () => (location.href = "/") },
  ],
});

/* ---------- Clicks and keys ---------- */

const COMMANDS = {
  "toggle-rail": toggleRail,
  "back-to-list": closeEmail,
  palette: () => overlays.palette(),
  process: processMail,
  theme: () => setTheme(isDark() ? "light" : "dark"),
  help: () => overlays.help(),
  chat: () => chat.toggle(),
};

document.addEventListener("click", (event) => {
  const command = event.target.closest("[data-cmd]");
  if (command && COMMANDS[command.dataset.cmd]) {
    event.preventDefault();
    return COMMANDS[command.dataset.cmd]();
  }
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const anchor = event.target.closest("a[href]");
  if (!anchor) return;
  const href = anchor.getAttribute("href");
  if (href.startsWith("#") && href.length > 1) {
    const target = document.getElementById(href.slice(1));
    if (target) {
      event.preventDefault();
      if (target.tagName === "DETAILS") target.open = true;
      target.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "start" });
    }
    return;
  }
  if (!("nav" in anchor.dataset) || anchor.target === "_blank") return;
  const url = new URL(anchor.href, location.href);
  if (url.origin !== location.origin || !url.pathname.startsWith("/app")) return;
  event.preventDefault();
  if (anchor.dataset.key) S.sel = anchor.dataset.key;
  navigate(url.pathname + url.search, { replace: "replace" in anchor.dataset });
});

let goPending = 0;
const GO = { t: "/app", i: "/app/folder/important", n: "/app/folder/informational", r: "/app/folder/reference", a: "/app/all", k: "/app/tasks", f: "/app/fraud", c: "/app/coding", d: "/app/digest", s: "/app/settings" };

/* A field that takes typing: a textarea, a contenteditable, or a text-like input (not a checkbox or a button). */
const TEXT_INPUTS = new Set(["", "text", "search", "email", "number", "url", "tel", "password"]);
function isTextField(target) {
  if (!target) return false;
  if (target.isContentEditable || target.tagName === "TEXTAREA") return true;
  return target.tagName === "INPUT" && TEXT_INPUTS.has((target.getAttribute("type") || "").toLowerCase());
}

document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === "k") {
    event.preventDefault();
    if (overlays.isOpen()) overlays.hide();
    else overlays.palette();
    return;
  }
  if (overlays.isOpen()) {
    if (event.key === "Escape") {
      event.preventDefault();
      overlays.hide();
    }
    return;
  }
  if (event.key === "Escape") {
    if (chat.closeHistory()) return event.preventDefault();
    if (document.activeElement && document.activeElement.closest("#chat")) {
      if (chat.busy()) return;
      event.preventDefault();
      chat.close();
      $("#chat-btn").focus();
      return;
    }
    // In a text field of the reading pane (a reply draft, a note, find in file), Esc leaves the field, not the
    // email. A ticked checkbox or a chosen option keeps focus but holds nothing typed, so Esc closes as usual.
    if (isTextField(event.target)) {
      event.preventDefault();
      event.target.blur();
      return;
    }
    if (S.route && (S.route.emailId || S.route.file)) {
      event.preventDefault();
      closeEmail();
    }
    return;
  }
  if (isTyping(event.target) || event.metaKey || event.ctrlKey || event.altKey) return;
  if (goPending) {
    clearTimeout(goPending);
    goPending = 0;
    if (GO[event.key]) {
      event.preventDefault();
      navigate(GO[event.key]);
    }
    return;
  }
  // On a full page (the digest, Setup), the list behind it is hidden: its keys mustn't act on it.
  if (ws.classList.contains("doc") && ["j", "k", "ArrowDown", "ArrowUp", "Enter", "o", "e"].includes(event.key)) return;
  const inList = listPane.contains(document.activeElement) || document.activeElement === document.body;
  const onControl = event.target.closest && event.target.closest("a, button, summary, [role=tab]");
  switch (event.key) {
    case "j":
      event.preventDefault();
      return move(1);
    case "k":
      event.preventDefault();
      return move(-1);
    case "ArrowDown":
    case "ArrowUp":
      if (!inList || onControl) return;
      event.preventDefault();
      return move(event.key === "ArrowDown" ? 1 : -1);
    case "Enter":
    case "o":
      if (event.key === "Enter" && onControl) return;
      if (!S.sel) return;
      event.preventDefault();
      return openSelected();
    case "e":
      event.preventDefault();
      return doneSelected();
    case "/": {
      const search = $(".list-search", listPane);
      if (!search || ws.classList.contains("doc")) return overlays.palette();
      event.preventDefault();
      search.focus();
      search.select();
      return;
    }
    case "?":
      event.preventDefault();
      return overlays.help();
    case "c":
      event.preventDefault();
      return chat.toggle();
    case "[":
      event.preventDefault();
      return toggleRail();
    case "u":
      event.preventDefault();
      return closeEmail();
    case "g":
      goPending = setTimeout(() => (goPending = 0), 1200);
      return;
  }
});

/* ---------- Start ---------- */

async function boot() {
  buildRail();
  themeButton();
  replace($(".palette-icon"), icon("search", 15));
  replace($("[data-cmd=help]"), icon("help", 17));
  replace($(".top-back"), icon("back", 17));
  replace($(".chat-icon"), icon("chat", 16));
  try {
    S.meta = await getJSON("/api/state");
  } catch (error) {
    replace(reader, errorView(`CloseDesk didn't answer (${error.message}). Is it still running?`, () => location.reload()));
    return;
  }
  renderTop();
  updateRail("");
  chat.setModel(S.meta.model);
  await sync();
  S.poll = setTimeout(poll, S.meta.job.state === "running" ? 1500 : 15000);
  document.body.classList.add("ready");
}

boot();
