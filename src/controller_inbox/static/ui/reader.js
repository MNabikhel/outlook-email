/* The reading pane: one email (header, why it was flagged, what was read from it, tasks, files, coding,
   fraud check), and an attachment's Page, Tables and Text views. */

import { h, icon, replace, toast, plural, isTyping, $ } from "./dom.js";
import { getJSON, getBlob, postJSON, mailPath, filePath } from "./api.js";

const enc = encodeURIComponent;

const section = (title, attrs, ...children) =>
  h("section", { class: `rd-sec ${attrs.class || ""}`, id: attrs.id || null }, title ? h("h2", { class: "rd-h" }, title, attrs.aside || null) : null, ...children);

function btn(label, attrs = {}, iconName = "") {
  const { class: cls = "", ...rest } = attrs;
  return h("button", { type: "button", ...rest, class: `btn ${cls}` }, iconName ? icon(iconName, 15) : null, h("span", null, label));
}

/* ---------- Header and toolbar ---------- */

function toolbar(d, ctx) {
  const pos = ctx.position();
  const doneBtn = btn(d.done ? "Undo done" : "Done", { class: d.done ? "btn-quiet" : "btn-primary", title: d.done ? "Put it back in its list" : "Mark done (e)", onclick: () => ctx.emailDone(d, !d.done) }, d.done ? "undo" : "check");
  return h(
    "div",
    { class: "rd-bar" },
    d.folder ? doneBtn : null,
    btn("Ask", { class: "btn-quiet", title: "Ask CloseDesk about this email", onclick: () => ctx.ask("What does this email need from me?", d.id) }, "chat"),
    btn("Draft reply", { class: "btn-quiet", onclick: (event) => draftReply(d, event.currentTarget.closest(".rd")) }, "pen"),
    btn("Open in Outlook", { class: "btn-quiet", title: d.has_original ? "Open the original file with your mail app" : "Opens a copy of this email in your mail app", onclick: () => openOriginal(d) }, "mail"),
    d.locked || d.original_downloads === false ? null : h("a", { class: "btn btn-quiet", href: `${mailPath(d.id)}/original`, download: true, title: d.has_original ? "Download original" : "Download .eml" }, icon("download", 15), h("span", { class: "hide-narrow" }, "Download")),
    h("span", { class: "rd-bar-gap" }),
    pos.total
      ? h(
          "span",
          { class: "rd-pos" },
          h("button", { type: "button", class: "icon-btn", title: "Previous (k)", "aria-label": "Previous email", disabled: pos.index <= 0 ? true : null, onclick: () => ctx.step(-1) }, icon("up")),
          h("span", { class: "rd-pos-text" }, pos.index >= 0 ? `${pos.index + 1} of ${pos.total}` : `${pos.total}`),
          h("button", { type: "button", class: "icon-btn", title: "Next (j)", "aria-label": "Next email", disabled: pos.index >= pos.total - 1 ? true : null, onclick: () => ctx.step(1) }, icon("down"))
        )
      : null
  );
}

function header(d) {
  const differentReply = d.reply_to && d.reply_to.toLowerCase() !== (d.sender_email || "").toLowerCase();
  return h(
    "header",
    { class: "rd-head" },
    h(
      "div",
      { class: "rd-tags" },
      h("span", { class: `pill imp-${d.importance}` }, `${d.importance_label} · ${d.score}`),
      h("span", { class: "rd-tag" }, d.category_label),
      d.folder ? h("span", { class: "rd-tag" }, d.folder.charAt(0).toUpperCase() + d.folder.slice(1)) : null,
      d.done ? h("span", { class: "rd-tag ok" }, icon("check", 12), "Done") : null,
      d.model_status === "bionic" ? h("span", { class: "rd-tag" }, "Read by local model") : d.model_status === "corrected" ? h("span", { class: "rd-tag" }, "Corrected by you") : null
    ),
    h("h1", { class: "rd-subject" }, d.subject),
    h(
      "p",
      { class: "rd-from" },
      h("b", null, d.sender_name || d.sender_email),
      d.sender_name ? h("span", { class: "muted" }, ` <${d.sender_email}>`) : null,
      h("span", { class: "muted" }, ` · ${d.when_full}`)
    ),
    differentReply ? h("p", { class: "rd-replyto" }, icon("alert", 13), " Replies go to ", h("b", null, d.reply_to)) : null
  );
}

function alertBox(d) {
  if (d.locked) {
    return h(
      "div",
      { class: "rd-alert danger", role: "note" },
      icon("lock", 18),
      h(
        "div",
        null,
        h("b", null, "Possible payment fraud. Verify by phone before anything else."),
        h("p", null, "Use a number you already have. Don't pay, and don't reply with account details.", d.files ? " Its files stay locked until you mark it safe in the fraud check below." : ""),
        h("a", { href: "#rd-fraud", class: "rd-alert-link" }, "See the fraud check")
      )
    );
  }
  if (d.caution) {
    return h(
      "div",
      { class: "rd-alert warn", role: "note" },
      icon("alert", 18),
      h("div", null, h("b", null, "Worth a second look before any money moves."), h("p", null, "It doesn't block work. ", h("a", { href: "#rd-fraud" }, "See why")))
    );
  }
  return null;
}

/* ---------- What was read from it ---------- */

function facts(d) {
  const f = d.fields;
  const coding = d.coding;
  const rows = [
    ["Invoice", f.invoices.join(", ")],
    ["PO", f.pos.join(", ")],
    ["Amount", f.amounts.slice(0, 4).join(", ") + (f.amounts.length > 4 ? ` +${f.amounts.length - 4}` : "")],
    ["Due", f.due_dates.join(", ")],
    ["Vendor", f.vendors.slice(0, 3).join(", ")],
    ["Account", f.accounts.join(", ")],
    ["Cost code", coding && coding.codes.length ? coding.codes.map((c) => c.code).join(", ") : ""],
  ].filter(([, value]) => value);
  if (f.bank_details) rows.push(["Bank details", "Mentioned in the email"]);
  const reasons = d.reasons.length
    ? h("ul", { class: "rd-why" }, d.reasons.map((reason) => h("li", null, reason)))
    : h("p", { class: "muted" }, "No particular reason: filed by its category.");
  const flags = (d.flags || []).filter((flag) => !["needs_model"].includes(flag));
  return h(
    "div",
    { class: "rd-grid2" },
    section("Why it was flagged", {}, reasons, flags.length ? h("div", { class: "rd-flags" }, flags.map((flag) => h("span", { class: `flag flag-${flag}` }, flag.replace(/_/g, " ")))) : null),
    section(
      "What was read from it",
      {},
      rows.length
        ? h("dl", { class: "rd-facts" }, rows.map(([label, value]) => [h("dt", null, label), h("dd", { class: label === "Amount" ? "num" : "" }, value)]))
        : h("p", { class: "muted" }, "No invoice numbers, amounts or dates found.")
    )
  );
}

function tasks(d, ctx) {
  if (!d.tasks.length) return null;
  const open = d.tasks.filter((task) => task.status !== "done").length;
  return section(
    "Tasks",
    { aside: h("span", { class: "rd-h-aside" }, open ? `${open} open` : "all done") },
    h(
      "ul",
      { class: "rd-tasks" },
      d.tasks.map((task) => {
        const done = task.status === "done";
        return h(
          "li",
          { class: done ? "is-done" : "" },
          h(
            "button",
            {
              type: "button",
              class: "task-check",
              role: "checkbox",
              "aria-checked": done ? "true" : "false",
              "aria-label": done ? `Reopen: ${task.title}` : `Mark done: ${task.title}`,
              title: done ? "Reopen" : "Mark done",
              onclick: () => ctx.taskStatus(task, done ? "open" : "done", d),
            },
            icon("check", 13)
          ),
          h(
            "div",
            null,
            h("b", null, task.title),
            h("small", { class: task.overdue ? "late" : "" }, `${task.priority_label}${task.due_label ? ` · ${task.due_label}` : ""}`),
            task.detail && task.detail !== task.title ? h("p", null, task.detail) : null
          )
        );
      })
    )
  );
}

