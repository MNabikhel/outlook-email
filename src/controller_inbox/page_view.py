"""A file's page as a picture, with where each piece of its text was read marked on it.

The workspace shows a PDF page or a picture with a box over every line or cell CloseDesk read, so you can see what
was read where and how sure the reading is. A page with a text layer (a PDF saved from a program) is marked from
that text, which is exact. A scanned page or a picture is read with RapidOCR, which says how sure it is of each line
(0 to 1). When the vision model (OvisOCR2) has read the page too, each box also says what the model read there and
whether the two readings agree: a box with figures agrees when the model's reading has every one of its figures; a
box of words agrees when the model has a line close to it.

Drawing a page and reading a scan take a second or more, so both are kept under the data folder by the file's
SHA-256 and the page number, and opening the page again is instant.
"""

from __future__ import annotations

import bisect
import difflib
import hashlib
import io
import json
import os
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from controller_inbox import ocr, vision

if TYPE_CHECKING:
    from controller_inbox.config import Settings

# About 150 DPI, the long side at most 1,600 pixels: sharp on a laptop screen, quick to send.
VIEW = vision.Reader("the page view", dpi=150, max_side=1600)
# Raise when regions are worked out differently, so ones kept by an older version are read again.
VERSION = 1
# A page of tiny print can have thousands of runs of text; past this many boxes the page is unreadable anyway.
MAX_REGIONS = 1500
# Pages and readings kept; past this, the oldest are dropped.
KEEP_FILES = 600
# A box of words is the model's line when they are this alike (difflib's ratio), and agrees from AGREE on.
NEAR = 0.6
AGREE = 0.85


def folder(settings: Settings) -> Path:
    return settings.data_dir / "page_views"


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def page_count(data: bytes, filename: str) -> int:
    """How many pages the file has: a picture is one."""
    if Path(filename or "").suffix.lower() != ".pdf":
        return 1
    import pypdfium2 as pdfium

    with vision._PDFIUM:
        pdf = pdfium.PdfDocument(data)
        try:
            return len(pdf)
        finally:
            pdf.close()


def _kept(settings: Settings, name: str) -> bytes | None:
    try:
        return (folder(settings) / name).read_bytes()
    except OSError:
        return None


def _keep(settings: Settings, name: str, data: bytes) -> None:
    """Keep a drawn page or a reading; a data folder that can't be written to only makes the next open slower."""
    place = folder(settings)
    try:
        place.mkdir(parents=True, exist_ok=True)
        # Written whole under another name and then moved into place, so no one reads it half written.
        with tempfile.NamedTemporaryFile(dir=place, prefix=f".{name}.", suffix=".part", delete=False) as part:
            part.write(data)
        try:
            os.replace(part.name, place / name)
        except OSError:
            Path(part.name).unlink(missing_ok=True)
            raise
        files = sorted(place.iterdir(), key=lambda path: path.stat().st_mtime)
        for old in files[: max(0, len(files) - KEEP_FILES)]:
            old.unlink(missing_ok=True)
    except OSError:
        pass


def page_png(settings: Settings, data: bytes, filename: str, page: int) -> bytes:
    """The page as a PNG, drawn once and kept. ValueError when the file has no such page."""
    if page != 1 and Path(filename or "").suffix.lower() != ".pdf":
        raise ValueError(f"a picture has no page {page}")
    name = f"{file_hash(data)}-{page}.png"
    png = _kept(settings, name)
    if png is not None:
        from PIL import Image

        try:
            with Image.open(io.BytesIO(png)) as image:
                image.verify()
        except Exception:  # cut short (a crash while it was kept): drawn again
            png = None
    if png is None:
        png = vision.render(data, filename, page, reader=VIEW)
        _keep(settings, name, png)
    return png


def png_size(png: bytes) -> tuple[int, int]:
    """The drawn page's width and height in pixels."""
    from PIL import Image

    with Image.open(io.BytesIO(png)) as image:
        return image.width, image.height


# Where the text is ----------------------------------------------------------------------------


