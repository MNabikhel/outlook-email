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
import hashlib
import importlib.util
import io
import json
import logging
import math
import re
import statistics
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING

from controller_inbox import tables
from controller_inbox.documents import MAX_PDF_PAGES
from controller_inbox.local_llm import (
    EmptyReply,
    check_model,
    forget_reader_failures,
    load_for_reading,
    reader_load_problem,
    stream_text,
    strip_thinking,
)

if TYPE_CHECKING:
    from controller_inbox.config import Settings
    from controller_inbox.models import AttachmentRecord, EmailRecord
    from controller_inbox.store import Store

log = logging.getLogger(__name__)
# PDFium can't be used from two threads at once (a read in the background, a file page opened meanwhile).
_PDFIUM = threading.Lock()

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
# A dense full page (a 12-column register) is some 4,000 tokens; a reading stops sooner when the model loops.
MAX_TOKENS = 8192
# A laptop without a graphics card can look at a page for minutes before writing the first word.
WAIT_SECONDS = 900.0
# In "auto", pages are read while the question waits when they take no longer than this all told; longer reads are
# offered with the time they'd take.
QUICK_SECONDS = 60.0
# With a document reader (trusted_reader), a question about a scan waits for its reading when that takes up to this
# long, and before this computer's speed is known, for up to this many pages; Process new mail reads waiting scans as
# long as the overnight run does. A general model's reading only helps where it agrees with OCR, so it is waited for
# only when quick.
READER_WAIT_SECONDS = 600.0
READER_FIRST_PAGES = 3
# One request reads at most this many pages, so a long scan isn't a job of hours by accident.
MAX_PAGES = 20
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".bmp", ".webp"}
MODES = ("auto", "ask", "off")
NOTE = "[Read two ways"
MODEL_NAME = "the vision model"
# A page the first reading found nothing on (a scan without OCR): the model's reading is the only one.
ALONE = "found no text on this page"
# How many differing or model-only figures a page's note lists; the answer check flags each one listed. Enough for
# a dense page whose OCR text was cut short (a trial balance: 114 figures only the model read).
LISTED = 150
# After this many failures in a row on the same file, a page is only read when the user asks.
TRIES = 2


@dataclass(frozen=True)
class Reader:
    """How a model is asked to read a page. Empty fields: a general model's settings above."""

    label: str
    prompt: str = ""
    dpi: int = 0
    max_side: int = 0
    max_tokens: int = 0
    context: int = 0  # LM Studio loads it with this much context (a page's picture and its reading)


# OvisOCR2 (0.85B, Apache-2.0, a Qwen3.5-0.8B trained to read document pages) with its own prompt, which asks for
# tables in HTML (merged headings and all). Measured on scanned finance reports it never saw, it read the figures
# more accurately than Qwen3.5-9B, several times faster: see README. 200 DPI with the long side at 2,048 pixels (LM
# Studio shrinks larger pictures to that anyway) gives it about 3,700 image tokens of a letter page; with its reading
# of up to 12,288 tokens, it is loaded with a 20,480-token context.
OVIS_PROMPT = (
    "\nExtract all readable content from the image in natural human reading order and output the result as a single "
    "Markdown document. For charts or images, represent them using an HTML image tag: <img src=\"images/bbox_{left}_"
    "{top}_{right}_{bottom}.jpg\" />, where left, top, right, bottom are bounding box coordinates scaled to [0, 1000). "
    "Format formulas as LaTeX. Format tables as HTML: <table>...</table>. Transcribe all other text as standard "
    "Markdown. Preserve the original text without translation or paraphrasing."
)
GENERAL = Reader("a general model that can see")
# Models made for reading document pages, by a word in their name: preferred over a general one when downloaded.
READERS = {"ovisocr": Reader("OvisOCR2, a document reader", OVIS_PROMPT, 200, 2048, 12288, 20480)}


def reader_for(model: str) -> Reader:
    """How this model reads pages: a known document reader's own way, else a general model's."""
    plain = re.sub(r"[^a-z0-9]", "", (model or "").lower())
    return next((reader for key, reader in READERS.items() if key in plain), GENERAL)


def trusted_reader(model: str) -> bool:
    """A document reader's reading is the page, over OCR's: on 29 scanned pages of 26 documents none of the models had
    seen, OvisOCR2 read none of 2,209 figures wrong and put 99.6% in their right row, where OCR misread 24 and lost the
    table (README). OCR's reading stays for a page the reader found nothing on, and its differences are listed."""
    return reader_for(model or "") is not GENERAL


# A document reader's reading of the whole page has at least this share of the different figures OCR read (OvisOCR2:
# 87% or more on all 39 test pages); one with less stopped early or read something else, and is weighed as any other.
READER_COVERS = 0.5


def _covers(comparison: Comparison) -> bool:
    if not comparison.first_kinds:  # OCR read no figures, or a comparison kept before the count was: not known to cover
        return not comparison.first_figures
    return comparison.confirmed_kinds / comparison.first_kinds >= READER_COVERS


def shown_choice(comparison: Comparison, model_page: str, *, model: str, first_name: str) -> str:
    """Which reading a page is shown as: a document reader's over OCR's (not over a PDF's own text) when it read the
    page and has most of OCR's figures, else as the comparison chose. The chat, the file's page and the workspace
    all go by this."""
    if first_name == "OCR" and trusted_reader(model) and said_something(model_page) and _covers(comparison):
        return "model"
    return comparison.choice


def reading_model(settings: Settings) -> str:
    """The model that reads pages: the one chosen in Setup, while the server has it; else a document reader the server
    has (LM Studio loads a downloaded one when it is first asked), unless LM Studio couldn't load it lately; else the
    chat model when it can see, the older method. "" when there is none."""
    if settings.vision_mode == "off":
        return ""
    status = check_model(settings)
    if not status.reachable:
        return ""
    chosen = (settings.vision_model or "").strip()
    if chosen and chosen != "auto":
        return chosen if chosen in status.vision_models else ""
    for model in status.vision_models:
        if reader_for(model) is not GENERAL and not reader_load_problem(model):
            return model
    return status.model if status.active and status.vision else ""


def reader_name(model: str) -> str:
    """The page reader as the user knows it: a document reader by its name ("OvisOCR2"), any other model by its id."""
    reader = reader_for(model)
    return reader.label.split(",")[0] if reader is not GENERAL else model


def older_method(settings: Settings, model: str | None = None) -> str:
    """How scans are read when no document reader can: the model's own vision beside OCR (the reading CloseDesk had
    before document readers), or OCR alone. ``model``: ``reading_model(settings)``, when already asked."""
    model = reading_model(settings) if model is None else model
    if model and not trusted_reader(model):
        return f"{model}'s own vision, side by side with OCR"
    return "OCR only"


def why_not_reader(settings: Settings, model: str | None = None) -> str:
    """Why scans aren't read by a document reader (OvisOCR2) now, as one sentence; "" when they are."""
    if settings.vision_mode == "off":
        return "Reading scans with a vision model is turned off in Setup."
    if not can_render():
        return "The page renderer isn't installed (double-click CloseDesk once, or run pip install -e .)."
    model = reading_model(settings) if model is None else model
    if model and trusted_reader(model):
        return ""
    status = check_model(settings)
    if not status.reachable:
        return "LM Studio's server isn't answering."
    chosen = (settings.vision_model or "").strip()
    if chosen and chosen != "auto":
        if chosen not in status.vision_models:
            return f"The model chosen in Setup to read pages ({chosen}) isn't in LM Studio now."
        return f"Setup has {chosen} chosen to read pages; choose Automatic there to read them with OvisOCR2."
    for reader in status.vision_models:
        if trusted_reader(reader) and (problem := reader_load_problem(reader)):
            return f"{problem.rstrip('.')}. CloseDesk tries {reader_name(reader)} again in half an hour, or when Setup is saved."
    return "OvisOCR2 isn't downloaded in LM Studio (search OvisOCR2, ATH-MaaS build, Q8_0, about 1 GB)."