const SEARCH_STATE = { indexed: "Indexed for search", waiting: "Not indexed yet", changed: "Changed since indexed" };

function attachments(d, ctx) {
  if (!d.attachments.length) {
    return (d.flags || []).includes("missing_attachment")
      ? section("Attachments", {}, h("p", { class: "muted" }, "No attachments, though the sender said there was one."))
      : null;
  }
  const cards = d.attachments.map((file) => {
    const base = filePath(d.id, file.n);
    const tables = file.tables || 0;
    return h(
      "div",
      { class: `file${d.locked ? " locked" : ""}` },
      h(
        "div",
        { class: "file-top" },
        h("span", { class: "file-ic" }, icon(d.locked ? "lock" : tables ? "table" : "file", 16)),
        h("div", { class: "file-name" }, h("b", { title: file.name }, file.name), h("small", null, `${file.kind} · ${file.size} · ${file.type_label}${file.confidence ? ` ${file.confidence}%` : ""}`))
      ),
      h(
        "div",
        { class: "file-tags" },
        file.invoice ? h("span", { class: "chip" }, file.invoice) : null,
        file.amount_label ? h("span", { class: "chip chip-num" }, file.amount_label) : null,
        tables ? h("span", { class: "chip" }, plural(tables, "table")) : null,
        SEARCH_STATE[file.search] ? h("span", { class: "chip faint" }, SEARCH_STATE[file.search]) : null
      ),
      d.locked
        ? h("p", { class: "file-locked" }, "Locked: possible payment fraud. Not shown, downloaded or read by Ask CloseDesk until you mark the email safe.")
        : file.clip
          ? h("p", { class: "file-clip" }, file.clip)
          : h("p", { class: "file-clip muted" }, "No readable text (a scan or a picture)."),
      d.locked
        ? null
        : h(
            "div",
            { class: "file-actions" },
            file.preview ? h("a", { class: "btn btn-sm", href: ctx.fileUrl(file.n, "page"), dataset: { nav: "" }, title: "The page itself, with where each piece of text was read" }, icon("eye", 14), "Page") : null,
            tables ? h("a", { class: "btn btn-sm btn-primary", href: ctx.fileUrl(file.n, "tables"), dataset: { nav: "" } }, icon("table", 14), "Tables") : null,
            file.has_text ? h("a", { class: "btn btn-sm", href: ctx.fileUrl(file.n, "text"), dataset: { nav: "" } }, icon("text", 14), "Text") : null,
            file.view ? h("a", { class: "btn btn-sm", href: `${base}/view`, target: "_blank", rel: "noopener" }, icon("external", 14), "Open") : null,
            file.download ? h("a", { class: "btn btn-sm", href: `${base}/download` }, icon("download", 14), "Download") : null,
            file.blocked_type ? h("span", { class: "muted small", title: "Programs, scripts and macro files only open from Outlook" }, "Opens from Outlook") : null,
            file.has_text
              ? h("button", { type: "button", class: "btn btn-sm", onclick: () => ctx.ask(`Summarize the attachment "${file.name}": what it is, the key figures, and anything I need to act on.`, d.id) }, "Summarize")
              : null
          )
    );
  });
  return section("Attachments", { aside: h("span", { class: "rd-h-aside" }, plural(d.attachments.length, "file")) }, h("div", { class: "files" }, cards));
}

/* ---------- AP coding ---------- */

const CODING_STATE = { confirmed: "Confirmed", suggested: "Suggested · check and confirm", unmatched: "No code found" };

function coding(d, ctx) {
  const c = d.coding;
  if (!c) return null;
  const save = async (body) => {
    try {
      const result = await postJSON(`/api/mail/${enc(d.id)}/coding`, body);
      toast(result.message, { tone: "ok" });
      ctx.reload();
    } catch (error) {
      toast(error.message, { tone: "error" });
    }
  };
  let picker = null;
  if (c.codebook_size) {
    const filter = h("input", { type: "search", placeholder: "Filter by code or description", "aria-label": "Filter cost codes", autocomplete: "off" });
    const options = c.choices.map((pick) =>
      h(
        "label",
        { class: `code-pick${pick.likely ? " likely" : ""}`, dataset: { text: `${pick.code} ${pick.description}`.toLowerCase() } },
        h("input", { type: "checkbox", value: pick.code, checked: pick.checked ? true : null }),
        h("code", { class: "cost-code" }, pick.code),
        h("span", null, pick.description),
        pick.likely ? h("small", null, "possible") : null
      )
    );
    const none = h("p", { class: "muted small", hidden: true }, "No code matches that filter.");
    filter.addEventListener("input", () => {
      const words = filter.value.toLowerCase().split(/\s+/).filter(Boolean);
      let shown = 0;
      options.forEach((label) => {
        const match = words.every((word) => label.dataset.text.includes(word));
        label.hidden = !match;
        shown += match ? 1 : 0;
      });
      none.hidden = shown > 0;
    });
    picker = h(
      "details",
      { class: "code-revise" },
      h("summary", null, c.codes.length ? "Revise" : "Choose a code"),
      filter,
      h("div", { class: "code-list" }, options),
      none,
      h("p", { class: "muted small" }, "Tick more than one when the invoice is split across codes."),
      btn("Save and confirm", { class: "btn-primary btn-sm", onclick: () => save({ codes: options.filter((label) => $("input", label).checked).map((label) => $("input", label).value) }) })
    );
  }
  return section(
    "AP coding",
    { id: "rd-coding", class: `coding coding-${c.status}`, aside: h("span", { class: `tag tag-coding-${c.status}` }, CODING_STATE[c.status] + (c.status === "confirmed" && c.decided_label ? ` · ${c.decided_label}` : "")) },
    c.codes.length
      ? h(
          "ul",
          { class: "codes" },
          c.codes.map((item) =>
            h(
              "li",
              null,
              h("code", { class: "cost-code" }, item.code),
              h("div", null, h("b", null, item.description || "No description in the workbook"), h("small", null, `${item.source || ""}${item.where ? ` · ${item.where}` : ""}`), item.evidence ? h("q", null, item.evidence) : null)
            )
          )
        )
      : h("p", { class: "muted" }, c.codebook_size ? `None of your ${c.codebook_size} cost codes is written on this invoice, and no description matches it.` : "Your cost code workbook is empty. Fill it in from the classic AP coding page."),
    c.others && c.others.length && c.status !== "confirmed" ? h("p", { class: "small" }, "Also possible: ", c.others.map((item, i) => [i ? ", " : "", h("code", { class: "cost-code" }, item.code), ` ${item.description || ""}`])) : null,
    c.unlisted && c.unlisted.length ? h("p", { class: "note" }, "On the invoice but not in your workbook: ", c.unlisted.join(", ")) : null,
    h(
      "div",
      { class: "row-actions" },
      c.status !== "confirmed" && c.codes.length ? btn(c.codes.length > 1 ? "Confirm codes" : "Confirm code", { class: "btn-primary btn-sm", onclick: () => save({}) }, "check") : null,
      picker
    )
  );
}

/* ---------- Fraud check ---------- */

