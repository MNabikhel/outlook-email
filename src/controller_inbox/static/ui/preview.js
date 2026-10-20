/* Seeing a file as it came: the Preview tab and the small preview on each attachment of an email.

   A PDF opens in the browser's own viewer (every page, zoom, find, print); a picture is shown as it is; a TIFF or
   HEIC is drawn page by page; a workbook or CSV is shown as a grid with its sheets, number formats, merged cells and
   formulas; a Word document as its headings, paragraphs and tables. Nothing here runs anything in the file. */

import { h, icon, replace, plural } from "./dom.js";
import { getJSON, getBlob, filePath } from "./api.js";

const enc = encodeURIComponent;
const apiFile = (emailId, n) => `/api/mail/${enc(emailId)}/files/${Number(n)}`;

/** The Preview tab's content for ``data`` (the file JSON). */
export function previewPane(data, ctx) {
  const { email, file } = data;
  const base = filePath(email.id, file.n);
  const note = (...words) => h("p", { class: "pvw-note muted small" }, ...words);
  if (file.show === "pdf") {
    return h(
      "div",
      { class: "pvw" },
      note(
        "The PDF as it came. Use ",
        h("a", { href: ctx.fileUrl(file.n, "page"), dataset: { nav: "", replace: "" } }, "Page"),
        " to see where each piece of text was read, fix a reading, or teach CloseDesk a field."
      ),
      h("iframe", { class: "pvw-pdf", src: `${base}/view#view=FitH`, title: `${file.name}, as it came` })
    );
  }
  if (file.show === "image") {
    return h("div", { class: "pvw" }, h("div", { class: "pvw-image" }, h("img", { src: `${base}/view`, alt: file.name })));
  }
  if (file.show === "pages") return pagesPane(email.id, file);
  if (["sheet", "doc", "slides", "text", "zip"].includes(file.show)) return sheetPane(email.id, file);
  return h("div", { class: "pvw" }, h("p", { class: "muted" }, "There's no preview for this kind of file. Its Text tab has what CloseDesk read from it."));
}

/* ---------- Pages drawn as pictures (TIFF, HEIC, BMP) ---------- */

function pagesPane(emailId, file) {
  const holder = h("div", { class: "pvw-pages" }, h("p", { class: "muted" }, "Drawing the pages…"));
  (async () => {
    try {
      const first = await getJSON(`${apiFile(emailId, file.n)}/pages/1/regions`);
      const count = Math.min(first.pages || 1, 30);
      replace(holder);
      for (let p = 1; p <= count; p += 1) {
        const img = h("img", { alt: `Page ${p} of ${file.name}`, class: "pvw-page" });
        holder.append(h("figure", null, img, h("figcaption", { class: "muted small" }, `Page ${p} of ${first.pages}`)));
        const blob = await getBlob(`${apiFile(emailId, file.n)}/pages/${p}.png`);
        if (!holder.isConnected) return;
        showBlob(img, blob);
      }
      if (first.pages > count) holder.append(h("p", { class: "muted small" }, `${plural(first.pages - count, "more page")} not drawn here: see the Page tab.`));
    } catch (error) {
      replace(holder, h("p", { class: "muted" }, `Couldn't draw the pages (${error.message}).`));
    }
  })();
  return h("div", { class: "pvw" }, holder);
}

/** Shows ``blob`` in ``img`` and lets the browser free it once the picture is drawn (or fails to be): the image
    stays on the page, and a blob URL that is never revoked keeps its bytes in memory until the tab closes. */
function showBlob(img, blob) {
  const url = URL.createObjectURL(blob);
  const done = () => URL.revokeObjectURL(url);
  img.addEventListener("load", done, { once: true });
  img.addEventListener("error", done, { once: true });
  img.src = url;
}

/* ---------- Workbooks, CSV and Word ---------- */

function sheetPane(emailId, file) {
  const holder = h("div", { class: "pvw-sheet-wrap" }, h("p", { class: "muted" }, "Opening the file…"));
  getJSON(`${apiFile(emailId, file.n)}/sheet`)
    .then((got) => {
      if (!holder.isConnected) return;
      if (got.note) return replace(holder, h("p", { class: "muted" }, got.note));
      const views = { doc: () => docView(got.blocks), slides: () => slidesView(got.slides), text: () => textView(got.text), zip: () => zipView(got) };
      replace(holder, (views[got.kind] || (() => workbook(got.sheets)))());
    })
    .catch((error) => holder.isConnected && replace(holder, h("p", { class: "muted" }, error.message)));
  return h("div", { class: "pvw" }, holder);
}