def shown_by(row: dict) -> str:
    """Which reading a page read by a model is shown as: "reader" (a document reader's), "model" (a general model's,
    the older method) or "first" (OCR's or the PDF's own text), as ``shown_text`` shows it."""
    saved = _saved(row)
    model = row.get("model") or ""
    model_page = page_text(row.get("model_text") or "")
    named = saved.get("first_name") or "OCR"
    if not model_page.strip() and not said_something(row.get("first") or ""):
        # A blank page (the back of a sheet): the model read it and found nothing, as OCR did.
        return "reader" if trusted_reader(model) else "model"
    if saved:
        comparison = Comparison.from_dict(saved)
    else:
        comparison = compare(row.get("first") or "", model_page, ocr=named == "OCR", trusted=trusted_reader(model))
    if shown_choice(comparison, model_page, model=model, first_name=named) != "model":
        return "first"
    return "reader" if trusted_reader(model) else "model"


def scan_readings(store: Store, settings: Settings, email: EmailRecord, *, question: str = "") -> list[dict]:
    """For each scanned PDF and picture on the email (those the question names, when it names any): which of its
    scanned pages are shown as a document reader read them (``reader``), as a general model read them (``model``,
    the older method) or as OCR read them (``ocr``; ``not_shown``: those of them a document reader read, its reading
    not shown, and ``left_out_by``: that reader's name), and the names of the models whose reading is shown
    (``models``)."""
    from controller_inbox import fraud

    if fraud.attachments_locked(email):
        return []
    scans = [att for att in email.attachments if readable_file(att.filename) and looks_scanned(att)]
    named = [att for att in scans if att.filename.lower() in (question or "").lower()]
    if not named and any(att.filename.lower() in (question or "").lower() for att in email.attachments):
        return []  # the question names a file that isn't a scan
    out = []
    for att in named or scans:
        if Path(att.filename or "").suffix.lower() in IMAGE_SUFFIXES:
            pages = [1]
        else:
            data = original_bytes(settings, email, att)
            pages = scanned_pages(data) if data is not None else []
            if not pages:  # the original isn't kept: the pages OCR read are taken for the scanned ones
                pages = [number for number, _body in page_bodies(att.extracted_text or "")]
        rows = store.page_readings(att.id, att.sha256)
        found: dict = {
            "file": att.filename, "pages": pages, "reader": [], "model": [], "ocr": [], "not_shown": [], "left_out_by": [], "models": [],
        }
        for page in pages:
            row = rows.get(page)
            way = shown_by(row) if row else "first"
            found["ocr" if way == "first" else way].append(page)
            name = reader_name(row.get("model") or "") if row else ""
            if way == "first":
                # A general model's reading set aside is the older method's; it says nothing of the page reader.
                if row and trusted_reader(row.get("model") or ""):
                    found["not_shown"].append(page)
                    if name not in found["left_out_by"]:
                        found["left_out_by"].append(name)
            elif name and name not in found["models"]:
                found["models"].append(name)
        out.append(found)
    return out


# Whether it can be used ------------------------------------------------------------------------


MODE_KEY = "vision_mode"


MODEL_KEY = "vision_model"


def apply_saved_mode(settings: Settings, store: Store) -> None:
    """The choices made in Setup (how and with which model), kept across restarts."""
    saved = store.get_state(MODE_KEY)
    if saved in MODES:
        settings.vision_mode = saved
    model = store.get_state(MODEL_KEY)
    if model is not None:
        settings.vision_model = model


def save_mode(settings: Settings, store: Store, mode: str, model: str | None = None) -> None:
    """``model``: the model that reads pages ("" or "auto": chosen as ``reading_model`` says)."""
    if mode not in MODES:
        raise ValueError(f"Vision reading is one of {', '.join(MODES)}.")
    store.set_state(MODE_KEY, mode)
    settings.vision_mode = mode
    forget_reader_failures()
    if model is not None:
        model = "" if model.strip() == "auto" else model.strip()[:200]
        store.set_state(MODEL_KEY, model)
        settings.vision_model = model


def reads_by_default(settings: Settings) -> bool:
    """Scans are read with a document reader, whose reading is the page: they are read without being asked."""
    return settings.vision_mode == "auto" and trusted_reader(reading_model(settings))


def process_minutes(settings: Settings) -> float:
    """How long Process new mail may spend reading waiting scans."""
    return settings.vision_minutes_per_run if reads_by_default(settings) else QUICK_SECONDS / 60


def available(settings: Settings) -> bool:
    """A model that can read pages is there and vision reading isn't turned off."""
    return bool(reading_model(settings))


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
    with _PDFIUM:
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
# Written after a PDF's last page read (documents.py): kept after that page, never part of it.
_TAIL = re.compile(r"\n*(\[CloseDesk read the first \d+ of \d+ pages\.\])\s*$")
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
        out.append((int(page.group(1)), _TAIL.sub("", text[mark.end():end]).strip()))
    return out


# Looking at a page ---------------------------------------------------------------------------


def render(data: bytes, filename: str, page: int, *, reader: Reader = GENERAL) -> bytes:
    """The page as a PNG at the reader's resolution (a general model: about 100 DPI, its long side at most
    ``MAX_SIDE`` pixels)."""
    from PIL import Image, ImageOps

    dpi, max_side = reader.dpi or DPI, reader.max_side or MAX_SIDE
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".pdf":
        import pypdfium2 as pdfium

        with _PDFIUM:
            pdf = pdfium.PdfDocument(data)
            try:
                if not 1 <= page <= len(pdf):
                    raise ValueError(f"the PDF has no page {page}")
                sheet = pdf[page - 1]
                try:
                    width, height = sheet.get_size()
                    scale = min(dpi / 72, max_side / max(width, height, 1))
                    image = sheet.render(scale=scale).to_pil().copy()
                finally:
                    sheet.close()
            finally:
                pdf.close()
    else:
        image = Image.open(io.BytesIO(data))
        if getattr(image, "n_frames", 1) > 1:
            image.seek(0)
        image = ImageOps.exif_transpose(image)
        image.thumbnail((max_side, max_side))
    out = io.BytesIO()
    image.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()


# Greedy decoding copies a page most faithfully, but now and then falls into writing one line or cell over and over.
# A page it loops on is read once more with the sampling Qwen recommends for its instruct models, which breaks such
# loops: on a scanned commission report that looped from its first line, the second reading had all 162 figures
# right.
RETRY_SAMPLING = {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "presence_penalty": 1.5}


def transcribe(
    settings: Settings, png: bytes, *, on_piece: Callable[[int], None] | None = None, model: str | None = None
) -> str:
    """The model's reading of the page: its text, with tables in markdown (or HTML). ``model``: the one that reads
    pages (``reading_model``), asked its own way (``reader_for``). Raises ``Blank`` for a page with nothing on it,
    ``EmptyReply`` when it wrote nothing, ``CutOff`` when it stopped at its length limit or got stuck repeating itself
    (twice), and ``httpx.HTTPError`` when the server failed."""
    model = model or reading_model(settings) or None
    reader = reader_for(model or "")
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}},
                {"type": "text", "text": reader.prompt or PROMPT},
            ],
        }
    ]
    limit = reader.max_tokens or MAX_TOKENS
    text, looped = _transcribe_once(settings, messages, on_piece, {"temperature": 0.0}, model=model, limit=limit)
    if not looped:
        return text
    log.info("The vision model looped on a page; reading it once more with sampling")
    try:
        again, looped_again = _transcribe_once(settings, messages, on_piece, RETRY_SAMPLING, model=model, limit=limit)
    except (CutOff, EmptyReply, Blank):
        again, looped_again = "", True
    best = again if not looped_again else max(text, again, key=len)
    if len(best) < 200:
        raise CutOff("the model got stuck repeating itself near the top of the page")
    return best