def _text_layer(data: bytes, page: int) -> list[dict] | None:
    """The PDF page's own text as boxes (fractions of the page), one per run of text on a line, or None when the
    page has no text to speak of (a scan)."""
    import pypdfium2 as pdfium

    with vision._PDFIUM:
        pdf = pdfium.PdfDocument(data)
        try:
            if not 1 <= page <= len(pdf):
                raise ValueError(f"the PDF has no page {page}")
            sheet = pdf[page - 1]
            try:
                left, bottom, right, top = sheet.get_cropbox()
                turn = sheet.get_rotation() % 360
                textpage = sheet.get_textpage()
                try:
                    if len(textpage.get_text_range().split()) < 5:
                        return None
                    runs = []
                    for index in range(min(textpage.count_rects(), MAX_REGIONS * 2)):
                        box = textpage.get_rect(index)
                        text = textpage.get_text_bounded(*box)
                        if text.strip():
                            runs.append((box, text))
                finally:
                    textpage.close()
            finally:
                sheet.close()
        finally:
            pdf.close()
    width, height = max(right - left, 1.0), max(top - bottom, 1.0)
    out = []
    for (x0, y0, x1, y1), text in _joined(runs):
        # Fractions of the page as stored (from its top left), then turned the way the page is shown.
        corners = [((x - left) / width, (top - y) / height) for x, y in ((x0, y1), (x1, y0))]
        corners = [_turned(fx, fy, turn) for fx, fy in corners]
        xs, ys = [fx for fx, _ in corners], [fy for _, fy in corners]
        out.append(_region(min(xs), min(ys), max(xs), max(ys), text, None))
    return out[:MAX_REGIONS]


def _turned(fx: float, fy: float, turn: int) -> tuple[float, float]:
    """A point on the page as stored, where it lands once the page is turned (``/Rotate``, clockwise)."""
    if turn == 90:
        return 1 - fy, fx
    if turn == 180:
        return 1 - fx, 1 - fy
    if turn == 270:
        return fy, 1 - fx
    return fx, fy


def _joined(runs: list[tuple[tuple[float, float, float, float], str]]) -> list[tuple[tuple[float, float, float, float], str]]:
    """Runs of text on one line close enough to be one phrase (a word in another font, letters placed one by one)
    joined; runs a column gap apart stay separate, so a table's cells are marked one by one."""
    out: list[list] = []
    for (x0, y0, x1, y1), text in sorted(runs, key=lambda run: (-round(run[0][3]), run[0][0])):
        size = max(y1 - y0, 1.0)
        if out:
            (a0, b0, a1, b1), before = out[-1]
            same_line = min(y1, b1) - max(y0, b0) >= 0.5 * min(size, b1 - b0)
            if same_line and 0 <= x0 - a1 <= 0.9 * size:
                out[-1] = [(a0, min(b0, y0), x1, max(b1, y1)), f"{before.rstrip()} {text.strip()}"]
                continue
        out.append([(x0, y0, x1, y1), text])
    return [(box, " ".join(text.split())) for box, text in out]


def _region(x0: float, y0: float, x1: float, y1: float, text: str, confidence: float | None) -> dict:
    x0, y0, x1, y1 = (min(max(value, 0.0), 1.0) for value in (x0, y0, x1, y1))
    return {
        "x": round(x0, 5),
        "y": round(y0, 5),
        "w": round(max(x1 - x0, 0.0), 5),
        "h": round(max(y1 - y0, 0.0), 5),
        "text": text,
        "confidence": None if confidence is None else round(confidence, 3),
    }


def _ocr_regions(png: bytes) -> list[dict] | None:
    """The scanned page read with RapidOCR, as boxes with how sure it is of each; None without RapidOCR."""
    lines = ocr.line_boxes(png)
    if lines is None:
        return None
    width, height = (max(side, 1) for side in png_size(png))
    return [
        _region(line["left"] / width, line["top"] / height, line["right"] / width, line["bottom"] / height, line["text"], line["score"])
        for line in lines[:MAX_REGIONS]
    ]


def regions(settings: Settings, data: bytes, filename: str, page: int) -> dict:
    """What was read where on the page: {source: "text" (the PDF's own text) or "ocr", regions, reason}. Kept, so
    a page is only read once. ValueError when the file has no such page."""
    name = f"{file_hash(data)}-{page}.json"
    kept = _kept(settings, name)
    if kept is not None:
        try:
            saved = json.loads(kept)
            if saved.get("version") == VERSION:
                return saved
        except ValueError:
            pass
    found = _text_layer(data, page) if Path(filename or "").suffix.lower() == ".pdf" else None
    if found is not None:
        result = {"source": "text", "regions": found, "reason": ""}
    else:
        boxes = _ocr_regions(page_png(settings, data, filename, page))
        if boxes is None:
            # Not kept: once RapidOCR is installed, the page is read.
            return {
                "version": VERSION,
                "source": "ocr",
                "regions": [],
                "reason": "RapidOCR isn't installed, so CloseDesk can't show where it read this scan. "
                "Double-click CloseDesk once to install it.",
            }
        result = {"source": "ocr", "regions": boxes, "reason": "" if boxes else "OCR found no text on this page."}
    result["version"] = VERSION
    _keep(settings, name, json.dumps(result).encode())
    return result


