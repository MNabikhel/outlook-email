"""A page read a second way: the local model looks at the page itself, beside the reading CloseDesk already has.

OCR reads a scanned page's words but loses its table: the title runs into the column names, a row splits in two.
A model that can see reads the page whole. Its reading is kept beside the first one and the two are compared figure
by figure: a figure both readings have is confirmed; where they differ, both are shown, and the totals check says
which reading adds up. The page the chat and the table lookup read is the reading that holds up best, with the
differences written under it, and the answer check flags a figure only the model read.

Pages read this way: a scanned PDF's pages, a picture, and a PDF page whose table doesn't add up. Reading a page
takes a laptop without a graphics card minutes, so how long one takes on this computer is kept, and CloseDesk asks
before a long read (``Settings.vision_mode``).
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import logging
import math
import re
import statistics
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING

from controller_inbox import tables
from controller_inbox.documents import MAX_OCR_PAGES, MAX_PDF_PAGES
from controller_inbox.local_llm import EmptyReply, check_model, strip_thinking, stream_text

if TYPE_CHECKING:
    from controller_inbox.config import Settings
    from controller_inbox.models import AttachmentRecord, EmailRecord
    from controller_inbox.store import Store

log = logging.getLogger(__name__)

PROMPT = (
    "Transcribe this document page exactly. Return all the text as it appears, top to bottom. "
    "Write every table as a markdown table with its column headings, one table row per printed row, and an empty "
    "cell where the page leaves a cell blank. Copy every number exactly as printed, with its commas, decimals, "
    "currency signs, minus signs and parentheses. Do not use bold or other formatting. Do not calculate, summarize, "
    "or add anything that is not on the page. If something can't be read, write [unreadable]."
)
# 100 DPI reads a printed page well; the long side is capped so a large sheet doesn't cost thousands of image tokens.
DPI = 100
MAX_SIDE = 1600
MAX_TOKENS = 6000
# A laptop without a graphics card can look at a page for minutes before writing the first word.
WAIT_SECONDS = 900.0
# In "auto", pages are read while the question waits when they take no longer than this all told; longer reads are
# offered with the time they'd take.
QUICK_SECONDS = 60.0
# One request reads at most this many pages, so a long scan isn't a job of hours by accident.
MAX_PAGES = 20
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".bmp", ".webp"}
MODES = ("auto", "ask", "off")
NOTE = "[Read two ways"
MODEL_NAME = "the vision model"
# A page the first reading found nothing on (a scan without OCR): the model's reading is the only one.
ALONE = "found no text on this page"
# How many differing or model-only figures a page's note lists; the answer check flags each one listed.
LISTED = 40


# Whether it can be used ------------------------------------------------------------------------


MODE_KEY = "vision_mode"


def apply_saved_mode(settings: Settings, store: Store) -> None:
    """The choice made in Setup, kept across restarts."""
    saved = store.get_state(MODE_KEY)
    if saved in MODES:
        settings.vision_mode = saved


def save_mode(settings: Settings, store: Store, mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"Vision reading is one of {', '.join(MODES)}.")
    store.set_state(MODE_KEY, mode)
    settings.vision_mode = mode


def available(settings: Settings) -> bool:
    """A model that can see is loaded and vision reading isn't turned off."""
    if settings.vision_mode == "off":
        return False
    status = check_model(settings)
    return status.active and status.vision


def can_render() -> bool:
    """The page renderer (pypdfium2) and Pillow are installed."""
    return all(importlib.util.find_spec(name) is not None for name in ("PIL", "pypdfium2"))


def readable_file(filename: str) -> bool:
    suffix = Path(filename or "").suffix.lower()
    return suffix == ".pdf" or suffix in IMAGE_SUFFIXES


# Which pages ----------------------------------------------------------------------------------


def wanted_pages(data: bytes, filename: str, text: str) -> list[int]:
    """The pages worth a second reading: every page of a picture, a PDF's scanned pages (no text layer), and PDF
    pages whose table's printed totals don't add up as CloseDesk read them."""
    suffix = Path(filename or "").suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return [1]
    if suffix != ".pdf":
        return []
    pages = set(scanned_pages(data))
    pages |= set(doubtful_pages(text))
    return sorted(pages)