function fraudPanel(d, ctx) {
  const f = d.fraud;
  const level = f.level || "none";
  const note = h("input", { type: "text", maxlength: "300", placeholder: "Note for the log (optional), e.g. called Dana at the number on file", "aria-label": "Note for the fraud log" });
  const verdict = async (choice) => {
    try {
      const result = await postJSON(`/api/mail/${enc(d.id)}/fraud`, { choice, note: note.value });
      toast(result.message, { tone: "ok", ms: 6000 });
      ctx.reload(true);
    } catch (error) {
      toast(error.message, { tone: "error", ms: 6000 });
    }
  };
  const choices = [];
  if (f.verdict !== "safe" && (level !== "none" || f.verdict === "fraud")) choices.push(btn("Not fraud", { class: "btn-sm", onclick: () => verdict("safe:email") }));
  if (f.can_trust_sender) choices.push(btn(`Trust ${d.sender_email}`, { class: "btn-sm btn-quiet", onclick: () => verdict("safe:sender") }));
  if (f.can_trust_domain) choices.push(btn(`Trust @${f.sender_domain}`, { class: "btn-sm btn-quiet", onclick: () => verdict("safe:domain") }));
  if (f.verdict !== "fraud") choices.push(btn("This is fraud", { class: "btn-sm btn-danger", onclick: () => verdict("fraud:email") }));
  if (d.sender_email) choices.push(btn("Report this sender", { class: "btn-sm btn-danger-quiet", onclick: () => verdict("fraud:sender") }));
  const label = level === "high" ? "Possible payment fraud" : level === "caution" ? "Double-check before paying" : "Nothing suspicious";
  const body = [
    f.verdict === "safe" ? h("p", { class: "note" }, "You marked this email as not fraud.") : f.verdict === "fraud" ? h("p", { class: "note" }, "You reported this email as fraud.") : null,
    f.signals && f.signals.length
      ? h(
          "ul",
          { class: "signals" },
          f.signals.map((s) => h("li", null, h("b", { class: `pts ${s.points < 0 ? "minus" : "plus"}` }, `${s.points > 0 ? "+" : ""}${s.points}`), h("span", null, s.label, s.detail ? h("small", null, s.detail) : null)))
        )
      : null,
    h("div", { class: "verdict" }, note, h("div", { class: "row-actions" }, choices)),
    h("p", { class: "muted small" }, "Every answer goes to the fraud log and changes how much these signals count next time. A request to change bank details always gets at least a caution, even from someone you trust."),
  ];
  const head = h("span", { class: `tag ${level === "high" ? "tag-fraud" : level === "caution" ? "tag-due_soon" : "tag-ok"}` }, `${label} · score ${f.score}`);
  if (level === "none") {
    return h("details", { class: "rd-sec rd-fold", id: "rd-fraud" }, h("summary", { class: "rd-h" }, "Fraud check", h("span", { class: "rd-h-aside" }, head)), ...body);
  }
  return section("Fraud check", { id: "rd-fraud", class: `fraud-${level}`, aside: head }, ...body);
}

/* ---------- Notes, correction, body ---------- */

function findings(d, ctx) {
  if (!d.findings.length) return null;
  return section(
    "Notes from Ask CloseDesk",
    { aside: h("button", { type: "button", class: "btn btn-sm btn-quiet", onclick: async () => {
      try {
        toast((await postJSON(`/api/mail/${enc(d.id)}/findings/clear`)).message);
        ctx.reload();
      } catch (error) {
        toast(error.message, { tone: "error" });
      }
    } }, "Clear notes") },
    h("ul", { class: "rd-notes" }, d.findings.map((row) => h("li", null, row.text, row.question || row.when ? h("small", null, `${row.question ? `While answering “${row.question}”` : ""}${row.when ? ` · ${row.when}` : ""}`) : null)))
  );
}

function correction(d, ctx) {
  const select = h("select", { "aria-label": "Correct category" }, ctx.categories().map((c) => h("option", { value: c.value, selected: c.value === d.category ? true : null }, c.label)));
  const reason = h("textarea", { rows: "2", minlength: "3", required: true, placeholder: "Why was this wrong? e.g. Friday files from this vendor are remittances, not invoices.", "aria-label": "Why was this wrong?" });
  const save = async () => {
    if (reason.value.trim().length < 3) {
      reason.focus();
      return toast("Say briefly why the category was wrong.", { tone: "error" });
    }
    try {
      toast((await postJSON(`/api/mail/${enc(d.id)}/category`, { category: select.value, reason: reason.value })).message, { tone: "ok", ms: 6000 });
      ctx.reload(true);
    } catch (error) {
      toast(error.message, { tone: "error" });
    }
  };
  return h(
    "details",
    { class: "rd-sec rd-fold" },
    h("summary", { class: "rd-h" }, "Wrong category?", h("span", { class: "rd-h-aside" }, d.category_label)),
    h("p", { class: "muted small" }, "Say what this actually is, and why. The next message from this sender follows the correction, and the example is saved for the local model."),
    h("div", { class: "correct-form" }, select, reason, btn("Save and learn", { class: "btn-sm btn-primary", onclick: save }))
  );
}

/* ---------- Actions that talk to the server ---------- */

async function openOriginal(d) {
  try {
    const response = await fetch(`${mailPath(d.id)}/open`, { method: "POST", headers: { "X-CloseDesk": "1" } });
    const data = await response.json();
    if (!response.ok) return toast(data.detail || data.message || "CloseDesk couldn't open it.", { tone: "error" });
    toast(data.message || "Opening…");
    if (!data.ok && data.download) location.href = `${mailPath(d.id)}/original`;
  } catch (error) {
    location.href = `${mailPath(d.id)}/original`;
  }
}

