/* The middle pane: what each list is, where its rows come from, how a row looks, and its quick filters. */

import { h, icon, plural } from "./dom.js";
import { getJSON } from "./api.js";

const enc = encodeURIComponent;

const mailFilters = [
  { id: "all", label: "All" },
  { id: "high", label: "High+", test: (d) => d.importance === "critical" || d.importance === "high" },
  { id: "files", label: "Files", test: (d) => d.files > 0 },
  { id: "money", label: "Amounts", test: (d) => d.amount !== null && d.amount !== undefined },
  { id: "tasks", label: "Open tasks", test: (d) => d.open_tasks > 0 },
  { id: "flagged", label: "Fraud flags", test: (d) => d.locked || d.caution },
];

const focusFilters = [
  { id: "all", label: "All" },
  { id: "late", label: "Overdue", test: (d) => d.kind === "overdue" },
  { id: "today", label: "Due today", test: (d) => d.kind === "due_today" },
  { id: "soon", label: "This week", test: (d) => d.kind === "due_soon" || d.kind === "due_week" },
  { id: "new", label: "New", test: (d) => d.kind === "new_task" || d.kind === "decide" },
  { id: "fraud", label: "Verify", test: (d) => d.kind === "fraud" },
];

const mailText = (d) => `${d.subject} ${d.sender} ${d.sender_email || ""} ${d.summary || ""} ${d.why || ""} ${d.invoice || ""} ${d.category_label || ""}`.toLowerCase();

const asMail = (listKey) => (d) => ({ key: d.id, emailId: d.id, kind: "mail", data: d, text: mailText(d), done: Boolean(d.done), listKey });

/** What a list key ("today", "folder:important", "search:acme", "tasks:open", ...) means. */
export function listSpec(key) {
  const [base, arg = "", extra = ""] = key.split(":");
  if (base === "folder") {
    const done = extra === "done";
    return {
      key,
      nav: `folder:${arg}`,
      url: `/app/folder/${enc(arg)}${done ? "?done=1" : ""}`,
      api: `/api/mail?folder=${enc(arg)}${done ? "&done=1" : ""}`,
      kind: "mail",
      folder: arg,
      done,
      filters: mailFilters,
      empty: done ? "Nothing marked done here yet." : "Nothing in this folder needs you. Nice.",
      items: (data) => data.items.map(asMail(key)),
      title: (data) => (data ? data.title : arg.charAt(0).toUpperCase() + arg.slice(1)) + (done ? " · done" : ""),
    };
  }
  if (base === "search" || base === "all") {
    const q = base === "search" ? key.slice("search:".length) : "";
    return {
      key,
      nav: "all",
      url: q ? `/app/all?q=${enc(q)}` : "/app/all",
      api: q ? `/api/mail?q=${enc(q)}` : "/api/mail",
      kind: "mail",
      q,
      filters: mailFilters,
      empty: q ? `No mail mentions “${q}”.` : "No mail yet. Process new mail to fill it.",
      items: (data) => data.items.map(asMail(key)),
      title: () => (q ? "Search" : "All mail"),
    };
  }
  if (base === "tasks") {
    const status = ["open", "done", "all"].includes(arg) ? arg : "open";
    return {
      key: `tasks:${status}`,
      nav: "tasks",
      url: `/app/tasks${status === "open" ? "" : `?status=${status}`}`,
      api: `/api/tasks?status=${status}`,
      kind: "task",
      status,
      filters: [
        { id: "all", label: "All" },
        { id: "late", label: "Overdue", test: (d) => d.overdue },
        { id: "high", label: "High+", test: (d) => d.priority === "critical" || d.priority === "high" },
        { id: "dated", label: "Dated", test: (d) => Boolean(d.due) },
      ],
      empty: status === "done" ? "No finished tasks yet." : "No open tasks. Everything is done.",
      items: (data) =>
        data.items.map((d) => ({ key: d.id, emailId: d.email_id, kind: "task", data: d, text: `${d.title} ${d.detail} ${d.subject} ${d.sender}`.toLowerCase(), done: d.status === "done" })),
      title: () => "Tasks",
    };
  }
  if (base === "fraud") {
    return {
      key: "fraud",
      nav: "fraud",
      url: "/app/fraud",
      api: "/api/fraud",
      kind: "fraud",
      filters: [
        { id: "all", label: "All" },
        { id: "high", label: "Blocked", test: (d) => d.level === "high" },
        { id: "caution", label: "Caution", test: (d) => d.level === "caution" },
      ],
      empty: "Nothing is flagged right now.",
      items: (data) =>
        data.flagged.map((d) => ({ key: d.id, emailId: d.id, kind: "fraud", data: d, text: `${d.subject} ${d.sender} ${d.sender_email} ${d.signals.join(" ")}`.toLowerCase() })),
      title: () => "Fraud check",
    };
  }
  if (base === "coding") {
    const status = ["review", "suggested", "unmatched", "confirmed", "all"].includes(arg) ? arg : "review";
    return {
      key: `coding:${status}`,
      nav: "coding",
      url: `/app/coding${status === "review" ? "" : `?status=${status}`}`,
      api: `/api/coding?status=${status}`,
      kind: "coding",
      status,
      filters: [{ id: "all", label: "All" }],
      empty: status === "review" ? "Nothing waiting: every AP invoice is confirmed." : "No AP invoices here.",
      items: (data) =>
        data.items.map((d) => ({
          key: d.id,
          emailId: d.id,
          kind: "coding",
          data: d,
          text: `${d.subject} ${d.sender} ${d.invoice} ${d.codes.map((c) => `${c.code} ${c.description}`).join(" ")}`.toLowerCase(),
        })),
      title: () => "AP coding",
    };
  }
  return {
    key: "today",
    nav: "today",
    url: "/app",
    api: "/api/focus",
    kind: "focus",
    filters: focusFilters,
    empty: "Nothing needs you right now.",
    items: (data) =>
      data.focus.map((d) => ({
        key: d.action_id || d.email_id,
        emailId: d.email_id,
        kind: "focus",
        data: d,
        text: `${d.title} ${d.subject} ${d.sender} ${d.summary || ""} ${d.label}`.toLowerCase(),
      })),
    title: () => "Today",
  };
}

