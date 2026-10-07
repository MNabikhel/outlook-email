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

  /* Times on screen use the zone chosen in Setup, not the browser's. */
  const displayZone = document.body.dataset.tz || undefined;

  function zoneParts(when, zone = displayZone) {
    const parts = {};
    new Intl.DateTimeFormat("en-US", {
      timeZone: zone,
      weekday: "short",
      month: "short",
      day: "2-digit",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    })
      .formatToParts(when)
      .forEach((part) => (parts[part.type] = part.value));
    return parts;
  }

  function zoneDay(when, zone = displayZone) {
    const p = zoneParts(when, zone);
    return Date.UTC(Number(p.year), new Date(`${p.month} 1, 2000`).getMonth(), Number(p.day));
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

  async function openCodes() {
    try {
      const response = await fetch("/coding/open", { method: "POST", headers: JSON_HEADERS });
      const data = await response.json();
      toast(data.message || "Opening the workbook…", 6000);
    } catch (error) {
      toast("Couldn't open the workbook from here. Its location is on the AP coding page.", 6000);
    }
  }

  // Revise: narrow the list of cost codes as you type.
  document.addEventListener("input", (event) => {
    const filter = event.target.closest("[data-code-filter]");
    if (!filter) return;
    const form = filter.closest("form");
    const words = filter.value.toLowerCase().split(/\s+/).filter(Boolean);
    let shown = 0;
    $$("label[data-code-text]", form).forEach((label) => {
      const match = words.every((word) => label.dataset.codeText.includes(word));
      label.hidden = !match;
      shown += match ? 1 : 0;
    });
    $("[data-code-none]", form).hidden = shown > 0;
  });

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
  const chatWindow = document.body.classList.contains("chat-window");
  const CHAT_KEY = "closedesk-chat-id";
  const SIZE_KEY = "closedesk-chat-size";
  let chatId = null;
  let turns = [];
  let chatFiles = [];
  let loaded = false;
  let busy = false;
  let chatRound = 0;
  try {
    chatId = localStorage.getItem(CHAT_KEY);
  } catch (error) {
    chatId = null;
  }

  const rememberChat = (id) => {
    chatId = id;
    try {
      if (id) localStorage.setItem(CHAT_KEY, id);
      else localStorage.removeItem(CHAT_KEY);
    } catch (error) {
      /* private mode: the conversation is still saved, just not reopened on the next page */
    }
  };

  const mailHref = (source) =>
    source.chat && (source.files || []).length
      ? `/inbox/${encodeURIComponent(source.id)}/files/1`
      : `/inbox/${encodeURIComponent(source.id)}`;

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
      return `<a class="cite" href="${mailHref(source)}" title="${escapeHtml(source.subject)}">[${n}]</a>`;
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
                `<li><a class="cite" href="${mailHref(s)}">[${s.n}] ${escapeHtml(s.subject)}</a> <small>${escapeHtml(s.sender)}</small>` +
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
          "Add files with the paperclip or drop them here to ask about them too. " +
          "Click a [number] in my answers to open that email.",
      });
    }
    turns.forEach((turn) => renderTurn(turn));
    chat.classList.toggle("has-turns", turns.length > 0);
    renderFiles();
  }

  function setTitle(title) {
    const box = $("[data-chat-title]", chat);
    if (!box) return;
    box.textContent = title || "Ask CloseDesk";
    box.title = title || "";
  }

  async function loadChat(id) {
    const round = ++chatRound;
    if (!id) {
      turns = [];
      chatFiles = [];
      setTitle("");
      loaded = true;
      return renderChat();
    }
    try {
      const response = await fetch(`/chats/${encodeURIComponent(id)}`);
      if (round !== chatRound) return;
      if (!response.ok) throw new Error(String(response.status));
      const saved = await response.json();
      rememberChat(saved.id);
      turns = saved.turns || [];
      chatFiles = saved.files || [];
      setTitle(saved.title);
    } catch (error) {
      rememberChat(null);
      turns = [];
      chatFiles = [];
      setTitle("");
    }
    loaded = true;
    renderChat();
  }

  function newChat() {
    rememberChat(null);
    closeHistory();
    loadChat(null);
    chatInput.focus();
  }

  function openChat(focusInput = true) {
    if (!chat) return;
    chat.hidden = false;
    if (chatToggle) {
      chatToggle.setAttribute("aria-expanded", "true");
      chatToggle.classList.add("hidden-fab");
    }
    if (!loaded) loadChat(chatId);
    chatLog.scrollTop = chatLog.scrollHeight;
    if (focusInput) chatInput.focus();
  }

  function closeChat() {
    if (chatWindow) return window.close();
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
    closeHistory();
    // Load the saved conversation first: loading it afterwards would start a new round and drop this answer.
    if (chat && !loaded) await loadChat(chatId);
    const round = ++chatRound;
    openChat(false);
    if (!turns.length) chatLog.innerHTML = "";
    turns.push({ role: "user", text: question });
    chat.classList.add("has-turns");
    renderTurn(turns[turns.length - 1]);
    const answer = { role: "assistant", text: "", sources: [], steps: [], notes: [], pending: true };
    const bubble = renderTurn(answer);
    $("button[type='submit']", chatForm).disabled = true;
    try {
      const response = await fetch("/chat", {
        method: "POST",
        headers: JSON_HEADERS,
        body: JSON.stringify({ message: question, chat_id: chatId, email_id: emailId || chatInput.dataset.emailId || currentEmailId() }),
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
          if (event.type === "chat") {
            rememberChat(event.id);
            setTitle(event.title);
          } else if (event.type === "sources") {
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

  /* Past conversations */

  const historyPanel = $("#chat .chat-history");
  const historySearch = $("#chat .chat-history input");
  const historyList = $("#chat .chat-history-list");

  function dayLabel(iso) {
    const when = new Date(iso);
    if (Number.isNaN(when.getTime())) return "";
    const days = Math.round((zoneDay(new Date()) - zoneDay(when)) / 86400000);
    if (days === 0) return "Today";
    if (days === 1) return "Yesterday";
    if (days < 7) return "This week";
    return when.toLocaleDateString(undefined, { month: "long", year: "numeric", timeZone: displayZone });
  }

  async function showHistory() {
    const query = historySearch.value.trim();
    const response = await fetch(`/chats?q=${encodeURIComponent(query)}`).catch(() => null);
    const saved = response && response.ok ? (await response.json()).chats : [];
    if (!saved.length) {
      historyList.innerHTML = `<li class="chat-history-empty">${query ? "No conversation mentions that." : "Your conversations will show up here."}</li>`;
      return;
    }
    let group = "";
    historyList.innerHTML = saved
      .map((item) => {
        const label = dayLabel(item.updated_at);
        const heading = label !== group ? `<li class="chat-history-day">${escapeHtml((group = label))}</li>` : "";
        const time = new Date(item.updated_at).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit", timeZone: displayZone });
        const bits = [`${item.questions} question${item.questions === 1 ? "" : "s"}`];
        if (item.files) bits.push(`${item.files} file${item.files === 1 ? "" : "s"}`);
        return (
          heading +
          `<li class="chat-history-item${item.id === chatId ? " current" : ""}">` +
          `<button type="button" class="chat-history-open" data-chat-open="${escapeHtml(item.id)}">` +
          `<b>${escapeHtml(item.title || "Files only")}</b><small>${escapeHtml(time)} · ${bits.join(" · ")}</small></button>` +
          `<button type="button" class="icon chat-history-delete" data-chat-delete="${escapeHtml(item.id)}" title="Delete this conversation" aria-label="Delete this conversation">` +
          '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>' +
          "</button></li>"
        );
      })
      .join("");
  }

  function toggleHistory() {
    if (!historyPanel) return;
    historyPanel.hidden = !historyPanel.hidden;
    $("[data-action='chat-history']", chat).classList.toggle("on", !historyPanel.hidden);
    if (!historyPanel.hidden) {
      showHistory();
      historySearch.focus();
    }
  }

  function closeHistory() {
    if (!historyPanel || historyPanel.hidden) return false;
    historyPanel.hidden = true;
    $("[data-action='chat-history']", chat).classList.remove("on");
    return true;
  }

  async function deleteChat(id) {
    if (!window.confirm("Delete this conversation and the files added to it?")) return;
    const response = await fetch(`/chats/${encodeURIComponent(id)}/delete`, { method: "POST", headers: JSON_HEADERS }).catch(() => null);
    if (!response || !response.ok) return toast("That conversation couldn't be deleted.");
    if (id === chatId) {
      rememberChat(null);
      loadChat(null);
    }
    showHistory();
  }

  /* Files added to the conversation */

  const filesRow = $("#chat .chat-files");
  const fileInput = $("#chat .chat-file-input");
  const dropZone = $("#chat .chat-drop");

  const fileSize = (bytes) =>
    bytes >= 1e6 ? `${(bytes / 1e6).toFixed(1)} MB` : bytes >= 1e3 ? `${Math.round(bytes / 1e3)} KB` : `${bytes} B`;

  function renderFiles(pending = []) {
    if (!filesRow) return;
    filesRow.hidden = !chatFiles.length && !pending.length;
    filesRow.innerHTML =
      chatFiles
        .map(
          (file) =>
            `<span class="chat-file${file.text ? "" : " no-text"}" title="${file.text ? "" : "No readable text found"}">` +
            `<a href="${escapeHtml(file.href)}" target="_blank" rel="noopener">${escapeHtml(file.name)}</a>` +
            `<small>${fileSize(file.size)}</small>` +
            `<button type="button" data-file-remove="${file.n}" aria-label="Remove ${escapeHtml(file.name)}" title="Remove">&times;</button></span>`
        )
        .join("") + pending.map((name) => `<span class="chat-file pending">${escapeHtml(name)}<small>Reading…</small></span>`).join("");
  }

  async function ensureChat() {
    if (chatId) return chatId;
    const response = await fetch("/chats", { method: "POST", headers: JSON_HEADERS });
    if (!response.ok) throw new Error(String(response.status));
    rememberChat((await response.json()).id);
    return chatId;
  }

  async function addFiles(list) {
    const files = Array.from(list || []);
    if (!files.length) return;
    // As in ask(): a conversation still loading would replace the list of files this upload returns.
    if (chat && !loaded) await loadChat(chatId);
    openChat(false);
    renderFiles(files.map((file) => file.name));
    try {
      const id = await ensureChat();
      const form = new FormData();
      files.forEach((file) => form.append("files", file, file.name));
      const response = await fetch(`/chats/${encodeURIComponent(id)}/files`, { method: "POST", headers: { "X-CloseDesk": "1" }, body: form });
      if (!response.ok) throw new Error(`the server said ${response.status}`);
      const result = await response.json();
      chatFiles = result.files || [];
      if (result.problems && result.problems.length) toast(result.problems.join(" "), 6000);
      else toast(`Added ${files.length === 1 ? files[0].name : `${files.length} files`}. Ask away.`);
    } catch (error) {
      toast(`The files weren't added (${error.message}).`);
    }
    renderFiles();
    chatInput.focus();
  }

  async function removeFile(n) {
    const response = await fetch(`/chats/${encodeURIComponent(chatId)}/files/${n}/delete`, { method: "POST", headers: JSON_HEADERS }).catch(() => null);
    if (!response || !response.ok) return toast("That file couldn't be removed.");
    chatFiles = (await response.json()).files || [];
    renderFiles();
  }

  /* Size: drag the top-left corner, or switch to the large panel */

  function applySize() {
    if (!chat || chatWindow) return;
    let size = null;
    try {
      size = JSON.parse(localStorage.getItem(SIZE_KEY) || "null");
    } catch (error) {
      size = null;
    }
    const large = Boolean(size && size.large);
    chat.classList.toggle("large", large);
    $("[data-action='expand-chat']", chat).setAttribute("aria-pressed", String(large));
    chat.style.width = size && size.width && !large ? `${size.width}px` : "";
    chat.style.height = size && size.height && !large ? `${size.height}px` : "";
  }

  function saveSize(size) {
    try {
      if (size) localStorage.setItem(SIZE_KEY, JSON.stringify(size));
      else localStorage.removeItem(SIZE_KEY);
    } catch (error) {
      /* the size just isn't remembered */
    }
    applySize();
  }

  const grip = $("#chat .chat-grip");
  if (grip) {
    grip.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      const box = chat.getBoundingClientRect();
      const start = { x: event.clientX, y: event.clientY, width: box.width, height: box.height };
      chat.classList.remove("large");
      chat.classList.add("resizing");
      grip.setPointerCapture(event.pointerId);
      const move = (e) => {
        const width = Math.min(window.innerWidth - 24, Math.max(340, start.width + start.x - e.clientX));
        const height = Math.min(window.innerHeight - 24, Math.max(380, start.height + start.y - e.clientY));
        chat.style.width = `${width}px`;
        chat.style.height = `${height}px`;
      };
      const stop = () => {
        grip.removeEventListener("pointermove", move);
        chat.classList.remove("resizing");
        const box = chat.getBoundingClientRect();
        saveSize({ width: Math.round(box.width), height: Math.round(box.height) });
      };
      grip.addEventListener("pointermove", move);
      grip.addEventListener("pointerup", stop, { once: true });
      grip.addEventListener("pointercancel", stop, { once: true });
    });
    grip.addEventListener("dblclick", () => saveSize(null));
  }

  function toggleLarge() {
    saveSize(chat.classList.contains("large") ? null : { large: true });
  }

  function popOut() {
    const popup = window.open("/chat/window", "closedesk-chat", "popup,width=560,height=820");
    if (!popup) return toast("Your browser blocked the window. Allow pop-ups for CloseDesk and try again.");
    closeChat();
  }

  if (chat) {
    applySize();
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
    chatInput.addEventListener("paste", (event) => {
      const files = event.clipboardData && event.clipboardData.files;
      if (files && files.length) {
        event.preventDefault();
        addFiles(files);
      }
    });
    if (chatToggle) chatToggle.addEventListener("click", () => (chat.hidden ? openChat() : closeChat()));
    fileInput.addEventListener("change", () => {
      addFiles(fileInput.files);
      fileInput.value = "";
    });
    let dragDepth = 0;
    const carriesFiles = (event) => event.dataTransfer && Array.from(event.dataTransfer.types || []).includes("Files");
    chat.addEventListener("dragenter", (event) => {
      if (!carriesFiles(event)) return;
      event.preventDefault();
      dragDepth++;
      dropZone.hidden = false;
    });
    chat.addEventListener("dragover", (event) => {
      if (carriesFiles(event)) event.preventDefault();
    });
    chat.addEventListener("dragleave", () => {
      dragDepth = Math.max(0, dragDepth - 1);
      if (!dragDepth) dropZone.hidden = true;
    });
    chat.addEventListener("drop", (event) => {
      if (!carriesFiles(event)) return;
      event.preventDefault();
      dragDepth = 0;
      dropZone.hidden = true;
      addFiles(event.dataTransfer.files);
    });
    let searchTimer = null;
    historySearch.addEventListener("input", () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(showHistory, 200);
    });
    chat.addEventListener("click", (event) => {
      const open = event.target.closest("[data-chat-open]");
      if (open) {
        closeHistory();
        return loadChat(open.dataset.chatOpen);
      }
      const remove = event.target.closest("[data-chat-delete]");
      if (remove) return deleteChat(remove.dataset.chatDelete);
      const file = event.target.closest("[data-file-remove]");
      if (file) return removeFile(file.dataset.fileRemove);
    });
    // Another window (the pop-out or a second tab) switched conversations: follow it when idle.
    window.addEventListener("storage", (event) => {
      if (event.key === CHAT_KEY && !busy && event.newValue !== chatId) {
        chatId = event.newValue;
        if (!chat.hidden || chatWindow) loadChat(chatId);
        else loaded = false;
      }
    });
    if (chatWindow) openChat();
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
        case "open-codes":
          return openCodes();
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
        case "new-chat":
          return newChat();
        case "chat-history":
          return toggleHistory();
        case "expand-chat":
          return toggleLarge();
        case "popout-chat":
          return popOut();
        case "attach-file":
          return fileInput.click();
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
      if (closeHistory() || closePreview() || (!chatWindow && closeChat())) event.preventDefault();
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

  /* Setup: show the time in the zone being chosen, before it is saved. */
  const tzSelect = $("[data-tz-select]");
  const tzPreview = $("[data-tz-preview]");
  if (tzSelect && tzPreview) {
    const showZone = () => {
      const option = tzSelect.selectedOptions[0];
      if (!option) return;
      const p = zoneParts(new Date(), option.dataset.zone);
      const name = option.textContent.replace(/^This computer: /, "").replace(/^\([^)]*\)\s*/, "");
      const daylight = option.dataset.daylight ? " (daylight saving time)" : "";
      tzPreview.innerHTML =
        `Time there now: <b>${escapeHtml(`${p.weekday}, ${p.month} ${p.day}, ${p.year} · ${p.hour}:${p.minute}`)}</b>` +
        ` · ${escapeHtml(name)} is ${escapeHtml(option.dataset.now)} right now${daylight}.`;
    };
    tzSelect.addEventListener("change", showZone);
  }
})();
