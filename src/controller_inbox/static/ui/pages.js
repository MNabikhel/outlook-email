/* What the reading pane shows when no email is open (the day's overview, the fraud and coding summaries),
   and the two full-width pages: the daily digest and settings. */

import { h, icon, plural, toast } from "./dom.js";

const enc = encodeURIComponent;
const mailLink = (id, text, listKey) => h("a", { href: `/app/mail/${enc(id)}${listKey ? `?in=${enc(listKey)}` : ""}`, dataset: { nav: "" } }, text);

function kpi(label, value, tone = "", href = "") {
  const inner = [h("span", null, label), h("b", null, String(value))];
  return href ? h("a", { class: `kpi ${tone}`, href, dataset: { nav: "" } }, inner) : h("div", { class: `kpi ${tone}` }, inner);
}

function card(title, ...children) {
  return h("section", { class: "card" }, title ? h("h2", { class: "rd-h" }, title) : null, ...children);
}

function itemList(items, empty, render) {
  return items.length ? h("ul", { class: "plain-list" }, items.map((item) => h("li", null, render(item)))) : h("p", { class: "muted" }, empty);
}

export function emptyReader(text, hints = true) {
  return h(
    "div",
    { class: "rd-empty" },
    icon("mail", 30),
    h("p", null, text),
    hints
      ? h(
          "p",
          { class: "muted small keys" },
          h("kbd", null, "j"),
          " ",
          h("kbd", null, "k"),
          " to move · ",
          h("kbd", null, "Enter"),
          " to open · ",
          h("kbd", null, "e"),
          " done · ",
          h("kbd", null, "?"),
          " all shortcuts"
        )
      : null
  );
}

export function todayView(data, meta) {
  const k = data.kpis;
  return h(
    "div",
    { class: "page" },
    h(
      "div",
      { class: "page-inner" },
      h("p", { class: "kicker" }, meta.today_long, meta.finance ? ` · ${plural(meta.days_to_close, "day")} to month-end` : ""),
      h("h1", { class: "page-title" }, data.headline),
      h(
        "div",
        { class: "kpis" },
        kpi("Need you", k.need_you, "", "/app/folder/important"),
        kpi("Open tasks", k.open_actions, "", "/app/tasks"),
        kpi("Overdue", k.overdue_actions, k.overdue_actions ? "warn" : ""),
        kpi("Due today", k.due_today),
        kpi("Fraud flags", meta.counts.fraud_alerts, meta.counts.fraud_alerts ? "danger" : "", "/app/fraud"),
        meta.coding.review ? kpi("AP to code", meta.coding.review, "", "/app/coding") : null
      ),
      data.alerts.length
        ? h(
            "section",
            { class: "card card-danger" },
            h("h2", { class: "rd-h" }, icon("alert", 14), " Do not process — verify by phone"),
            itemList(data.alerts, "", (item) => [mailLink(item.id, item.subject, "today"), h("small", null, `${item.sender} · ${item.summary || ""}`)])
          )
        : null,
      h(
        "div",
        { class: "cards2" },
        card(
          "Coming up in the next 7 days",
          itemList(data.due_this_week, "Nothing else is dated this week.", (item) => [mailLink(item.email_id, item.title, "today"), h("small", null, `Due ${item.due_label} · ${item.subject}`)])
        ),
        card(
          `What came in since ${data.since_label}`,
          h(
            "ul",
            { class: "plain-list folders" },
            meta.folders.map((folder) =>
              h(
                "li",
                null,
                h("a", { href: `/app/folder/${folder.key}`, dataset: { nav: "" } }, folder.label),
                h("b", { class: "num" }, String(data.new_mail[folder.key] || 0))
              )
            )
          ),
          h("p", { class: "muted small" }, `${plural(meta.counts.emails, "email")} in all · `, h("a", { href: "/app/digest", dataset: { nav: "" } }, "the daily digest"), " is this page as a file to print or forward.")
        )
      ),
      h("p", { class: "muted small keys" }, "Work down the focus list: ", h("kbd", null, "j"), "/", h("kbd", null, "k"), " to move, ", h("kbd", null, "Enter"), " to open, ", h("kbd", null, "e"), " to mark done.")
    )
  );
}