# What the vision model read there ------------------------------------------------------------


def _plain(text: str) -> str:
    """Text to compare: OCR often drops the spaces between words ("Billto"), so they don't count, nor do case,
    currency signs or table rules."""
    return re.sub(r"[\s|$€£*_]", "", (text or "").lower())


def _model_lines(model_text: str) -> tuple[list[str], list[str]]:
    """The model's reading as lines (a paragraph line, a table row with its cells joined) and as pieces: single
    cells, and a form's label and value ("Due date: 10/30/2026"), which the page prints apart."""
    lines, cells = [], []
    for block in vision.markdown_blocks(model_text or ""):
        if block["kind"] == "text":
            lines.append(block["text"])
            continue
        # A table printed without headings gets made-up ones ("Column 2"), which aren't on the page.
        header = [] if all(not cell or re.fullmatch(r"Column \d+", cell) for cell in block["header"]) else block["header"]
        for row in [header, *block["rows"]]:
            filled = [cell.strip() for cell in row if cell and cell.strip()]
            if filled:
                lines.append(" | ".join(filled))
                cells += filled
    cells += [piece.strip() for line in lines if ": " in line for piece in line.split(": ") if piece.strip()]
    return lines, cells


def _best(text: str, choices: list[str], floor: float) -> tuple[str | None, float]:
    """The choice most like the text, and how alike (0 to 1), when at least ``floor``."""
    plain = _plain(text)
    best, score = None, floor
    for choice in choices:
        other = _plain(choice)
        matcher = difflib.SequenceMatcher(None, plain, other, autojunk=False)
        if matcher.real_quick_ratio() < score or matcher.quick_ratio() < score:
            continue
        ratio = matcher.ratio()
        if ratio >= score:
            best, score = choice, ratio
    return best, (score if best is not None else 0.0)


def _rows(found: list[dict]) -> list[str]:
    """For each box, the text of every box on its line, left to right: what to look for when a figure differs."""
    order = sorted(found, key=lambda region: region["y"])
    tops = [region["y"] for region in order]
    tallest = max((region["h"] for region in found), default=0.0)
    out = []
    for region in found:
        middle = region["y"] + region["h"] / 2
        start = bisect.bisect_left(tops, region["y"] - tallest)
        end = bisect.bisect_right(tops, region["y"] + region["h"])
        line = sorted(
            (other for other in order[start:end] if other["y"] <= middle <= other["y"] + other["h"] or region["y"] <= other["y"] + other["h"] / 2 <= region["y"] + region["h"]),
            key=lambda other: other["x"],
        )
        out.append(" ".join(other["text"] for other in line))
    return out


def with_model(found: list[dict], model_text: str) -> list[dict]:
    """Each box with what the vision model read there: {text, agrees}. ``agrees`` is True when the model's reading
    has the box's figures (or a line close to its words), False when it has a figure the box doesn't, None when
    it can't be told."""
    lines, cells = _model_lines(model_text)
    # By size only: a phone number's "(509)" reads as a negative figure where its closing bracket touches the digits.
    known = {abs(value) for value in vision.figures("\n".join(lines))}
    line_figures = [{abs(value) for value in vision.figures(line)} for line in lines]
    out = []
    for region, row in zip(found, _rows(found)):
        figures = {abs(value) for value in vision.figures(region["text"])}
        if figures:
            agrees = figures <= known
            # The model's line holding most of these figures; else, for a figure it read differently, the line
            # most like this one.
            holding = [(len(figures & mine), line) for line, mine in zip(lines, line_figures) if figures & mine]
            if holding:
                most = max(count for count, _line in holding)
                text, _ = _best(row, [line for count, line in holding if count == most], 0.0)
            else:
                text, _ = _best(row, [line for line, mine in zip(lines, line_figures) if mine], 0.45)
        else:
            text, likeness = _best(region["text"], cells + lines, NEAR)
            agrees = True if likeness >= AGREE else None
        out.append({**region, "model": {"text": text, "agrees": agrees}})
    return out