def _transcribe_once(
    settings: Settings, messages: list[dict], on_piece, sampling: dict, *, model: str | None = None, limit: int = 0
) -> tuple[str, bool]:
    """One reading of the page, stopped early if it loops: (text without the loop, whether it looped)."""
    written = []
    finished: dict = {}
    looped = False
    settings_ = {key: value for key, value in sampling.items() if key != "temperature"}
    pieces = stream_text(
        settings, messages, max_tokens=limit or MAX_TOKENS, wait=WAIT_SECONDS, temperature=sampling.get("temperature"),
        finished=finished, sampling=settings_ or None, model=model,
    )
    checked = 0
    try:
        for piece in pieces:
            written.append(piece)
            size = sum(len(p) for p in written)
            if on_piece:
                on_piece(size)
            if ("\n" in piece or size - checked >= 400) and _looping("".join(written)):
                looped = True  # stop it here: the rest would be the same until the token limit
                break
            if "\n" in piece or size - checked >= 400:
                checked = size
    except EmptyReply:
        if finished.get("reason") == "stop" and not finished.get("thought"):
            raise Blank("the page has nothing on it to read") from None
        raise  # it thought until its budget ran out, or sent nothing: a failure, tried again later
    finally:
        pieces.close()
    text, _trimmed = trim_loop(strip_thinking("".join(written)))
    text = text.strip()
    if not text and not looped:
        if finished.get("reason") == "stop" and not finished.get("thought"):
            raise Blank("the page has nothing on it to read")
        raise EmptyReply("the model wrote nothing for the page")
    if finished.get("reason") == "length" and not looped:
        # Half a page would hide the rest of it: the reading is not kept.
        raise CutOff(f"the reading stopped at the {limit or MAX_TOKENS:,}-token limit before the end of the page")
    return text, looped


# An account or routing number beside its label in the model's markdown ("| Account Number | 123456789012 |",
# "**Routing:** 021000021", or an HTML table's "<td>Account No.</td><td>123456789012</td>"): only the number is
# masked, so the table keeps its cells. The number is one word, or groups of digits ("1234 5678 9012"): not the words
# and amount after it ("account 987654321 for 4,750.00").
_SECRET = re.compile(
    r"(\b(?:routing(?:\s+(?:number|no\.?))?|aba|sort\s+code|iban|account(?:\s+(?:number|no\.?|#))?|"
    r"acct\.?(?:\s+(?:number|no\.?))?|a/c)\b(?:[\s#:*_|.]|</?t[dhr]\b[^>]*>)*)([A-Z]{2}\d{2}[A-Z0-9]{10,30}|(?=(?:[A-Z]*[ -]?\d){6})[A-Z0-9]+(?:[ -]\d+)*(?![\d,.]\d))\b",
    re.IGNORECASE,
)


def mask_secrets(markdown: str) -> str:
    """The model's reading with bank account, routing and IBAN numbers masked to their last four digits, as
    CloseDesk masks every file's text."""
    from controller_inbox.extract import redact_financial_secrets

    def mask(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(2))
        return f"{match.group(1)}****{digits[-4:]}"

    return redact_financial_secrets(_SECRET.sub(mask, markdown or ""))


# A model reading a page greedily can fall into writing one line over and over (an empty table row) until its token
# limit. A run this long of one line is that; a line with nothing but table pipes is that sooner.
LOOP_LINES = 20
LOOP_BLANK_ROWS = 30  # an invoice can print a dozen empty line-item rows; thirty in a row is the model looping


def _repeated_tail(lines: list[str]) -> int:
    """How many of the last lines are the same line."""
    if not lines:
        return 0
    last = lines[-1].strip()
    count = 0
    for line in reversed(lines):
        if line.strip() != last:
            break
        count += 1
    return count


def _blank_row(line: str) -> bool:
    return bool(_TABLE_ROW.match(line)) and not re.sub(r"[|\s:-]", "", line)


# The same few characters over and over at the end of the reading: empty cells written across one line without
# end ("|  |  |  …"). Sixty in a row is more columns than any printed table has. A run of nothing but dots, dashes,
# underscores and spaces is the page's own (a line to sign on, a dot leader), not a loop.
_RUN = re.compile(r"(?!(?:[^\w|<>]|_)*$)(.{1,12}?)\1{59,}$", re.S)
# A longer run written on one line: OvisOCR2 writes a whole table on one line, so a loop on an empty row is
# "<tr><td></td><td></td></tr>" over and over. Thirty in a row, as for empty markdown rows.
LONG_RUN = (13, 160, LOOP_BLANK_ROWS)


def _long_run(text: str) -> tuple[int, int]:
    """Where a run of one stretch of 13 to 160 characters, written thirty times or more, ends the text (start,
    length of the stretch); (-1, 0) when it doesn't. A stretch repeats wherever each character is the one a stretch
    before it, so the run is found wherever the reading stopped within a stretch."""
    shortest, longest, times = LONG_RUN
    tail = text[-longest * (times + 1):]
    for size in range(shortest, longest + 1):
        span = size * times
        if len(tail) >= span + size and tail[-span:] == tail[-span - size:-size]:
            start = len(text) - span - size
            while start > 0 and text[start - 1] == text[start - 1 + size]:
                start -= 1
            return start, size
    return -1, 0


def _looping(text: str) -> bool:
    lines = [line for line in text.split("\n")[:-1] if line.strip()]  # complete lines only
    run = _repeated_tail(lines)
    if lines and run >= (LOOP_BLANK_ROWS if _blank_row(lines[-1]) else LOOP_LINES):
        return True
    return bool(_RUN.search(text[-1500:].rstrip())) or _long_run(text.rstrip())[0] >= 0


def trim_loop(text: str) -> tuple[str, bool]:
    """The reading without a line the model repeated at its end (kept once when it says something), or without
    the characters it repeated across its last line. Blank lines between repeated lines are passed over, as
    ``_looping`` does."""
    tail = _RUN.search(text[-1500:].rstrip())
    if tail:
        cut = len(text[-1500:].rstrip()) - len(tail.group(0))
        start = text.rstrip()[: len(text.rstrip()) - len(text[-1500:].rstrip()) + cut].rstrip(" |")
        last = start.rsplit("\n", 1)[-1]
        return start + (" |" if last.lstrip().startswith("|") else ""), True  # the table row closed where it stopped
    lines = text.rstrip().split("\n")
    filled = [index for index, line in enumerate(lines) if line.strip()]
    if len(filled) > 1:
        last, before = lines[filled[-1]].strip(), lines[filled[-2]].strip()
        if last != before and before.startswith(last):
            filled = filled[:-1]  # the repeated line, cut off part way when the reply stopped
    kept = [lines[index] for index in filled]
    run = _repeated_tail(kept)
    if not kept or run < (LOOP_BLANK_ROWS if _blank_row(kept[-1]) else LOOP_LINES):
        return _without_long_run(text)
    first_dropped = len(kept) - run + (0 if _blank_row(kept[-1]) else 1)
    end = filled[first_dropped] if first_dropped < len(filled) else len(lines)
    return "\n".join(lines[:end]).rstrip(), True


