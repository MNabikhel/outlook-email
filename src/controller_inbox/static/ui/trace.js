/* The whole matter an email belongs to (/app/trace/<id>): every email linked to it, two links out, oldest first,
   each with why it is here, and what changed between the versions of the files sent along the way. */

import { h, icon, plural } from "./dom.js";

const enc = encodeURIComponent;
const mailHref = (id) => `/app/mail/${enc(id)}`;

function step(s) {
  const mark = s.locked
    ? h("span", { class: "tag tag-fraud", title: "Held as possible payment fraud: its files are not opened" }, "Held: possible fraud")
    : null;
  const why = s.here ? [h("span", { class: "tr-why here" }, "The email you came from")] : s.reasons.map((reason) => h("span", { class: "tr-why" }, reason));
  return h(
    "li",
    { class: `tr-step${s.here ? " here" : ""}` },
    h(
      "a",
      { class: "tr-card", href: mailHref(s.id), dataset: { nav: "" } },
      h("span", { class: "tr-top" }, h("b", { class: "trunc" }, s.sender), h("span", { class: "muted small", title: s.when_full }, s.when)),
      h("span", { class: "tr-subject" }, s.subject),
      s.first_line ? h("span", { class: "muted small tr-first" }, s.first_line) : null,
      s.file_names.length ? h("span", { class: "muted small rel-files" }, icon("clip", 12), h("span", null, s.file_names.join(", "))) : null,
      h("span", { class: "tr-whys" }, ...why, mark)
    )
  );
}

function fileLink(f) {
  return h("a", { href: `${mailHref(f.email_id)}/file/${f.n}?tab=text`, dataset: { nav: "" } }, f.name);
}

function version(v) {
  return h(
    "li",
    { class: "tr-version" },
    h("p", { class: "tr-version-h" }, fileLink(v.old), h("span", { class: "muted small" }, ` ${v.old.when} · ${v.old.sender}`), h("span", { class: "tr-arrow", "aria-hidden": "true" }, " → "), h("span", { class: "sr-only" }, " then "), fileLink(v.new), h("span", { class: "muted small" }, ` ${v.new.when} · ${v.new.sender}`)),
    v.changes.length
      ? h("ul", { class: "tr-changes" }, v.changes.map((change) => h("li", null, change)), v.more ? h("li", { class: "muted" }, `and ${plural(v.more, "more change")}`) : null)
      : h("p", { class: "muted small" }, "No change in what was read from the two files (their layout or pictures may differ).")
  );
}

export function traceView(data) {
  const others = data.steps.length - 1;
  return h(
    "article",
    { class: "rd trace" },
    h("div", { class: "rd-bar" }, h("a", { class: "btn btn-quiet", href: mailHref(data.email.id), dataset: { nav: "" }, title: "Back to the email (Esc)" }, icon("back", 15), h("span", { class: "trunc" }, data.email.subject))),
    h(
      "div",
      { class: "rd-scroll" },
      h(
        "div",
        { class: "rd-inner" },
        h(
          "header",
          { class: "rd-head" },
          h("h1", { class: "rd-subject" }, "The whole matter"),
          h(
            "p",
            { class: "muted" },
            others
              ? `${plural(others, "other email")} linked to this one: its conversation, and mail sharing an invoice, a PO or a file with it or with those. Oldest first.`
              : "Nothing else is linked to this email yet."
          )
        ),
        data.versions.length
          ? h("section", { class: "rd-sec" }, h("h2", { class: "rd-h" }, "What changed in the files"), h("ul", { class: "tr-versions" }, data.versions.map(version)))
          : null,
        h("section", { class: "rd-sec" }, h("h2", { class: "rd-h" }, "Every email, oldest first"), h("ol", { class: "tr-steps" }, data.steps.map(step)))
      )
    )
  );
}
