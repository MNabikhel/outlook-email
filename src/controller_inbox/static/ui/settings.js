/* The Settings page: how the workspace looks, and everything the classic Setup page sets (the profile, the time
   zone, the local model and its context, reading scans, search by meaning, the drop folder, the sample mailbox).
   Each change goes to the same server code as the classic form; the page is then drawn again from what the
   server says now, with a line under the change saying what happened. */

import { $, $$, h, icon, plural, replace, skeletonReader } from "./dom.js";
import { getJSON, postJSON } from "./api.js";

const SHORTCUTS = [
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

// The page's sections, in the order of the side list, grouped as it shows them.
const SECTIONS = [
  ["Workspace", [["appearance", "Appearance"], ["profile", "Inbox profile"], ["timezone", "Time zone"]]],
  ["Local model", [["model", "Model and status"], ["context", "Context length"], ["vision", "Reading scans"], ["search", "Search by meaning"]]],
  ["Mail", [["mail-in", "Getting mail in"], ["schedule", "Every morning"], ["cost-codes", "AP cost codes"], ["teaching", "Teaching it"], ["outlook", "Outlook connection"], ["sample", "Sample mailbox"]]],
  ["Help", [["keys", "Keyboard"], ["classic", "Classic view"]]],
];

const STATE_LABELS = { on: "Ready", fallback: "Older method", off: "Off" };
// The section a change's message shows in, when it isn't the change's own name.
const SECTION_OF = { chat: "model" };
const b = (text) => h("b", null, text);
const code = (text) => h("code", null, text);

/** The date and time now in ``zone``, as the time zone preview shows it. */
function zoneNow(zone) {
  const parts = {};
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: zone, weekday: "short", month: "short", day: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })
      .formatToParts(new Date())
      .forEach((part) => (parts[part.type] = part.value));
  } catch (error) {
    return "";
  }
  return `${parts.weekday}, ${parts.month} ${parts.day}, ${parts.year} · ${parts.hour}:${parts.minute}`;
}

/**
 * ctx: appearance() → { theme, railCollapsed }, setTheme(value), toggleRail(), processMail(), job() → the job now,
 * show(node, { keepScroll }) puts the page in the reading pane, isShown() says it is still there, and
 * changed(reply) is told about a change the rest of the workspace should pick up (a new time zone or profile).
 */