export function fraudView(data) {
  return h(
    "div",
    { class: "page" },
    h(
      "div",
      { class: "page-inner" },
      h("p", { class: "kicker" }, "Fraud check"),
      h("h1", { class: "page-title" }, data.flagged.length ? `${plural(data.flagged.length, "email")} flagged right now.` : "Nothing is flagged right now."),
      h(
        "p",
        { class: "lede" },
        `Each email gets a score from its signals. At ${data.high_at} or more it is blocked as possible payment fraud: nothing on it is processed and its files are locked. From ${data.caution_at} it gets a “double-check before paying” note that doesn't block work.`
      ),
      h(
        "div",
        { class: "kpis" },
        kpi("Blocked", data.flagged.filter((f) => f.level === "high").length, "danger"),
        kpi("Caution", data.flagged.filter((f) => f.level === "caution").length, "warn"),
        kpi("Trusted domains", data.trusted_domains),
        kpi("Trusted senders", data.trusted_senders),
        kpi("Reported", data.reported)
      ),
      card(
        "Fraud log",
        itemList(data.log, "Nothing logged yet. Flags, level changes, and your verdicts appear here.", (row) => [
          h("span", { class: `tag tag-log` }, row.event.replace(/_/g, " ")),
          " ",
          row.email_id ? mailLink(row.email_id, row.subject || "(no subject)", "fraud") : h("b", null, row.sender_email),
          h("small", null, [row.when, row.level ? `${row.level} ${row.score}` : "", row.note].filter(Boolean).join(" · ")),
        ]),
        h("p", { class: "small" }, h("a", { href: "/fraud" }, "Manage trusted and reported domains"), " · ", h("a", { href: "/fraud/log.csv" }, "Download the log (CSV)"))
      )
    )
  );
}

export function codingView(data) {
  const c = data.counts;
  return h(
    "div",
    { class: "page" },
    h(
      "div",
      { class: "page-inner" },
      h("p", { class: "kicker" }, "AP coding"),
      h("h1", { class: "page-title" }, c.review ? `${plural(c.review, "invoice")} to review.` : "Every AP invoice is confirmed."),
      h("p", { class: "lede" }, "Codes come from your workbook: a code printed on the invoice, the one you last confirmed for the sender, or a description whose words are on it. Nothing is final until you confirm it."),
      h("div", { class: "kpis" }, kpi("Suggested", c.suggested), kpi("No code found", c.unmatched, c.unmatched ? "warn" : ""), kpi("Confirmed", c.confirmed, "ok"), kpi("Codes in workbook", data.codebook_size)),
      data.warnings.map((warning) => h("p", { class: "note" }, warning)),
      h("p", { class: "small" }, h("a", { href: "/coding" }, "Open the workbook and check invoices again"), " (classic AP coding page)")
    )
  );
}

/* ---------- Digest ---------- */