function workbook(sheets) {
  if (!sheets.length) return h("p", { class: "muted" }, "This workbook has no sheets to show.");
  const formula = h("div", { class: "xl-fx", "aria-live": "polite" }, h("span", { class: "xl-fx-ref" }, ""), h("span", { class: "xl-fx-label" }, "fx"), h("code", { class: "xl-fx-text" }, "Click a cell to see what's in it."));
  const body = h("div", { class: "xl-body" });
  const visible = sheets.map((sheet, index) => ({ sheet, index })).filter(({ sheet }) => !sheet.hidden);
  const first = (visible[0] || { index: 0 }).index;
  const tabs = sheets.map((sheet, index) =>
    h(
      "button",
      { type: "button", class: `xl-tab${index === first ? " on" : ""}${sheet.hidden ? " hidden-sheet" : ""}`, role: "tab", "aria-selected": String(index === first), onclick: () => pick(index), title: sheet.hidden ? "A hidden sheet" : null },
      sheet.name
    )
  );
  function pick(index) {
    tabs.forEach((tab, i) => {
      tab.classList.toggle("on", i === index);
      tab.setAttribute("aria-selected", String(i === index));
    });
    replace(body, grid(sheets[index], formula));
    $fx(formula, "", "", "Click a cell to see what's in it.");
  }
  pick(first);
  return h("div", { class: "xl" }, formula, body, h("div", { class: "xl-tabs", role: "tablist", "aria-label": "Sheets" }, tabs));
}

function $fx(formula, ref, label, text) {
  const [refEl, , textEl] = formula.children;
  refEl.textContent = ref;
  textEl.textContent = text;
  textEl.classList.toggle("formula", label === "f");
}

function grid(sheet, formula) {
  const cols = sheet.cols.filter((col) => !col.hidden);
  if (!sheet.rows.length) return h("p", { class: "muted xl-empty" }, "This sheet is empty.");
  const colgroup = h("colgroup", null, h("col", { style: { width: "44px" } }), cols.map((col) => h("col", { style: { width: `${col.w}px` } })));
  const head = h("thead", null, h("tr", null, h("th", { class: "xl-corner" }), cols.map((col) => h("th", { scope: "col" }, col.letter))));
  const letters = new Map(sheet.cols.map((col) => [col.n, col.letter]));
  // Places under a merged cell, as "row index:column index" in the grid. The server counts a merge's rs and cs in
  // the rows and columns it shows (hidden ones are left out), so they're counted here the same way.
  const covered = new Set();
  const rows = sheet.rows.map((row, ri) => {
    const byCol = new Map(row.cells.map((cell) => [cell.c, cell]));
    const tds = [];
    cols.forEach((col, ci) => {
      if (covered.has(`${ri}:${ci}`)) return;
      const cell = byCol.get(col.n);
      if (!cell) {
        tds.push(h("td", null));
        return;
      }
      const cls = [cell.num ? "num" : "", cell.b ? "b" : "", cell.i ? "i" : "", cell.u ? "u" : "", cell.wrap ? "wrap" : "", cell.red ? "red" : "", cell.f ? "has-f" : "", cell.nocalc ? "nocalc" : "", ...(cell.bd || "").split("").map((side) => `edge-${side}`)].filter(Boolean).join(" ");
      const style = {};
      if (cell.bg) style.background = cell.bg;
      // A [Red] section of the number format wins over the font's colour, as in Excel.
      if (cell.fg && !cell.red) style.color = cell.fg;
      if (cell.al) style.textAlign = cell.al;
      if (cell.ind) style.paddingLeft = `${6 + cell.ind * 9}px`;
      const cs = Math.max(1, Math.min(cell.cs || 1, cols.length - ci));
      const rs = Math.max(1, cell.rs || 1);
      for (let dr = 0; dr < rs; dr += 1) for (let dc = 0; dc < cs; dc += 1) if (dr || dc) covered.add(`${ri + dr}:${ci + dc}`);
      const ref = `${letters.get(col.n) || ""}${row.n}`;
      tds.push(
        h(
          "td",
          {
            class: cls || null,
            style,
            colspan: cs > 1 ? String(cs) : null,
            rowspan: rs > 1 ? String(rs) : null,
            title: cell.nocalc ? "Saved without its result: the formula is shown" : cell.f ? cell.f : null,
            onclick: () => $fx(formula, ref, cell.f ? "f" : "", cell.f || cell.v),
          },
          cell.v
        )
      );
    });
    return h("tr", null, h("th", { scope: "row" }, String(row.n)), tds);
  });
  const more = [sheet.more_rows ? plural(sheet.more_rows, "more row") : "", sheet.more_cols ? plural(sheet.more_cols, "more column") : ""].filter(Boolean).join(" and ");
  return h(
    "div",
    { class: "xl-scroll" },
    h("table", { class: "xl-grid" }, colgroup, head, h("tbody", null, rows)),
    more ? h("p", { class: "muted small xl-more" }, `${more} not shown here. Download the file, or ask CloseDesk about them.`) : null
  );
}