async function draftReply(d, root) {
  let box = $(".draft", root);
  if (!box) {
    const text = h("textarea", { class: "draft-text", rows: "8", "aria-label": "Reply draft" });
    const askFor = h("input", { type: "text", maxlength: "300", placeholder: "Optional: what should it say? (e.g. yes, Friday)", "aria-label": "What the reply should say" });
    const note = h("span", { class: "draft-note muted small" });
    const mailto = h("a", { class: "btn btn-sm", hidden: true }, "Open in mail app");
    box = h(
      "section",
      { class: "draft" },
      h("div", { class: "draft-head" }, h("b", null, "Reply draft"), note, h("button", { type: "button", class: "icon-btn", "aria-label": "Close the draft", onclick: () => box.remove() }, icon("x", 14))),
      text,
      h(
        "div",
        { class: "draft-row" },
        askFor,
        btn("Redo", { class: "btn-sm", onclick: () => write() }),
        btn("Copy", { class: "btn-sm btn-primary", onclick: async () => {
          try {
            await navigator.clipboard.writeText(text.value);
          } catch (error) {
            text.select();
            document.execCommand("copy");
          }
          toast("Draft copied. Paste it into your reply.");
        } }, "copy"),
        mailto
      )
    );
    const write = async () => {
      text.value = "";
      text.placeholder = "Writing a draft…";
      note.textContent = "";
      try {
        const data = await postJSON(`${mailPath(d.id)}/draft`, { instructions: askFor.value });
        text.value = data.text || "";
        note.textContent = data.mode === "model" ? "Written by your local model. Check it before sending." : data.note || "";
        mailto.hidden = !data.mailto;
        if (data.mailto && /^mailto:/i.test(data.mailto)) mailto.setAttribute("href", data.mailto);
        box.classList.toggle("safety", data.mode === "safety");
      } catch (error) {
        note.textContent = `Couldn't write a draft: ${error.message}`;
      } finally {
        text.placeholder = "";
      }
    };
    box.write = write;
    $(".rd-head", root).after(box);
  }
  box.write();
  box.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

/* ---------- The email ---------- */

export function emailView(d, ctx) {
  const body = d.body.trim();
  return h(
    "article",
    { class: "rd", dataset: { email: d.id } },
    toolbar(d, ctx),
    h(
      "div",
      { class: "rd-scroll" },
      h(
        "div",
        { class: "rd-inner" },
        header(d),
        alertBox(d),
        d.summary ? h("p", { class: "rd-summary" }, d.summary) : null,
        facts(d),
        tasks(d, ctx),
        attachments(d, ctx),
        coding(d, ctx),
        section("Message", { class: "rd-body-sec" }, h("div", { class: "rd-body" }, body || "(no text)")),
        fraudPanel(d, ctx),
        findings(d, ctx),
        correction(d, ctx)
      )
    )
  );
}

/* ---------- An attachment: Tables and Text ---------- */

function checkBadge(check) {
  if (check.mismatched.length) {
    const n = check.mismatched.length;
    return h("span", { class: "badge warn", title: check.mismatched.join("\n") }, icon("alert", 13), `${n === 1 ? "1 total doesn't" : `${n} totals don't`} add up as read`);
  }
  if (check.matched) return h("span", { class: "badge ok" }, icon("ok", 13), `Totals check out (${check.matched})`);
  return h("span", { class: "badge" }, "No printed totals to check");
}

function grid(table) {
  const numeric = table.kinds.map((kind) => kind === "figure");
  const pages = new Set(table.rows.map((row) => row.page).filter(Boolean));
  const head = h("thead", null, h("tr", null, table.labels.map((label, i) => h("th", { scope: "col", class: numeric[i] ? "num" : "" }, label))));
  const body = h("tbody");
  let group = null;
  let page = null;
  for (const row of table.rows) {
    if (pages.size > 1 && row.page && row.page !== page) {
      page = row.page;
      body.append(h("tr", { class: "page-row" }, h("td", { colspan: String(table.labels.length) }, page)));
    }
    if (row.group && row.group !== group) {
      body.append(h("tr", { class: "group-row" }, h("td", { colspan: String(table.labels.length) }, row.group)));
    }
    group = row.group;
    body.append(
      h(
        "tr",
        { class: row.total ? "total" : "" },
        row.cells.map((cell, i) =>
          h(
            i === 0 ? "th" : "td",
            {
              scope: i === 0 ? "row" : null,
              class: `${numeric[i] ? "num" : ""}${row.flags && row.flags[i] ? " off" : ""}`,
              title: [row.refs[i], row.flags && row.flags[i]].filter(Boolean).join(" · ") || null,
            },
            cell
          )
        )
      )
    );
  }
  return h("div", { class: "grid-wrap", tabindex: "0", role: "region", "aria-label": `Table ${table.n}` }, h("table", { class: "grid" }, head, body));
}

function tableCard(table) {
  const body = table.rows.filter((row) => !row.total).length;
  const totals = table.rows.length - body;
  return h(
    "section",
    { class: "tbl" },
    h(
      "div",
      { class: "tbl-head" },
      h("div", null, h("b", null, `Table ${table.n}`), h("small", null, [table.where, plural(body, "row"), totals ? plural(totals, "total") : "", plural(table.labels.length, "column")].filter(Boolean).join(" · "))),
      checkBadge(table.check)
    ),
    table.check.mismatched.length
      ? h(
          "div",
          { class: "tbl-warn" },
          h("p", null, "These printed totals don't match the rows above them as CloseDesk read them, so a figure may be in the wrong row or column. Check them against the original."),
          h("ul", null, table.check.mismatched.map((message) => h("li", null, message)))
        )
      : null,
    grid(table),
    table.truncated ? h("p", { class: "muted small" }, `${table.truncated} more rows aren't shown.`) : null
  );
}

function highlight(text, words) {
  if (!words.length) return [text];
  const pattern = new RegExp(`(${words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "gi");
  return text.split(pattern).map((piece, i) => (i % 2 ? h("mark", null, piece) : piece));
}

/* ---------- Pages read two ways (OCR and the vision model) ---------- */

function marked(text, marks) {
  const wanted = [...new Set(marks || [])].filter(Boolean).sort((a, b) => b.length - a.length);
  if (!wanted.length) return [text];
  const pattern = new RegExp(`(?<![\\w.,])(${wanted.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})(?![\\w]|[.,]\\d)`, "g");
  return text.split(pattern).map((piece, i) => (i % 2 ? h("mark", null, piece) : piece));
}

function modelReading(blocks, marks) {
  return blocks.map((block) =>
    block.kind === "table"
      ? h(
          "div",
          { class: "vision-table-wrap" },
          h(
            "table",
            { class: "vision-table" },
            h("thead", null, h("tr", null, block.header.map((cell) => h("th", null, marked(cell, marks))))),
            h("tbody", null, block.rows.map((row) => h("tr", null, row.map((cell) => h("td", null, marked(cell, marks))))))
          )
        )
      : h("p", null, marked(block.text, marks))
  );
}

function visionBox(data, ctx, { brief = false } = {}) {
  const vision = data.vision || {};
  const offer = vision.offer;
  const readings = vision.readings || [];
  if (!offer || !(offer.pages.length || readings.length || (offer.reason && !data.parts.length))) return null;
  const { email, file } = data;
  const status = h("span", { class: "muted small", "aria-live": "polite" });
  const start = (again) => async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    status.textContent = "Starting…";
    const result = await ctx.visionRead(email.id, file.n, again);
    status.textContent = result ? result.message : "";
    if (!result || !result.started) button.disabled = false;
  };
  let body;
  if (ctx.visionReading(email.id, file.n)) {
    body = [h("p", null, "The vision model is reading this file now. Its reading shows here when it's done; the bar at the top can stop it.")];
  } else if (offer.available && offer.pages.length) {
    const which = `${offer.pages.length === 1 ? "page" : "pages"} ${offer.pages.join(", ")}`;
    body = [
      h("p", null, `${offer.text} The model looks at ${which} itself, and its reading is compared with the first one figure by figure.`),
      h("div", { class: "vision-go" }, btn("Read with the vision model", { class: "btn-sm btn-primary", onclick: start(false) }, "eye"), status),
    ];
  } else if (offer.reason) {
    body = [h("p", { class: "muted" }, offer.reason)];
  } else {
    body = [
      h("p", { class: "muted" }, `Every page that needed it has been read both ways${brief ? "" : " (below)"}.`),
      offer.available ? h("div", { class: "vision-go" }, btn("Read them again", { class: "btn-sm btn-quiet", onclick: start(true) }, "refresh"), status) : null,
    ];
  }
  const pages = readings.map((r) => {
    const c = r.comparison;
    const shown = c.choice === "model" ? "the vision model's" : `${r.first_name}'s`;
    return h(
      "article",
      { class: "vision-page", id: `vision-p${r.page}` },
      h(
        "h3",
        null,
        `Page ${r.page} · ${c.confirmed} of ${c.figures} figures read the same`,
        c.differ.length ? ` · ${c.differ.length} read differently` : "",
        ` · shown: ${shown} reading`,
        r.seconds ? ` · read in ${r.seconds}s` : ""
      ),
      h(
        "div",
        { class: "vision-columns" },
        h("div", null, h("h4", null, r.first_name), h("pre", { class: "vision-first" }, marked(r.first_text, r.marks_first))),
        h("div", null, h("h4", null, r.model ? `Vision model (${r.model})` : "Vision model"), h("div", { class: "vision-reading" }, modelReading(r.model_blocks, r.marks_model)))
      )
    );
  });
  return h(
    "section",
    { class: "vision-box" },
    h("h2", { class: "rd-h" }, icon("eye", 15), "Read with the vision model too"),
    body,
    // On the Page tab the two readings are on the page itself; the full side by side stays on the other tabs.
    readings.length && brief
      ? h("p", { class: "muted small" }, "Point at a box on the page to see both readings there, or ", h("a", { href: ctx.fileUrl(data.file.n, "text"), dataset: { nav: "", replace: "" } }, "see them side by side"), ".")
      : readings.length
      ? h(
          "div",
          { class: "vision-pages" },
          h("p", { class: "muted small" }, "Figures both readings have are confirmed. A ", h("mark", null, "marked"), " figure was read differently, or by one of them only: check it against the original. Ask CloseDesk reads each page as shown here."),
          pages
        )
      : null
  );
}

/* ---------- An attachment's page, with where each piece of text was read marked on it ---------- */

// One hover card for every page view, kept on <body> so the reading pane's scrolling and animation can't clip it.
let tip = null;
let pageUrl = "";
// A card opened to fix a box stays open while the pointer moves to it, until it is saved, cancelled or left.
let pinned = false;

function hideTip(force = false) {
  if (!tip || (pinned && force !== true)) return;
  pinned = false;
  tip.hidden = true;
  tip.classList.remove("pinned");
}

function showTip(box, children, { pin = false } = {}) {
  if (!tip) {
    tip = h("div", { class: "pv-tip", role: "tooltip", id: "pv-tip", hidden: true });
    document.body.append(tip);
    document.addEventListener("scroll", (event) => (!pinned || !tip.contains(event.target)) && hideTip(true), true);
    window.addEventListener("resize", () => hideTip(true));
    document.addEventListener("pointerdown", (event) => pinned && !tip.contains(event.target) && hideTip(true), true);
    document.addEventListener(
      "keydown",
      (event) => {
        if (event.key === "Escape" && pinned) {
          event.preventDefault();
          event.stopImmediatePropagation();
          hideTip(true);
        }
      },
      true
    );
  }
  if (pinned && !pin) return;
  pinned = pin;
  tip.classList.toggle("pinned", pin);
  tip.setAttribute("role", pin ? "dialog" : "tooltip");
  replace(tip, children);
  tip.hidden = false;
  // Below the box when it fits, else above it; never past the edges of the window.
  const at = box.getBoundingClientRect();
  const { offsetWidth: w, offsetHeight: tall } = tip;
  const gap = 8;
  const left = Math.min(Math.max(gap, at.left + at.width / 2 - w / 2), window.innerWidth - w - gap);
  let top = at.bottom + gap;
  if (top + tall > window.innerHeight - gap) top = at.top - tall - gap;
  tip.style.left = `${Math.max(gap, left)}px`;
  tip.style.top = `${Math.max(gap, Math.min(top, window.innerHeight - tall - gap))}px`;
}

const sure = (value) => `${Math.round(value * 100)}% sure`;
// Below this, how sure the vision model says it was makes a box worth checking even where OCR read the same.
const MODEL_SURE = 0.9;
const modelUnsure = (model) => Boolean(model) && model.sure !== null && model.sure !== undefined && model.sure < MODEL_SURE;

/** "ok" (green), "check" (amber) or "differs" (red), as the legend above the page explains. */
function tone(region, source) {
  if (region.fixed) return "fixed";
  const model = region.model;
  if (model && model.agrees === false) return "differs";
  if (source === "text") return "ok";
  if (modelUnsure(model)) return "check";
  if (model && model.agrees === true) return "ok";
  if (region.confidence !== null && region.confidence < 0.8) return "check";
  return model ? "check" : "ok";
}

function tipFor(region, view) {
  const lines = [];
  if (region.fixed) {
    lines.push(
      h("div", { class: "pv-tip-row" }, h("b", null, region.fixed.by === "you" ? "You fixed this" : "Fixed from what you taught it"), h("span", { class: "pv-tip-tag fixed" }, "fixed")),
      h("p", { class: "pv-tip-text" }, region.text),
      h("p", { class: "pv-tip-text muted" }, region.fixed.by === "you" ? `It was read as “${region.fixed.was}”.` : `Read as “${region.fixed.was}”, which you fixed on an earlier file from this sender.`)
    );
    return lines;
  }
  if (view.source === "text") {
    lines.push(h("div", { class: "pv-tip-row" }, h("b", null, "From the file's own text"), h("span", { class: "pv-tip-tag ok" }, "exact")));
  } else {
    const low = region.confidence !== null && region.confidence < 0.8;
    lines.push(h("div", { class: "pv-tip-row" }, h("b", null, "OCR read"), region.confidence === null ? null : h("span", { class: `pv-tip-tag ${low ? "check" : "ok"}` }, sure(region.confidence))));
  }
  lines.push(h("p", { class: "pv-tip-text" }, region.text));
  const model = region.model;
  if (model) {
    const [shade, words] =
      model.agrees === true ? ["ok", "agrees"] : model.agrees === false ? ["differs", "differs"] : model.text ? ["check", "close, not the same"] : ["check", "not in its reading"];
    const told = model.sure === null || model.sure === undefined ? null : h("span", { class: `pv-tip-tag ${modelUnsure(model) ? "check" : "ok"}` }, sure(model.sure));
    lines.push(
      h("div", { class: "pv-tip-row pv-tip-model" }, h("b", null, `${view.model_name} read`), told, h("span", { class: `pv-tip-tag ${shade}` }, words)),
      model.text ? h("p", { class: "pv-tip-text" }, model.text) : h("p", { class: "pv-tip-text muted" }, "Nothing it read matches this.")
    );
  }
  return lines;
}

// How big the page is drawn: the width of the pane, or its printed size (the page is drawn at 150 dots an inch, a
// screen shows 96) and half as big again. Remembered on this computer.
const ZOOMS = [
  ["fit", "Fit width"],
  ["100", "100%"],
  ["150", "150%"],
];
const ZOOM_KEY = "closedesk-page-zoom";

function savedZoom() {
  try {
    const value = localStorage.getItem(ZOOM_KEY);
    return ZOOMS.some(([key]) => key === value) ? value : "fit";
  } catch {
    return "fit";
  }
}

function saveZoom(value) {
  try {
    if (value === "fit") localStorage.removeItem(ZOOM_KEY);
    else localStorage.setItem(ZOOM_KEY, value);
  } catch {
    /* Private windows can refuse storage; the zoom then lasts until the page is left. */
  }
}

// Esc stops drawing a table, then closes a table opened over the page, before it leaves the file; one listener for
// every page view.
let closeOpenTable = null;
let stopOpenDrawing = null;
window.addEventListener(
  "keydown",
  (event) => {
    if (event.key !== "Escape" || isTyping(event.target)) return;
    if (stopOpenDrawing && stopOpenDrawing()) {
      event.preventDefault();
      event.stopImmediatePropagation();
      return;
    }
    if (!closeOpenTable) return;
    if (closeOpenTable()) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  },
  true
);

/** Calls back once the window has stopped scrolling (a box just scrolled to), so its card isn't closed by it. */
function settled(callback) {
  let timer = setTimeout(done, 90);
  function moved() {
    clearTimeout(timer);
    timer = setTimeout(done, 90);
  }
  function done() {
    document.removeEventListener("scroll", moved, true);
    callback();
  }
  document.addEventListener("scroll", moved, true);
}

// The box last flashed, so only one flashes at a time.
let flashed = null;

function flash(el) {
  if (flashed && flashed !== el) flashed.classList.remove("pv-flash");
  flashed = el;
  el.classList.remove("pv-flash");
  void el.offsetWidth; // start the animation again when it is already running
  el.classList.add("pv-flash");
  setTimeout(() => el.classList.remove("pv-flash"), 1800);
}

/** Boxes in reading order: line by line down the page, left to right along a line. */
function readingOrder(regions, indices) {
  const middle = (r) => r.y + r.h / 2;
  const sorted = [...indices].sort((a, b) => middle(regions[a]) - middle(regions[b]));
  const lines = [];
  for (const index of sorted) {
    const line = lines[lines.length - 1];
    const first = line && regions[line[0]];
    if (first && Math.abs(middle(first) - middle(regions[index])) <= 0.6 * Math.max(first.h, regions[index].h)) line.push(index);
    else lines.push([index]);
  }
  return lines.flatMap((line) => line.sort((a, b) => regions[a].x - regions[b].x));
}

/** How tall the file's bar is, which stays at the top of the reading pane. */
function barHeight() {
  const bar = document.querySelector(".file-view .rd-bar");
  return bar ? bar.offsetHeight : 0;
}

function pageView(data) {
  const { email, file } = data;
  const api = `/api/mail/${encodeURIComponent(email.id)}/files/${file.n}/pages`;
  const sheet = h("div", { class: "pv-sheet loading" });
  const frame = h("div", { class: "pv-frame" }, sheet);
  const where = h("span", { class: "pv-where", "aria-live": "polite" }, "Page 1");
  const prev = h("button", { type: "button", class: "icon-btn pv-prev", title: "Previous page", "aria-label": "Previous page", disabled: true }, icon("right", 15));
  const next = h("button", { type: "button", class: "icon-btn", title: "Next page", "aria-label": "Next page", disabled: true }, icon("right", 15));
  const about = h("p", { class: "pv-about" });
  const legend = h("div", { class: "pv-legend" });
  const keys = h("div", { class: "pv-keys", hidden: true });
  const tools = h("div", { class: "pv-tools", hidden: true });
  const panel = h("section", { class: "pv-panel", hidden: true, "aria-live": "polite" });
  let page = 1;
  let pages = 0;
  let token = 0;
  let zoom = savedZoom();
  let view = null;

  const key = (shade, words) => h("span", { class: "pv-key" }, h("i", { class: `pv-swatch ${shade}` }), words);

  // Fixes: saved, then the page is shown again with them.
  async function send(body) {
    const at = page;
    try {
      const reply = await postJSON(`${api}/${at}/fixes`, body);
      hideTip(true);
      toast(reply.message, { action: "Undo", onAction: () => undo(reply.id, at), ms: 8000 });
      if (at === page) await show(at);
    } catch (error) {
      toast(error.message, { tone: "error" });
    }
  }

  async function undo(id, at = page) {
    try {
      const reply = await postJSON(`${api}/${at}/fixes/${enc(id)}/undo`);
      hideTip(true);
      toast(reply.message);
      if (at === page) await show(at);
    } catch (error) {
      toast(error.message, { tone: "error" });
    }
  }

  // Marking a table CloseDesk missed: drag a box round it on the page.
  let drawing = false;
  let rubber = null;
  let from = null;
  const markLabel = h("span", null, "Mark a table");
  const markTable = h(
    "button",
    { type: "button", class: "btn btn-sm pv-mark", "aria-pressed": "false", title: "Drag a box round a table CloseDesk didn't outline" },
    icon("table", 14),
    markLabel
  );
  markTable.addEventListener("click", () => (drawing ? stopDrawing() : startDrawing()));

  function startDrawing() {
    drawing = true;
    hideTip(true);
    sheet.classList.add("drawing");
    markTable.setAttribute("aria-pressed", "true");
    markLabel.textContent = "Drag across the table (Esc to stop)";
    stopOpenDrawing = () => (drawing ? (stopDrawing(), true) : false);
  }

  function stopDrawing() {
    drawing = false;
    from = null;
    if (rubber) rubber.remove();
    rubber = null;
    sheet.classList.remove("drawing");
    markTable.setAttribute("aria-pressed", "false");
    markLabel.textContent = "Mark a table";
  }

  const spot = (event) => {
    const rect = sheet.getBoundingClientRect();
    const clamp = (value) => Math.min(Math.max(value, 0), 1);
    return { x: clamp((event.clientX - rect.left) / rect.width), y: clamp((event.clientY - rect.top) / rect.height) };
  };
  const boxFrom = (a, b) => ({ x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), w: Math.abs(a.x - b.x), h: Math.abs(a.y - b.y) });
  sheet.addEventListener("pointerdown", (event) => {
    if (!drawing || event.button !== 0) return;
    event.preventDefault();
    from = spot(event);
    rubber = h("div", { class: "pv-rubber" });
    sheet.append(rubber);
    sheet.setPointerCapture(event.pointerId);
  });
  sheet.addEventListener("pointermove", (event) => {
    if (!drawing || !from || !rubber) return;
    const box = boxFrom(from, spot(event));
    Object.assign(rubber.style, { left: `${box.x * 100}%`, top: `${box.y * 100}%`, width: `${box.w * 100}%`, height: `${box.h * 100}%` });
  });
  sheet.addEventListener("pointerup", (event) => {
    if (!drawing || !from) return;
    const box = boxFrom(from, spot(event));
    stopDrawing();
    if (box.w < 0.02 || box.h < 0.01) return toast("Drag across the whole table to mark it.");
    send({ kind: "table", box });
  });

  const zoomButtons = ZOOMS.map(([value, words]) =>
    h("button", { type: "button", class: `seg${value === zoom ? " on" : ""}`, "aria-pressed": String(value === zoom), dataset: { zoom: value }, onclick: () => setZoom(value) }, words)
  );
  const zooms = h("div", { class: "segs small pv-zoom", role: "group", "aria-label": "Page size" }, zoomButtons);

  function sizeSheet() {
    if (!view) return;
    sheet.style.width = zoom === "fit" ? "" : `${Math.round((view.width * 96) / 150 * (Number(zoom) / 100))}px`;
    frame.classList.toggle("zoomed", zoom !== "fit");
  }

  function setZoom(value) {
    zoom = value;
    saveZoom(value);
    hideTip(true);
    for (const button of zoomButtons) {
      const on = button.dataset.zoom === value;
      button.classList.toggle("on", on);
      button.setAttribute("aria-pressed", String(on));
    }
    sizeSheet();
  }

  async function show(number) {
    const mine = ++token;
    hideTip(true);
    closeTable();
    stopDrawing();
    page = number;
    where.textContent = pages ? `Page ${page} of ${pages}` : `Page ${page}`;
    prev.disabled = next.disabled = true;
    sheet.classList.add("loading");
    let picture;
    let got;
    try {
      [got, picture] = await Promise.all([getJSON(`${api}/${page}/regions`), getBlob(`${api}/${page}.png`)]);
    } catch (error) {
      if (mine !== token) return;
      sheet.classList.remove("loading");
      replace(sheet, h("p", { class: "pv-fail muted" }, `Couldn't show this page (${error.message}).`));
      keys.hidden = tools.hidden = true;
      prev.disabled = page <= 1;
      next.disabled = !pages || page >= pages;
      return;
    }
    if (mine !== token || !sheet.isConnected) return;
    view = got;
    pages = view.pages;
    where.textContent = `Page ${page} of ${pages}`;
    prev.disabled = page <= 1;
    next.disabled = page >= pages;
    if (pageUrl) URL.revokeObjectURL(pageUrl);
    pageUrl = URL.createObjectURL(picture);
    draw(view);
  }

  function draw(view) {
    const reading = Boolean(view.model_name);
    const counts = { ok: 0, check: 0, differs: 0, fixed: 0 };
    const shades = view.regions.map((region) => tone(region, view.source));
    for (const shade of shades) counts[shade] += 1;
    // The cells of the open table that each box was read into, to point at them from the page.
    const cellsOf = new Map();
    const boxes = view.regions.map((region, index) => {
      const box = h("div", {
        class: `pv-box ${shades[index]}`,
        tabindex: "0",
        role: "button",
        "aria-label": region.text,
        "aria-describedby": "pv-tip",
        dataset: { region: String(index) },
        style: { left: `${region.x * 100}%`, top: `${region.y * 100}%`, width: `${region.w * 100}%`, height: `${region.h * 100}%` },
      });
      const open = () => showTip(box, [...tipFor(region, view), h("p", { class: "pv-tip-hint muted" }, "Click to fix it if it's wrong.")]);
      const mark = (on) => (cellsOf.get(index) || []).forEach((cell) => cell.classList.toggle("pv-hl", on));
      box.addEventListener("pointerenter", () => (open(), mark(true)));
      box.addEventListener("focus", open);
      box.addEventListener("click", () => edit(index));
      box.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          edit(index);
        }
      });
      box.addEventListener("pointerleave", () => (hideTip(), mark(false)));
      box.addEventListener("blur", hideTip);
      return box;
    });

    // Fixing a box: what the page says there, typed over what was read.
    function edit(index) {
      const region = view.regions[index];
      const box = boxes[index];
      if (!region || !box) return;
      const input = h("input", { type: "text", class: "pv-fix-input", value: region.text, maxlength: "500", "aria-label": "What the page says here" });
      const save = h("button", { type: "submit", class: "btn btn-sm btn-primary" }, "Save fix");
      const cancel = h("button", { type: "button", class: "btn btn-sm", onclick: () => hideTip(true) }, "Cancel");
      let extra = null;
      if (region.fixed && region.fixed.by === "you") {
        extra = h("button", { type: "button", class: "btn btn-sm btn-quiet", onclick: () => undo(region.fixed.id) }, "Undo my fix");
      } else if (region.fixed) {
        // A word learnt from another file that is right as it was read here.
        extra = h("button", { type: "button", class: "btn btn-sm btn-quiet", onclick: () => send({ kind: "text", region: index, now: region.fixed.was }) }, "It was right here");
      }
      const form = h(
        "form",
        { class: "pv-fix" },
        h("label", { class: "pv-fix-h" }, "What does the page say here?", input),
        h("div", { class: "pv-fix-btns" }, save, cancel, extra),
        h("p", { class: "pv-fix-note muted" }, "The file's text gets your fix. A word or name fixed here is put right on this sender's later files too; a figure is fixed on this file only.")
      );
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        if (input.value.trim() === region.text.trim()) return hideTip(true);
        send({ kind: "text", region: index, now: input.value });
      });
      hideTip(true);
      showTip(box, [...tipFor(region, view), form], { pin: true });
      input.focus();
      input.select();
    }

    function reveal(index, { card = false } = {}) {
      const box = boxes[index];
      if (!box) return;
      hideTip();
      box.scrollIntoView({ block: "center", inline: "center" });
      flash(box);
      if (card) settled(() => box.isConnected && showTip(box, tipFor(view.regions[index], view)));
    }

    // The tables on the page: an outline each, under the boxes so the boxes can still be pointed at.
    const outlines = (view.tables || []).filter((table) => table.box).map((table) => {
      const outline = h(
        "div",
        {
          class: "pv-tbl",
          role: "button",
          tabindex: "0",
          "aria-label": `${table.label}: show this table`,
          title: `${table.label}: click to show it as a table`,
          dataset: { table: table.id },
          style: { left: `${table.box.x * 100}%`, top: `${table.box.y * 100}%`, width: `${table.box.w * 100}%`, height: `${table.box.h * 100}%` },
        },
        h("span", { class: "pv-tbl-tag" }, table.label)
      );
      outline.addEventListener("click", () => toggleTable(table));
      outline.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          toggleTable(table);
        }
      });
      return outline;
    });

    sizeSheet();
    sheet.style.aspectRatio = `${view.width} / ${view.height}`;
    sheet.classList.remove("loading");
    const img = h("img", { alt: `Page ${page} of ${file.name}`, draggable: "false" });
    img.src = pageUrl; // a blob: address made just above, which h() only takes for links to this server
    replace(sheet, img, outlines, boxes);

    // A table shown as a grid above the page: pointing at a cell marks its box, and the other way round.
    let openId = "";
    const chips = (view.tables || []).map((table) =>
      h("button", { type: "button", class: "pv-chip", "aria-pressed": "false", dataset: { table: table.id }, onclick: () => toggleTable(table) }, icon("table", 13), table.label)
    );

    function toggleTable(table) {
      if (openId === table.id) closeTable();
      else openTable(table);
    }

    function openTable(table) {
      closeTable();
      openId = table.id;
      for (const chip of chips) chip.setAttribute("aria-pressed", String(chip.dataset.table === table.id));
      for (const outline of outlines) outline.classList.toggle("on", outline.dataset.table === table.id);
      const cell = (tag, text, region, attrs = {}) => {
        const el = h(tag, { ...attrs, class: `${attrs.class || ""}${region === null || region === undefined ? " pv-nobox" : ""}`.trim() || null }, text);
        if (region !== null && region !== undefined) {
          el.dataset.region = String(region);
          if (!cellsOf.has(region)) cellsOf.set(region, []);
          cellsOf.get(region).push(el);
          const box = boxes[region];
          el.addEventListener("pointerenter", () => box && box.classList.add("pv-hl"));
          el.addEventListener("pointerleave", () => box && box.classList.remove("pv-hl"));
          el.addEventListener("click", () => reveal(region));
          el.title = "Click to find it on the page";
        }
        return el;
      };
      const numeric = table.columns.map((_c, i) => {
        const filled = table.rows.map((row) => (row[i] ? row[i].text : "")).filter(Boolean);
        return filled.length > 0 && filled.every((text) => /^[-(]?\s*[$€£]?\s*[\d.,]+\)?%?$/.test(text.trim()));
      });
      const headed = table.columns.some(Boolean);
      const grid = h(
        "table",
        { class: "grid pv-grid" },
        headed
          ? h("thead", null, h("tr", null, table.columns.map((label, i) => cell("th", label, (table.column_regions || [])[i], { scope: "col", class: numeric[i] ? "num" : "" }))))
          : null,
        h(
          "tbody",
          null,
          table.rows.map((row) => h("tr", null, row.map((item, i) => cell("td", item.text, item.region, { class: numeric[i] ? "num" : "" }))))
        )
      );
      const found = table.rows.flat().filter((item) => item.text && item.region !== null).length;
      const filled = table.rows.flat().filter((item) => item.text).length;
      replace(
        panel,
        h(
          "div",
          { class: "pv-panel-head" },
          h("div", null, h("b", null, table.label), h("small", null, `${plural(table.rows.length, "row")} · ${found} of ${plural(filled, "cell")} found on the page · point at a cell to see where it was read`)),
          h(
            "div",
            { class: "pv-panel-acts" },
            table.fixed && table.fixed.by === "you"
              ? h("button", { type: "button", class: "btn btn-sm btn-quiet", onclick: () => undo(table.fixed.id) }, "Remove my table")
              : table.box
                ? h("button", { type: "button", class: "btn btn-sm btn-quiet", title: "Stop outlining this here and on this sender's later pages", onclick: () => send({ kind: "not_table", box: table.box }) }, "Not a table")
                : null,
            h("button", { type: "button", class: "icon-btn", title: "Close (Esc)", "aria-label": "Close the table", onclick: closeTable }, icon("x", 15))
          )
        ),
        h("div", { class: "grid-wrap" }, grid)
      );
      panel.hidden = false;
      const stuck = barHeight() + (tools.hidden ? 0 : tools.offsetHeight);
      panel.style.top = `${stuck}px`;
      closeOpenTable = () => (sheet.isConnected && openId ? (closeTable(), true) : false);
      // Bring the table on the page into view below the grid.
      const outline = outlines.find((o) => o.dataset.table === table.id);
      const scroller = sheet.closest(".reader-pane");
      if (outline && scroller) {
        // Where the page shows from once the grid is kept at the top of the pane.
        const cover = scroller.getBoundingClientRect().top + stuck + panel.offsetHeight + 32; // room for its label
        const at = outline.getBoundingClientRect();
        if (at.top < cover || at.top > window.innerHeight - 80) scroller.scrollBy({ top: at.top - cover });
      }
    }

    closeTable = () => {
      if (!openId) return;
      openId = "";
      cellsOf.clear();
      for (const chip of chips) chip.setAttribute("aria-pressed", "false");
      for (const outline of outlines) outline.classList.remove("on");
      for (const box of boxes) box.classList.remove("pv-hl");
      panel.hidden = true;
      replace(panel);
    };

    // What to check: the amber and red boxes, in reading order.
    const toCheck = readingOrder(
      view.regions,
      shades.flatMap((shade, index) => (shade === "check" || shade === "differs" ? [index] : []))
    );
    let at = -1;
    const count = h("span", { class: "pv-count-check", "aria-live": "polite" }, toCheck.length ? `${plural(toCheck.length, "box", "boxes")} to check` : "");
    const step = h(
      "button",
      { type: "button", class: "btn btn-sm pv-next", disabled: !toCheck.length, title: toCheck.length ? "Go to the next amber or red box" : null },
      icon(toCheck.length ? "down" : "check", 14),
      h("span", null, toCheck.length ? "Next to check" : "Nothing to check")
    );
    step.addEventListener("click", () => {
      if (!toCheck.length) return;
      at = (at + 1) % toCheck.length;
      count.textContent = `${at + 1} of ${toCheck.length} to check`;
      reveal(toCheck[at], { card: true });
    });
    const only = h("input", { type: "checkbox", class: "pv-only-input" });
    only.addEventListener("change", () => {
      hideTip();
      sheet.classList.toggle("only-check", only.checked);
    });
    sheet.classList.remove("only-check");
    replace(
      tools,
      h("div", { class: "pv-check" }, step, count, view.regions.length ? h("label", { class: "pv-only" }, only, "Only show what needs checking") : null, markTable),
      chips.length ? h("div", { class: "pv-chips" }, h("span", { class: "pv-chips-h" }, `${chips.length === 1 ? "Table" : "Tables"} on this page`), chips) : null
    );
    tools.hidden = !view.regions.length;
    // Kept in view under the file's own bar, so "Next to check" stays at hand while the page scrolls.
    tools.style.top = `${barHeight()}px`;

    // Key details, each pointing at the box it was read from.
    const fields = view.fields || [];
    replace(
      keys,
      h("h2", { class: "pv-keys-h" }, "Key details"),
      h(
        "div",
        { class: "pv-keys-list" },
        fields.map((field) => {
          const shade = field.region === null ? "" : shades[field.region];
          const said = { ok: view.source === "text" ? "exact" : "readings agree", check: "check it", differs: "read differently" }[shade] || "";
          return h(
            "button",
            {
              type: "button",
              class: "pv-field",
              dataset: { label: field.label },
              title: `Show where it is on the page${said ? ` (${said})` : ""}`,
              onclick: () => reveal(field.region),
            },
            h("span", { class: "pv-field-label" }, field.label),
            h("span", { class: "pv-field-value" }, h("i", { class: `pv-dot ${shade}`, "aria-label": said || null, role: said ? "img" : null }), h("span", { class: "pv-field-text", title: field.value }, field.value))
          );
        })
      )
    );
    keys.hidden = !fields.length;

    const told = [];
    if (view.source === "text") told.push("Read from the file's own text (exact): each box is exactly what the file says.");
    else if (view.regions.length) told.push("This page is a scan. OCR read it on this computer, and each box says how sure it was.");
    if (view.reason) told.push(view.reason);
    if (reading) told.push(`${view.model_name} read this page too, and each box shows what it read there.`);
    else if (view.source === "ocr") told.push("The vision model hasn't read this page.");
    if (view.regions.length) told.push("Point at a box, or tab to it, to see what was read.");
    if (outlines.length) told.push(`Dashed outlines are tables: click one to see it as a table.`);
    if (view.regions.length) told.push("Click a box to fix what was read, or mark a table CloseDesk missed.");
    replace(about, told.join(" "));
    replace(
      legend,
      view.regions.length
        ? [
            key("ok", view.source === "text" ? (reading ? "Exact, and the vision model agrees" : "Exact") : reading ? "Both readings agree" : "Read clearly (80% sure or more)"),
            view.source === "ocr" || counts.check
              ? key(
                  "check",
                  reading
                    ? `Check it: OCR less sure, ${view.regions.some((r) => r.model && r.model.sure != null) ? "the vision model under 90% sure, " : ""}or not clearly in the vision model's reading`
                    : "Check it: OCR under 80% sure"
                )
              : null,
            reading ? key("differs", "The vision model read a different figure") : null,
            counts.fixed ? key("fixed", "Fixed by you, or from what you fixed before") : null,
            h("span", { class: "pv-count muted" }, `${plural(view.regions.length, "box", "boxes")}${counts.differs ? ` · ${counts.differs} differ` : ""}${counts.check ? ` · ${counts.check} to check` : ""}`),
          ]
        : null
    );
  }

  let closeTable = () => {};

  prev.addEventListener("click", () => page > 1 && show(page - 1));
  next.addEventListener("click", () => page < pages && show(page + 1));
  show(1);
  return h(
    "div",
    { class: "pv" },
    h("div", { class: "pv-bar" }, h("div", { class: "pv-nav" }, prev, where, next), zooms, legend),
    about,
    keys,
    tools,
    panel,
    frame
  );
}