def _without_long_run(text: str) -> tuple[str, bool]:
    """The reading without one stretch of a line repeated at its end, the stretch kept once (it can be a row the page
    has), from a line or tag end on: the run can begin part way into a row."""
    clean = text.rstrip()
    start, size = _long_run(clean)
    if start < 0:
        return text, False
    ends = [index for index in (clean.find("\n", start, start + size), clean.find(">", start, start + size)) if index >= 0]
    end = min(ends) + 1 + size if ends else start + size
    return clean[:end].rstrip(), True


class CutOff(Exception):
    """The model's reply ended at its length limit, part way down the page."""


class Blank(Exception):
    """The model finished without writing anything: the page is blank (the back of a sheet)."""


_reading = 0
_reading_lock = threading.Lock()


def busy() -> bool:
    """A page is being read with the vision model now (the background job, or a question's quick read)."""
    return _reading > 0


# The model's markdown as a page CloseDesk reads ------------------------------------------------


_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_RULE_ROW = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")
# Bold and code marks; a run of four asterisks is a masked number ("****9012"), not formatting.
_FORMATTING = re.compile(r"(?<!\*)\*\*(?!\*)|__|`")


_TABLE_TAG = re.compile(r"<(/?)table\b[^>]*>", re.I)
_STRAY_TAG = re.compile(r"</?(?:table|thead|tbody|tfoot|tr|td|th)\b[^>]*>", re.I)
_PICTURE = re.compile(r"^\s*<img\b[^>]*>\s*$", re.M | re.I)  # a region the model saw as a picture (OvisOCR2)


def _html_table_spans(text: str) -> list[tuple[int, int]]:
    """Where each table is: from its <table> to its </table>, or to the next <table> (a table inside a cell is
    rare; one the reading never closed ends there), or to the end of the reading (one it stopped in)."""
    spans, start = [], None
    for match in _TABLE_TAG.finditer(text):
        if not match.group(1):
            if start is not None:
                spans.append((start, match.start()))
            start = match.start()
        elif start is not None:
            spans.append((start, match.end()))
            start = None
    if start is not None:
        spans.append((start, len(text)))
    return spans