export function createSettings(ctx) {
  let data = null;
  let flash = null; // { id, tone, text }: the line under the last change
  let token = 0;
  let wasRunning = false;
  let observer = null;
  let root = null; // the page drawn now
  const dirty = new Set(); // sections changed here and not saved yet: drawing again leaves them as they are

  async function load({ recheck = false, keepScroll = false } = {}) {
    const mine = ++token;
    try {
      const fresh = await getJSON(`/api/settings${recheck ? "?recheck=1" : ""}`);
      if (mine !== token || !ctx.isShown()) return;
      data = fresh;
      draw({ keepScroll });
    } catch (error) {
      if (mine !== token || !ctx.isShown()) return;
      if (data) {
        flash = { id: "model", tone: "error", text: `Couldn't read the settings again (${error.message}).` };
        draw({ keepScroll: true });
      } else {
        ctx.show(errorPage(error.message), {});
      }
    }
  }

  function open() {
    flash = null;
    dirty.clear();
    root = null;
    wasRunning = ctx.job().state === "running";
    if (data) draw({ keepScroll: false });
    else ctx.show(skeletonReader(), {});
    load({ keepScroll: Boolean(data) });
  }

  function errorPage(message) {
    return h(
      "div",
      { class: "page" },
      h("div", { class: "page-inner narrow" }, h("p", { class: "kicker" }, "Settings"), h("h1", { class: "page-title" }, "The settings didn't load."), h("p", { class: "lede" }, message), h("p", null, h("button", { type: "button", class: "btn", onclick: () => open() }, icon("refresh", 14), "Try again")))
    );
  }

  /** Draw the page; when it is already showing, only its sections nobody is in the middle of changing. */
  function draw({ keepScroll = true } = {}) {
    if (!data) return;
    const active = document.activeElement;
    const focused = active && active.id && root && root.contains(active) ? active.id : "";
    if (!root || !root.isConnected) {
      root = page();
      ctx.show(root, { keepScroll });
    } else {
      for (const [id, build] of Object.entries(BUILDERS)) {
        const old = $(`#set-${id}`, root);
        if (!old) continue;
        // The model section's only control is the chat-model choice: keep that as it is and draw the rest
        // (the banner, Check again, the table) from what the server says now.
        const kept = dirty.has(id) && id === "model" ? $("#set-chat-model", old) : null;
        if (dirty.has(id) && !kept) continue;
        const fresh = build();
        const slot = kept ? $("#set-chat-model", fresh) : null;
        if (slot) slot.replaceWith(kept);
        else if (kept) dirty.delete(id); // no choice to make any more
        old.replaceWith(fresh);
        if (observer) {
          observer.unobserve(old);
          observer.observe(fresh);
        }
      }
    }
    // A section left as it was still says what happened to its own change (an error keeps it unsaved).
    const line = flash && root ? $(`[data-status="${flash.id}"]`, root) : null;
    if (line && dirty.has(SECTION_OF[flash.id] || flash.id)) line.replaceWith(status(flash.id));
    if (focused && !(active && active.isConnected)) {
      const again = document.getElementById(focused);
      if (again) again.focus({ preventScroll: true });
    }
  }

  /** Send a change; draw the page again from what the server says now, with what happened under ``id``. */
  async function save(id, path, body, button, { busyText = "Saving…", after = null } = {}) {
    const label = button ? button.textContent : "";
    if (button) {
      button.disabled = true;
      button.textContent = busyText;
    }
    const line = root ? $(`[data-status="${id}"]`, root) : null;
    if (line) {
      line.className = "set-status";
      line.textContent = "";
    }
    try {
      const reply = await postJSON(path, body);
      dirty.delete(SECTION_OF[id] || id);
      flash = { id, tone: "ok", text: reply.message || "Saved." };
      if (after) after(reply);
      ctx.changed(reply);
    } catch (error) {
      flash = { id, tone: "error", text: error.message };
    }
    if (!ctx.isShown()) return;
    if (button) {
      button.disabled = false;
      button.textContent = label;
    }
    await load({ keepScroll: true });
  }

  /* ---------- Pieces ---------- */

  function section(id, title, iconName, desc, ...body) {
    return h(
      "section",
      { class: "card set-sec", id: `set-${id}`, "aria-labelledby": `set-${id}-title` },
      h("header", { class: "set-sec-head" }, h("h2", { class: "set-title", id: `set-${id}-title` }, icon(iconName, 16), title), desc ? h("p", { class: "set-desc" }, desc) : null),
      ...body
    );
  }

  function status(id) {
    const said = flash && flash.id === id ? flash : null;
    return h("p", { class: `set-status${said ? ` ${said.tone}` : ""}`, role: "status", "aria-live": "polite", dataset: { status: id } }, said ? [icon(said.tone === "ok" ? "check" : "alert", 14), h("span", null, said.text)] : null);
  }

  function actions(id, ...buttons) {
    return h("div", { class: "set-actions" }, ...buttons, status(id));
  }

  const field = (label, forId, control, help = null) =>
    h("div", { class: "set-field" }, h("label", { class: "set-label", for: forId }, label), control, help ? h("p", { class: "set-help" }, help) : null);

  function choices(name, options, current, { stack = false, label = "" } = {}) {
    return h(
      "div",
      { class: `set-choices${stack ? " stack" : ""}`, role: "radiogroup", "aria-label": label || null },
      options.map((option) =>
        h(
          "label",
          { class: `set-choice${option.value === current ? " on" : ""}` },
          h("input", {
            type: "radio",
            name,
            value: option.value,
            checked: option.value === current,
            onchange: (event) => $$(".set-choice", event.target.closest(".set-choices")).forEach((el) => el.classList.toggle("on", $("input", el).checked)),
          }),
          h("span", { class: "set-choice-text" }, h("b", null, option.title), option.desc ? h("span", null, option.desc) : null)
        )
      )
    );
  }

  const picked = (form, name) => {
    const input = $(`input[name="${name}"]:checked`, form);
    return input ? input.value : "";
  };

  const pill = (state, text = STATE_LABELS[state] || state) => h("span", { class: `model-state ${state}` }, text);

  function facts(rows) {
    return h("dl", { class: "set-facts" }, rows.filter(Boolean).map(([term, value]) => [h("dt", null, term), h("dd", null, value)]));
  }

  function pathRow(label, path) {
    return [label, h("span", { class: "set-path" }, code(path))];
  }

  /* ---------- Sections ---------- */

  function appearance() {
    const { theme, railCollapsed } = ctx.appearance();
    const segment = (value, label) => h("button", { type: "button", class: `seg${theme === value ? " on" : ""}`, "aria-pressed": String(theme === value), onclick: () => ctx.setTheme(value) }, label);
    return section(
      "appearance",
      "Appearance",
      "sun",
      "Saved in this browser only.",
      h("div", { class: "set-row" }, h("span", { class: "set-row-label" }, "Theme"), h("div", { class: "segs", role: "group", "aria-label": "Theme" }, segment("system", "Match the computer"), segment("light", "Light"), segment("dark", "Dark"))),
      h("div", { class: "set-row" }, h("span", { class: "set-row-label" }, "Side bar"), h("button", { type: "button", class: "btn btn-sm", onclick: ctx.toggleRail }, icon("sidebar", 14), railCollapsed ? "Show labels" : "Collapse to icons"))
    );
  }

  function profile() {
    const options = data.profile.choices.map((choice) => {
      const [title, desc] = choice.label.split(" — ");
      return { value: choice.value, title, desc: desc ? desc.charAt(0).toUpperCase() + desc.slice(1) : "" };
    });
    let form;
    const submit = (event) => {
      event.preventDefault();
      save("profile", "/api/settings/profile", { profile: picked(form, "profile") }, $("button[type=submit]", form));
    };
    form = h(
      "form",
      { class: "set-form", onsubmit: submit },
      choices("profile", options, data.profile.current, { label: "What kind of inbox is this?" }),
      actions("profile", h("button", { type: "submit", class: "btn btn-primary btn-sm" }, "Save"))
    );
    return section(
      "profile",
      "What kind of inbox is this?",
      "important",
      "General works for anyone: replies, approvals, meetings, deadlines, and notifications. Finance adds the month-end countdown and close sections. Invoices and payment warnings are caught either way.",
      form
    );
  }

  function timezone() {
    const tz = data.timezone;
    const select = h(
      "select",
      { id: "set-tz", class: "set-select", name: "zone" },
      tz.options.map((row) => h("option", { value: row.value, selected: row.value === tz.choice }, row.label))
    );
    const preview = h("p", { class: "set-help set-preview", "aria-live": "polite" });
    const showZone = () => {
      const row = tz.options.find((option) => option.value === select.value);
      if (!row) return;
      const name = row.label.replace(/^This computer: /, "").replace(/^\([^)]*\)\s*/, "");
      const now = row.value === tz.choice ? tz.now_local : zoneNow(row.zone);
      replace(preview, "Time there now: ", b(now), ` · ${name} is ${row.now} right now${row.daylight ? " (daylight saving time)" : ""}.`);
    };
    select.addEventListener("change", showZone);
    replace(preview, "Time there now: ", b(tz.now_local), ` · ${tz.zone_name} is ${tz.offset} right now${tz.daylight ? " (daylight saving time)" : ""}.`);
    const form = h(
      "form",
      {
        class: "set-form",
        onsubmit: (event) => {
          event.preventDefault();
          save("timezone", "/api/settings/timezone", { zone: select.value }, $("button[type=submit]", event.currentTarget));
        },
      },
      field("Time zone", "set-tz", select, preview),
      actions("timezone", h("button", { type: "submit", class: "btn btn-primary btn-sm" }, "Save"))
    );
    return section(
      "timezone",
      "Time zone",
      "history",
      "Times follow this computer unless you pick another zone. The list matches Windows and Outlook: each zone shows its standard offset, and daylight saving time is applied for you. Mail stays stored in UTC, and a change here updates “today”, due dates, and every clock on screen.",
      form
    );
  }

  function modelSection() {
    const m = data.model;
    const down = m.mode !== "off" && !m.reachable;
    const recheck = h(
      "button",
      {
        type: "button",
        class: "btn btn-sm",
        onclick: async (event) => {
          const button = event.currentTarget;
          button.disabled = true;
          button.lastChild.textContent = "Checking…";
          flash = null;
          await load({ recheck: true, keepScroll: true });
          if (ctx.isShown() && !flash) {
            flash = { id: "model", tone: "ok", text: "Checked again just now." };
            draw();
          }
        },
      },
      icon("refresh", 14),
      h("span", null, "Check again")
    );
    const banner = down
      ? h(
          "div",
          { class: "set-banner off" },
          h("span", { class: "dot", "aria-hidden": "true" }),
          h(
            "div",
            null,
            h("p", null, "No local model is running. That's fine: sorting, summaries, tasks, the digest, search, and ", b("Ask CloseDesk"), " all work without one."),
            h("p", null, "To add one later: open LM Studio (lmstudio.ai), load a small instruct model (3B–8B), and click ", b("Start server"), " on the Developer tab. CloseDesk finds it on its own."),
            h("p", { class: "muted small" }, `Tried ${m.base_url}: ${m.error}`)
          )
        )
      : h("div", { class: `set-banner ${m.active ? "on" : "off"}` }, h("span", { class: "dot", "aria-hidden": "true" }), h("div", null, h("p", null, m.describe)));
    const table = h(
      "div",
      { class: "set-models", role: "table", "aria-label": "Models CloseDesk uses" },
      h("div", { class: "set-models-row head", role: "row" }, h("span", { role: "columnheader" }, "Job"), h("span", { role: "columnheader" }, "Model"), h("span", { role: "columnheader" }, "Status")),
      data.models.map((row) =>
        h(
          "div",
          { class: "set-models-row", role: "row" },
          h("span", { class: "set-models-job", role: "rowheader" }, row.role),
          h("span", { class: "set-models-name", role: "cell" }, row.model ? b(row.model) : h("span", { class: "muted" }, "none")),
          h("span", { class: "set-models-status", role: "cell" }, pill(row.state), " ", row.status, row.note ? h("small", { class: "muted" }, row.note) : null)
        )
      )
    );
    return section(
      "model",
      "Local model",
      "chat",
      "An optional model on this computer, through LM Studio, reads the short packet the scripts pull from each email, writes summaries and tasks, and answers in Ask CloseDesk. Nothing leaves this computer.",
      h("div", { class: "set-banner-row" }, banner, recheck),
      status("model"),
      h("h3", { class: "set-sub" }, "Models CloseDesk uses"),
      table,
      m.loaded.length ? h("p", { class: "set-help" }, `Loaded in ${m.server} now: ${m.loaded.join(", ")}`) : null,
      chatModel(),
      h(
        "details",
        { class: "set-more" },
        h("summary", null, "How CloseDesk uses the model"),
        h("p", null, "The fast scripts pull text, amounts, dates, and file types, then file a draft. The model reads that short packet — never the raw files — and writes the category, folder, a one-line summary, and tasks. The most important mail is read first."),
        h("p", null, "In ", b("Ask CloseDesk"), ", the model reads the text pulled from attachments (PDF pages, workbook cells and formulas, Word drafts with tracked changes, slides) and can open other emails and their files to answer. It notes what it finds and checks its answer against those notes. Files on an email flagged as possible fraud are never given to it."),
        h("p", null, "Guard rails that hold even with a small model:"),
        h(
          "ul",
          { class: "set-bullets" },
          h("li", null, "A payment-instruction change stays in Important with “verify by phone”, whatever the model says."),
          h("li", null, "A summary that quotes a dollar amount not in the email is replaced by the script summary."),
          h("li", null, "A due date the model made up is dropped. A task due this week keeps the email in Important."),
          h("li", null, "If the server stops answering, the run stops asking and the mail stays filed from the draft.")
        ),
        h("p", { class: "set-help" }, "Mode: ", b(m.mode), ` (${m.configured_url}). `, code("auto"), " uses a model whenever LM Studio's server is running. Set ", code("CONTROLLER_INBOX_LLM=false"), " to never use one."),
        h("p", { class: "set-help" }, "Check speed from a terminal: ", code("python -m controller_inbox llm-check"))
      )
    );
  }

  function chatModel() {
    const chat = data.chat;
    if (!chat) return null;
    if (chat.pinned) {
      return h(
        "div",
        { class: "set-block", id: "set-chat-model" },
        h("h3", { class: "set-sub" }, "Model that answers your questions"),
        h("p", { class: "set-help" }, "The model that answers is set in ", code(".env"), " (", code(`CONTROLLER_INBOX_LLM_MODEL=${chat.env_model}`), "). Remove that line to choose it here.")
      );
    }
    const select = h(
      "select",
      { id: "set-chat", class: "set-select", name: "model", required: true },
      chat.current ? null : h("option", { value: "" }, "Choose a model"),
      chat.models.map((item) => h("option", { value: item.key, selected: item.key === chat.current }, `${item.key}${item.loaded ? " (loaded)" : ""}`))
    );
    const min = chat.min_context_tokens;
    return h(
      "form",
      {
        class: "set-form set-block",
        id: "set-chat-model",
        onsubmit: (event) => {
          event.preventDefault();
          if (!select.value) return select.focus();
          save("chat", "/api/settings/chat-model", { model: select.value }, $("button[type=submit]", event.currentTarget), { busyText: "Loading in LM Studio…" });
        },
      },
      h("h3", { class: "set-sub" }, "Model that answers your questions"),
      h("div", { class: "set-inline" }, select, h("button", { type: "submit", class: "btn btn-primary btn-sm" }, "Load and use")),
      h(
        "p",
        { class: "set-help" },
        `CloseDesk has LM Studio load it${min ? ` with a ${min.toLocaleString("en-US")}-token context (or the most it supports)` : ""} and unload the other chat model, so the two don't share the memory. The page reader and the search model are left as they are. Loading can take a minute or two, and an answer being written then stops.`
      ),
      h("div", { class: "set-actions" }, status("chat"))
    );
  }

  function context() {
    const c = data.context;
    const steps = c.steps;
    const label = h("b", null, steps[c.step].label);
    const capacity = h("p", { id: "set-context-capacity", class: `set-capacity ${steps[c.step].tier}`, "aria-live": "polite" }, steps[c.step].text);
    const slider = h("input", {
      type: "range",
      id: "set-context-slider",
      class: "set-range",
      min: "0",
      max: String(steps.length - 1),
      step: "1",
      value: String(c.step),
      "aria-describedby": "set-context-capacity",
      "aria-valuetext": steps[c.step].label,
    });
    const showStep = () => {
      const step = steps[Number(slider.value)] || steps[0];
      label.textContent = step.label;
      capacity.textContent = step.text;
      capacity.className = `set-capacity ${step.tier}`;
      slider.setAttribute("aria-valuetext", step.label);
      slider.style.setProperty("--fill", `${(100 * Number(slider.value)) / (steps.length - 1)}%`);
    };
    slider.addEventListener("input", showStep);
    slider.style.setProperty("--fill", `${(100 * c.step) / (steps.length - 1)}%`);
    const w = c.window;
    const form = h(
      "form",
      {
        class: "set-form",
        onsubmit: (event) => {
          event.preventDefault();
          save("context", "/api/settings/context", { step: Number(slider.value) }, $("button[type=submit]", event.currentTarget));
        },
      },
      h("label", { class: "set-label", for: "set-context-slider" }, "Minimum context for Ask CloseDesk: ", label),
      slider,
      h("div", { class: "set-ticks", "aria-hidden": "true" }, steps.map((step) => h("span", null, step.tokens ? `${step.tokens / 1024}k` : "Off"))),
      capacity,
      actions("context", h("button", { type: "submit", class: "btn btn-primary btn-sm" }, "Save")),
      h("p", { class: "set-help" }, "LM Studio models loaded with less are reloaded with this on the next question. Other servers: set the context length there.")
    );
    return section(
      "context",
      "Context length",
      "text",
      "How much of an attachment the model can read at once when you ask about it. More reads whole files but needs more memory.",
      w.shown ? h("p", { class: `set-window ${w.tone}` }, "Context window now: ", b(w.label), ". ", w.advice) : null,
      form
    );
  }

  function visionSection() {
    const v = data.vision;
    let state;
    if (!v.reachable) state = ["No model server is answering, so scans are read with OCR only. Start LM Studio's server to read them with a model as well."];
    else if (v.missing) state = ["The model chosen to read pages, ", b(v.missing), ", isn't in LM Studio now or can't look at pictures, so scans are read with OCR only. Choose another one below, or Automatic."];
    else if (v.sees && v.mode === "off") state = ["Off: scans are read with OCR only. When it's on, pages are read by ", b(v.reader), ` (${v.reader_label}) as well, side by side with OCR.`];
    else if (v.sees) {
      state = [
        "Pages are read by ",
        b(v.reader),
        ` (${v.reader_label}). `,
        v.by_default
          ? "Scanned PDFs and pictures are read by it and by OCR; its reading is the one the chat and the table lookup use, and the file's page shows where OCR read differently. "
          : "Scanned PDFs and pictures are read by OCR and by the model side by side: figures both read the same way are confirmed, and the file's page shows where they differ. ",
        v.speed || "The first page read shows how long one takes on this computer; a laptop without a graphics card can take a few minutes.",
      ];
    } else {
      state = ["No model in LM Studio can look at pictures, so scans are read with OCR only. Download ", b("OvisOCR2"), " in LM Studio (a small model made for reading document pages: the most accurate and fastest we measured), or load one that can see (Qwen3.5 9B, Gemma 3), to read them both ways."];
    }
    const minutes = v.minutes_per_run;
    const modes = [
      {
        value: "auto",
        title: "Automatic",
        desc: v.by_default
          ? `Scans are read with the document reader, and its reading is the one the chat uses. Process new mail and the overnight run read waiting scans (${minutes} minutes at most each); a question about a scan not read yet waits for it when that takes under 10 minutes, and a longer read is offered with its time.`
          : `The overnight run reads waiting scans (${minutes} minutes at most). Process new mail and a question only read pages this computer reads in under a minute; a longer read is offered with its time.`,
      },
      { value: "ask", title: "Only when I ask", desc: "Nothing is read until you click Read with the vision model, after seeing how long it will take." },
      { value: "off", title: "Off", desc: "OCR only." },
    ];
    const select = v.choices.length
      ? h(
          "select",
          { id: "set-vision-model", class: "set-select", name: "model" },
          h("option", { value: "auto", selected: v.chosen === "auto" }, `Automatic: a document reader when there is one${v.chosen === "auto" && v.reader ? ` (now ${v.reader})` : ""}`),
          v.choices.map((choice) => h("option", { value: choice, selected: v.chosen === choice }, `${choice}${choice === v.missing ? " (can't read pages now)" : ""}`))
        )
      : null;
    let form;
    form = h(
      "form",
      {
        class: "set-form",
        onsubmit: (event) => {
          event.preventDefault();
          const body = { mode: picked(form, "vision-mode") };
          if (select) body.model = select.value;
          save("vision", "/api/settings/vision", body, $("button[type=submit]", form));
        },
      },
      h("div", { class: "set-label" }, "Read scans with the model"),
      choices("vision-mode", modes, v.mode, { stack: true, label: "Read scans with the model" }),
      select ? field("Model that reads pages", "set-vision-model", select) : null,
      actions("vision", h("button", { type: "submit", class: "btn btn-primary btn-sm" }, "Save"), v.pages_read ? h("span", { class: "set-help" }, `${plural(v.pages_read, "page")} read both ways so far.`) : null)
    );
    return section(
      "vision",
      "Reading scans",
      "eye",
      "Scanned PDFs and pictures are read with OCR on this computer, and with a model that can look at pages when there is one.",
      h("div", { class: `set-banner ${v.sees && v.mode !== "off" && !v.missing ? "on" : "off"}` }, h("span", { class: "dot", "aria-hidden": "true" }), h("div", null, h("p", null, state))),
      v.renderer ? null : h("p", { class: "note" }, "The page renderer isn't installed. Double-click CloseDesk once (or run ", code("pip install -e ."), ") to add it."),
      data.ocr_engine
        ? h("p", { class: "set-help" }, icon("ok", 14), " Scanned PDFs and pictures are read with ", b(data.ocr_engine), " on this computer.")
        : h("p", { class: "note" }, "Scanned PDFs and pictures can't be read yet. To add it, run ", code('pip install -e ".[ocr]"'), " in the CloseDesk folder (inside ", code(".venv"), ")."),
      form
    );
  }

  function search() {
    const s = data.search;
    const job = ctx.job();
    if (!s.model) {
      return section(
        "search",
        "Search by meaning",
        "search",
        "",
        h("div", { class: "set-banner off" }, h("span", { class: "dot", "aria-hidden": "true" }), h("div", null, h("p", null, "Off: search matches words only. Load an embedding model in LM Studio (it ships ", b("nomic-embed-text"), "), then come back here to index your mail."))),
        status("search")
      );
    }
    let action;
    if (job.state === "running") {
      const indexing = job.stage === "indexing";
      action = h(
        "div",
        { class: "set-progress", id: "set-index-progress" },
        h("span", { class: "set-progress-label" }, indexing ? `Indexing${job.total ? ` · ${job.done} of ${job.total}` : ""}` : "Processing mail"),
        h("span", { class: "job-bar" }, h("span", { class: job.total ? "" : "indeterminate", style: { width: job.total ? `${Math.round((100 * job.done) / job.total)}%` : "30%" } }))
      );
    } else if (s.waiting) {
      action = actions(
        "search",
        h("button", { type: "button", class: "btn btn-primary btn-sm", onclick: (event) => save("search", "/api/settings/index", {}, event.currentTarget, { busyText: "Starting…", after: () => (wasRunning = true) }) }, "Index all mail now"),
        h("span", { class: "set-help" }, `${plural(s.waiting, "email")} waiting. The overnight run also does this.`)
      );
    } else {
      action = h("p", { class: "set-indexed" }, icon("ok", 15), "Everything is indexed");
    }
    return section(
      "search",
      "Search by meaning",
      "search",
      ["Ask CloseDesk finds mail and file sections that say the same thing in other words, using ", b(s.model), "."],
      h(
        "div",
        { class: "set-stats" },
        h("div", null, h("span", null, "Emails"), h("b", null, `${s.emails_done}`, h("small", null, ` of ${s.emails}`))),
        h("div", null, h("span", null, "File sections"), h("b", null, `${s.sections_done}`, h("small", null, ` of ${s.sections}`))),
        h("div", null, h("span", null, "Last indexed"), h("b", { class: "set-stat-text" }, s.indexed_label || "never"))
      ),
      action,
      job.state === "running" ? status("search") : null
    );
  }

  function mailIn() {
    const f = data.folders;
    const running = ctx.job().state === "running";
    return section(
      "mail-in",
      "Getting mail in",
      "mail",
      "No Outlook add-in or IT approval needed: save messages into the drop folder and CloseDesk reads them.",
      h(
        "ol",
        { class: "set-steps" },
        h("li", null, b("Classic Outlook (Windows):"), " select yesterday's mail (click the first, Shift+click the last), then drag the selection onto the ", code("incoming"), " folder in File Explorer. Each message is saved as a ", code(".msg"), " with its attachments inside."),
        h("li", null, b("New Outlook / Outlook on the web:"), " open a message, choose ", b("… → Save as"), " (or ", b("Download"), "), and save the ", code(".eml"), " into the ", code("incoming"), " folder. Dragging to the folder works too."),
        h("li", null, b("Outlook for Mac:"), " drag messages from the list into the ", code("incoming"), " folder in Finder."),
        h("li", null, "Then click ", b("Process new mail"), " here or at the top of the page, or double-click ", code("CloseDesk"), " in the project folder.")
      ),
      h("p", { class: "set-help" }, "A desktop shortcut to the drop folder makes this a ten-second job. Dropping the same email twice is fine — it is recognized and not filed twice."),
      facts([
        pathRow("Drop folder", f.incoming),
        pathRow("Read files move to", f.processed),
        pathRow("Attachments unpacked to", f.extracted),
        pathRow("Couldn't be read", f.failed),
        pathRow("Attachments saved apart", `${f.attachments}/<same name as the message>/`),
      ]),
      h("p", { class: "set-help" }, "Anything that cannot be read goes to the folder above with a note saying why."),
      h("div", { class: "set-actions" }, h("button", { type: "button", class: "btn btn-primary btn-sm", disabled: running, onclick: () => ctx.processMail() }, icon("play", 13), running ? "Processing…" : "Process new mail"))
    );
  }

  function schedule() {
    const r = data.runs;
    return section(
      "schedule",
      "Running it every morning",
      "today",
      "",
      h("p", null, "Double-click ", code("CloseDesk.bat"), " (Windows) or ", code("CloseDesk.command"), " (Mac) in the project folder. It reads the drop folder, lets the model read, writes today's digest, and opens this dashboard."),
      h("p", { class: "set-help" }, "Unattended: schedule ", code("python -m controller_inbox overnight"), " (see README), or leave ", code("python -m controller_inbox watch"), " running."),
      facts([
        ["Last run", r.last_run || "never"],
        ["Drop folder last read", r.last_folder || "never"],
      ])
    );
  }

  function costCodes() {
    const c = data.cost_codes;
    const open = async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const reply = await postJSON("/coding/open");
        flash = { id: "cost-codes", tone: reply.ok ? "ok" : "error", text: reply.message };
      } catch (error) {
        flash = { id: "cost-codes", tone: "error", text: `Couldn't open the workbook from here (${error.message}). It is at ${c.workbook}.` };
      }
      if (ctx.isShown()) draw();
    };
    return section(
      "cost-codes",
      "AP cost codes",
      "coding",
      "",
      h("p", null, "Keep your JDE cost codes in the workbook below: one row each, with ", b("Description"), " and ", b("Cost Code"), " (for example ", code("1100.6110.100"), "). Every AP invoice is checked against it, and the email page shows the code with ", b("Confirm"), " and ", b("Revise"), "."),
      facts([pathRow("Workbook", c.workbook), ["To review", `${c.counts.review || 0} · confirmed ${c.counts.confirmed || 0}`]]),
      actions("cost-codes", h("button", { type: "button", class: "btn btn-sm", onclick: open }, icon("external", 14), "Open workbook"), h("a", { class: "btn btn-sm btn-quiet", href: "/app/coding", dataset: { nav: "" } }, "Go to AP coding", icon("right", 14)))
    );
  }

  function teaching() {
    return section(
      "teaching",
      "Teaching it",
      "pen",
      "",
      h("p", null, "When a category is wrong, open the message and use ", b("Wrong category?"), ". Write one sentence on why. The next email from that sender follows your correction, the model never overwrites the message you fixed, and the example is saved to ", code(data.corrections_file), "."),
      facts([["Corrections saved", String(data.corrections)]]),
      h("p", { class: "set-help" }, "A saved correction cannot clear a payment-instruction warning on a new message."),
      h("p", null, "For fraud flags, use the ", b("Fraud check"), " on the email: “Not fraud”, trust the sender or their whole domain, or report it. See what is flagged on the ", h("a", { href: "/app/fraud", dataset: { nav: "" } }, "Fraud check page"), ", or list trusted domains (for example your own company's) in ", code("CONTROLLER_INBOX_TRUSTED_DOMAINS"), ".")
    );
  }

  function outlook() {
    return section(
      "outlook",
      "Outlook direct connection",
      "external",
      "Optional, and needs IT. The drop folder keeps working either way.",
      h("p", null, "If IT later approves a Microsoft Entra app with delegated ", code("Mail.Read"), ", set ", code("AZURE_CLIENT_ID"), ", then run ", code("python -m controller_inbox auth"), " and ", code("sync"), "."),
      facts([
        ["Status", data.graph_configured ? pill("on", "Ready to sign in") : pill("off", "Not configured")],
        ["Last Outlook sync", data.runs.last_sync || "never"],
      ])
    );
  }

  function sample() {
    const s = data.sample;
    const load = (event) =>
      save("sample", "/api/settings/sample", {}, event.currentTarget, {
        busyText: "Loading…",
        // The whole mailbox changed: start the workspace again on Today, as the classic page goes to its dashboard.
        after: () => setTimeout(() => location.assign("/app"), 600),
      });
    return section(
      "sample",
      "Sample mailbox",
      "all",
      "",
      facts([["Mail now", `${plural(s.emails, "email")}${s.is_sample ? " (the sample mailbox)" : ""}`]]),
      s.can_load
        ? [
            h("p", null, "Load the built-in sample to see the dashboard before your own mail is in the folder. The first time you process your own mail, the sample is removed."),
            actions("sample", h("button", { type: "button", class: "btn btn-sm", onclick: load }, "Load sample mailbox")),
          ]
        : [
            h("p", { class: "set-help" }, "Your own mail is loaded, so the sample is switched off here — loading it would erase your mail. To look at the sample, run it with a separate data folder:"),
            h("pre", { class: "set-cmd" }, "CONTROLLER_INBOX_DATA_DIR=./data-sample python -m controller_inbox demo --serve"),
            status("sample"),
          ]
    );
  }

  function keys() {
    return section("keys", "Keyboard", "help", "", h("dl", { class: "keys-list" }, SHORTCUTS.map(([combo, what]) => [h("dt", null, h("kbd", null, combo)), h("dd", null, what)])));
  }

  function classic() {
    return section("classic", "Classic view", "classic", "", h("p", null, "Every page of the classic dashboard still works, and links back here. ", h("a", { href: "/" }, "Open the classic view"), "."));
  }

  /* ---------- The page ---------- */

  function nav() {
    return h(
      "nav",
      { class: "set-nav", "aria-label": "Settings sections" },
      SECTIONS.map(([group, items]) =>
        h("div", { class: "set-nav-group" }, h("span", { class: "set-nav-heading" }, group), items.map(([id, label]) => h("a", { href: `#set-${id}`, class: "set-nav-link", dataset: { section: id } }, label)))
      )
    );
  }

  function page() {
    const main = h("div", { class: "set-main" }, Object.values(BUILDERS).map((build) => build()));
    const node = h(
      "div",
      { class: "page settings" },
      h(
        "div",
        { class: "page-inner set-layout" },
        h("header", { class: "set-head" }, h("p", { class: "kicker" }, "Settings"), h("h1", { class: "page-title" }, "Settings and setup"), h("p", { class: "lede" }, "How the workspace looks, and how CloseDesk reads your mail on this computer.")),
        nav(),
        main
      )
    );
    // A control changed and not saved yet keeps its section as it is when the page is drawn again.
    const touched = (event) => {
      const sec = event.target.closest(".set-sec");
      if (sec && sec.id !== "set-appearance") dirty.add(sec.id.slice(4));
    };
    node.addEventListener("input", touched);
    node.addEventListener("change", touched);
    watchSections(node);
    return node;
  }

  const BUILDERS = {
    appearance,
    profile,
    timezone,
    model: modelSection,
    context,
    vision: visionSection,
    search,
    "mail-in": mailIn,
    schedule,
    "cost-codes": costCodes,
    teaching,
    outlook,
    sample,
    keys,
    classic,
  };

  // The side list marks the section being read.
  function watchSections(node) {
    if (observer) observer.disconnect();
    if (!("IntersectionObserver" in window)) return;
    const links = new Map($$(".set-nav-link", node).map((link) => [`set-${link.dataset.section}`, link]));
    const seen = new Map();
    observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) seen.set(entry.target.id, entry.isIntersecting ? entry.boundingClientRect.top : null);
        const first = [...seen.entries()].filter(([, top]) => top !== null).sort((a, b2) => a[1] - b2[1])[0];
        if (!first) return;
        for (const [id, link] of links) link.classList.toggle("on", id === first[0]);
      },
      { rootMargin: "-10% 0px -55% 0px" }
    );
    requestAnimationFrame(() => $$(".set-sec", node).forEach((sec) => observer.observe(sec)));
  }

  /** The job changed (polled by the workspace): move the index progress along in place, and read the page again
      when a job starts or ends (the Process and Index buttons, the counts). */
  function job(now) {
    if (!data || !ctx.isShown()) return;
    const running = now.state === "running";
    if (running !== wasRunning) {
      wasRunning = running;
      return load({ keepScroll: true });
    }
    const bar = running ? $("#set-index-progress") : null;
    if (!bar) return;
    $(".set-progress-label", bar).textContent = now.stage === "indexing" ? `Indexing${now.total ? ` · ${now.done} of ${now.total}` : ""}` : "Processing mail";
    const fill = $(".job-bar > span", bar);
    fill.className = now.total ? "" : "indeterminate";
    fill.style.width = now.total ? `${Math.round((100 * now.done) / now.total)}%` : "30%";
  }

  return { open, redraw: () => draw(), job };
}
