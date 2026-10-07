/* Ctrl/Cmd+K: jump to a view, find an email, run a command, or ask Ask CloseDesk. And the ? help sheet. */

import { h, icon, replace } from "./dom.js";
import { getJSON } from "./api.js";

const SHORTCUTS = [
  ["Moving", [["j", "Next in the list"], ["k", "Previous in the list"], ["Enter", "Open the selected email"], ["u", "Back to the list"], ["Esc", "Close what's open"]]],
  ["Acting", [["e", "Mark done, with Undo"], ["c", "Show or hide Ask CloseDesk"], ["/", "Search"], ["Ctrl K", "Jump, find or ask"]]],
  ["Going", [["g t", "Today"], ["g i", "Important"], ["g n", "Informational"], ["g r", "Reference"], ["g a", "All mail"], ["g k", "Tasks"], ["g f", "Fraud check"], ["g c", "AP coding"], ["g d", "Digest"], ["g s", "Settings"]]],
  ["Layout", [["[", "Collapse the side bar"], ["?", "This sheet"]]],
];

export function createOverlays({ overlay, commands, localMail, ask, openMail }) {
  let mode = "";
  let returnFocus = null;

  function show(panel, name) {
    returnFocus = document.activeElement;
    mode = name;
    replace(overlay, panel);
    overlay.hidden = false;
    overlay.onclick = (event) => {
      if (event.target === overlay) hide();
    };
  }

  function hide() {
    if (overlay.hidden) return false;
    overlay.hidden = true;
    replace(overlay);
    mode = "";
    if (returnFocus && document.contains(returnFocus)) returnFocus.focus({ preventScroll: true });
    return true;
  }

  /* ---------- Palette ---------- */

  function palette(initial = "") {
    const input = h("input", { type: "text", placeholder: "Jump to a folder, find an email, or ask a question", "aria-label": "Search or jump", autocomplete: "off", spellcheck: "false" });
    const list = h("ul", { class: "pal-list", role: "listbox", id: "pal-list" });
    input.setAttribute("aria-controls", "pal-list");
    const panel = h(
      "div",
      { class: "palette", role: "dialog", "aria-modal": "true", "aria-label": "Search or jump" },
      h("div", { class: "pal-input" }, icon("search", 16), input, h("kbd", null, "Esc")),
      list,
      h("div", { class: "pal-foot" }, h("span", null, h("kbd", null, "↑"), h("kbd", null, "↓"), " move"), h("span", null, h("kbd", null, "Enter"), " choose"), h("span", null, h("kbd", null, "Ctrl Enter"), " ask CloseDesk"))
    );
    let results = [];
    let index = 0;
    let remote = [];
    let timer = 0;
    let controller = null;

    const matches = (text, words) => words.every((word) => text.includes(word));

    function compute() {
      const q = input.value.trim();
      const words = q.toLowerCase().split(/\s+/).filter(Boolean);
      const out = [];
      const cmds = commands().filter((cmd) => !words.length || matches(`${cmd.label} ${cmd.keywords || ""}`.toLowerCase(), words));
      if (cmds.length) out.push({ group: "Go to and do" }, ...cmds.slice(0, words.length ? 6 : 12));
      const seen = new Set();
      const mail = [];
      for (const item of [...localMail(words), ...remote]) {
        if (seen.has(item.id)) continue;
        seen.add(item.id);
        mail.push({ label: item.subject, hint: item.sender, icon: item.locked ? "lock" : "mail", run: () => openMail(item.id) });
      }
      if (words.length && mail.length) out.push({ group: "Emails" }, ...mail.slice(0, 8));
      if (q) out.push({ group: "Ask CloseDesk" }, { label: `Ask: ${q}`, icon: "chat", hint: "Ctrl Enter", run: () => ask(q), ask: true });
      results = out;
      const choices = results.filter((r) => !r.group);
      index = Math.min(index, Math.max(0, choices.length - 1));
      draw();
    }

    function draw() {
      let n = -1;
      replace(
        list,
        results.length
          ? results.map((r) => {
              if (r.group) return h("li", { class: "pal-group", role: "presentation" }, r.group);
              n += 1;
              const mine = n;
              return h(
                "li",
                {
                  class: `pal-item${mine === index ? " on" : ""}`,
                  role: "option",
                  id: `pal-${mine}`,
                  "aria-selected": String(mine === index),
                  onmousemove: () => {
                    if (index !== mine) {
                      index = mine;
                      draw();
                    }
                  },
                  onclick: () => choose(mine),
                },
                icon(r.icon || "right", 15),
                h("span", { class: "pal-label" }, r.label),
                r.hint ? h("span", { class: "pal-hint" }, r.hint) : null
              );
            })
          : h("li", { class: "pal-empty" }, "Nothing matches. Press Enter to ask CloseDesk.")
      );
      input.setAttribute("aria-activedescendant", `pal-${index}`);
      const on = list.querySelector(".pal-item.on");
      if (on) on.scrollIntoView({ block: "nearest" });
    }

    function choose(i) {
      const choices = results.filter((r) => !r.group);
      const picked = choices[i];
      const q = input.value.trim();
      hide();
      if (picked) picked.run();
      else if (q) ask(q);
    }

    input.addEventListener("input", () => {
      index = 0;
      compute();
      clearTimeout(timer);
      const q = input.value.trim();
      if (q.length < 2) {
        remote = [];
        return;
      }
      timer = setTimeout(async () => {
        if (controller) controller.abort();
        controller = new AbortController();
        try {
          const data = await getJSON(`/api/mail?q=${encodeURIComponent(q)}&limit=8`, { signal: controller.signal });
          if (input.value.trim() === q) {
            remote = data.items;
            compute();
          }
        } catch (error) {
          /* typing on, or the search failed: local matches stay */
        }
      }, 180);
    });
    input.addEventListener("keydown", (event) => {
      // The palette owns these keys while it is open; the page's own shortcuts never see them.
      event.stopPropagation();
      const count = results.filter((r) => !r.group).length;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        hide();
      } else if (event.key === "ArrowDown") {
        event.preventDefault();
        index = count ? (index + 1) % count : 0;
        draw();
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        index = count ? (index - 1 + count) % count : 0;
        draw();
      } else if (event.key === "Enter") {
        event.preventDefault();
        if (event.ctrlKey || event.metaKey) {
          const q = input.value.trim();
          hide();
          if (q) ask(q);
        } else choose(index);
      } else if (event.key === "Escape") {
        event.preventDefault();
        hide();
      } else if (event.key === "Tab") {
        event.preventDefault();
      }
    });
    show(panel, "palette");
    input.value = initial;
    compute();
    input.focus();
  }

  /* ---------- Help ---------- */

  function help() {
    const closeBtn = h("button", { type: "button", class: "icon-btn", "aria-label": "Close", onclick: () => hide() }, icon("x"));
    const panel = h(
      "div",
      { class: "help", role: "dialog", "aria-modal": "true", "aria-label": "Keyboard shortcuts" },
      h("div", { class: "help-head" }, h("h2", null, "Keyboard shortcuts"), closeBtn),
      h(
        "div",
        { class: "help-cols" },
        SHORTCUTS.map(([title, rows]) =>
          h("section", null, h("h3", null, title), h("dl", null, rows.map(([keys, what]) => [h("dt", null, keys.split(" ").map((key) => h("kbd", null, key))), h("dd", null, what)])))
        )
      )
    );
    panel.addEventListener("keydown", (event) => {
      event.stopPropagation();
      if (event.key === "Escape" || event.key === "?") {
        event.preventDefault();
        hide();
      }
    });
    show(panel, "help");
    closeBtn.focus();
  }

  return { palette, help, hide, mode: () => mode, isOpen: () => !overlay.hidden };
}