/** Mail lists come 200 at a time; ``want`` asks for more of them, a page at a time. */
export const MAIL_PAGE = 200;

export async function loadList(spec, signal, want = MAIL_PAGE) {
  if (spec.kind !== "mail") return getJSON(spec.api, { signal });
  const sep = spec.api.includes("?") ? "&" : "?";
  const data = await getJSON(`${spec.api}${sep}limit=${Math.min(want, 500)}`, { signal });
  const seen = new Set(data.items.map((d) => d.id));
  while (data.items.length < Math.min(want, data.total)) {
    const next = await getJSON(`${spec.api}${sep}limit=${Math.min(want - data.items.length, 500)}&offset=${data.items.length}`, { signal });
    if (!next.items.length) break;
    // Mail filed away between two pages shifts the rest by one; show each email once.
    data.items.push(...next.items.filter((d) => !seen.has(d.id) && seen.add(d.id)));
    data.total = next.total;
  }
  return data;
}

/* ---------- Rows ---------- */

const chip = (text, cls = "", iconName = "") => h("span", { class: `chip ${cls}` }, iconName ? icon(iconName, 12) : null, text);

function mailRow(d) {
  return [
    h("div", { class: "item-row1" }, h("span", { class: "item-from" }, d.sender || "(unknown sender)"), h("time", { class: "item-when", title: d.when_full }, d.when)),
    h("div", { class: "item-subj" }, d.subject),
    d.summary || d.why ? h("div", { class: "item-snip" }, d.summary || d.why) : null,
    h(
      "div",
      { class: "item-meta" },
      d.locked ? chip("Files locked", "chip-danger", "lock") : d.caution ? chip("Check before paying", "chip-warn", "alert") : null,
      d.amount_label ? chip(d.amount_label, "chip-num") : null,
      d.due ? chip(`Due ${d.due}`) : null,
      d.files ? chip(String(d.files), "", "clip") : null,
      d.open_tasks ? chip(plural(d.open_tasks, "task"), "", "tasks") : null,
      h("span", { class: "item-cat" }, d.category_label)
    ),
  ];
}