export function digestView(data) {
  const p = data.payload;
  const nav = h(
    "div",
    { class: "doc-nav" },
    data.older ? h("a", { class: "btn btn-sm", href: `/app/digest?date=${enc(data.older)}`, dataset: { nav: "" } }, "← ", data.older) : null,
    data.date ? h("span", { class: "doc-date" }, data.date) : null,
    data.newer ? h("a", { class: "btn btn-sm", href: `/app/digest?date=${enc(data.newer)}`, dataset: { nav: "" } }, data.newer, " →") : null,
    h("span", { class: "rd-bar-gap" }),
    data.printable ? h("a", { class: "btn btn-sm", href: data.printable, target: "_blank", rel: "noopener" }, icon("external", 14), "Printable page") : null,
    h("a", { class: "btn btn-sm", href: "/digests" }, "All past digests")
  );
  if (!p) {
    return h("div", { class: "page" }, h("div", { class: "page-inner" }, nav, h("h1", { class: "page-title" }, "No digest yet."), h("p", { class: "lede" }, "Process new mail and a digest is saved for the day.")));
  }
  const list = (items, empty, summary = false) =>
    itemList(items || [], empty, (item) => [
      mailLink(item.email_id || item.id, item.title || item.subject),
      h("small", null, [item.sender || item.priority || "", item.due_label || item.due_date || item.due ? `due ${item.due_label || item.due_date || item.due}` : "", item.amount ? `$${Number(item.amount).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : ""].filter(Boolean).join(" · ")),
      summary && item.summary ? h("p", { class: "muted small" }, item.summary) : null,
    ]);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(data.markdown || "");
      toast("Markdown copied. Paste it into an email or notes.");
    } catch (error) {
      toast("Couldn't copy. Select the text instead.", { tone: "error" });
    }
  };
  return h(
    "div",
    { class: "page" },
    h(
      "div",
      { class: "page-inner" },
      nav,
      h("p", { class: "kicker" }, `Daily digest · ${p.date_long || p.date}`),
      h("h1", { class: "page-title" }, p.headline || `Digest for ${p.date}`),
      p.window ? h("p", { class: "lede" }, `Covers mail since ${p.window.since_label}; open tasks carry over until you mark them done.`) : null,
      p.critical_alerts && p.critical_alerts.length ? h("section", { class: "card card-danger" }, h("h2", { class: "rd-h" }, icon("alert", 14), " Do not process — verify by phone"), list(p.critical_alerts, "")) : null,
      p.focus
        ? card(
            "Your focus",
            p.focus.length
              ? h("ol", { class: "plain-list ranked" }, p.focus.map((row) => h("li", null, h("span", { class: `tag tag-${row.kind}` }, row.label), " ", mailLink(row.email_id, row.title), h("small", null, `${row.sender}${row.more_tasks ? ` · +${row.more_tasks} more` : ""}`))))
              : h("p", { class: "muted" }, "Nothing needed you.")
          )
        : null,
      p.new_mail
        ? h(
            "div",
            { class: "cards3" },
            [["important", "Needs you"], ["informational", "Worth knowing"], ["reference", "Filed for reference"]].map(([key, label]) =>
              card(`${label} (${(p.new_mail[key] || []).length})`, list((p.new_mail[key] || []).slice(0, 12), "None.", key !== "reference"))
            )
          )
        : null,
      h(
        "div",
        { class: "cards2" },
        card("Overdue", list(p.overdue_actions, "Nothing overdue.")),
        card("Due today", list(p.due_today, "Nothing due today.")),
        card("Coming up in the next 7 days", list(p.due_this_week, "Nothing else dated this week.")),
        p.finance || (p.invoices_to_enter || []).length || (p.cash_to_apply || []).length
          ? card("Invoices to enter", list(p.invoices_to_enter, "No new vendor invoices."), h("h2", { class: "rd-h" }, "Cash to apply"), list(p.cash_to_apply, "No remittances."))
          : card("Open tasks with no due date", list((p.undated_actions || []).slice(0, 8), "Nothing undated is open."))
      ),
      data.markdown
        ? h("details", { class: "card" }, h("summary", { class: "rd-h" }, "Markdown copy"), h("div", { class: "row-actions" }, h("button", { type: "button", class: "btn btn-sm", onclick: copy }, icon("copy", 14), "Copy")), h("pre", { class: "md-copy" }, data.markdown))
        : null
    )
  );
}

/* ---------- Settings ---------- */

export function settingsView(meta, { theme, setTheme, railCollapsed, toggleRail }) {
  const segment = (value, label) =>
    h("button", { type: "button", class: `seg${theme === value ? " on" : ""}`, "aria-pressed": String(theme === value), onclick: () => setTheme(value) }, label);
  const shortcuts = [
    ["j / k", "Next / previous in the list"],
    ["Enter or o", "Open the selected email"],
    ["e", "Mark done (Undo in the message that appears)"],
    ["/", "Search"],
    ["Ctrl K or ⌘ K", "Jump to a folder or email, or ask a question"],
    ["c", "Show or hide Ask CloseDesk"],
    ["u or Esc", "Back to the list"],
    ["[", "Collapse the side bar"],
    ["g then t, i, n, r, a, k, f, c, d, s", "Go to Today, Important, Informational, Reference, All mail, Tasks, Fraud, AP coding, Digest, Settings"],
    ["?", "All shortcuts"],
  ];
  return h(
    "div",
    { class: "page" },
    h(
      "div",
      { class: "page-inner narrow" },
      h("p", { class: "kicker" }, "Settings"),
      h("h1", { class: "page-title" }, "How the workspace looks and works"),
      card(
        "Appearance",
        h("div", { class: "set-row" }, h("span", null, "Theme"), h("div", { class: "segs", role: "group", "aria-label": "Theme" }, segment("system", "Match the computer"), segment("light", "Light"), segment("dark", "Dark"))),
        h("div", { class: "set-row" }, h("span", null, "Side bar"), h("button", { type: "button", class: "btn btn-sm", onclick: toggleRail }, railCollapsed ? "Show labels" : "Collapse to icons"))
      ),
      card(
        "This computer",
        h(
          "dl",
          { class: "rd-facts" },
          h("dt", null, "Local model"),
          h("dd", null, meta.model.describe),
          h("dt", null, "Time zone"),
          h("dd", null, meta.tz),
          h("dt", null, "Profile"),
          h("dd", null, meta.finance ? "Finance (month-end and close sections)" : "General"),
          h("dt", null, "Mail"),
          h("dd", null, `${plural(meta.counts.emails, "email")}${meta.is_sample ? " (the sample mailbox)" : ""}${meta.last_run ? ` · last processed ${meta.last_run}` : ""}`)
        ),
        h("p", { class: "small" }, "The model, time zone, profile, search index and sample mailbox are set on the ", h("a", { href: "/settings" }, "classic Setup page"), ".")
      ),
      card(
        "Models CloseDesk uses",
        h(
          "dl",
          { class: "rd-facts models-list" },
          (meta.models || []).map((row) => [
            h("dt", null, row.role),
            h(
              "dd",
              null,
              h("span", { class: `model-state ${row.state}` }, { on: "Ready", fallback: "Older method", off: "Off" }[row.state] || row.state),
              row.model ? h("b", null, ` ${row.model}`) : null,
              h("span", { class: "small" }, ` ${row.status}`),
              row.note ? h("span", { class: "small muted model-note" }, row.note) : null
            ),
          ])
        ),
        h("p", { class: "small" }, "Which model answers, and which reads pages, are chosen on the ", h("a", { href: "/settings#models" }, "classic Setup page"), ".")
      ),
      card("Keyboard", h("dl", { class: "keys-list" }, shortcuts.map(([keys, what]) => [h("dt", null, h("kbd", null, keys)), h("dd", null, what)]))),
      card("Classic view", h("p", null, "Every page of the classic dashboard still works, and links back here. ", h("a", { href: "/" }, "Open the classic view"), "."))
    )
  );
}