def scanned_pages(data: bytes) -> list[int]:
    """The PDF's pages with no text layer to speak of: pictures of pages."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return []
    found = []
    try:
        pdf = pdfium.PdfDocument(data)
    except Exception:
        return []
    try:
        for index in range(min(len(pdf), MAX_PDF_PAGES)):
            page = pdf[index]
            try:
                textpage = page.get_textpage()
                words = len(textpage.get_text_range().split())
                textpage.close()
            except Exception:
                words = 0
            finally:
                page.close()
            if words < 5:
                found.append(index + 1)
            if len(found) >= MAX_OCR_PAGES:
                break
    finally:
        pdf.close()
    return found


def doubtful_pages(text: str) -> list[int]:
    """Pages whose tables, as read, have a printed total that doesn't add up the rows above it."""
    from controller_inbox.table_lookup import tables_in, verify

    found = []
    for number, body in page_bodies(text):
        if body.startswith(NOTE):
            continue
        if any(verify(table).mismatched for table in tables_in(f"[page {number}]\n{body}")):
            found.append(number)
    return found


_PAGE_MARK = re.compile(r"^\[page (\d+)\]\s*$", re.M)
_MARK = re.compile(r"^\[(?:page \d+|sheet \"[^\"\n]*\"[^\]\n]*|slide \d+|part \d+)\]\s*$", re.M)


def page_bodies(text: str) -> list[tuple[int, str]]:
    """Each ``[page N]`` block's number and text. Text without page marks (a picture) is page 1."""
    text = text or ""
    marks = list(_MARK.finditer(text))
    if not marks:
        body = text.strip()
        return [(1, body)] if body else []
    out = []
    for i, mark in enumerate(marks):
        page = _PAGE_MARK.match(mark.group(0))
        if not page:
            continue
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out.append((int(page.group(1)), text[mark.end():end].strip()))
    return out


# Looking at a page ---------------------------------------------------------------------------


def render(data: bytes, filename: str, page: int) -> bytes:
    """The page as a PNG at about 100 DPI, its long side at most ``MAX_SIDE`` pixels."""
    from PIL import Image, ImageOps

    suffix = Path(filename or "").suffix.lower()
    if suffix == ".pdf":
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(data)
        try:
            if not 1 <= page <= len(pdf):
                raise ValueError(f"the PDF has no page {page}")
            sheet = pdf[page - 1]
            try:
                width, height = sheet.get_size()
                scale = min(DPI / 72, MAX_SIDE / max(width, height, 1))
                image = sheet.render(scale=scale).to_pil()
            finally:
                sheet.close()
        finally:
            pdf.close()
    else:
        image = Image.open(io.BytesIO(data))
        if getattr(image, "n_frames", 1) > 1:
            image.seek(0)
        image = ImageOps.exif_transpose(image)
        image.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    image.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()


def transcribe(settings: Settings, png: bytes, *, on_piece: Callable[[int], None] | None = None) -> str:
    """The model's reading of the page: its text, with tables in markdown. Raises ``EmptyReply`` when it wrote
    nothing and ``httpx.HTTPError`` when the server failed."""
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}},
                {"type": "text", "text": PROMPT},
            ],
        }
    ]
    written = []
    for piece in stream_text(settings, messages, max_tokens=MAX_TOKENS, wait=WAIT_SECONDS, temperature=0.0):
        written.append(piece)
        if on_piece:
            on_piece(sum(len(p) for p in written))
    text = strip_thinking("".join(written)).strip()
    if not text:
        raise EmptyReply("the model wrote nothing for the page")
    return text


# The model's markdown as a page CloseDesk reads ------------------------------------------------


_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_RULE_ROW = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")
_FORMATTING = re.compile(r"\*\*|__|`")


def page_text(markdown: str) -> str:
    """The model's markdown written the way CloseDesk writes a page it read: each table row by row with every cell
    named by its column (``Label: value``) and section rows as ``Group:``, the rest as notes, so the table lookup,
    the totals check and the chat read it like any other page."""
    lines = strip_thinking(markdown or "").splitlines()
    out: list[str] = []
    notes: list[str] = []

    def flush() -> None:
        kept = [line for line in notes if line.strip()]
        if kept:
            out.append("[notes]\n" + "\n".join(kept))
        notes.clear()

    index = 0
    while index < len(lines):
        line = lines[index]
        if _TABLE_ROW.match(line) and index + 1 < len(lines) and _RULE_ROW.match(lines[index + 1]):
            flush()
            header = _cells(line)
            index += 2
            rows = []
            while index < len(lines) and _TABLE_ROW.match(lines[index]):
                if not _RULE_ROW.match(lines[index]):
                    rows.append(_cells(lines[index]))
                index += 1
            out.append("[table]\n" + "\n".join(_table_lines(header, rows)))
            continue
        clean = _FORMATTING.sub("", line).strip()
        if clean.startswith("#"):
            flush()
            out.append("[heading]\n" + clean.lstrip("#").strip())
        elif _TABLE_ROW.match(line):
            # A table without its rule line: its cells, kept apart.
            notes.append(" | ".join(cell for cell in _cells(line) if cell))
        else:
            notes.append(clean)
        index += 1
    flush()
    return "\n\n".join(out)