class _TableCells(HTMLParser):
    """A table's rows of cells [kind, colspan, rowspan, text parts], as a browser would read them: a cell or a row
    ends at the next one even without its closing tag, a cell after a row's end starts a row, other tags are spaces,
    and text outside any cell is kept (``outside``)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[list]] = []
        self.outside: list[str] = []
        self.cell: list | None = None
        self.row_open = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.cell = None
            self.rows.append([])
            self.row_open = True
        elif tag in {"td", "th"}:
            if not self.row_open:
                self.rows.append([])
                self.row_open = True
            spans = {name: value for name, value in attrs}

            def span(name: str, most: int) -> int:
                number = re.match(r"\s*(\d+)", spans.get(name) or "")
                return max(1, min(int(number.group(1)), most)) if number else 1

            self.cell = [tag, span("colspan", 50), span("rowspan", 200), []]
            self.rows[-1].append(self.cell)
        else:
            self.handle_data(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"}:
            self.cell = None
        elif tag == "tr":
            self.cell = None
            self.row_open = False
        else:
            self.handle_data(" ")

    def handle_data(self, data: str) -> None:
        (self.cell[3] if self.cell is not None else self.outside).append(data)


def _html_grid(table: str) -> tuple[list[str], list[list[str]], int]:
    """The table's title lines, its cells on a grid, and how many rows at the top of the grid are column headings.

    Document models write a report's title into the table (one text across the row from its first column: the
    company, the report, its period) and mark no cell as a heading, so the titles come out as lines above the table.
    The first heading row has labels over the figure columns and no figures (a date or an aging bucket there is a
    label: "October 31", "10/09/26", "1-30"); a row under it is a heading too while a heading above reaches into it
    (merged down) or groups the columns it names ("Q3 2026" over Actual and Budget). A row with one label across it
    (ASSETS, "Operating Receipts") starts a section. A two-column table is a list of labels and values ("Invoice No. |
    INV-20417") unless it marks its headings. A merged heading is written over every column and row it covers (it
    names each of them); a merged cell in the body only in its first column, so a label or figure isn't repeated
    across the row, though a label merged down the rows (a category) is kept on each of them."""
    parser = _TableCells()
    parser.feed(table)
    parser.close()
    said = " ".join("".join(parser.outside).split())
    grid: list[list[str]] = []
    marked: list[bool] = []  # every cell of the row a <th>
    grouping: list[bool] = []  # a cell merged across figure columns
    reached: list[set[int]] = []  # the rows above whose cells are merged down into this one
    copies: set[tuple[int, int]] = set()  # cells holding a merged cell's text again
    pending: dict[tuple[int, int], tuple[str, int]] = {}  # (row, column) -> (text, row) of a cell merged down into it
    for r, cells in enumerate(parser.rows):
        row: list[str] = []
        from_above: set[int] = set()

        def take_pending() -> None:
            while (r, len(row)) in pending:
                text, source = pending.pop((r, len(row)))
                from_above.add(source)
                if grid_is_data(text):
                    copies.add((r, len(row)))
                row.append(text)

        groups = False
        for _kind, colspan, rowspan, parts in cells:
            take_pending()
            text = " ".join("".join(parts).split()).replace("|", "/")
            groups = groups or (colspan > 1 and len(row) > 0)
            for offset in range(colspan):
                if offset:
                    copies.add((r, len(row)))
                for down in range(1, rowspan):
                    pending[(r + down, len(row))] = (text, r)
                row.append(text)
        take_pending()
        grid.append(row)
        marked.append(bool(cells) and all(cell[0] == "th" for cell in cells))
        grouping.append(groups)
        reached.append(from_above)
    width = max((len(row) for row in grid), default=0)

    def title(row: list[str]) -> bool:
        """Blank, or one text from the first column on (a text over the figure columns only is their heading)."""
        return all(not cell or cell == row[0] for cell in row)

    def labels_only(r: int) -> bool:
        row = grid[r]
        # A dash or "n/a" holds a figure's place; it is no label.
        labels = [cell for cell in row[1:] if cell and cell != row[0] and not (tables.is_value(cell) and not re.search(r"\d", cell))]
        return bool(labels) and not any(_heading_figure(cell) for cell in row if cell)

    def first_heading(r: int) -> bool:
        return marked[r] or (width >= 3 and labels_only(r))

    # Titles: the rows of one text (or none) above the first heading row, when there is one.
    top = 0
    while top < min(len(grid), 8) and title(grid[top]) and not marked[top]:
        top += 1
    if not (top < len(grid) and first_heading(top)):
        top = 0
    titles = [*([said] if said else []), *(next(cell for cell in row if cell) for row in grid[:top] if any(row))]
    heading_rows = 0
    if top < len(grid) - 1 and first_heading(top):
        heading_rows = 1
        while heading_rows < 3 and top + heading_rows < len(grid) - 1:
            r = top + heading_rows
            above = range(top, r)
            if not (marked[r] or (labels_only(r) and (reached[r] & set(above) or grouping[r - 1]))):
                break
            heading_rows += 1
    grid = grid[top:]
    for r, column in copies:
        if r - top >= heading_rows and r >= top and column < len(grid[r - top]):
            grid[r - top][column] = ""
    return titles, grid, heading_rows


# An amount, as a heading row can't hold one ("1-30", "Oct 2026" and "10/09/26" are labels there).
_AMOUNT = re.compile(r"[-−(]?\s?[$€£]?\s?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\)?%?")


def _heading_figure(text: str) -> bool:
    text = text.strip()
    return bool(_AMOUNT.fullmatch(text)) and not _YEAR_LABEL.match(text)


def grid_is_data(text: str) -> bool:
    """A figure (a year in a heading is a label)."""
    return tables.is_value(text) and not _YEAR_LABEL.match(text)


def _html_tables_as_markdown(text: str) -> str:
    """Each HTML table (OvisOCR2 and other document models write them, merged cells and all) as a markdown table with
    one heading line: the heading rows joined per column ("Revenue Recognized" over "Oct-26": "Revenue Recognized
    Oct-26"), so the rest of this module reads it like any other."""

    def one(table: str) -> str:
        titles, grid, heading_rows = _html_grid(table)
        if not grid:
            return "\n\n" + "\n\n".join(titles) + "\n\n" if titles else ""
        width = max(len(row) for row in grid)
        grid = [[*row, *[""] * (width - len(row))] for row in grid]
        filled = [row for row in grid if any(row)]
        if (
            not heading_rows and width == 2 and len(filled) >= 2
            and all(_form_label(label) and value.strip() for label, value in filled)
            and 2 * sum(tables.is_value(value) for _label, value in filled) <= len(filled)
        ):
            # A form ("Payee | Latah County Treasurer", "GL account | 6420 - Property Taxes"): a value on every line,
            # mostly words. Each line is written as its label and value. Labels beside figures stay a table.
            lines = [f"{label.rstrip(': ')}: {value}" for label, value in filled]
            return "\n\n" + "".join(title + "\n\n" for title in titles) + "\n".join(lines) + "\n\n"
        if heading_rows:
            header = []
            for column in range(width):
                parts: list[str] = []
                for row in grid[:heading_rows]:
                    if row[column] and (not parts or parts[-1] != row[column]):
                        parts.append(row[column])
                header.append(" ".join(parts))
            body = grid[heading_rows:]
        else:  # the names a table without headings gets anyway, so its first row isn't taken for a second heading line
            header, body = ["", *(f"Column {index + 1}" for index in range(1, width))], grid
        lines = ["| " + " | ".join(header) + " |", "|" + "---|" * width]
        lines += ["| " + " | ".join(row) + " |" for row in body]
        return "\n\n" + "".join(title + "\n\n" for title in titles) + "\n".join(lines) + "\n\n"

    text = _PICTURE.sub("", text or "")
    if "<table" not in text.lower():
        return text
    out, last = [], 0
    for start, end in _html_table_spans(text):
        out += [_STRAY_TAG.sub(" ", text[last:start]), one(text[start:end])]
        last = end
    return "".join(out) + _STRAY_TAG.sub(" ", text[last:])


def _form_label(cell: str) -> bool:
    """A form's label: a few words, not a figure ("Payee", "Date needed by", "GL account")."""
    cell = (cell or "").strip()
    return bool(cell) and len(cell) <= 48 and len(cell.split()) <= 6 and not tables.is_value(cell)


def page_text(markdown: str) -> str:
    """The model's markdown written the way CloseDesk writes a page it read: each table row by row with every cell
    named by its column (``Label: value``) and section rows as ``Group:``, the rest as notes, so the table lookup,
    the totals check and the chat read it like any other page. HTML tables are read too."""
    lines = _html_tables_as_markdown(strip_thinking(markdown or "")).splitlines()
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


_YEAR_LABEL = re.compile(r"^(19|20)\d\d$")


def _second_heading(header: list[str], row: list[str]) -> bool:
    """The table's first row is the second line of its column headings: labels only (a year counts as one), and at
    least one sits under a heading spanning several columns ("Revenue Recognized" over Oct-26, Nov-26 …), which a
    markdown table can only write as an empty heading cell. A second table's own headings fill no such gap."""
    filled = [cell for cell in row if cell]
    if len(filled) < 2 or any(tables.is_value(cell) and not _YEAR_LABEL.match(cell) for cell in filled):
        return False
    padded = [*header, *[""] * (len(row) - len(header))]
    return any(cell and not padded[index] for index, cell in enumerate(row) if index > 0)


def _joined_headings(header: list[str], row: list[str]) -> list[str]:
    """Two heading lines as one: a spanning heading is carried over the columns under it ("Revenue Recognized
    Oct-26"), and a column with one line keeps it."""
    width = max(len(header), len(row))
    top = [*header, *[""] * (width - len(header))]
    sub = [*row, *[""] * (width - len(row))]
    out, carried = [], ""
    for above, below in zip(top, sub):
        carried = above or carried
        out.append(f"{above or carried} {below}".strip() if below else above)
    return out


def _aligned(header: list[str], rows: list[list[str]]) -> list[str]:
    """The headings over the right figures: when the model gave the heading lines more blank cells at the left than
    the rows have (every row with figures is shorter), the extra blanks go, so "Month of October 2026" heads the
    2026 column, not the row names'."""
    widths = Counter(len(row) for row in rows if sum(1 for cell in row if cell) >= 2)
    if not widths:
        return header
    width = widths.most_common(1)[0][0]
    extra = len(header) - width
    if extra > 0 and all(len(row) <= width for row in rows) and not any(header[: extra + 1]):
        return header[extra:]
    return header


def _table_lines(header: list[str], rows: list[list[str]]) -> list[str]:
    from controller_inbox.table_lookup import ROW_LABEL

    if rows and _second_heading(header, rows[0]):
        header, rows = _joined_headings(header, rows[0]), rows[1:]
    header = _aligned(header, rows)
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
    first_figures: int = 0
    first_kinds: int = 0  # different figures in the first reading
    confirmed_kinds: int = 0  # different figures both readings have

    def to_dict(self) -> dict:
        return {
            "figures": self.figures, "confirmed": self.confirmed, "differ": self.differ, "only_model": self.only_model,
            "only_first": self.only_first, "model_totals": list(self.model_totals), "first_totals": list(self.first_totals),
            "choice": self.choice, "first_figures": self.first_figures, "first_kinds": self.first_kinds,
            "confirmed_kinds": self.confirmed_kinds,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Comparison:
        return cls(
            figures=int(data.get("figures", 0)), confirmed=int(data.get("confirmed", 0)),
            differ=[tuple(pair) for pair in data.get("differ", [])], only_model=list(data.get("only_model", [])),
            only_first=list(data.get("only_first", [])), model_totals=tuple(data.get("model_totals", (0, 0))),
            first_totals=tuple(data.get("first_totals", (0, 0))), choice=str(data.get("choice", "first")),
            first_figures=int(data.get("first_figures", 0)), first_kinds=int(data.get("first_kinds", 0)),
            confirmed_kinds=int(data.get("confirmed_kinds", 0)),
        )


def compare(first: str, model_page: str, *, ocr: bool = True, trusted: bool = False) -> Comparison:
    """Figure by figure, and by each reading's printed totals: which reading the page is shown as. ``ocr``: the
    first reading is OCR's (a scan or a picture), not a PDF's own text. ``trusted``: the model is a document reader
    (``trusted_reader``), whose reading is shown whenever it read the page."""
    mine, theirs = figures(_ungrouped(model_page)), figures(_ungrouped(first))
    model_count = Counter({value: len(shown) for value, shown in mine.items()})
    first_count = Counter({value: len(shown) for value, shown in theirs.items()})
    both = model_count & first_count
    only_model = model_count - first_count
    only_first = first_count - model_count
    comparison = Comparison(
        figures=sum(model_count.values()), confirmed=sum(both.values()), first_figures=sum(first_count.values()),
        first_kinds=len(first_count), confirmed_kinds=len(both),
    )
    pairs, left_model, left_first = _pairs(only_model, only_first)
    comparison.differ = [(mine[a][0], theirs[b][0]) for a, b in pairs]
    comparison.only_model = [mine[value][0] for value in left_model]
    comparison.only_first = [theirs[value][0] for value in left_first]
    comparison.model_totals = _totals(model_page)
    comparison.first_totals = _totals(first)
    if trusted and ocr and said_something(model_page) and _covers(comparison):
        comparison.choice = "model"
    else:
        comparison.choice = _choose(comparison, ocr=ocr) if said_something(first) else ("model" if model_page.strip() else "first")
    return comparison


def _ungrouped(page: str) -> str:
    """The page without its section names written on each row ("Group: 6100 Office Supplies", then "6100 Office
    Supplies | Line: …" on every row under it), so a figure in a section's name counts once, as printed."""
    out = []
    group = ""
    for line in (page or "").splitlines():
        if line.startswith("Group: "):
            group = line[len("Group: "):].rstrip(": ").strip()
            out.append(group)
            continue
        if line.startswith("["):
            group = ""
        if group and line.startswith(group + " | "):
            line = line[len(group) + 3:]
        out.append(line)
    return "\n".join(out)


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


def _choose(comparison: Comparison, *, ocr: bool = True) -> str:
    """Which reading the page is shown as.

    The model's when its totals hold up at least as well as the first reading's, most of its figures are the first
    reading's too (a reading that shares few figures is of something else, or made up), and it has most of the
    first reading's figures (one that stopped early would hide the rest of the page). That is counted over the
    different figures: OCR's text writes a column's heading into every cell under it ("October 1-31, 2026 Debit
    6100 Salaries and Wages: 61,240.00"), so a figure in a heading is in it once per row, though printed once.

    Against OCR, also the model's when it has nearly all of OCR's figures (85%), whatever its totals: it read the
    same page, and its table puts each figure in its row and column where OCR's text runs them together. OCR's
    text can't be totalled at all, so a model table whose totals don't all add up isn't worse than it; what OCR
    lacked (cut short on a dense page) or read differently is listed under the page and flagged in answers. Measured
    on 15 unseen scanned pages, this kept OCR's reading on the dense pages where the model misread small print."""
    def score(totals: tuple[int, int]) -> int:
        return totals[0] - 2 * totals[1]

    share = comparison.confirmed / comparison.figures if comparison.figures else 0.0
    covered = comparison.confirmed_kinds / comparison.first_kinds if comparison.first_kinds else 1.0
    if ocr and covered >= 0.85 and share >= 0.3:
        return "model"
    if share >= 0.5 and covered >= 0.5 and score(comparison.model_totals) >= score(comparison.first_totals):
        return "model"
    return "first"


def _pairs(only_model: Counter, only_first: Counter) -> tuple[list[tuple[Decimal, Decimal]], list[Decimal], list[Decimal]]:
    """Figures the two readings wrote differently: a model figure and a first-reading figure of the same length a
    digit or two apart are one figure read two ways ("1,240.00" / "1,246.00")."""
    model_left = [value for value, count in only_model.items() for _ in range(count)]
    first_left = [value for value, count in only_first.items() for _ in range(count)]
    candidates = []
    for i, value in enumerate(model_left):
        mine = _digits(value)
        for j, other in enumerate(first_left):
            theirs = _digits(other)
            if abs(other) == abs(value):  # the sign is all that differs
                candidates.append((0, Decimal(0), i, j))
            elif len(theirs) == len(mine) and (apart := sum(a != b for a, b in zip(mine, theirs))) <= 2:
                candidates.append((apart, abs(abs(value) - abs(other)), i, j))
    # Fewest digits apart first, then the nearest in value: each figure is paired with the one it was most likely.
    pairs, used_model, used_first = [], set(), set()
    for _apart, _gap, i, j in sorted(candidates):
        if i not in used_model and j not in used_first:
            pairs.append((model_left[i], first_left[j]))
            used_model.add(i)
            used_first.add(j)
    rest_model = [value for i, value in enumerate(model_left) if i not in used_model]
    rest_first = [value for j, value in enumerate(first_left) if j not in used_first]
    return pairs, rest_model, rest_first


def _digits(value: Decimal) -> str:
    return re.sub(r"\D", "", format(abs(value), "f"))


# The page as CloseDesk shows it ---------------------------------------------------------------


def merged_page(
    first: str, model_markdown: str, comparison: Comparison, *, first_name: str, model: str, model_page: str | None = None
) -> str:
    """The page as the chat and the table lookup read it: the reading that holds up best, with what the other one
    read differently written under it. ``model_page``: ``page_text(model_markdown)``, when already made."""
    model_page = page_text(model_markdown) if model_page is None else model_page
    shown = model_page if comparison.choice == "model" else first
    who = f"{MODEL_NAME} ({model})" if model else MODEL_NAME
    if comparison.choice == "model" and trusted_reader(model) and first_name == "OCR":
        return _reader_page(first, model_page, comparison, first_name=first_name, who=who)
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
    if comparison.choice == "model" and comparison.only_first:
        lines.append(f"[Read only by {first_name}: {'; '.join(comparison.only_first[:LISTED])}]")
    return "\n".join(line for line in lines if line)


def _reader_page(first: str, model_page: str, comparison: Comparison, *, first_name: str, who: str) -> str:
    """A document reader's page: shown as it read it. What the first reading had differently is listed for whoever
    checks the page, worded so the answer check doesn't flag the reader's figures (OCR's misreads, mostly)."""
    if not said_something(first):
        return f"{NOTE}, by {first_name} and by {who}, a document reader: {first_name} found no text here.]\n{model_page}"
    same = f"{comparison.confirmed} of {comparison.figures} figures read the same" if comparison.figures else "no figures to compare"
    matched, mismatched = comparison.model_totals
    totals = ""
    if matched or mismatched:
        totals = f"; its printed totals {'add up' if not mismatched else f'add up {matched} of {matched + mismatched} times'}"
    lines = [f"{NOTE}, by {first_name} and by {who}: {same}. Shown: the document reader's reading{totals}.]", model_page]
    if comparison.differ:
        listed = "; ".join(f"{theirs} where the reader read {mine}" for mine, theirs in comparison.differ[:LISTED])
        lines.append(f"[{first_name} read differently: {listed}]")
    if comparison.only_first:
        lines.append(f"[Read only by {first_name}: {'; '.join(comparison.only_first[:LISTED])}]")
    return "\n".join(lines)


_ONLY_MODEL = re.compile(r"^\[Read only by the vision model: (.+)\]$", re.M)
_DIFFER = re.compile(r"^\[Where the two readings differ \(the vision model / ([^)]+)\): (.+)\]$", re.M)
_DIFFER_FIRST = re.compile(r"^\[Where the two readings differ \(([^)]+) / the vision model\): (.+)\]$", re.M)


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
    # The first reading shown: the model's side of each difference is only in the note.
    for match in _DIFFER_FIRST.finditer(text or ""):
        for pair in match.group(2).split("; "):
            theirs, _, mine = pair.partition(" / ")
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
    unmarked = {page: row for page, row in rows.items() if _saved(row).get("kind") == "unmarked"}
    if not marks:
        body = text.strip()
        if not unmarked:
            row = rows.get(1)
            if not row:
                return text
            # A picture: its whole text is page 1.
            return _page_for(row["first"] if body.startswith(NOTE) else body, row)
        # A PDF CloseDesk couldn't read page by page (its text is only a note): each page read follows the note.
        pages = [f"[page {page}]\n{_page_for('', row)}" for page, row in sorted(unmarked.items())]
        return "\n\n".join(part for part in [body, *pages] if part)
    seen = {int(found.group(1)) for found in _PAGE_MARK.finditer(text)}
    extra = [f"[page {page}]\n{_page_for('', row)}" for page, row in sorted(rows.items()) if page not in seen]
    pieces = [text[: marks[0].start()]]
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        page = _PAGE_MARK.match(mark.group(0))
        row = rows.get(int(page.group(1))) if page else None
        if row is None:
            pieces.append(text[mark.start():end])
            continue
        body = text[mark.end():end]
        tail = _TAIL.search(body)
        body = (body[: tail.start()] if tail else body).strip()
        first = row["first"] if body.startswith(NOTE) else body
        after = f"\n\n{tail.group(1)}" if tail else ""
        gap = "\n\n" if i + 1 < len(marks) else ""
        pieces.append(f"{mark.group(0).strip()}\n{_page_for(first, row)}{after}{gap}")
    return "".join(pieces) + "".join(f"\n\n{page}" for page in extra)


def _saved(row: dict) -> dict:
    """The comparison stored with a reading, or {} when it can't be read."""
    try:
        saved = json.loads(row.get("comparison") or "{}")
    except (TypeError, ValueError):
        return {}
    return saved if isinstance(saved, dict) else {}


def _page_for(first: str, row: dict) -> str:
    saved = _saved(row)
    model_text = row.get("model_text") or ""
    first_name = saved.get("first_name") or "OCR"
    model = row.get("model") or ""
    model_page = page_text(model_text)
    if row.get("first") == first and saved:
        comparison = Comparison.from_dict(saved)
    else:
        comparison = compare(first, model_page, ocr=first_name == "OCR", trusted=trusted_reader(model))
    # Also a reading kept before document readers were shown over OCR.
    comparison.choice = shown_choice(comparison, model_page, model=model, first_name=first_name)
    return merged_page(first, model_text, comparison, first_name=first_name, model=model, model_page=model_page)


# Reading pages and keeping them ---------------------------------------------------------------


@dataclass
class Result:
    pages: int = 0
    seconds: float = 0.0
    shown_model: int = 0
    failed: list[str] = field(default_factory=list)
    stopped: bool = False


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
    should_stop: Callable[[], bool] | None = None,
) -> Result:
    """Read ``pages`` of the attachment with the vision model, compare each with its first reading and keep both.
    The attachment's text shows each page as the reading that holds up best from then on (``shown_text``). A page
    read again is compared with the first reading as stored now (a reader update may have changed it)."""
    global _reading
    with _reading_lock:
        _reading += 1
    try:
        return _read_pages(store, settings, email, att, data, pages, on_progress=on_progress, should_stop=should_stop)
    finally:
        with _reading_lock:
            _reading -= 1


def _read_pages(store, settings, email, att, data, pages, *, on_progress=None, should_stop=None) -> Result:
    result = Result()
    if not same_file(att, data):
        result.failed.append("the file kept under this name isn't this attachment")
        return result
    model = reading_model(settings)
    if not model:  # the model went away since the read was offered: nothing is counted against the pages
        problem = next((reader_load_problem(name) for name in check_model(settings).vision_models if reader_load_problem(name)), "")
        result.failed.append(problem.rstrip(".") or "no model that can look at pictures is answering")
        return result
    reader = reader_for(model)
    problem = load_for_reading(settings, model, reader.context)
    if problem:
        log.warning("%s", problem)
        status = check_model(settings)
        if model not in status.models and model not in {key for key, _size in status.instances.values()}:
            result.failed.append(problem)  # LM Studio won't load it when asked either: nothing is counted against the pages
            return result
    suffix = Path(att.filename).suffix.lower()
    scanned = set(scanned_pages(data)) if suffix == ".pdf" else set()
    stored_text = store.stored_text(att.id)
    raw = (att.extracted_text or "") if stored_text is None else stored_text
    kind = "picture" if suffix in IMAGE_SUFFIXES else ("pdf" if _PAGE_MARK.search(raw) else "unmarked")
    bodies = dict(page_bodies(raw)) if kind != "unmarked" else {}
    stored = store.page_readings(att.id, att.sha256)
    todo = pages[:MAX_PAGES]
    for index, page in enumerate(todo, start=1):
        if should_stop and should_stop():
            result.stopped = True
            break
        first = bodies.get(page, "")
        if first.startswith(NOTE):  # the page as shown, saved back: its first reading is the one kept with it
            if page not in stored:
                continue
            first = stored[page]["first"]
        if on_progress:
            on_progress(index - 1, len(todo), f"{att.filename}, page {page}")
        started = time.monotonic()
        try:
            png = render(data, att.filename, page, reader=reader)
            markdown = mask_secrets(transcribe(settings, png, model=model))
        except Blank:
            markdown = ""  # a blank page (the back of a sheet): nothing on it to read
        except Exception as exc:  # one page failing doesn't lose the others
            log.warning("Vision reading of %s page %s failed: %s", att.filename, page, exc)
            store.note_page_failure(att.id, page, sha256=att.sha256, error=str(exc), model=model)
            result.failed.append(f"page {page}: {str(exc)[:160]}")
            continue
        seconds = time.monotonic() - started
        named = first_name(first, page, scanned, att.filename)
        comparison = compare(first, page_text(markdown), ocr=named == "OCR", trusted=trusted_reader(model))
        store.save_page_reading(
            att.id, page, first=first, model_text=markdown, model=model, seconds=seconds, sha256=att.sha256,
            comparison=json.dumps({**comparison.to_dict(), "first_name": named, "kind": kind}),
        )
        result.pages += 1
        result.seconds += seconds
        result.shown_model += comparison.choice == "model"
        if on_progress:
            on_progress(index, len(todo), f"{att.filename}, page {page}")
    return result


def same_file(att: AttachmentRecord, data: bytes) -> bool:
    """The bytes kept are this attachment's (a file inside a zip can share its name with another one)."""
    return not att.sha256 or hashlib.sha256(data).hexdigest() == att.sha256


def original_bytes(settings: Settings, email: EmailRecord, att: AttachmentRecord) -> bytes | None:
    """The attachment as it arrived, when CloseDesk kept it and it is this attachment."""
    from controller_inbox import agent

    path = agent.original_file(settings, email, att)
    if path is None:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data if same_file(att, data) else None


def unread_pages(
    store: Store, att: AttachmentRecord, data: bytes, *, doubtful: bool = True, retry_failed: bool = False, upgrade: bool = False,
    model: str | None = None,
) -> list[int]:
    """The pages worth a second reading that haven't had one. ``doubtful``: also text pages whose totals don't add
    up (finding them reads every table, so the overnight run looks at scans and pictures only). A page the model
    (``model``, when given: another model's failures don't count) failed on ``TRIES`` times is left out unless
    ``retry_failed`` (the user asked). ``upgrade``: a document reader reads pages now, so a page read the older way
    (by a general model) is read again by it."""
    readings = store.page_readings(att.id, att.sha256)
    done = {page for page, row in readings.items() if not upgrade or trusted_reader(row.get("model") or "")}
    if not retry_failed:
        done |= {page for page, tries in store.page_failures(att.id, att.sha256, model).items() if tries >= TRIES}
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
    from controller_inbox import fraud

    if fraud.attachments_locked(email):
        return []
    model = reading_model(settings)
    upgrade = trusted_reader(model)
    out = []
    for position, att in enumerate(email.attachments, start=1):
        if not readable_file(att.filename) or (not doubtful and not looks_scanned(att)):
            continue
        data = original_bytes(settings, email, att)
        if data is None:
            continue
        pages = unread_pages(store, att, data, doubtful=doubtful, upgrade=upgrade, model=model)
        if pages:
            out.append((position, att, data, pages))
    return out


# One run may read at most this many pages, however fast they go.
RUN_PAGES = 60
# Before this computer's speed is known, a page is taken to need this long (a laptop without a graphics card).
UNKNOWN_SECONDS = 300.0


def read_waiting(
    store: Store,
    settings: Settings,
    *,
    minutes: float | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> Result:
    """In "auto": the scanned pages and pictures still waiting for a second reading, newest mail first, for as long
    as the run may spend (``minutes``; default ``Settings.vision_minutes_per_run``). A page is only started when it
    is expected to fit, at this computer's speed (``UNKNOWN_SECONDS`` before the first page shows it), and while
    ``should_stop`` (Stop in the bar at the top) says go on."""
    total = Result()
    if settings.vision_mode != "auto" or not can_render() or not available(settings):
        return total
    budget = 60.0 * (settings.vision_minutes_per_run if minutes is None else minutes)

    def expected() -> float:
        per_page = seconds_per_page(store, settings)
        return UNKNOWN_SECONDS if per_page is None else per_page

    if expected() > budget:
        return total  # not even one page fits: no need to look through the mail
    started = time.monotonic()
    files_failed_in_a_row = 0
    for email in store.list_emails(limit=400):
        for _position, att, data, pages in files_to_read(store, settings, email, doubtful=False):
            for page in pages:
                if time.monotonic() - started + expected() > budget or total.pages + len(total.failed) >= RUN_PAGES:
                    return total
                if should_stop and should_stop():
                    total.stopped = True
                    return total
                if on_progress:
                    on_progress(total.pages, 0, f"{att.filename}, page {page}")
                one = read_pages(store, settings, email, att, data, [page])
                total.pages += one.pages
                total.seconds += one.seconds
                total.shown_model += one.shown_model
                total.failed += one.failed
                if one.failed:
                    break  # the rest of this file waits for another night (a page failing twice is left out)
            else:
                files_failed_in_a_row = 0
                continue
            files_failed_in_a_row += 1
            if files_failed_in_a_row >= 2:
                return total  # the model server is likely down or out of memory
    return total


# How long it takes on this computer -----------------------------------------------------------


def seconds_per_page(store: Store, settings: Settings) -> float | None:
    """The typical time the model that reads pages has taken on this computer, or None before its first page."""
    model = reading_model(settings)
    recent = store.vision_seconds(model, limit=12) if model else []
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
    if result.stopped and not result.pages:
        return "Stopped before any page was read."
    if not result.pages:
        return "No page could be read: " + "; ".join(result.failed[:3]) if result.failed else "No page needed reading."
    each = f" ({duration(result.seconds / result.pages)} a page)" if result.pages else ""
    text = f"Read {result.pages} page{'s' if result.pages != 1 else ''} with the vision model{each}."
    if result.failed:
        text += f" {len(result.failed)} couldn't be read: {'; '.join(result.failed[:2])}."
    if result.stopped:
        text += " Stopped as asked; the rest can be read later."
    return text + " Ask again to use what it read."


# The file page: both readings side by side ----------------------------------------------------


def side_by_side(rows: dict[int, dict]) -> list[dict]:
    """For the file page: each read page's first reading and the model's, with the figures they differ on marked."""
    from markupsafe import Markup, escape

    out = []
    for page, row in sorted(rows.items()):
        saved = _saved(row)
        comparison = Comparison.from_dict(saved)
        comparison.choice = shown_choice(
            comparison, page_text(row.get("model_text") or ""), model=row.get("model") or "", first_name=saved.get("first_name") or "OCR"
        )
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
    """Escaped text with each of ``shown`` (as written in it) marked, in one pass, longest first, never part of a
    longer figure ("100" in "100.25")."""
    from markupsafe import escape

    items = sorted({str(escape(item)) for item in shown if item}, key=len, reverse=True)
    if not items:
        return html
    pattern = re.compile(r"(?<![\w.,])(" + "|".join(map(re.escape, items)) + r")(?![\w]|[.,]\d)")
    return pattern.sub(r"<mark>\1</mark>", html)


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


LOCKED = "This email may be payment fraud, so its files aren't given to the model."


def offer(store: Store, settings: Settings, email: EmailRecord, att: AttachmentRecord) -> dict:
    """What the file's page and the chat show about reading the file with the vision model as well: the pages that
    would be read and how long that takes on this computer, or why it can't be done."""
    from controller_inbox import fraud

    reason = ""
    pages: list[int] = []
    if not readable_file(att.filename):
        reason = "Only PDFs and pictures can be read with the vision model."
    elif fraud.attachments_locked(email):
        reason = LOCKED
    elif settings.vision_mode == "off":
        reason = "Reading scans with the vision model is turned off in Setup."
    elif not can_render():
        reason = "The page renderer isn't installed. Double-click CloseDesk once (or run pip install -e .) to add it."
    elif not check_model(settings).reachable:
        reason = "No model server is answering. Start LM Studio's server to read scans both ways."
    elif settings.vision_model and not available(settings):
        reason = (
            f"The model chosen in Setup to read pages ({settings.vision_model}) isn't in LM Studio now, or can't look "
            "at pictures. Choose another one in Setup, or Automatic, to read scans both ways."
        )
    elif not available(settings):
        reason = (
            "No model in LM Studio can look at pictures. Download OvisOCR2 (a small model made for reading document "
            "pages) in LM Studio, or load one that can see (Qwen3.5, Gemma 3), to read scans both ways."
        )
    else:
        data = original_bytes(settings, email, att)
        if data is None:
            reason = "The original file wasn't kept, so its pages can't be looked at."
        else:
            pages = unread_pages(store, att, data, retry_failed=True, upgrade=trusted_reader(reading_model(settings)))
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
    told = offer(store, settings, email, att)
    if told["reason"]:
        return {"ok": False, "started": False, "message": told["reason"]}, 403 if told["reason"] == LOCKED else 409
    data = original_bytes(settings, email, att) or b""
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
        result = read_pages(
            store, settings, email, att, data, pages,
            on_progress=lambda i, k, note: progress("vision", i, k, note), should_stop=lambda: job.stopping,
        )
        return {
            "kind": "vision", "email_id": email.id, "n": n, "file": att.filename, "pages": result.pages, "failed": result.failed,
            "seconds": round(result.seconds), "href": href, "message": result_text(result),
        }

    if not job.start(run, about={"kind": "vision", "email_id": email.id, "n": n, "file": att.filename}):
        return {"ok": False, "started": False, "message": "CloseDesk is busy with another job. Try again when it finishes."}, 409
    seconds = estimate(store, settings, len(pages))
    return {"ok": True, "started": True, "pages": pages, "estimate": round(seconds) if seconds is not None else None, "message": offer_text(len(pages), seconds)}, 200


def readings_json(rows: dict[int, dict]) -> list[dict]:
    """For the workspace: each read page's two readings as data (no HTML), with the figures to mark in each."""
    out = []
    for page, row in sorted(rows.items()):
        saved = _saved(row)
        comparison = Comparison.from_dict(saved)
        comparison.choice = shown_choice(
            comparison, page_text(row.get("model_text") or ""), model=row.get("model") or "", first_name=saved.get("first_name") or "OCR"
        )
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
    lines = _html_tables_as_markdown(strip_thinking(markdown)).splitlines()
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
