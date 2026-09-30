(() => {
  "use strict";

  const JSON_HEADERS = { "Content-Type": "application/json", "X-CloseDesk": "1" };
  const MAIL_PATH = /^\/inbox\/([^/?#]+)$/;
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  const escapeHtml = (text) =>
    String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

  function toast(message, ms = 3500) {
    const box = $("#toast");
    if (!box) return;
    box.textContent = message;
    box.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => (box.hidden = true), ms);
  }

  function isTyping(target) {
    return target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName));
  }

  /* ---------- Email preview panel ---------- */

  const drawer = $("#drawer");
  const drawerBody = $("#drawer .drawer-body");
  const backdrop = $("#drawer-backdrop");
  let openId = null;
  let returnFocus = null;

  function mailIdFromLink(link) {
    if (!link || link.target === "_blank" || link.hasAttribute("download") || link.hasAttribute("data-full")) return null;
    if (link.closest(".drawer-bar")) return null;
    const url = new URL(link.getAttribute("href") || "", location.href);
    if (url.origin !== location.origin) return null;
    const match = url.pathname.match(MAIL_PATH);
    return match ? decodeURIComponent(match[1]) : null;
  }

  function markOpen(id) {
    $$(".is-open").forEach((el) => el.classList.remove("is-open"));
    if (!id) return;
    $$("[data-email]").forEach((el) => {
      if (el.dataset.email === id) el.classList.add("is-open");
    });
  }

  async function openPreview(id, trigger) {
    if (!drawer) {
      location.href = "/inbox/" + encodeURIComponent(id);
      return;
    }
    returnFocus = trigger || document.activeElement;
    openId = id;
    drawer.hidden = false;
    backdrop.hidden = false;
    document.body.classList.add("drawer-open");
    $(".drawer-full", drawer).href = "/inbox/" + encodeURIComponent(id);
    drawerBody.innerHTML = '<p class="muted">Opening…</p>';
    markOpen(id);
    try {
      const next = location.pathname + location.search;
      const response = await fetch(`/inbox/${encodeURIComponent(id)}/preview?next=${encodeURIComponent(next)}`);
      const html = await response.text();
      if (openId !== id) return;
      drawerBody.innerHTML = html;
      drawerBody.scrollTop = 0;
      $(".drawer-close", drawer).focus({ preventScroll: true });
    } catch (error) {
      location.href = "/inbox/" + encodeURIComponent(id);
    }
  }

  function closePreview() {
    if (!drawer || drawer.hidden) return false;
    drawer.hidden = true;
    backdrop.hidden = true;
    document.body.classList.remove("drawer-open");
    openId = null;
    markOpen(null);
    if (returnFocus && document.contains(returnFocus)) returnFocus.focus({ preventScroll: true });
    return true;
  }

  /* ---------- Open original, draft a reply ---------- */

  async function openOriginal(id) {
    try {
      const response = await fetch(`/inbox/${encodeURIComponent(id)}/open`, { method: "POST", headers: JSON_HEADERS });
      const data = await response.json();
      toast(data.message || "Opening…");
      if (!data.ok && data.download) location.href = data.download;
    } catch (error) {
      location.href = `/inbox/${encodeURIComponent(id)}/original`;
    }
  }

  async function draftReply(id, button) {
    const scope = button.closest(".preview, .detail") || document;
    const box = $(`.draft-box[data-draft-for="${CSS.escape(id)}"]`, scope);
    if (!box) return;
    const text = $(".draft-text", box);
    const note = $(".draft-note", box);
    const ask = $(".draft-ask", box);
    box.hidden = false;
    text.value = "";
    text.placeholder = "Writing a draft…";
    note.textContent = "";
    $$("button[data-action='draft']", scope).forEach((b) => (b.disabled = true));
    try {
      const response = await fetch(`/inbox/${encodeURIComponent(id)}/draft`, {
        method: "POST",
        headers: JSON_HEADERS,
        body: JSON.stringify({ instructions: ask.value }),
      });
      if (!response.ok) throw new Error(`the server said ${response.status}`);
      const data = await response.json();
      text.value = data.text || "";
      note.textContent = data.mode === "model" ? "Written by your local model. Check it before sending." : data.note || "";
      const mailto = $(".draft-mailto", box);
      mailto.hidden = !data.mailto;
      if (data.mailto) mailto.href = data.mailto;
      box.classList.toggle("safety", data.mode === "safety");
    } catch (error) {
      note.textContent = "Couldn't write a draft: " + error.message;
    } finally {
      text.placeholder = "";
      $$("button[data-action='draft']", scope).forEach((b) => (b.disabled = false));
    }
  }

  async function copyDraft(button) {
    const text = $(".draft-text", button.closest(".draft-box"));
    try {
      await navigator.clipboard.writeText(text.value);
    } catch (error) {
      text.select();
      document.execCommand("copy");
    }
    toast("Draft copied. Paste it into your reply.");
  }

  /* ---------- Chat ---------- */

  const chat = $("#chat");
  const chatToggle = $("#chat-toggle");
  const chatLog = $("#chat .chat-log");
  const chatForm = $("#chat .chat-form");
  const chatInput = $("#chat textarea");
  const STORE_KEY = "closedesk-chat-v1";
  let turns = [];
  let busy = false;
  let chatRound = 0;
  try {
    turns = JSON.parse(sessionStorage.getItem(STORE_KEY) || "[]");
  } catch (error) {
    turns = [];
  }

  const saveTurns = () => {
    try {
      sessionStorage.setItem(STORE_KEY, JSON.stringify(turns.slice(-30)));
    } catch (error) {
      /* private mode: history just is not kept */
    }
  };

  /* A citation opens the file it is about: the file named in the same sentence, or the email's one
     readable file when the sentence talks about a page, sheet or file. PDFs open at the cited page;
     other files open in the text view at the cited page, slide, sheet or cell. */
  const PAGE_AT = /\b(?:page|p\.)\s?(\d{1,4})\b/i;
  const SLIDE_AT = /\bslide\s?(\d{1,3})\b/i;
  const SHEET_AT = /\bsheet\s+["“']([^"”'\n]+)["”']/i;
  const CELL_AT = /(?:(?:'([^'\n]+)'|"([^"\n]+)"|([A-Za-z][\w]*))!)?\b([A-Z]{1,3}\d{1,6})\b/g;
  const CELL_WORD = /\bcells?\s+$/i;
  const SHEET_FILE = /\.(xlsx|xlsm|xls|csv|tsv)$/i;
  const ABOUT_A_FILE = /\b(attach\w*|file|pdf|document|spreadsheet|workbook|sheet|tab|deck|slide|page|cell|row|table)\b/i;
  const SENTENCE_END = /(?<!\b(?:p|pp|no|e\.g|i\.e|vs))[.!?](?=\s)|\n/gi;

  function sentences(text) {
    const out = [];
    let start = 0;
    for (const match of text.matchAll(SENTENCE_END)) {
      out.push(text.slice(start, match.index + 1));
      start = match.index + 1;
    }
    if (start < text.length) out.push(text.slice(start));
    return out;
  }

  function citedCell(context) {
    // "cell C4" or Budget!C4 over a bare reference, which could as well be "Q4" in a sentence.
    const found = [...context.matchAll(CELL_AT)];
    return (
      found.find((m) => CELL_WORD.test(context.slice(0, m.index))) || found.find((m) => m[1] || m[2] || m[3]) || found[found.length - 1] || null
    );
  }

  function fileHref(source, file, context) {
    const base = `/inbox/${encodeURIComponent(source.id)}/files/${file.n}`;
    context = context.split(file.name).join(" ");
    const page = context.match(PAGE_AT);
    if (file.view) return `${base}/view${/\.pdf$/i.test(file.name) && page ? `#page=${page[1]}` : ""}`;
    const slide = context.match(SLIDE_AT);
    const sheet = context.match(SHEET_AT);
    const cell = SHEET_FILE.test(file.name) ? citedCell(context) : null;
    let at = "";
    if (page) at = `page ${page[1]}`;
    else if (slide) at = `slide ${slide[1]}`;
    else if (cell) {
      const name = cell[1] || cell[2] || cell[3] || (sheet && sheet[1]);
      at = name ? `${name}!${cell[4]}` : cell[4];
    } else if (sheet) at = `sheet "${sheet[1]}"`;
    return at ? `${base}?at=${encodeURIComponent(at)}` : base;
  }

  function citedFile(source, context) {
    const files = source.files || [];
    const lower = context.toLowerCase();
    const named = files.find((file) => lower.includes(file.name.toLowerCase()));
    if (named) return named;
    const readable = files.filter((file) => file.text);
    return readable.length === 1 && ABOUT_A_FILE.test(context) ? readable[0] : null;
  }

  const fileLink = (href, title, inner, cls) =>
    `<a class="${cls}" href="${escapeHtml(href)}" target="_blank" rel="noopener" title="Open ${escapeHtml(title)}">${inner}</a>`;

  const LOCATION = new RegExp([PAGE_AT, SLIDE_AT, SHEET_AT, /\bcells?\s+[A-Z]{1,3}\d/, /!\$?[A-Z]{1,3}\$?\d/].map((re) => re.source).join("|"), "i");

  function citeTarget(source, context, whole) {
    // The sentence decides; else a file the rest of the answer names ("Source: roster.pdf, page 1").
    const file = citedFile(source, context);
    if (file) return { file, href: fileHref(source, file, context) };
    const files = source.files || [];
    const named = files.filter((item) => whole.toLowerCase().includes(item.name.toLowerCase()));
    if (named.length !== 1) return null;
    const where = LOCATION.test(context.split(named[0].name).join(" ")) ? context : whole;
    return { file: named[0], href: fileHref(source, named[0], where) };
  }

  function formatSentence(sentence, context, sources, names, whole) {
    let html = escapeHtml(sentence)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*(?!\s)([^*\n]+?)\*(?=[\s).,!?:;]|$)/g, "$1<em>$2</em>")
      .replace(/`([^`\n]+)`/g, "<code>$1</code>");
    if (names.pattern) {
      html = html.replace(names.pattern, (match) => {
        const hit = names.byName.get(match.toLowerCase());
        return hit ? fileLink(fileHref(hit.source, hit.file, context), hit.file.name, match, "cite-name") : match;
      });
    }
    return html.replace(/\[(\d{1,2})\]/g, (match, n) => {
      const source = sources.find((item) => String(item.n) === n);
      if (!source) return match;
      const target = citeTarget(source, context, whole);
      if (target) return fileLink(target.href, target.file.name, `[${n}]`, "cite");
      return `<a class="cite" href="/inbox/${encodeURIComponent(source.id)}" title="${escapeHtml(source.subject)}">[${n}]</a>`;
    });
  }

  function fileNames(sources) {
    const byName = new Map();
    const counts = new Map();
    sources.forEach((source) =>
      (source.files || []).forEach((file) => {
        const key = file.name.toLowerCase();
        counts.set(key, (counts.get(key) || 0) + 1);
        byName.set(key, { source, file });
      })
    );
    // Matched in the escaped HTML, so keyed by the escaped name.
    const unique = new Map(
      [...byName].filter(([key]) => counts.get(key) === 1 && key.length >= 5).map(([key, hit]) => [escapeHtml(key), hit])
    );
    if (!unique.size) return { byName: unique, pattern: null };
    const escaped = [...unique.keys()]
      .sort((a, b) => b.length - a.length)
      .map((key) => key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
    return { byName: unique, pattern: new RegExp(`(?<![\\w/])(?:${escaped.join("|")})(?![\\w])`, "gi") };
  }

  function formatAnswer(text, sources) {
    sources = sources || [];
    const names = fileNames(sources);
    const parts = sentences(text);
    return parts
      .map((sentence, index) => {
        // "... is blank. [1]": a citation after the full stop belongs to the sentence before.
        const lead = sentence.split(/\[\d{1,2}\]/)[0];
        const context = !lead.trim() && index ? parts[index - 1] + sentence : sentence;
        return formatSentence(sentence, context, sources, names, text);
      })
      .join("")
      .replace(/\n/g, "<br>");
  }

  function renderSteps(turn) {
    const steps = turn.steps || [];
    const notes = turn.notes || [];
    if (!steps.length && !notes.length) return "";
    const items =
      steps.map((step) => `<li>${escapeHtml(step)}</li>`).join("") +
      notes.map((note) => `<li class="noted">Noted: ${escapeHtml(note)}</li>`).join("");
    const label = `Read ${steps.length} thing${steps.length === 1 ? "" : "s"}${notes.length ? `, noted ${notes.length}` : ""}`;
    return `<details class="msg-steps"${turn.pending ? " open" : ""}><summary>${label}</summary><ul>${items}</ul></details>`;
  }

  function renderTurn(turn, bubble) {
    const el = bubble || document.createElement("div");
    el.className = `msg ${turn.role}${turn.pending ? " pending" : ""}`;
    if (turn.role === "user") {
      el.textContent = turn.text;
    } else {
      let html = turn.warning ? `<p class="msg-warn">${escapeHtml(turn.warning)}</p>` : "";
      if (turn.note) html += `<p class="msg-note">${escapeHtml(turn.note)}</p>`;
      html += renderSteps(turn);
      const waiting = turn.steps && turn.steps.length ? turn.steps[turn.steps.length - 1] + "…" : "Thinking…";
      html += `<div>${turn.text ? formatAnswer(turn.text, turn.sources) : `<span class="typing">${escapeHtml(waiting)}</span>`}</div>`;
      if (turn.checks && turn.checks.length) {
        html += `<ul class="msg-check">${turn.checks.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
      }
      if (turn.context) html += `<p class="msg-context">${escapeHtml(turn.context)} <a href="/settings">Setup</a></p>`;
      const cited =
        turn.mode === "model" ? (turn.sources || []).filter((s) => turn.text.includes(`[${s.n}]`)) : [];
      if (!turn.pending && cited.length) {
        html +=
          '<ul class="msg-sources">' +
          cited
            .map(
              (s) =>
                `<li><a class="cite" href="/inbox/${encodeURIComponent(s.id)}">[${s.n}] ${escapeHtml(s.subject)}</a> <small>${escapeHtml(s.sender)}</small>` +
                (s.files || [])
                  .filter((file) => turn.text.toLowerCase().includes(file.name.toLowerCase()))
                  .map((file) => fileLink(fileHref(s, file, ""), file.name, escapeHtml(file.name), "cite-file"))
                  .join("") +
                "</li>"
            )
            .join("") +
          "</ul>";
      }
      el.innerHTML = html;
    }
    if (!bubble) chatLog.append(el);
    chatLog.scrollTop = chatLog.scrollHeight;
    return el;
  }

  function renderChat() {
    if (!chatLog) return;
    chatLog.innerHTML = "";
    if (!turns.length) {
      renderTurn({
        role: "assistant",
        text:
          "Hi! Ask me about your mail: what's urgent, what needs a reply, or anything from a person or company. " +
          "Click a [number] in my answers to open that email.",
      });
    }
    turns.forEach((turn) => renderTurn(turn));
  }

  function openChat(focusInput = true) {
    if (!chat) return;
    chat.hidden = false;
    chatToggle.setAttribute("aria-expanded", "true");
    chatToggle.classList.add("hidden-fab");
    if (!chatLog.children.length) renderChat();
    chatLog.scrollTop = chatLog.scrollHeight;
    if (focusInput) chatInput.focus();
  }

  function closeChat() {
    if (!chat || chat.hidden) return false;
    chat.hidden = true;
    chatToggle.setAttribute("aria-expanded", "false");
    chatToggle.classList.remove("hidden-fab");
    chatToggle.focus({ preventScroll: true });
    return true;
  }

  const currentEmailId = () => openId || document.body.dataset.emailId || null;

  async function ask(question, emailId) {
    question = (question || "").trim();
    if (!question || busy) return;
    busy = true;
    const round = ++chatRound;
    openChat(false);
    const history = turns.slice(-6).map(({ role, text }) => ({ role, text }));
    turns.push({ role: "user", text: question });
    renderTurn(turns[turns.length - 1]);
    const answer = { role: "assistant", text: "", sources: [], steps: [], notes: [], pending: true };
    const bubble = renderTurn(answer);
    $("button[type='submit']", chatForm).disabled = true;
    try {
      const response = await fetch("/chat", {
        method: "POST",
        headers: JSON_HEADERS,
        body: JSON.stringify({ message: question, history, email_id: emailId || chatInput.dataset.emailId || currentEmailId() }),
      });
      if (!response.ok || !response.body) throw new Error(`the server said ${response.status}`);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        if (round !== chatRound) {
          reader.cancel().catch(() => {});
          break;
        }
        buffer += decoder.decode(value, { stream: true });
        let cut;
        while ((cut = buffer.indexOf("\n")) >= 0) {
          const line = buffer.slice(0, cut).trim();
          buffer = buffer.slice(cut + 1);
          if (!line) continue;
          const event = JSON.parse(line);
          if (round !== chatRound) break;
          if (event.type === "sources") {
            answer.sources = event.sources || [];
            if (event.warning) answer.warning = event.warning;
            answer.mode = event.mode;
          } else if (event.type === "step" || event.type === "note") {
            const list = event.type === "step" ? (answer.steps = answer.steps || []) : (answer.notes = answer.notes || []);
            list.push(event.text);
            renderTurn(answer, bubble);
          } else if (event.type === "revise") {
            answer.text = event.text;
            renderTurn(answer, bubble);
          } else if (event.type === "check") {
            answer.checks = event.items || [];
            renderTurn(answer, bubble);
          } else if (event.type === "context") {
            answer.context = event.text;
            renderTurn(answer, bubble);
          } else if (event.type === "mode") {
            answer.mode = event.mode;
            answer.note = event.note || "";
            renderTurn(answer, bubble);
          } else if (event.type === "error") {
            answer.text = (answer.text ? answer.text + "\n\n" : "") + event.text;
            renderTurn(answer, bubble);
          } else if (event.type === "delta") {
            answer.text += event.text;
            renderTurn(answer, bubble);
          }
        }
      }
    } catch (error) {
      answer.text = (answer.text ? answer.text + "\n\n" : "") + `Something went wrong (${error.message}). Try again.`;
    }
    busy = false;
    $("button[type='submit']", chatForm).disabled = false;
    if (round !== chatRound) return;
    answer.pending = false;
    if (!answer.text) answer.text = "I didn't get an answer. Try asking another way.";
    turns.push(answer);
    saveTurns();
    renderTurn(answer, bubble);
    chatInput.focus({ preventScroll: true });
  }

  function askFile(id, file, mode) {
    if (mode === "summary") {
      return ask(`Summarize the attachment "${file}": what it is, the key figures, and anything I need to act on.`, id);
    }
    openChat(false);
    chatInput.value = `About "${file}": `;
    chatInput.dataset.emailId = id;
    chatInput.focus();
    chatInput.setSelectionRange(chatInput.value.length, chatInput.value.length);
  }

  if (chat) {
    chatForm.addEventListener("submit", (event) => {
      event.preventDefault();
      if (busy) return;
      const question = chatInput.value;
      const emailId = chatInput.dataset.emailId;
      chatInput.value = "";
      delete chatInput.dataset.emailId;
      ask(question, emailId);
    });
    chatInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        chatForm.requestSubmit();
      }
    });
    chatToggle.addEventListener("click", () => (chat.hidden ? openChat() : closeChat()));
  }

  /* ---------- One click handler for the whole page ---------- */

  document.addEventListener("click", (event) => {
    if (event.defaultPrevented || event.button !== 0) return;
    const target = event.target;

    const actionButton = target.closest("[data-action]");
    if (actionButton) {
      const id = actionButton.dataset.id;
      switch (actionButton.dataset.action) {
        case "open-original":
          return openOriginal(id);
        case "draft":
          return draftReply(id, actionButton);
        case "copy-draft":
          return copyDraft(actionButton);
        case "ask":
          return ask("What does this email need from me?", id);
        case "ask-file":
          return askFile(id, actionButton.dataset.file, actionButton.dataset.mode);
        case "close-drawer":
          return closePreview();
        case "close-chat":
          return closeChat();
        case "clear-chat":
          chatRound++;
          turns = [];
          saveTurns();
          return renderChat();
      }
    }

    const suggestion = target.closest("[data-ask]");
    if (suggestion) return ask(suggestion.dataset.ask);

    if (target === backdrop) return closePreview();

    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;

    const link = target.closest("a[href]");
    if (link) {
      const id = mailIdFromLink(link);
      if (id) {
        event.preventDefault();
        openPreview(id, link);
      }
      return;
    }

    const row = target.closest("[data-email]");
    if (row && !target.closest("button, form, input, select, textarea, label") && !window.getSelection().toString()) {
      openPreview(row.dataset.email, row);
    }
  });

  const contextSlider = $("[data-context-steps]");
  if (contextSlider) {
    const steps = JSON.parse(contextSlider.dataset.contextSteps);
    const form = contextSlider.closest("form");
    const showStep = () => {
      const step = steps[Number(contextSlider.value)] || steps[0];
      $("[data-context-label]", form).textContent = step.label;
      const text = $("[data-context-text]", form);
      text.textContent = step.text;
      text.className = `context-capacity ${step.tier}`;
    };
    contextSlider.addEventListener("input", showStep);
    showStep();
  }

  const citedPart = $(".file-part.target");
  if (citedPart && !location.hash) ($("mark.cited", citedPart) || citedPart).scrollIntoView({ block: "center" });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (closePreview() || closeChat()) event.preventDefault();
      return;
    }
    if (event.key === "/" && !isTyping(event.target) && !event.metaKey && !event.ctrlKey) {
      const search = $(".top-search input");
      if (search) {
        event.preventDefault();
        search.focus();
        search.select();
      }
    }
  });
})();
