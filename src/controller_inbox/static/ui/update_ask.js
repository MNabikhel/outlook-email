/* The question asked each time CloseDesk opens, on the first page shown (a workspace page or a classic one): when a
   newer CloseDesk is out, "Update now" or "Not now". The server keeps the answer for this run (updates.Question), so
   Not now isn't asked again on the next page, only the next time CloseDesk opens. While GitHub is still being asked
   (a few seconds at most) the page asks again shortly; offline, nothing is asked and nothing is in the way. Update
   now is Settings' Update: refused while mail is being processed, and offered again once that is done (also on the
   page shown next, when the page loads again meanwhile: the server says the update was held back). It says which
   version the page shows, so a tab left open from before an update can't stop the new CloseDesk for nothing. */

import { $, h, replace } from "./dom.js";
import { getJSON, postJSON } from "./api.js";

const AGAIN_MS = 1000; // while GitHub is still being asked
const TRIES = 30; // the asking waits a few seconds at most: past this, CloseDesk opens as normal
const BUSY_MS = 2000; // while the mail being processed holds the update back
const SLOW_MS = 5 * 60 * 1000;
export const SLOW = "CloseDesk hasn't opened again yet. Its window says what it is doing; if it has closed, double-click CloseDesk.";

const pause = (ms) => new Promise((done) => setTimeout(done, ms));

/** CloseDesk has stopped to update: wait for it to answer again (as the new version, or as it was when the update
    couldn't be done), then ``onBack``; ``onSlow`` once if that takes over five minutes. ``before``: the commit that
    asked, and ``run``: the start of CloseDesk that asked (/health says which answers), so a start again that is quick
    enough to come between two looks still counts, as the new version or as the same one. */
export function waitForCloseDesk(before, { run = "", onBack, onSlow }) {
  const started = Date.now();
  let gone = false; // CloseDesk stopped answering: the next answer is the new one
  let slow = false;
  const again = async () => {
    try {
      const response = await fetch("/health", { cache: "no-store" });
      const health = await response.json();
      if (health.app === "closedesk" && (gone || health.commit !== before || (run && health.run && health.run !== run))) return onBack();
    } catch (error) {
      gone = true;
    }
    if (!slow && Date.now() - started > SLOW_MS) {
      slow = true;
      onSlow();
    }
    setTimeout(again, 2000);
  };
  setTimeout(again, 2000);
}

/** Asks, when there is something to ask. ``updating(reply)``: what the page does once Update now was answered (the
    workspace shows its own updating page); without it, the dialog says CloseDesk is updating and the page loads
    again once it is back. */
export async function askOnOpening({ updating = null } = {}) {
  let view = null;
  for (let tries = 0; tries < TRIES; tries += 1) {
    try {
      view = await getJSON("/api/update/question");
    } catch (error) {
      return; // CloseDesk didn't answer: nothing is asked
    }
    if (!view.checking) break;
    await pause(AGAIN_MS);
  }
  if (view && view.ask && !view.checking && !$("#update-ask")) show(view, updating);
}

function show(view, updating) {
  // "asking"; "sending" while Update now is on its way; then "updating" (the dialog says so until CloseDesk is back)
  // or "handed" (the page took over).
  let state = "asking";
  const staying = () => state === "sending" || state === "updating";
  const line = h("p", { class: "update-ask-status", role: "status" });
  const say = (text, tone = "") => {
    line.textContent = text;
    line.className = `update-ask-status${tone ? ` ${tone}` : ""}`;
  };
  const now = view.newer ? h("button", { type: "button", class: "btn btn-sm btn-primary", onclick: () => updateNow() }, "Update now") : null;
  const later = h("button", { type: "button", class: view.newer ? "btn btn-sm ghost" : "btn btn-sm btn-primary", onclick: () => dialog.close() }, view.newer ? "Not now" : "OK");
  const dialog = h(
    "dialog",
    { class: "update-ask", id: "update-ask", "aria-labelledby": "update-ask-title", "aria-describedby": "update-ask-body" },
    h("h2", { id: "update-ask-title" }, view.title),
    h(
      "div",
      { id: "update-ask-body" },
      view.failed ? h("p", { class: "update-ask-failed" }, view.failed_message) : null,
      view.newer ? h("p", null, view.message) : null,
      view.whats_new ? h("p", { class: "update-ask-new" }, h("b", null, "What's new: "), view.whats_new) : null,
      view.newer ? h("p", { class: "update-ask-help" }, view.how) : null
    ),
    line,
    h("div", { class: "update-ask-actions" }, later, now)
  );
  // The keys stay with the question: the page's own shortcuts behind it don't run.
  dialog.addEventListener("keydown", (event) => event.stopPropagation());
  // Esc is Not now, except once Update now is pressed: CloseDesk updating stays in sight until it is back.
  dialog.addEventListener("cancel", (event) => {
    if (staying()) event.preventDefault();
  });
  dialog.addEventListener("close", () => {
    if (staying()) return dialog.showModal();
    if (state === "asking") postJSON("/api/update/question", { answer: "not-now" }).catch(() => {});
    dialog.remove();
  });

  async function updateNow() {
    now.disabled = true;
    later.disabled = true;
    say("");
    state = "sending";
    try {
      const reply = await postJSON("/api/update", { commit: view.commit });
      if (updating) {
        state = "handed";
        dialog.close();
        updating(reply);
      } else {
        updatingHere(reply);
      }
    } catch (error) {
      state = "asking";
      later.disabled = false;
      const busy = error.status === 409 && error.message === view.busy_message;
      say(error.message, busy ? "wait" : "error");
      if (busy) waitForTheMail();
      else now.disabled = false;
    }
  }

  // Refused while mail is being processed: offered again once that is done.
  async function waitForTheMail() {
    while (dialog.isConnected) {
      await pause(BUSY_MS);
      let found;
      try {
        found = await getJSON("/api/update/question");
      } catch (error) {
        continue;
      }
      if (!dialog.isConnected) return;
      if (!found.busy) {
        now.disabled = false;
        say(view.done_message, "ok");
        now.focus();
        return;
      }
    }
  }

  function updatingHere(reply) {
    state = "updating";
    dialog.classList.add("updating");
    const message = h("p", { id: "update-ask-body" }, reply.message || view.updating);
    replace(dialog, h("h2", { id: "update-ask-title" }, "Updating CloseDesk"), message, h("progress", { "aria-label": "Updating" }));
    waitForCloseDesk(reply.commit || view.commit, { run: reply.run, onBack: () => location.reload(), onSlow: () => (message.textContent = SLOW) });
  }

  document.body.append(dialog);
  dialog.showModal();
  // Not now has the focus: the question can come up while a key meant for the page is being pressed (Enter in the
  // chat or the search), and that key must not update CloseDesk.
  later.focus();
  if (now && view.held) {
    // Update now was refused while mail was processed (here, or on the page before this one loaded again).
    if (view.busy) {
      now.disabled = true;
      say(view.busy_message, "wait");
      waitForTheMail();
    } else {
      say(view.done_message, "ok");
    }
  }
}