function docView(blocks) {
  if (!blocks.length) return h("p", { class: "muted" }, "This document has no text to show.");
  return h(
    "div",
    { class: "doc-page" },
    blocks.map((block) => {
      if (block.t === "table") return h("div", { class: "grid-wrap" }, h("table", { class: "grid doc-table" }, h("tbody", null, block.rows.map((row) => h("tr", null, row.map(docCell))))));
      if (block.t === "li") return h("p", { class: "doc-li" }, h("span", { class: "doc-li-mark" }, block.num || "•"), block.text);
      if (/^h[1-4]$/.test(block.t)) return h(block.t, { class: "doc-h" }, block.text);
      return h("p", { class: block.b ? "doc-p b" : "doc-p" }, block.text);
    })
  );
}

/** A PowerPoint deck, slide by slide: its title, its text as paragraphs and bullet points, its tables, how many
    pictures and charts it has (they aren't drawn), and its speaker notes. */
function slidesView(slides) {
  if (!slides.length) return h("p", { class: "muted" }, "This presentation has no slides to show.");
  return h(
    "div",
    { class: "slides" },
    slides.map((slide) =>
      h(
        "section",
        { class: `slide${slide.hidden ? " hidden-slide" : ""}`, "aria-label": `Slide ${slide.n}` },
        h("span", { class: "slide-n" }, `Slide ${slide.n}${slide.hidden ? " · hidden" : ""}`),
        slide.title ? h("h2", { class: "slide-title" }, slide.title) : null,
        slide.blocks.map((block) => {
          if (block.t === "table") return h("div", { class: "grid-wrap" }, h("table", { class: "grid doc-table" }, h("tbody", null, block.rows.map((row) => h("tr", null, row.map((cell) => h("td", null, cell)))))));
          if (block.t === "li") return h("p", { class: "slide-li", style: { marginLeft: `${12 + 18 * (block.level || 0)}px` } }, "• ", block.text);
          return h("p", { class: "slide-p" }, block.text);
        }),
        slide.pictures ? h("p", { class: "slide-pics muted small" }, `${plural(slide.pictures, "picture or chart", "pictures or charts")} on this slide (not drawn here)`) : null,
        slide.notes ? h("details", { class: "slide-notes" }, h("summary", null, "Speaker notes"), h("p", null, slide.notes)) : null,
        !slide.title && !slide.blocks.length && !slide.pictures ? h("p", { class: "muted small" }, "No text on this slide.") : null
      )
    )
  );
}

/** A text, RTF, web page or calendar file as its text (a web page's markup is never rendered). */
function textView(text) {
  if (!(text || "").trim()) return h("p", { class: "muted" }, "This file has no text to show.");
  return h("pre", { class: "text-file" }, text);
}

/** What's in a zip, with sizes. */
function zipView(got) {
  if (!got.files.length) return h("p", { class: "muted" }, "This zip is empty.");
  const size = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : n >= 1024 ? `${Math.round(n / 1024)} KB` : `${n} B`);
  return h(
    "div",
    { class: "grid-wrap" },
    h(
      "table",
      { class: "grid zip-list" },
      h("thead", null, h("tr", null, h("th", { scope: "col" }, "File"), h("th", { scope: "col", class: "num" }, "Size"))),
      h("tbody", null, got.files.map((f) => h("tr", null, h("td", null, f.name, f.encrypted ? h("span", { class: "chip faint" }, "password") : null), h("td", { class: "num" }, size(f.size)))))
    ),
    got.more ? h("p", { class: "muted small" }, `${plural(got.more, "more file")} not listed.`) : null
  );
}

/** A Word table cell: its text, or {v, cs} when it spans more than one column. */
function docCell(cell) {
  if (cell && typeof cell === "object") return h("td", { colspan: cell.cs > 1 ? String(cell.cs) : null }, cell.v);
  return h("td", null, cell);
}