function focusRow(d) {
  return [
    h(
      "div",
      { class: "item-row1" },
      h("span", { class: `tag tag-${d.kind}` }, d.label),
      d.amount_label ? h("span", { class: "item-amount" }, d.amount_label) : null
    ),
    h("div", { class: "item-subj" }, d.title),
    h("div", { class: "item-snip" }, d.sender, d.title !== d.subject ? ` · ${d.subject}` : ""),
    d.more_tasks || d.files || d.locked
      ? h(
          "div",
          { class: "item-meta" },
          d.locked ? chip("Files locked", "chip-danger", "lock") : null,
          d.more_tasks ? chip(`+${plural(d.more_tasks, "more task")}`) : null,
          d.files ? chip(String(d.files), "", "clip") : null
        )
      : null,
  ];
}

function taskRow(d) {
  return [
    h(
      "div",
      { class: "item-row1" },
      h("span", { class: `tag tag-prio-${d.priority}` }, d.priority_label),
      d.due ? h("span", { class: `item-when${d.overdue ? " late" : ""}` }, d.due_label) : null
    ),
    h("div", { class: "item-subj" }, d.title),
    h("div", { class: "item-snip" }, `${d.sender} · ${d.subject}`),
  ];
}

function fraudRow(d) {
  return [
    h(
      "div",
      { class: "item-row1" },
      h("span", { class: `tag ${d.level === "high" ? "tag-fraud" : "tag-due_soon"}` }, d.level === "high" ? "Blocked" : "Caution"),
      d.cleared ? h("span", { class: "chip" }, "you cleared it") : null,
      h("span", { class: "item-when" }, `score ${d.score} · ${d.when}`)
    ),
    h("div", { class: "item-subj" }, d.subject),
    h("div", { class: "item-snip" }, d.sender_email || d.sender),
    d.signals.length ? h("div", { class: "item-snip faint" }, d.signals.join(" · ")) : null,
  ];
}

const CODING_STATE = { confirmed: "Confirmed", suggested: "Suggested", unmatched: "No code found" };

function codingRow(d) {
  return [
    h(
      "div",
      { class: "item-row1" },
      h("span", { class: `tag tag-coding-${d.status}` }, CODING_STATE[d.status] || d.status),
      h("span", { class: "item-when" }, d.when)
    ),
    h("div", { class: "item-subj" }, d.subject),
    h("div", { class: "item-snip" }, d.sender, d.invoice ? ` · ${d.invoice}` : ""),
    h(
      "div",
      { class: "item-meta" },
      d.amount_label ? chip(d.amount_label, "chip-num") : null,
      d.codes.length ? d.codes.map((c) => h("code", { class: "cost-code", title: c.description }, c.code)) : h("span", { class: "item-cat" }, "no code")
    ),
  ];
}

const ROWS = { mail: mailRow, focus: focusRow, task: taskRow, fraud: fraudRow, coding: codingRow };

/** Can this row be marked done from the list (e, or its check button)? */
export function doneLabel(item, spec) {
  if (item.kind === "task") return item.done ? "Reopen task" : "Mark task done";
  if (item.kind === "focus") return item.data.action_id ? "Mark task done" : "Mark email done";
  if (item.kind === "mail") return spec.done || item.done ? "Put back in the list" : "Mark done";
  return "";
}

export function rowEl(item, spec, { selected, open, onDone }) {
  const d = item.data;
  const importance = d.importance || d.priority || "";
  const href = `/app/mail/${enc(item.emailId)}?in=${enc(spec.key)}`;
  const label = doneLabel(item, spec);
  const undoing = label === "Put back in the list" || label === "Reopen task";
  const li = h(
    "li",
    {
      class: `item kind-${item.kind}${importance ? ` imp-${importance}` : ""}${d.locked || d.level === "high" ? " is-locked" : ""}${item.done ? " is-done" : ""}${selected ? " is-sel" : ""}${open ? " is-open" : ""}`,
      dataset: { key: item.key },
      role: "option",
      "aria-selected": selected ? "true" : "false",
    },
    item.kind === "focus" ? h("span", { class: "item-rank", "aria-hidden": "true" }, String(d.rank || "")) : null,
    h("a", { class: "item-main", href, dataset: { nav: "", key: item.key }, tabindex: "-1" }, ROWS[item.kind](d)),
    label
      ? h(
          "button",
          {
            type: "button",
            class: `item-done${undoing ? " undo" : ""}`,
            title: `${label} (e)`,
            "aria-label": label,
            onclick: (event) => {
              event.preventDefault();
              event.stopPropagation();
              onDone(item);
            },
          },
          icon(undoing ? "undo" : "check", 15)
        )
      : null
  );
  return li;
}
