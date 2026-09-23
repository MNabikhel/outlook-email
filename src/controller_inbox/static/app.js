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

  function formatAnswer(text, sources) {
    let html = escapeHtml(text);
    html = html
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*(?!\s)([^*\n]+?)\*(?=[\s).,!?:;]|$)/g, "$1<em>$2</em>")
      .replace(/`([^`\n]+)`/g, "<code>$1</code>");
    html = html.replace(/\[(\d{1,2})\]/g, (match, n) => {
      const source = (sources || []).find((item) => String(item.n) === n);
      if (!source) return match;
      return `<a class="cite" href="/inbox/${encodeURIComponent(source.id)}" title="${escapeHtml(source.subject)}">[${n}]</a>`;
    });
    return html.replace(/\n/g, "<br>");
  }

  function renderTurn(turn, bubble) {
    const el = bubble || document.createElement("div");
    el.className = `msg ${turn.role}${turn.pending ? " pending" : ""}`;
    if (turn.role === "user") {
      el.textContent = turn.text;
    } else {
      let html = turn.warning ? `<p class="msg-warn">${escapeHtml(turn.warning)}</p>` : "";
      html += `<div>${turn.text ? formatAnswer(turn.text, turn.sources) : '<span class="typing">Thinking…</span>'}</div>`;
      const cited =
        turn.mode === "model" ? (turn.sources || []).filter((s) => turn.text.includes(`[${s.n}]`)) : [];
      if (!turn.pending && cited.length) {
        html +=
          '<ul class="msg-sources">' +
          cited
            .map(
              (s) =>
                `<li><a class="cite" href="/inbox/${encodeURIComponent(s.id)}">[${s.n}] ${escapeHtml(s.subject)}</a> <small>${escapeHtml(s.sender)}</small></li>`
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
    openChat(false);
    const history = turns.slice(-6).map(({ role, text }) => ({ role, text }));
    turns.push({ role: "user", text: question });
    renderTurn(turns[turns.length - 1]);
    const answer = { role: "assistant", text: "", sources: [], pending: true };
    const bubble = renderTurn(answer);
    $("button[type='submit']", chatForm).disabled = true;
    try {
      const response = await fetch("/chat", {
        method: "POST",
        headers: JSON_HEADERS,
        body: JSON.stringify({ message: question, history, email_id: emailId || currentEmailId() }),
      });
      if (!response.ok || !response.body) throw new Error(`the server said ${response.status}`);
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
          const event = JSON.parse(line);
          if (event.type === "sources") {
            answer.sources = event.sources || [];
            answer.warning = event.warning || "";
            answer.mode = event.mode;
          } else if (event.type === "mode") {
            answer.mode = event.mode;
          } else if (event.type === "delta") {
            answer.text += event.text;
            renderTurn(answer, bubble);
          }
        }
      }
    } catch (error) {
      answer.text = (answer.text ? answer.text + "\n\n" : "") + `Something went wrong (${error.message}). Try again.`;
    }
    answer.pending = false;
    if (!answer.text) answer.text = "I didn't get an answer. Try asking another way.";
    turns.push(answer);
    saveTurns();
    renderTurn(answer, bubble);
    busy = false;
    $("button[type='submit']", chatForm).disabled = false;
    chatInput.focus({ preventScroll: true });
  }

  if (chat) {
    chatForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const question = chatInput.value;
      chatInput.value = "";
      ask(question);
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
        case "close-drawer":
          return closePreview();
        case "close-chat":
          return closeChat();
        case "clear-chat":
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
