/* An answer from Ask CloseDesk as HTML: the text is escaped first, then a little markdown (bold, italics,
   code, lists, pipe tables) and the [n] citations are added. A citation opens the email it cites in the
   workspace, or the file the sentence is about (PDFs at the cited page, other files in the text view at the
   cited page, slide, sheet or cell), the same way the classic chat links them. */

import { escapeHtml } from "./dom.js";
import { filePath } from "./api.js";

const PAGE_AT = /\b(?:page|p\.)\s?(\d{1,4})\b/i;
const SLIDE_AT = /\bslide\s?(\d{1,3})\b/i;
const SHEET_AT = /\bsheet\s+["“']([^"”'\n]+)["”']/i;
const CELL_AT = /(?:(?:'([^'\n]+)'|"([^"\n]+)"|([A-Za-z][\w]*))!)?\b([A-Z]{1,3}\d{1,6})\b/g;
const CELL_WORD = /\bcells?\s+$/i;
const SHEET_FILE = /\.(xlsx|xlsm|xls|csv|tsv)$/i;
const ABOUT_A_FILE = /\b(attach\w*|file|pdf|document|spreadsheet|workbook|sheet|tab|deck|slide|page|cell|row|table)\b/i;
const SENTENCE_END = /(?<!\b(?:p|pp|no|e\.g|i\.e|vs))[.!?](?=\s)|\n/gi;
const LOCATION = new RegExp([PAGE_AT, SLIDE_AT, SHEET_AT, /\bcells?\s+[A-Z]{1,3}\d/, /!\$?[A-Z]{1,3}\$?\d/].map((re) => re.source).join("|"), "i");

export const mailHref = (source) =>
  source.chat && (source.files || []).length ? filePath(source.id, 1) : `/app/mail/${encodeURIComponent(source.id)}`;

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
  return found.find((m) => CELL_WORD.test(context.slice(0, m.index))) || found.find((m) => m[1] || m[2] || m[3]) || found[found.length - 1] || null;
}

export function fileHref(source, file, context) {
  const base = filePath(source.id, file.n);
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

function inline(sentence, context, sources, names, whole) {
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
    const external = source.chat ? ' target="_blank" rel="noopener"' : " data-nav";
    return `<a class="cite" href="${escapeHtml(mailHref(source))}"${external} title="${escapeHtml(source.subject)}">[${n}]</a>`;
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
  const unique = new Map([...byName].filter(([key]) => counts.get(key) === 1 && key.length >= 5).map(([key, hit]) => [escapeHtml(key), hit]));
  if (!unique.size) return { byName: unique, pattern: null };
  const escaped = [...unique.keys()].sort((a, b) => b.length - a.length).map((key) => key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  return { byName: unique, pattern: new RegExp(`(?<![\\w/])(?:${escaped.join("|")})(?![\\w])`, "gi") };
}

/** One line of the answer: its sentences, each with the citations it carries. */
function formatLine(line, previous, sources, names, whole) {
  const parts = sentences(line);
  return parts
    .map((sentence, index) => {
      // "... is blank. [1]": a citation after the full stop belongs to the sentence before.
      const lead = sentence.split(/\[\d{1,2}\]/)[0];
      const before = index ? parts[index - 1] : previous;
      const context = !lead.trim() && before ? before + sentence : sentence;
      return inline(sentence, context, sources, names, whole);
    })
    .join("");
}

const BULLET = /^\s*(?:[-*•])\s+(.*)$/;
const NUMBERED = /^\s*(\d{1,3})[.)]\s+(.*)$/;
const PIPE_ROW = /^\s*\|.*\|\s*$/;
const PIPE_RULE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
const HEADING = /^\s*#{1,4}\s+(.*)$/;

export function formatAnswer(text, sources = []) {
  text = String(text || "");
  const names = fileNames(sources);
  const lines = text.split("\n");
  const out = [];
  let list = null;
  let table = null;
  let previous = "";
  const fmt = (line) => {
    const html = formatLine(line, previous, sources, names, text);
    previous = line;
    return html;
  };
  const closeList = () => {
    if (list) out.push(`<${list.tag}>${list.items.map((item) => `<li>${item}</li>`).join("")}</${list.tag}>`);
    list = null;
  };
  const closeTable = () => {
    if (table) {
      const [head, ...body] = table;
      const cells = (row, tag) => row.map((cell) => `<${tag}>${cell}</${tag}>`).join("");
      out.push(
        `<div class="md-table"><table><thead><tr>${cells(head, "th")}</tr></thead><tbody>${body
          .map((row) => `<tr>${cells(row, "td")}</tr>`)
          .join("")}</tbody></table></div>`
      );
    }
    table = null;
  };
  for (const line of lines) {
    if (PIPE_ROW.test(line)) {
      closeList();
      if (PIPE_RULE.test(line)) continue;
      const cells = line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => fmt(cell.trim()));
      (table = table || []).push(cells);
      continue;
    }
    closeTable();
    const bullet = line.match(BULLET);
    const numbered = !bullet && line.match(NUMBERED);
    if (bullet || numbered) {
      const tag = bullet ? "ul" : "ol";
      if (!list || list.tag !== tag) {
        closeList();
        list = { tag, items: [] };
      }
      list.items.push(fmt(bullet ? bullet[1] : numbered[2]));
      continue;
    }
    closeList();
    const heading = line.match(HEADING);
    if (heading) out.push(`<p class="md-h">${fmt(heading[1])}</p>`);
    else if (line.trim()) out.push(`<p>${fmt(line)}</p>`);
  }
  closeList();
  closeTable();
  return out.join("");
}