def _cells(line: str) -> list[str]:
    inner = line.strip()
    inner = inner[1:] if inner.startswith("|") else inner
    inner = inner[:-1] if inner.endswith("|") else inner
    return [_FORMATTING.sub("", cell).strip() for cell in re.split(r"(?<!\\)\|", inner)]


def _table_lines(header: list[str], rows: list[list[str]]) -> list[str]:
    from controller_inbox.table_lookup import ROW_LABEL

    width = max([len(header), *(len(row) for row in rows)]) if rows else len(header)
    labels = [*header, *[""] * (width - len(header))]
    # Every column is named, the row names too ("Line: Less: Allowance for Doubtful Accounts" stays one cell).
    labels = _unique([label or (ROW_LABEL if index == 0 else f"Column {index + 1}") for index, label in enumerate(labels)])
    lines = [" | ".join(labels)]
    group = ""
    for row in rows:
        row = [*row, *[""] * (width - len(row))][:width]
        if [cell.casefold() for cell in row] == [label.casefold() for label in labels]:
            continue  # the column headings printed again
        filled = [cell for cell in row if cell]
        if not filled:
            continue
        if len(filled) == 1 and row[0] and width > 1 and not tables.is_value(row[0]):
            # A row naming the section under it ("Current Assets"): the rows below are in it.
            lines.append(f"Group: {row[0]}")
            group = row[0].rstrip(": ").strip()
            continue
        line = tables.labelled_row(labels, row)
        lines.append(f"{group} | {line}" if group else line)
    return lines


def _unique(labels: list[str]) -> list[str]:
    seen: Counter[str] = Counter()
    out = []
    for label in labels:
        seen[label] += 1
        out.append(label if not label or seen[label] == 1 else f"{label} ({seen[label]})")
    return out


# Two readings compared ------------------------------------------------------------------------


_DATE = re.compile(r"\b\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}\b")
_FIGURE = re.compile(r"(?<![\w.])([-−(]?)\s?[$€£]?\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(\)?)(%?)(?![\w])")


def figures(text: str) -> dict[Decimal, list[str]]:
    """Each amount in the text (signed: "(2,750)" and "-2,750" are the same figure) and how it was written. Short
    whole numbers (a page or row number) and years are left out, and so are dates."""
    found: dict[Decimal, list[str]] = {}
    for match in _FIGURE.finditer(_DATE.sub(" ", text or "")):
        sign, whole, decimals, close, percent = match.groups()
        digits = whole.replace(",", "")
        if not decimals and not percent and "," not in whole and (len(digits) < 3 or (len(digits) == 4 and 1900 <= int(digits) <= 2100)):
            continue
        try:
            value = Decimal(digits + (decimals or ""))
        except InvalidOperation:
            continue
        if sign in {"-", "−"} or (sign == "(" and close == ")"):
            value = -value
        found.setdefault(value, []).append(match.group(0).strip())
    return found