export function fileView(data, tab, ctx) {
  const { email, file } = data;
  const base = filePath(email.id, file.n);
  hideTip();
  // A PDF or a picture opens on its page; any other file on its tables, or its text when it has none.
  if (!tab || (tab === "page" && !file.preview)) tab = file.preview ? "page" : "tables";
  if (tab !== "page") tab = tab === "text" || !data.tables.length ? "text" : "tables";
  const tabs = h(
    "div",
    { class: "tabs", role: "tablist" },
    file.preview ? h("a", { class: `tab${tab === "page" ? " on" : ""}`, role: "tab", "aria-selected": String(tab === "page"), href: ctx.fileUrl(file.n, "page"), dataset: { nav: "", replace: "" } }, icon("eye", 14), "Page") : null,
    h("a", { class: `tab${tab === "tables" ? " on" : ""}`, role: "tab", "aria-selected": String(tab === "tables"), href: ctx.fileUrl(file.n, "tables"), dataset: { nav: "", replace: "" } }, icon("table", 14), `Tables (${data.tables.length})`),
    h("a", { class: `tab${tab === "text" ? " on" : ""}`, role: "tab", "aria-selected": String(tab === "text"), href: ctx.fileUrl(file.n, "text"), dataset: { nav: "", replace: "" } }, icon("text", 14), `Text (${data.parts.length})`)
  );
  let content;
  if (tab === "page") {
    content = pageView(data);
  } else if (tab === "tables") {
    const off = data.tables.filter((t) => t.check.mismatched.length).length;
    const ok = data.tables.reduce((n, t) => n + t.check.matched, 0);
    content = h(
      "div",
      { class: "tbls" },
      h(
        "p",
        { class: "tbls-sum" },
        `CloseDesk found ${plural(data.tables.length, "table")} in this file. `,
        off
          ? h("b", { class: "warn-text" }, `${plural(off, "table")} ha${off === 1 ? "s" : "ve"} totals that don't add up as read.`)
          : ok
            ? h("span", { class: "ok-text" }, "Every printed total it could check adds up.")
            : "None has printed totals to check against.",
        " Figures are shown exactly as read."
      ),
      data.tables.map(tableCard),
      data.more_tables ? h("p", { class: "muted small" }, `${plural(data.more_tables, "more table")} not shown.`) : null
    );
  } else {
    const find = h("input", { type: "search", placeholder: "Find in this file", "aria-label": "Find in this file" });
    const parts = h("div", { class: "parts" });
    const count = h("span", { class: "muted small" });
    const draw = () => {
      const words = find.value.toLowerCase().split(/\s+/).filter((w) => w.length > 1);
      const shown = data.parts.filter((part) => !words.length || words.every((w) => `${part.label}\n${part.text}`.toLowerCase().includes(w)));
      count.textContent = words.length ? `${plural(shown.length, "section")} of ${data.parts.length}` : "";
      replace(
        parts,
        shown.length
          ? shown.map((part) => h("section", { class: "part" }, part.label ? h("h3", null, part.label) : null, h("pre", null, highlight(part.text, words))))
          : h("p", { class: "muted" }, data.parts.length ? "No section mentions that." : "No readable text in this file. It is probably a scan or a picture.")
      );
    };
    let timer = 0;
    find.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(draw, 120);
    });
    draw();
    content = h("div", { class: "text-view" }, h("div", { class: "find" }, icon("search", 14), find, count), parts);
  }
  return h(
    "article",
    { class: "rd file-view" },
    h(
      "div",
      { class: "rd-bar" },
      h("a", { class: "btn btn-quiet", href: ctx.emailUrl(), dataset: { nav: "" }, title: "Back to the email (Esc)" }, icon("back", 15), h("span", { class: "trunc" }, email.subject)),
      h("span", { class: "rd-bar-gap" }),
      file.view ? h("a", { class: "btn btn-quiet", href: `${base}/view`, target: "_blank", rel: "noopener" }, icon("external", 15), "Original") : null,
      file.download ? h("a", { class: "btn btn-quiet", href: `${base}/download` }, icon("download", 15), "Download") : null,
      btn("Ask about this file", { class: "btn-quiet", onclick: () => ctx.prefill(`About "${file.name}": `, email.id) }, "chat")
    ),
    h(
      "div",
      { class: "rd-scroll" },
      h(
        "div",
        { class: "rd-inner wide" },
        h("header", { class: "rd-head" }, h("div", { class: "rd-tags" }, h("span", { class: "rd-tag" }, file.kind), h("span", { class: "rd-tag" }, file.size), h("span", { class: "rd-tag" }, file.type_label)), h("h1", { class: "rd-subject" }, file.name), h("p", { class: "rd-from muted" }, `From ${email.sender} · `, h("a", { href: ctx.emailUrl(), dataset: { nav: "" } }, email.subject))),
        visionBox(data, ctx, { brief: tab === "page" }),
        tabs,
        content
      )
    )
  );
}

export function errorView(message, retry) {
  return h("div", { class: "rd-empty" }, icon("alert", 28), h("p", null, message), retry ? btn("Try again", { class: "btn-sm", onclick: retry }, "refresh") : null);
}