/* ---------- The small preview on an email's attachment ---------- */

/** A small picture of the file (its first page, the picture, or the top of its first sheet), or null. Clicking it
    opens the file's Preview tab. */
export function thumb(emailId, file, href) {
  if (!file.show) return null;
  const box = h("a", { class: `file-thumb ${file.show}`, href, dataset: { nav: "" }, title: `Preview ${file.name}`, "aria-label": `Preview ${file.name}` });
  const fill = (...children) => replace(box, ...children, h("span", { class: "file-thumb-go" }, icon("eye", 13), "Preview"));
  fill(h("span", { class: "file-thumb-wait" }));
  // Drawn once it's on the page; if the email is closed while the file loads, the answer is dropped.
  let watched = false;
  const gone = () => watched && !box.isConnected;
  const load = async () => {
    try {
      if (file.show === "image") {
        const img = h("img", { alt: "" });
        img.src = `${filePath(emailId, file.n)}/view`;
        fill(img);
      } else if (file.show === "pdf" || file.show === "pages") {
        const blob = await getBlob(`${apiFile(emailId, file.n)}/pages/1.png`);
        if (gone()) return;
        const img = h("img", { alt: "" });
        showBlob(img, blob);
        fill(img);
      } else if (file.show === "sheet") {
        const got = await getJSON(`${apiFile(emailId, file.n)}/sheet?rows=8&cols=12`);
        if (gone()) return;
        if (got.note) return fill(h("span", { class: "muted small" }, "No preview"));
        const sheet = (got.sheets || []).find((s) => !s.hidden) || (got.sheets || [])[0];
        if (!sheet) return fill(h("span", { class: "muted small" }, "Empty workbook"));
        fill(
          h(
            "table",
            { class: "file-thumb-grid" },
            h("tbody", null, sheet.rows.slice(0, 8).map((row) => {
              const byCol = new Map(row.cells.map((cell) => [cell.c, cell]));
              return h("tr", null, sheet.cols.filter((col) => !col.hidden).slice(0, 6).map((col) => {
                const cell = byCol.get(col.n);
                return h("td", { class: cell && cell.num ? "num" : cell && cell.b ? "b" : null }, cell ? cell.v : "");
              }));
            }))
          ),
          (got.sheets || []).length > 1 ? h("span", { class: "file-thumb-sheets" }, plural(got.sheets.length, "sheet")) : null
        );
      } else if (file.show === "slides" || file.show === "text" || file.show === "zip") {
        const got = await getJSON(`${apiFile(emailId, file.n)}/sheet`);
        if (gone()) return;
        if (got.note) return fill(h("span", { class: "muted small" }, "No preview"));
        if (got.kind === "slides") {
          const first = got.slides[0];
          fill(h("div", { class: "file-thumb-slide" }, first ? [h("p", { class: "b" }, first.title || `Slide 1`), first.blocks.slice(0, 5).map((block) => h("p", null, block.t === "table" ? "▦ table" : block.text))] : null), h("span", { class: "file-thumb-sheets" }, plural(got.slides.length, "slide")));
        } else if (got.kind === "zip") {
          fill(h("div", { class: "file-thumb-doc" }, got.files.slice(0, 8).map((f) => h("p", null, f.name))), h("span", { class: "file-thumb-sheets" }, plural(got.files.length + (got.more || 0), "file")));
        } else {
          fill(h("div", { class: "file-thumb-doc mono" }, (got.text || "").split("\n").slice(0, 10).map((line) => h("p", null, line || "\u00a0"))));
        }
      } else if (file.show === "doc") {
        const got = await getJSON(`${apiFile(emailId, file.n)}/sheet`);
        if (gone()) return;
        if (got.note) return fill(h("span", { class: "muted small" }, "No preview"));
        fill(h("div", { class: "file-thumb-doc" }, (got.blocks || []).slice(0, 8).map((block) => h("p", { class: /^h/.test(block.t) ? "b" : null }, block.t === "table" ? "▦ table" : block.text))));
      }
    } catch (error) {
      if (!gone()) fill(h("span", { class: "muted small" }, "No preview"));
    }
  };
  // Drawn when it scrolls into view, so an email with many files doesn't draw them all at once.
  if ("IntersectionObserver" in window) {
    const seen = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        seen.disconnect();
        load();
      }
    });
    watched = true;
    queueMicrotask(() => seen.observe(box));
  } else load();
  return box;
}