@dataclass
class Comparison:
    """How the model's reading of a page compares with the first one (OCR's, or the PDF's own text)."""

    figures: int = 0
    confirmed: int = 0
    differ: list[tuple[str, str]] = field(default_factory=list)
    only_model: list[str] = field(default_factory=list)
    only_first: list[str] = field(default_factory=list)
    model_totals: tuple[int, int] = (0, 0)
    first_totals: tuple[int, int] = (0, 0)
    choice: str = "first"

    def to_dict(self) -> dict:
        return {
            "figures": self.figures, "confirmed": self.confirmed, "differ": self.differ, "only_model": self.only_model,
            "only_first": self.only_first, "model_totals": list(self.model_totals), "first_totals": list(self.first_totals),
            "choice": self.choice,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Comparison:
        return cls(
            figures=int(data.get("figures", 0)), confirmed=int(data.get("confirmed", 0)),
            differ=[tuple(pair) for pair in data.get("differ", [])], only_model=list(data.get("only_model", [])),
            only_first=list(data.get("only_first", [])), model_totals=tuple(data.get("model_totals", (0, 0))),
            first_totals=tuple(data.get("first_totals", (0, 0))), choice=str(data.get("choice", "first")),
        )


def compare(first: str, model_page: str) -> Comparison:
    """Figure by figure, and by each reading's printed totals: which reading the page is shown as."""
    mine, theirs = figures(model_page), figures(first)
    model_count = Counter({value: len(shown) for value, shown in mine.items()})
    first_count = Counter({value: len(shown) for value, shown in theirs.items()})
    both = model_count & first_count
    only_model = model_count - first_count
    only_first = first_count - model_count
    comparison = Comparison(figures=sum(model_count.values()), confirmed=sum(both.values()))
    pairs, left_model, left_first = _pairs(only_model, only_first)
    comparison.differ = [(mine[a][0], theirs[b][0]) for a, b in pairs]
    comparison.only_model = [mine[value][0] for value in left_model]
    comparison.only_first = [theirs[value][0] for value in left_first]
    comparison.model_totals = _totals(model_page)
    comparison.first_totals = _totals(first)
    comparison.choice = _choose(comparison) if said_something(first) else ("model" if model_page.strip() else "first")
    return comparison


def said_something(first: str) -> bool:
    """Whether the first reading has the page's text at all (a scan without OCR has "(no text on this page)")."""
    text = re.sub(r"^\[[^\]\n]*\]\s*$", " ", first or "", flags=re.M).replace("(no text on this page)", " ")
    return len(text.split()) >= 3


def _totals(text: str) -> tuple[int, int]:
    """(totals that add up, totals that don't) across the page's tables."""
    from controller_inbox.table_lookup import tables_in, verify

    matched = mismatched = 0
    for table in tables_in("[page 1]\n" + text):
        verdict = verify(table)
        matched += verdict.matched
        mismatched += len(verdict.mismatched)
    return matched, mismatched


def _choose(comparison: Comparison) -> str:
    """The model's reading when its totals hold up at least as well as the first reading's and most of its figures
    are the first reading's too (a reading that shares few figures is of something else, or made up)."""
    def score(totals: tuple[int, int]) -> int:
        return totals[0] - 2 * totals[1]

    share = comparison.confirmed / comparison.figures if comparison.figures else 0.0
    if share >= 0.5 and score(comparison.model_totals) >= score(comparison.first_totals):
        return "model"
    return "first"


def _pairs(only_model: Counter, only_first: Counter) -> tuple[list[tuple[Decimal, Decimal]], list[Decimal], list[Decimal]]:
    """Figures the two readings wrote differently: a model figure and a first-reading figure of the same length a
    digit or two apart are one figure read two ways ("1,240.00" / "1,246.00")."""
    model_left = [value for value, count in only_model.items() for _ in range(count)]
    first_left = [value for value, count in only_first.items() for _ in range(count)]
    pairs = []
    for value in list(model_left):
        mine = _digits(value)
        best = None
        for other in first_left:
            theirs = _digits(other)
            if len(theirs) == len(mine) and sum(a != b for a, b in zip(mine, theirs)) <= 2:
                best = other
                break
            if abs(other) == abs(value):  # the sign is all that differs
                best = other
                break
        if best is not None:
            pairs.append((value, best))
            model_left.remove(value)
            first_left.remove(best)
    return pairs, model_left, first_left


def _digits(value: Decimal) -> str:
    return re.sub(r"\D", "", format(abs(value), "f"))


# The page as CloseDesk shows it ---------------------------------------------------------------


def merged_page(first: str, model_markdown: str, comparison: Comparison, *, first_name: str, model: str) -> str:
    """The page as the chat and the table lookup read it: the reading that holds up best, with what the other one
    read differently written under it."""
    model_page = page_text(model_markdown)
    shown = model_page if comparison.choice == "model" else first
    who = f"{MODEL_NAME} ({model})" if model else MODEL_NAME
    if comparison.choice == "model" and not said_something(first):
        return (
            f"{NOTE}, by {first_name} and by {who}: {first_name} {ALONE}, so it is shown as {MODEL_NAME} read it and "
            f"none of its figures is confirmed: check them against the file.]\n{model_page}"
        )
    same = f"{comparison.confirmed} of {comparison.figures} figures read the same" if comparison.figures else "no figures to compare"
    matched, mismatched = comparison.model_totals if comparison.choice == "model" else comparison.first_totals
    totals = ""
    if matched or mismatched:
        totals = f"; its printed totals {'add up' if not mismatched else f'add up {matched} of {matched + mismatched} times'}"
    shown_name = MODEL_NAME + "'s" if comparison.choice == "model" else first_name + "'s"
    lines = [f"{NOTE}, by {first_name} and by {who}: {same}. Shown: {shown_name} reading{totals}.]", shown]
    if comparison.differ:
        if comparison.choice == "model":
            listed = "; ".join(f"{mine} / {theirs}" for mine, theirs in comparison.differ[:LISTED])
            lines.append(f"[Where the two readings differ ({MODEL_NAME} / {first_name}): {listed}]")
        else:
            listed = "; ".join(f"{theirs} / {mine}" for mine, theirs in comparison.differ[:LISTED])
            lines.append(f"[Where the two readings differ ({first_name} / {MODEL_NAME}): {listed}]")
    if comparison.choice == "model" and comparison.only_model:
        lines.append(f"[Read only by {MODEL_NAME}: {'; '.join(comparison.only_model[:LISTED])}]")
    return "\n".join(line for line in lines if line)


_ONLY_MODEL = re.compile(r"^\[Read only by the vision model: (.+)\]$", re.M)
_DIFFER = re.compile(r"^\[Where the two readings differ \(the vision model / ([^)]+)\): (.+)\]$", re.M)


def unconfirmed(text: str) -> dict[Decimal, str]:
    """Figures on shown pages that only the vision model read, each with what the other reading had ("" when it
    had none): an answer that uses one is told to check it against the page."""
    out: dict[Decimal, str] = {}
    for _number, body in page_bodies(text or ""):
        note, _, rest = body.partition("\n")
        if note.startswith(NOTE) and ALONE in note:
            for value in figures(rest):
                out.setdefault(value, "")
    for match in _ONLY_MODEL.finditer(text or ""):
        for shown in match.group(1).split("; "):
            for value in figures(shown):
                out.setdefault(value, "")
    for match in _DIFFER.finditer(text or ""):
        for pair in match.group(2).split("; "):
            mine, _, theirs = pair.partition(" / ")
            for value in figures(mine):
                out.setdefault(value, f"{match.group(1)} read {theirs.strip()}")
    return out


def shown_text(text: str, rows: dict[int, dict]) -> str:
    """The attachment's text with each page the vision model read shown as ``merged_page``. ``rows``: page -> the
    stored reading (``Store.page_readings``).

    The stored text stays the first reading; this runs as the attachment is loaded, so whatever saves the email
    again can't lose the model's reading. A page already shown this way (saved back by something that loaded it) is
    rebuilt from the stored first reading; a page whose first reading changed since (a reader update) is compared
    again."""
    if not rows:
        return text
    text = text or ""
    marks = list(_MARK.finditer(text))
    if not marks:
        row = rows.get(1)
        if not row:
            return text
        body = text.strip()
        return _page_for(row["first"] if body.startswith(NOTE) else body, row)
    pieces = [text[: marks[0].start()]]
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        page = _PAGE_MARK.match(mark.group(0))
        row = rows.get(int(page.group(1))) if page else None
        if row is None:
            pieces.append(text[mark.start():end])
            continue
        body = text[mark.end():end].strip()
        first = row["first"] if body.startswith(NOTE) else body
        tail = "\n\n" if i + 1 < len(marks) else ""
        pieces.append(f"{mark.group(0).strip()}\n{_page_for(first, row)}{tail}")
    return "".join(pieces)


def _page_for(first: str, row: dict) -> str:
    try:
        saved = json.loads(row.get("comparison") or "{}")
    except ValueError:
        saved = {}
    comparison = Comparison.from_dict(saved) if row.get("first") == first and saved else compare(first, page_text(row["model_text"]))
    return merged_page(first, row["model_text"], comparison, first_name=saved.get("first_name") or "OCR", model=row.get("model") or "")


# Reading pages and keeping them ---------------------------------------------------------------


@dataclass
class Result:
    pages: int = 0
    seconds: float = 0.0
    shown_model: int = 0
    failed: list[str] = field(default_factory=list)


def first_name(text: str, page: int, scanned: set[int], filename: str) -> str:
    """What the page's first reading came from, as the note names it."""
    if Path(filename or "").suffix.lower() in IMAGE_SUFFIXES or page in scanned:
        return "OCR"
    return "the PDF's text"


def read_pages(
    store: Store,
    settings: Settings,
    email: EmailRecord,
    att: AttachmentRecord,
    data: bytes,
    pages: list[int],
    *,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> Result:
    """Read ``pages`` of the attachment with the vision model, compare each with its first reading and keep both.
    The attachment's text shows each page as the reading that holds up best from then on (``shown_text``)."""
    model = check_model(settings).model or settings.llm_model
    scanned = set(scanned_pages(data)) if Path(att.filename).suffix.lower() == ".pdf" else set()
    result = Result()
    bodies = dict(page_bodies(att.extracted_text or ""))
    stored = store.page_readings(att.id, att.sha256)
    todo = pages[:MAX_PAGES]
    for index, page in enumerate(todo, start=1):
        current = bodies.get(page, "")
        first = stored[page]["first"] if page in stored and current.startswith(NOTE) else current
        if first.startswith(NOTE):
            continue
        if on_progress:
            on_progress(index - 1, len(todo), f"{att.filename}, page {page}")
        started = time.monotonic()
        try:
            png = render(data, att.filename, page)
            markdown = transcribe(settings, png)
        except Exception as exc:  # one page failing doesn't lose the others
            log.warning("Vision reading of %s page %s failed: %s", att.filename, page, exc)
            result.failed.append(f"page {page}: {str(exc)[:160]}")
            continue
        seconds = time.monotonic() - started
        comparison = compare(first, page_text(markdown))
        store.save_page_reading(
            att.id, page, first=first, model_text=markdown, model=model, seconds=seconds, sha256=att.sha256,
            comparison=json.dumps({**comparison.to_dict(), "first_name": first_name(first, page, scanned, att.filename)}),
        )
        result.pages += 1
        result.seconds += seconds
        result.shown_model += comparison.choice == "model"
        if on_progress:
            on_progress(index, len(todo), f"{att.filename}, page {page}")
    return result


def unread_pages(store: Store, att: AttachmentRecord, data: bytes, *, doubtful: bool = True) -> list[int]:
    """The pages worth a second reading that haven't had one. ``doubtful``: also text pages whose totals don't add
    up (finding them reads every table, so the overnight run looks at scans and pictures only)."""
    done = set(store.page_readings(att.id, att.sha256))
    if doubtful:
        wanted = wanted_pages(data, att.filename, att.extracted_text or "")
    elif Path(att.filename or "").suffix.lower() in IMAGE_SUFFIXES:
        wanted = [1]
    else:
        wanted = scanned_pages(data) if Path(att.filename or "").suffix.lower() == ".pdf" else []
    return [page for page in wanted if page not in done]


def looks_scanned(att: AttachmentRecord) -> bool:
    head = (att.extracted_text or "")[:400]
    return Path(att.filename or "").suffix.lower() in IMAGE_SUFFIXES or "read with OCR" in head or head.startswith("[This PDF looks scanned")


def files_to_read(
    store: Store, settings: Settings, email: EmailRecord, *, doubtful: bool = True
) -> list[tuple[int, AttachmentRecord, bytes, list[int]]]:
    """(position on the email from 1, attachment, its bytes, pages to read) for each file with pages waiting. Files
    of an email flagged as payment fraud are never sent to the model, and only files CloseDesk kept can be read."""
    from controller_inbox import agent, fraud

    if fraud.attachments_locked(email):
        return []
    out = []
    for position, att in enumerate(email.attachments, start=1):
        if not readable_file(att.filename) or (not doubtful and not looks_scanned(att)):
            continue
        path = agent.original_file(settings, email, att)
        if path is None:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        pages = unread_pages(store, att, data, doubtful=doubtful)
        if pages:
            out.append((position, att, data, pages))
    return out


# One run may read at most this many pages, however fast they go.
RUN_PAGES = 60
# Before this computer's speed is known, a page is taken to need this long (a laptop without a graphics card).
UNKNOWN_SECONDS = 300.0


def read_waiting(
    store: Store, settings: Settings, *, minutes: float | None = None, on_progress: Callable[[int, int, str], None] | None = None
) -> Result:
    """In "auto": the scanned pages and pictures still waiting for a second reading, newest mail first, for as long
    as the run may spend (``minutes``; default ``Settings.vision_minutes_per_run``). A page is only started when it
    is expected to fit, at this computer's speed (``UNKNOWN_SECONDS`` before the first page shows it)."""
    total = Result()
    if settings.vision_mode != "auto" or not can_render() or not available(settings):
        return total
    budget = 60.0 * (settings.vision_minutes_per_run if minutes is None else minutes)
    started = time.monotonic()
    failures_in_a_row = 0
    for email in store.list_emails(limit=400):
        for _position, att, data, pages in files_to_read(store, settings, email, doubtful=False):
            for page in pages:
                per_page = seconds_per_page(store, settings)
                expected = UNKNOWN_SECONDS if per_page is None else per_page
                if time.monotonic() - started + expected > budget:
                    return total
                if total.pages + len(total.failed) >= RUN_PAGES or failures_in_a_row >= 2:
                    return total
                if on_progress:
                    on_progress(total.pages, 0, f"{att.filename}, page {page}")
                one = read_pages(store, settings, email, att, data, [page])
                total.pages += one.pages
                total.seconds += one.seconds
                total.shown_model += one.shown_model
                total.failed += one.failed
                failures_in_a_row = failures_in_a_row + 1 if one.failed else 0
    return total


# How long it takes on this computer -----------------------------------------------------------


def seconds_per_page(store: Store, settings: Settings) -> float | None:
    """The typical time this computer's model has taken to read a page, or None before the first one."""
    model = check_model(settings).model or settings.llm_model
    recent = store.vision_seconds(model, limit=12)
    return statistics.median(recent) if recent else None


def estimate(store: Store, settings: Settings, pages: int) -> float | None:
    per_page = seconds_per_page(store, settings)
    return per_page * pages if per_page is not None else None


def duration(seconds: float) -> str:
    """'about 40 seconds', 'about 3 minutes', 'about 1 hour 20 minutes'."""
    if seconds < 90:
        return f"about {max(10, math.ceil(seconds / 10) * 10)} seconds"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"about {minutes} minutes"
    hours, rest = divmod(minutes, 60)
    return f"about {hours} hour{'s' if hours > 1 else ''}" + (f" {rest} minutes" if rest >= 5 else "")


def offer_text(pages: int, seconds: float | None) -> str:
    """What the page says before a read the user starts."""
    what = f"{pages} page{'s' if pages != 1 else ''}"
    if seconds is None:
        return f"Read {what} with the vision model as well. The first page shows how long this computer takes; a laptop without a graphics card can take a few minutes a page."
    return f"Read {what} with the vision model as well: {duration(seconds)} on this computer."


def result_text(result: Result) -> str:
    """What a finished read says."""
    if not result.pages:
        return "No page could be read: " + "; ".join(result.failed[:3]) if result.failed else "No page needed reading."
    each = f" ({duration(result.seconds / result.pages)} a page)" if result.pages else ""
    text = f"Read {result.pages} page{'s' if result.pages != 1 else ''} with the vision model{each}."
    if result.failed:
        text += f" {len(result.failed)} couldn't be read: {'; '.join(result.failed[:2])}."
    return text + " Ask again to use what it read."


# The file page: both readings side by side ----------------------------------------------------


def side_by_side(rows: dict[int, dict]) -> list[dict]:
    """For the file page: each read page's first reading and the model's, with the figures they differ on marked."""
    from markupsafe import Markup, escape

    out = []
    for page, row in sorted(rows.items()):
        try:
            saved = json.loads(row.get("comparison") or "{}")
        except ValueError:
            saved = {}
        comparison = Comparison.from_dict(saved)
        mine = {a for a, _b in comparison.differ} | set(comparison.only_model)
        theirs = {b for _a, b in comparison.differ} | set(comparison.only_first)
        out.append({
            "page": page,
            "first_name": saved.get("first_name") or "OCR",
            "model": row.get("model") or "",
            "seconds": float(row.get("seconds") or 0),
            "comparison": comparison,
            "first_html": Markup(f"<pre class=\"body\">{_marked(str(escape(row.get('first') or '')), theirs)}</pre>"),
            "model_html": Markup(_markdown_html(row.get("model_text") or "", mine)),
        })
    return out


def _marked(html: str, shown: set[str]) -> str:
    """Escaped text with each of ``shown`` (as written in it) marked."""
    from markupsafe import escape

    for item in sorted(shown, key=len, reverse=True):
        safe = str(escape(item))
        if safe:
            html = re.sub(rf"(?<![\w.,]){re.escape(safe)}(?![\w])", f"<mark>{safe}</mark>", html)
    return html


def _markdown_html(markdown: str, shown: set[str]) -> str:
    """The model's markdown as HTML: tables as tables, the rest as paragraphs, every bit of text escaped."""
    from markupsafe import escape

    def cell(text: str) -> str:
        return _marked(str(escape(text)), shown)

    out: list[str] = []
    for block in markdown_blocks(markdown):
        if block["kind"] == "table":
            head = "".join(f"<th>{cell(text)}</th>" for text in block["header"])
            rows = "".join("<tr>" + "".join(f"<td>{cell(text)}</td>" for text in row) + "</tr>" for row in block["rows"])
            out.append(f'<table class="vision-table"><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>')
        else:
            out.append(f"<p>{cell(block['text'])}</p>")
    return "".join(out)


# What the pages offer, and starting a read -----------------------------------------------------


def offer(store: Store, settings: Settings, email: EmailRecord, att: AttachmentRecord) -> dict:
    """What the file's page and the chat show about reading the file with the vision model as well: the pages that
    would be read and how long that takes on this computer, or why it can't be done."""
    from controller_inbox import agent, fraud

    reason = ""
    pages: list[int] = []
    if not readable_file(att.filename):
        reason = "Only PDFs and pictures can be read with the vision model."
    elif fraud.attachments_locked(email):
        reason = "This email may be payment fraud, so its files aren't given to the model."
    elif settings.vision_mode == "off":
        reason = "Reading scans with the vision model is turned off in Setup."
    elif not can_render():
        reason = "The page renderer isn't installed. Double-click CloseDesk once (or run pip install -e .) to add it."
    elif not available(settings):
        reason = "The model loaded in LM Studio can't look at pictures. Load one that can (Qwen3.5, Gemma 3) to read scans both ways."
    else:
        path = agent.original_file(settings, email, att)
        if path is None:
            reason = "The original file wasn't kept, so its pages can't be looked at."
        else:
            pages = unread_pages(store, att, path.read_bytes())
    seconds = estimate(store, settings, len(pages)) if pages else None
    return {
        "available": not reason,
        "reason": reason,
        "mode": settings.vision_mode,
        "pages": pages,
        "read": sorted(store.page_readings(att.id, att.sha256)),
        "estimate": round(seconds) if seconds is not None else None,
        "text": offer_text(len(pages), seconds) if pages else "",
    }


def start(store: Store, settings: Settings, job, email: EmailRecord, att: AttachmentRecord, n: int, *, again: bool = False) -> tuple[dict, int]:
    """Start reading the file's pages that need it (``again``: every one, a second time) as the background job.
    Returns the reply for the page and its HTTP status."""
    from controller_inbox import agent

    told = offer(store, settings, email, att)
    if told["reason"]:
        return {"ok": False, "started": False, "message": told["reason"]}, 409
    path = agent.original_file(settings, email, att)
    data = path.read_bytes() if path else b""
    pages = told["pages"]
    if again:
        pages = sorted(set(wanted_pages(data, att.filename, att.extracted_text or "")) | set(told["read"]))
    if not pages:
        return {"ok": True, "started": False, "message": "Every page that needs a second reading has had one."}, 200
    pages = pages[:MAX_PAGES]
    href = f"/inbox/{email.id}/files/{n}"

    def run(progress):
        progress("vision", 0, len(pages), att.filename)
        # Read again, a page's new reading replaces the old one; its first reading is the one kept with it.
        result = read_pages(store, settings, email, att, data, pages, on_progress=lambda i, k, note: progress("vision", i, k, note))
        return {
            "kind": "vision", "email_id": email.id, "n": n, "file": att.filename, "pages": result.pages, "failed": result.failed,
            "seconds": round(result.seconds), "href": href, "message": result_text(result),
        }

    if not job.start(run):
        return {"ok": False, "started": False, "message": "CloseDesk is busy with another job. Try again when it finishes."}, 409
    seconds = estimate(store, settings, len(pages))
    return {"ok": True, "started": True, "pages": pages, "estimate": round(seconds) if seconds is not None else None, "message": offer_text(len(pages), seconds)}, 200


def readings_json(rows: dict[int, dict]) -> list[dict]:
    """For the workspace: each read page's two readings as data (no HTML), with the figures to mark in each."""
    out = []
    for page, row in sorted(rows.items()):
        try:
            saved = json.loads(row.get("comparison") or "{}")
        except ValueError:
            saved = {}
        comparison = Comparison.from_dict(saved)
        out.append({
            "page": page,
            "first_name": saved.get("first_name") or "OCR",
            "model": row.get("model") or "",
            "seconds": round(float(row.get("seconds") or 0)),
            "comparison": comparison.to_dict(),
            "first_text": row.get("first") or "",
            "model_blocks": markdown_blocks(row.get("model_text") or ""),
            "marks_first": sorted({b for _a, b in comparison.differ} | set(comparison.only_first)),
            "marks_model": sorted({a for a, _b in comparison.differ} | set(comparison.only_model)),
        })
    return out


def markdown_blocks(markdown: str) -> list[dict]:
    """The model's markdown as blocks: {"kind": "table", "header", "rows"} and {"kind": "text", "text"}."""
    lines = strip_thinking(markdown).splitlines()
    out: list[dict] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if _TABLE_ROW.match(line) and index + 1 < len(lines) and _RULE_ROW.match(lines[index + 1]):
            header = _cells(line)
            index += 2
            rows = []
            while index < len(lines) and _TABLE_ROW.match(lines[index]):
                if not _RULE_ROW.match(lines[index]):
                    rows.append(_cells(lines[index]))
                index += 1
            out.append({"kind": "table", "header": header, "rows": rows})
            continue
        text = _FORMATTING.sub("", line).strip().lstrip("#").strip()
        if text:
            out.append({"kind": "text", "text": text})
        index += 1
    return out
