/* Ask CloseDesk, docked beside the reading pane. Answers stream in as newline-delimited JSON events
   (sources, step, note, delta, revise, check, context, vision, mode, error, done) from POST /chat, the same
   endpoint and events the classic chat uses. The question is about the open email unless you un-scope it. */

import { h, icon, append, replace, toast, plural, reducedMotion } from "./dom.js";
import { getJSON, postJSON, postStream } from "./api.js";
import { formatAnswer, mailHref, fileHref } from "./format.js";

const CHAT_KEY = "closedesk-chat-id";
const OPEN_KEY = "closedesk-ws-chat";

const store = {
  get(key) {
    try {
      return localStorage.getItem(key);
    } catch (error) {
      return null;
    }
  },
  set(key, value) {
    try {
      if (value === null) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
    } catch (error) {
      /* private mode */
    }
  },
};

export function createChat({ root, onToggle, visionRead }) {
  let chatId = store.get(CHAT_KEY);
  let turns = [];
  let loaded = false;
  let loading = null; // the load of the conversation on its way, if any
  let loadRound = 0; // a later load wins over an earlier one
  let busy = false;
  let controller = null;
  let round = 0; // the answer showing in the panel; an older one stops updating it
  let scope = null; // { id, subject }
  let scopeOff = false;
  let model = { active: false, name: "" };

  const title = h("strong", { class: "chat-name" }, "Ask CloseDesk");
  const status = h("small", { class: "chat-status" });
  const historyBtn = h("button", { type: "button", class: "icon-btn", title: "Past conversations", "aria-label": "Past conversations", onclick: () => toggleHistory() }, icon("history"));
  const head = h(
    "header",
    { class: "chat-head" },
    h("div", { class: "chat-title" }, title, status),
    h(
      "div",
      { class: "chat-tools" },
      historyBtn,
      h("button", { type: "button", class: "icon-btn", title: "New conversation", "aria-label": "New conversation", onclick: () => newChat() }, icon("plus")),
      h("button", { type: "button", class: "icon-btn", title: "Close (Esc)", "aria-label": "Close Ask CloseDesk", onclick: () => close() }, icon("x"))
    )
  );
  const log = h("div", { class: "chat-log", role: "log", "aria-live": "polite" });
  const historySearch = h("input", { type: "search", placeholder: "Search past conversations", "aria-label": "Search past conversations" });
  const historyList = h("ul", { class: "chat-history-list" });
  const history = h("div", { class: "chat-history", hidden: true }, h("div", { class: "chat-history-head" }, historySearch), historyList);
  const scopeBox = h("div", { class: "chat-scope" });
  const suggest = h("div", { class: "chat-suggest" });
  const input = h("textarea", { rows: "1", maxlength: "1000", placeholder: "Ask about your mail…", "aria-label": "Your question" });
  const send = h("button", { type: "submit", class: "chat-send", title: "Send (Enter)", "aria-label": "Send" }, icon("send"));
  const form = h("form", { class: "chat-form" }, input, send);
  const foot = h("div", { class: "chat-foot" }, scopeBox, suggest, form, h("p", { class: "chat-hint" }, "Enter to send · Shift+Enter for a new line"));
  replace(root, head, h("div", { class: "chat-body" }, log, history), foot);

  /* ---------- Rendering ---------- */

  function sourcesEl(turn) {
    const sources = turn.sources || [];
    if (!sources.length) return null;
    return h(
      "details",
      { class: "msg-sources-all" },
      h("summary", null, `Looked at ${plural(sources.length, "email")}`),
      h(
        "ul",
        null,
        sources.map((source) =>
          h(
            "li",
            null,
            h("a", { href: mailHref(source), dataset: source.chat ? {} : { nav: "" }, target: source.chat ? "_blank" : null }, `[${source.n}] ${source.subject}`),
            source.sender ? h("small", null, ` · ${source.sender}`) : null,
            source.fraud ? h("span", { class: "chip chip-danger" }, "fraud check") : null
          )
        )
      )
    );
  }

  function stepsEl(turn) {
    const steps = turn.steps || [];
    const notes = turn.notes || [];
    if (!steps.length && !notes.length) return null;
    const items = [
      ...steps.map((step, index) =>
        h("li", { class: turn.pending && index === steps.length - 1 && !turn.text ? "now" : "" }, step)
      ),
      ...notes.map((note) => h("li", { class: "noted" }, `Noted: ${note}`)),
    ];
    const label = `Read ${plural(steps.length, "thing")}${notes.length ? `, noted ${notes.length}` : ""}`;
    const box = h("details", { class: "msg-steps" }, h("summary", null, label), h("ol", { class: "timeline" }, items));
    if (turn.pending || turn.stepsOpen) box.open = true;
    box.addEventListener("toggle", () => (turn.stepsOpen = box.open));
    return box;
  }

  /* An answer about a scanned file can offer to read its pages with the vision model too: a read of minutes runs
     as the background job, and when it finishes the question can be asked again with both readings. */
  function visionEl(turn) {
    const vision = turn.vision;
    if (!vision || turn.pending) return null;
    if (vision.state === "done") {
      return h(
        "div",
        { class: "msg-vision" },
        h("p", null, vision.message),
        h("button", { type: "button", class: "btn btn-sm", onclick: () => ask(vision.question, vision.email_id) }, icon("refresh", 14), h("span", null, "Ask again"))
      );
    }
    if (vision.state === "reading") return h("div", { class: "msg-vision" }, h("p", { class: "muted" }, vision.message));
    const read = async (event) => {
      event.currentTarget.disabled = true;
      const result = visionRead ? await visionRead(vision.email_id, vision.n) : null;
      if (result && result.started) {
        vision.state = "reading";
        vision.message = `Reading ${plural(result.pages.length, "page")} of ${vision.file} with the vision model. Ask again when it's done.`;
      } else if (result) {
        vision.message = result.message;
      }
      renderLog();
    };
    return h(
      "div",
      { class: "msg-vision" },
      h("p", null, vision.text),
      vision.message ? h("p", { class: "muted" }, vision.message) : null,
      h("button", { type: "button", class: "btn btn-sm", onclick: read }, icon("eye", 14), h("span", null, "Read with the vision model"))
    );
  }

  window.addEventListener("closedesk:vision", (event) => {
    const done = event.detail || {};
    let changed = false;
    for (const turn of turns) {
      const vision = turn.vision;
      if (vision && vision.state !== "done" && vision.email_id === done.email_id && vision.n === done.n) {
        vision.state = "done";
        vision.message = done.message;
        changed = true;
      }
    }
    if (changed) renderLog();
  });

  function turnEl(turn, el) {
    el = el || h("div");
    el.className = `msg ${turn.role === "user" ? "user" : "bot"}${turn.pending ? " pending" : ""}`;
    if (turn.role === "user") {
      replace(el, h("p", null, turn.text));
      return el;
    }
    const answer = h("div", { class: "msg-text" });
    if (turn.text) answer.innerHTML = formatAnswer(turn.text, turn.sources); // escaped first, see format.js
    else {
      const steps = turn.steps || [];
      append(answer, h("span", { class: "typing" }, steps.length ? `${steps[steps.length - 1]}…` : "Thinking…"));
    }
    const cited = turn.mode === "model" && !turn.pending ? (turn.sources || []).filter((s) => turn.text.includes(`[${s.n}]`)) : [];
    replace(
      el,
      turn.warning ? h("p", { class: "msg-warn" }, icon("alert", 14), h("span", null, turn.warning)) : null,
      turn.note ? h("p", { class: "msg-note" }, turn.note) : null,
      stepsEl(turn),
      answer,
      turn.checks && turn.checks.length ? h("ul", { class: "msg-check" }, turn.checks.map((item) => h("li", null, icon("ok", 13), h("span", null, item)))) : null,
      turn.context ? h("p", { class: "msg-context" }, turn.context, " ", h("a", { href: "/settings" }, "Setup")) : null,
      visionEl(turn),
      cited.length
        ? h(
            "ul",
            { class: "msg-cited" },
            cited.map((s) =>
              h(
                "li",
                null,
                h("a", { class: "cite", href: mailHref(s), dataset: s.chat ? {} : { nav: "" } }, `[${s.n}] ${s.subject}`),
                (s.files || [])
                  .filter((file) => turn.text.toLowerCase().includes(file.name.toLowerCase()))
                  .map((file) => h("a", { class: "cite-file", href: fileHref(s, file, ""), target: "_blank", rel: "noopener" }, icon("file", 12), file.name))
              )
            )
          )
        : null,
      turn.stopped ? h("p", { class: "msg-stopped" }, "Stopped.") : null,
      turn.pending ? null : sourcesEl(turn)
    );
    return el;
  }

  const nearBottom = () => log.scrollHeight - log.scrollTop - log.clientHeight < 80;

  function renderLog() {
    if (!turns.length) {
      replace(
        log,
        h(
          "div",
          { class: "chat-empty" },
          h("p", { class: "chat-empty-title" }, "Ask about your mail"),
          h(
            "p",
            null,
            "What's urgent, what needs a reply, or anything from a person or company. With an email open, questions are about that email and its files. Click a [number] in an answer to open what it cites."
          )
        )
      );
      return;
    }
    replace(log, turns.map((turn) => turnEl(turn)));
    log.scrollTop = log.scrollHeight;
  }

  function renderScope() {
    const current = scope && !scopeOff ? scope : null;
    replace(
      scopeBox,
      current
        ? h(
            "span",
            { class: "scope-chip", title: "Questions are about this email" },
            icon("mail", 13),
            h("span", { class: "scope-text" }, current.subject || "(no subject)"),
            h("button", { type: "button", "aria-label": "Ask about all mail instead", title: "Ask about all mail instead", onclick: () => { scopeOff = true; renderScope(); } }, icon("x", 12))
          )
        : h("span", { class: "scope-all" }, "Asking about all your mail")
    );
    const asks = current
      ? [["Summarize this email", "Summarize this email and its attachments"], ["What does it need from me?", "What does this email need from me?"]]
      : [["What's urgent today?", "What's urgent today?"], ["Needs a reply", "What needs a reply from me?"], ["Approvals", "Anything waiting for my approval?"]];
    replace(
      suggest,
      asks.map(([label, question]) => h("button", { type: "button", class: "chip-btn", onclick: () => ask(question) }, label))
    );
    input.placeholder = current ? "Ask about this email…" : "Ask about your mail…";
  }

  function renderStatus() {
    status.textContent = model.active ? `Answers from ${model.name} on this computer` : "No local model: answers come from looking things up";
  }

  function setBusy(value) {
    busy = value;
    replace(send, icon(value ? "stop" : "send"));
    send.title = value ? "Stop" : "Send (Enter)";
    send.setAttribute("aria-label", value ? "Stop answering" : "Send");
    send.classList.toggle("stop", value);
    root.classList.toggle("busy", value);
  }

  /* ---------- Conversations ---------- */

  function remember(id) {
    chatId = id;
    store.set(CHAT_KEY, id || null);
  }

  /** Show conversation ``id``. ``switching``: another conversation was picked, so an answer still streaming
   * belongs to the old one and stops updating the panel. Opening the panel only fills it in. */
  function load(id, switching = true) {
    const mine = ++loadRound;
    if (switching) ++round;
    loaded = false;
    const done = (async () => {
      if (!id) {
        turns = [];
        title.textContent = "Ask CloseDesk";
      } else {
        try {
          const saved = await getJSON(`/chats/${encodeURIComponent(id)}`);
          if (mine !== loadRound) return;
          remember(saved.id);
          turns = (saved.turns || []).map((turn) => ({ ...turn, pending: false }));
          title.textContent = saved.title || "Ask CloseDesk";
        } catch (error) {
          if (mine !== loadRound) return;
          remember(null);
          turns = [];
          title.textContent = "Ask CloseDesk";
        }
      }
      loaded = true;
      renderLog();
    })();
    loading = done;
    done.finally(() => {
      if (loading === done) loading = null;
    });
    return done;
  }

  /** Wait for the conversation to be on screen: a question asked while it loads goes after its earlier turns. */
  async function ready() {
    while (!loaded) await (loading || load(chatId, false));
  }

  function newChat() {
    stop();
    remember(null);
    closeHistory();
    load(null);
    input.focus();
  }

  /* ---------- Asking ---------- */

  async function ask(question, emailId) {
    question = (question || "").trim();
    if (!question || busy) return;
    open(false);
    closeHistory();
    setBusy(true);
    await ready();
    const mine = ++round;
    const about = emailId || (scope && !scopeOff ? scope.id : null);
    turns.push({ role: "user", text: question });
    const answer = { role: "assistant", text: "", sources: [], steps: [], notes: [], pending: true };
    turns.push(answer);
    renderLog();
    const bubble = log.lastElementChild;
    let frame = 0;
    const paint = () => {
      if (frame) return;
      const pinned = nearBottom();
      frame = requestAnimationFrame(() => {
        frame = 0;
        turnEl(answer, bubble);
        if (pinned) log.scrollTop = log.scrollHeight;
      });
    };
    controller = new AbortController();
    try {
      await postStream(
        "/chat",
        { message: question, chat_id: chatId, email_id: about },
        (event) => {
          if (mine !== round) return;
          switch (event.type) {
            case "chat":
              remember(event.id);
              if (event.title) title.textContent = event.title;
              break;
            case "sources":
              answer.sources = event.sources || [];
              if (event.warning) answer.warning = event.warning;
              answer.mode = event.mode;
              break;
            case "step":
              answer.steps.push(event.text);
              break;
            case "note":
              answer.notes.push(event.text);
              break;
            case "delta":
              answer.text += event.text;
              break;
            case "revise":
              answer.text = event.text;
              break;
            case "check":
              answer.checks = event.items || [];
              break;
            case "context":
              answer.context = event.text;
              break;
            case "vision":
              answer.vision = { ...event, state: "offered", message: "" };
              break;
            case "mode":
              answer.mode = event.mode;
              answer.note = event.note || "";
              break;
            case "error":
              answer.text = (answer.text ? answer.text + "\n\n" : "") + event.text;
              break;
            default:
              return;
          }
          paint();
        },
        { signal: controller.signal }
      );
    } catch (error) {
      if (error.name === "AbortError") answer.stopped = true;
      else answer.text = (answer.text ? answer.text + "\n\n" : "") + `Something went wrong (${error.message}). Try again.`;
    }
    controller = null;
    if (frame) cancelAnimationFrame(frame);
    frame = 0;
    setBusy(false);
    if (mine !== round && !answer.stopped) return;
    answer.pending = false;
    if (!answer.text && !answer.stopped) answer.text = "I didn't get an answer. Try asking another way.";
    const pinned = nearBottom();
    turnEl(answer, bubble);
    if (pinned) log.scrollTop = log.scrollHeight;
    if (isOpen()) input.focus({ preventScroll: true });
  }

  function stop() {
    if (controller) controller.abort();
  }

  /* ---------- Past conversations ---------- */

  async function showHistory() {
    const query = historySearch.value.trim();
    let saved = [];
    try {
      saved = (await getJSON(`/chats?q=${encodeURIComponent(query)}`)).chats || [];
    } catch (error) {
      saved = [];
    }
    if (!saved.length) {
      replace(historyList, h("li", { class: "chat-history-empty" }, query ? "No conversation mentions that." : "Your conversations will show up here."));
      return;
    }
    replace(
      historyList,
      saved.map((item) =>
        h(
          "li",
          { class: `chat-history-item${item.id === chatId ? " current" : ""}` },
          h(
            "button",
            { type: "button", class: "chat-history-open", onclick: () => { closeHistory(); load(item.id); } },
            h("b", null, item.title || "Files only"),
            h("small", null, `${new Date(item.updated_at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: document.body.dataset.tz || undefined })} · ${plural(item.questions, "question")}`)
          ),
          h("button", { type: "button", class: "icon-btn", title: "Delete this conversation", "aria-label": "Delete this conversation", onclick: () => remove(item.id) }, icon("x", 14))
        )
      )
    );
  }

  async function remove(id) {
    if (!window.confirm("Delete this conversation and the files added to it?")) return;
    try {
      await postJSON(`/chats/${encodeURIComponent(id)}/delete`);
    } catch (error) {
      return toast("That conversation couldn't be deleted.", { tone: "error" });
    }
    if (id === chatId) newChat();
    showHistory();
  }

  function toggleHistory() {
    history.hidden = !history.hidden;
    historyBtn.classList.toggle("on", !history.hidden);
    if (!history.hidden) {
      showHistory();
      historySearch.focus();
    }
  }

  function closeHistory() {
    if (history.hidden) return false;
    history.hidden = true;
    historyBtn.classList.remove("on");
    return true;
  }

  /* ---------- Open and close ---------- */

  function isOpen() {
    return !root.hidden;
  }

  function open(focus = true) {
    if (!loaded && !loading) load(chatId, false);
    if (root.hidden) {
      root.hidden = false;
      document.documentElement.dataset.chat = "open";
      store.set(OPEN_KEY, "open");
      if (!reducedMotion()) {
        root.classList.add("entering");
        setTimeout(() => root.classList.remove("entering"), 200);
      }
      onToggle(true);
    }
    if (focus) input.focus({ preventScroll: true });
  }

  function close() {
    if (root.hidden) return false;
    closeHistory();
    root.hidden = true;
    delete document.documentElement.dataset.chat;
    store.set(OPEN_KEY, null);
    onToggle(false);
    return true;
  }

  /* ---------- Wiring ---------- */

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (busy) return stop();
    const question = input.value;
    input.value = "";
    grow();
    ask(question);
  });
  const grow = () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
  };
  input.addEventListener("input", grow);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  let searchTimer = 0;
  historySearch.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(showHistory, 200);
  });
  // Another window (the classic pop-out or a second tab) switched conversations: follow it when idle.
  window.addEventListener("storage", (event) => {
    if (event.key === CHAT_KEY && !busy && event.newValue !== chatId) {
      chatId = event.newValue;
      if (isOpen()) load(chatId);
      else loaded = false;
    }
  });

  renderScope();
  renderStatus();
  if (document.documentElement.dataset.chat === "open") {
    root.hidden = false;
    load(chatId, false);
  }

  return {
    open,
    close,
    isOpen,
    toggle: () => (isOpen() ? close() : open()),
    ask,
    stop,
    closeHistory,
    busy: () => busy,
    setScope(email) {
      const next = email ? { id: email.id, subject: email.subject } : null;
      if ((next && next.id) !== (scope && scope.id)) scopeOff = false;
      scope = next;
      renderScope();
    },
    setModel(next) {
      model = next || model;
      renderStatus();
    },
    prefill(text) {
      open();
      input.value = text;
      grow();
      input.setSelectionRange(text.length, text.length);
    },
  };
}
