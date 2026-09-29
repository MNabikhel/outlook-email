"""Read attachments into text a small model can navigate, and let it go back for more.

Every document becomes plain text with section markers:

    [page 3]                      PDF pages
    [sheet "Budget" A1:F40]       workbook sheets, one line per row, cells named A5, B5 …
    [slide 2]                     PowerPoint slides
    [part 4]                      long text without natural sections

Rows keep their cell references, and a formula is shown next to its value
(``C7: 45,000 (=SUM(C2:C6))``), so the model can follow a workbook's logic.
``split_parts`` cuts that text back into sections, ``search_parts`` finds the
ones that match a question, and ``read_cells`` / ``trace_cell`` open the
original workbook for exact ranges and formula precedents.
"""

from __future__ import annotations

import csv
import importlib.util
import io
import logging
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time

# pypdf warns on the small defects many real PDFs have ("EOF marker not found") and still
# reads them; with no logging set up, those warnings land in the user's terminal.
logging.getLogger("pypdf").setLevel(logging.ERROR)

MAX_TEXT = 400_000
MAX_PDF_PAGES = 300
MAX_SHEETS = 40
MAX_ROWS = 1_000
MAX_COLUMNS = 60
PART_SIZE = 1_800

SPREADSHEET_SUFFIXES = (".xlsx", ".xlsm", ".xltx", ".xltm")
MARKER_RE = re.compile(r"^\[(page \d+|sheet \"[^\"\n]*\"[^\]\n]*|slide \d+|part \d+)\]\s*$", re.M)


@dataclass
class Part:
    label: str
    text: str


def extract_document(filename: str, content_type: str, data: bytes) -> str:
    """Structured text for one attachment, or '' when the file type has no text to read."""
    name = (filename or "").lower()
    ctype = (content_type or "").lower()
    if name.endswith(".pdf") or "pdf" in ctype:
        return _cap(pdf_text(data))
    if name.endswith(".docx") or "wordprocessingml" in ctype:
        return _cap(docx_text(data))
    if name.endswith(SPREADSHEET_SUFFIXES) or "spreadsheetml" in ctype:
        return _cap(xlsx_text(data))
    if name.endswith(".xls") or ctype == "application/vnd.ms-excel":
        return _cap(xls_text(data))
    if name.endswith(".pptx") or "presentationml" in ctype:
        return _cap(pptx_text(data))
    if name.endswith((".csv", ".tsv")) or ctype in {"text/csv", "application/csv", "text/tab-separated-values"}:
        return _cap(csv_text(data, filename or "table.csv"))
    return ""


def _cap(text: str) -> str:
    if len(text) <= MAX_TEXT:
        return text
    return text[:MAX_TEXT].rsplit("\n", 1)[0] + "\n[CloseDesk stopped reading here: the file is very long.]"


# PDF -----------------------------------------------------------------------------------------


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            if not reader.decrypt(""):
                return "[This PDF is password-protected, so CloseDesk can't read it.]"
        except Exception:
            return "[This PDF is password-protected, so CloseDesk can't read it.]"
    pages = []
    words = 0
    total = len(reader.pages)
    for number, page in enumerate(reader.pages[:MAX_PDF_PAGES], start=1):
        text = _pdf_page(page)
        if not text.strip():
            text = _ocr_page_images(page)
        words += len(text.split())
        pages.append(f"[page {number}]\n{text.strip() or '(no text on this page)'}")
    if total > MAX_PDF_PAGES:
        pages.append(f"[CloseDesk read the first {MAX_PDF_PAGES} of {total} pages.]")
    if total and words < 5 * total:
        pages.insert(0, _SCANNED_NOTE)
    return "\n\n".join(pages)


_SCANNED_NOTE = (
    "[This PDF looks scanned: its pages are pictures with little or no text layer. "
    "Text from it may be missing; open the file to read it.]"
)


def _pdf_page(page) -> str:
    try:
        text = page.extract_text(extraction_mode="layout")
    except Exception:
        text = ""
    if not text.strip():
        try:
            return page.extract_text() or ""
        except Exception:
            return ""
    lines = []
    for line in text.splitlines():
        line = re.sub(r" {3,}", " | ", line.rstrip()).strip(" |")
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _ocr_page_images(page) -> str:
    """OCR a scanned page's pictures when Tesseract is installed; otherwise nothing."""
    if importlib.util.find_spec("pytesseract") is None:
        return ""
    from controller_inbox.extract import _image_text

    texts = []
    try:
        for image in page.images[:4]:
            texts.append(_image_text(image.data))
    except Exception:
        return ""
    return "\n".join(t for t in texts if t)


# Word ----------------------------------------------------------------------------------------

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_text(data: bytes) -> str:
    """Paragraphs and tables in reading order, with headings, tracked insertions, and comments."""
    from docx import Document

    document = Document(io.BytesIO(data))
    lines: list[str] = []
    for block in document.element.body.iterchildren():
        if block.tag == f"{_W}p":
            line = _docx_paragraph(block)
            if line:
                lines.append(line)
        elif block.tag == f"{_W}tbl":
            lines.extend(_docx_table(block))
            lines.append("")
    comments = _docx_comments(document)
    if comments:
        lines += ["", "Comments in the document:"] + [f"- {item}" for item in comments]
    if any("[deleted:" in line for line in lines):
        lines.insert(0, "[This draft has tracked changes: inserted text is shown, deletions are marked [deleted: …].]")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _docx_paragraph(element) -> str:
    parts = []
    for node in element.iter():
        if node.tag == f"{_W}t" and node.text:
            parts.append(node.text)
        elif node.tag == f"{_W}delText" and node.text:
            parts.append(f"[deleted: {node.text}]")
        elif node.tag == f"{_W}tab":
            parts.append("\t")
        elif node.tag in {f"{_W}br", f"{_W}cr"}:
            parts.append("\n")
    text = "".join(parts).strip()
    if not text:
        return ""
    style = element.find(f"{_W}pPr/{_W}pStyle")
    name = (style.get(f"{_W}val") if style is not None else "") or ""
    level = re.match(r"(?i)heading\s*(\d)", name) or re.match(r"(?i)^title$", name)
    if level:
        depth = int(level.group(1)) if level.groups() else 1
        return "#" * min(depth, 4) + " " + text
    if element.find(f"{_W}pPr/{_W}numPr") is not None or re.match(r"(?i)list", name):
        return "- " + text
    return text


def _docx_table(element) -> list[str]:
    rows = []
    for row in element.iter(f"{_W}tr"):
        cells = []
        for cell in row.iter(f"{_W}tc"):
            text = " ".join(_docx_paragraph(p) for p in cell.iter(f"{_W}p")).strip()
            cells.append(text)
        if any(cells):
            rows.append("| " + " | ".join(cells) + " |")
    return rows


def _docx_comments(document) -> list[str]:
    try:
        comments = list(document.comments)
    except Exception:
        return []
    out = []
    for comment in comments[:50]:
        text = " ".join(p.text for p in comment.paragraphs).strip()
        if text:
            who = (getattr(comment, "author", "") or "").strip()
            out.append(f"{who}: {text}" if who else text)
    return out


# Workbooks -----------------------------------------------------------------------------------


def xlsx_text(data: bytes) -> str:
    from openpyxl import load_workbook

    values = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    formulas = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
    lines: list[str] = []
    try:
        names = _defined_names(formulas)
        sheets = formulas.worksheets
        if len(sheets) > MAX_SHEETS:
            lines.append(f"[The workbook has {len(sheets)} sheets; CloseDesk read the first {MAX_SHEETS}.]")
        summary = ", ".join(f'"{ws.title}"' + (" (hidden)" if ws.sheet_state != "visible" else "") for ws in sheets)
        lines.append(f"Workbook with {len(sheets)} sheet{'s' if len(sheets) != 1 else ''}: {summary}")
        if names:
            lines.append("Named ranges: " + "; ".join(f"{k} = {v}" for k, v in names[:30]))
        for ws in sheets[:MAX_SHEETS]:
            lines.append("")
            lines.extend(_sheet_lines(ws, values[ws.title]))
    finally:
        values.close()
        formulas.close()
    return "\n".join(lines).strip()


def _defined_names(book) -> list[tuple[str, str]]:
    try:
        return [(name, str(item.attr_text)) for name, item in book.defined_names.items()]
    except Exception:
        return []


def _sheet_lines(formula_sheet, value_sheet) -> list[str]:
    from openpyxl.utils import get_column_letter

    rows = []
    more = 0
    formula_count = 0
    last_col = 0
    for index, (frow, vrow) in enumerate(
        zip(
            formula_sheet.iter_rows(max_col=MAX_COLUMNS),
            value_sheet.iter_rows(max_col=MAX_COLUMNS, values_only=True),
        )
    ):
        cells = []
        for column, cell in enumerate(frow):
            raw = getattr(cell, "value", None)
            value = vrow[column] if column < len(vrow) else None
            if raw is None and value is None:
                continue
            ref = f"{get_column_letter(column + 1)}{getattr(cell, 'row', index + 1)}"
            last_col = max(last_col, column + 1)
            if isinstance(raw, str) and raw.startswith("="):
                formula_count += 1
                shown = f"{_fmt(value)} ({raw})" if value is not None else raw
            else:
                shown = _fmt(value if value is not None else raw)
            cells.append(f"{ref}: {shown}")
        if not cells:
            continue
        if len(rows) >= MAX_ROWS:
            more += 1
            continue
        rows.append(" | ".join(cells))
    extent = ""
    try:
        extent = formula_sheet.calculate_dimension()
    except Exception:
        extent = f"A1:{get_column_letter(max(last_col, 1))}{len(rows)}"
    hidden = " hidden" if formula_sheet.sheet_state != "visible" else ""
    head = f'[sheet "{formula_sheet.title}" {extent}{hidden}]'
    note = f"({len(rows) + more} row{'s' if len(rows) + more != 1 else ''} with data" + (
        f", {formula_count} formula{'s' if formula_count != 1 else ''}" if formula_count else ""
    ) + ")"
    out = [head, note, *rows]
    if more:
        out.append(f"[{more} more rows not shown; ask for a range such as rows {MAX_ROWS + 1}–{MAX_ROWS + 200}.]")
    return out


def xls_text(data: bytes) -> str:
    import xlrd
    from openpyxl.utils import get_column_letter

    book = xlrd.open_workbook(file_contents=data)
    lines = [f"Workbook with {book.nsheets} sheet{'s' if book.nsheets != 1 else ''}: " + ", ".join(f'"{s.name}"' for s in book.sheets())]
    for sheet in book.sheets()[:MAX_SHEETS]:
        lines.append("")
        last = get_column_letter(max(1, min(sheet.ncols, MAX_COLUMNS)))
        lines.append(f'[sheet "{sheet.name}" A1:{last}{max(sheet.nrows, 1)}]')
        shown = 0
        for r in range(sheet.nrows):
            cells = []
            for c in range(min(sheet.ncols, MAX_COLUMNS)):
                cell = sheet.cell(r, c)
                if cell.value in ("", None):
                    continue
                value = cell.value
                if cell.ctype == xlrd.XL_CELL_DATE:
                    try:
                        value = xlrd.xldate.xldate_as_datetime(cell.value, book.datemode)
                    except Exception:
                        pass
                cells.append(f"{get_column_letter(c + 1)}{r + 1}: {_fmt(value)}")
            if cells:
                shown += 1
                if shown > MAX_ROWS:
                    lines.append(f"[{sheet.nrows - r} more rows not shown.]")
                    break
                lines.append(" | ".join(cells))
    return "\n".join(lines).strip()


def csv_text(data: bytes, filename: str) -> str:
    from openpyxl.utils import get_column_letter

    text = _decode(data)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel_tab if filename.lower().endswith(".tsv") else csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    width = max((len(r) for r in rows), default=1)
    lines = [f'[sheet "{filename}" A1:{get_column_letter(max(1, min(width, MAX_COLUMNS)))}{max(len(rows), 1)}]']
    for number, row in enumerate(rows[:MAX_ROWS], start=1):
        cells = [f"{get_column_letter(c + 1)}{number}: {v.strip()}" for c, v in enumerate(row[:MAX_COLUMNS]) if v.strip()]
        if cells:
            lines.append(" | ".join(cells))
    if len(rows) > MAX_ROWS:
        lines.append(f"[{len(rows) - MAX_ROWS} more rows not shown.]")
    return "\n".join(lines)


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time(0) else value.isoformat(sep=" ", timespec="minutes")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return str(value)
        if value.is_integer():
            return f"{int(value):,}"
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return f"{value:,}"
    return re.sub(r"\s+", " ", str(value)).strip()


# PowerPoint ----------------------------------------------------------------------------------


def pptx_text(data: bytes) -> str:
    from pptx import Presentation

    deck = Presentation(io.BytesIO(data))
    parts: list[str] = []
    for index, slide in enumerate(deck.slides, start=1):
        if index > 150:
            parts.append("[More slides not shown.]")
            break
        lines = [f"[slide {index}]"]
        title = slide.shapes.title.text.strip() if slide.shapes.title is not None and slide.shapes.title.has_text_frame else ""
        if title:
            lines.append(f"# {title}")
        for shape in slide.shapes:
            if shape == slide.shapes.title:
                continue
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    lines.append("| " + " | ".join(cell.text.strip() for cell in row.cells) + " |")
            elif getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                text = shape.text_frame.text.strip()
                if text:
                    lines.append(text)
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip() if slide.notes_slide.notes_text_frame else ""
            if notes:
                lines.append(f"Speaker notes: {notes}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


# Navigating the text -------------------------------------------------------------------------


def split_parts(text: str, *, size: int = PART_SIZE) -> list[Part]:
    """The document's sections, each no longer than ``size`` characters."""
    text = text or ""
    marks = list(MARKER_RE.finditer(text))
    sections: list[tuple[str, str]] = []
    if not marks:
        sections.append(("", text))
    else:
        head = text[: marks[0].start()].strip()
        if head:
            sections.append(("start", head))
        for i, mark in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            sections.append((mark.group(1), text[mark.end() : end].strip()))
    parts: list[Part] = []
    for label, body in sections:
        pieces = _pieces(body, size)
        for n, piece in enumerate(pieces, start=1):
            name = label or f"part {len(parts) + 1}"
            if len(pieces) > 1:
                name = _sub_label(label, piece, n) if label else f"part {len(parts) + 1}"
            parts.append(Part(name, piece))
    return [p for p in parts if p.text.strip()] or [Part("part 1", "")]


def _pieces(body: str, size: int) -> list[str]:
    if len(body) <= size:
        return [body]
    out, current = [], []
    length = 0
    for line in body.split("\n"):
        while len(line) > size:
            if current:
                out.append("\n".join(current))
                current, length = [], 0
            cut = line[:size].rfind(" ")
            cut = cut if cut > size // 2 else size
            out.append(line[:cut])
            line = line[cut:].lstrip()
        if length + len(line) + 1 > size and current:
            out.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += len(line) + 1
    if current:
        out.append("\n".join(current))
    return out


def _sub_label(label: str, piece: str, n: int) -> str:
    if label.startswith("sheet"):
        name = re.match(r'sheet "([^"]*)"', label)
        refs = re.findall(r"(?m)^[A-Z]{1,3}(\d+):", piece)
        if name and refs:
            return f'sheet "{name.group(1)}" rows {refs[0]}–{refs[-1]}'
    return f"{label} (cont. {n})"


def outline(text: str, *, limit: int = 40) -> list[str]:
    """One line per section: its label and how it starts."""
    rows = []
    for part in split_parts(text):
        first = next((line.strip() for line in part.text.splitlines() if line.strip() and not line.startswith("(")), "")
        rows.append(f"{part.label}: {first[:90]}")
    if len(rows) > limit:
        rows = rows[:limit] + [f"… {len(rows) - limit} more sections"]
    return rows


_TERM = re.compile(r"[A-Za-z0-9][A-Za-z0-9.$%&'-]*[A-Za-z0-9%]|[A-Za-z0-9]")


def terms_of(query: str, stop: set[str] | frozenset[str] = frozenset()) -> list[str]:
    words = []
    for match in _TERM.findall(query or ""):
        word = match.lower().strip(".'-")
        if (len(word) < 3 and not word.isdigit()) or word in stop:
            continue
        words.append(word)
    return list(dict.fromkeys(words))


def search_parts(text: str, query: str, *, limit: int = 3, stop: set[str] | frozenset[str] = frozenset()) -> list[Part]:
    """The sections that best match ``query`` (rarer words count more)."""
    parts = split_parts(text)
    words = terms_of(query, stop)
    if not words:
        return []
    lowered = [p.text.lower() + "\n" + p.label.lower() for p in parts]
    scored = []
    for index, body in enumerate(lowered):
        score = 0.0
        for word in words:
            hits = body.count(word)
            if hits:
                spread = sum(1 for other in lowered if word in other)
                score += (1 + math.log(hits)) * math.log(1 + len(parts) / spread)
        if score:
            scored.append((score, -index, parts[index]))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in scored[:limit]]


def read_part(text: str, label: str) -> Part | None:
    """A section by its label ("page 3", "slide 2", "Budget", "part 4"), matched loosely."""
    parts = split_parts(text)
    wanted = re.sub(r"\s+", " ", (label or "").strip().lower().strip('"'))
    if not wanted:
        return parts[0] if parts else None
    for part in parts:
        if part.label.lower() == wanted:
            return part
    number = re.fullmatch(r"(?:(page|slide|part)\s*)?(\d+)", wanted)
    if number:
        kind = number.group(1)
        for part in parts:
            if re.match(rf"(?:{kind or 'page|slide|part'}) {number.group(2)}\b", part.label.lower()):
                return part
        index = int(number.group(2)) - 1
        if not kind and 0 <= index < len(parts):
            return parts[index]
    for part in parts:
        if wanted in part.label.lower():
            return part
    return None


# Exact cells from the original workbook ------------------------------------------------------

_FORMULA_REF = re.compile(
    r"(?:'((?:[^']|'')+)'!|([A-Za-z0-9_.]+)!)?\$?([A-Z]{1,3})\$?(\d+)(?::\$?([A-Z]{1,3})\$?(\d+))?"
)


def read_cells(data: bytes, sheet: str, cells: str, *, limit: int = 200) -> str:
    """Values and formulas for a range such as ``B2:D20`` on ``sheet`` (name or 1-based number)."""
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter, range_boundaries

    values = load_workbook(io.BytesIO(data), data_only=True)
    formulas = load_workbook(io.BytesIO(data), data_only=False)
    try:
        ws = _find_sheet(formulas, sheet)
        if ws is None:
            return f"No sheet called {sheet!r}. Sheets: " + ", ".join(f'"{s}"' for s in formulas.sheetnames)
        vs = values[ws.title]
        try:
            min_col, min_row, max_col, max_row = range_boundaries((cells or "A1:J40").replace("$", "").upper())
        except (ValueError, TypeError):
            return f"{cells!r} isn't a cell range. Use something like A1:F20."
        min_col, min_row = min_col or 1, min_row or 1
        max_col = min(max_col or ws.max_column, min_col + MAX_COLUMNS - 1)
        max_row = min(max_row or ws.max_row, ws.max_row)
        lines = [f'Sheet "{ws.title}" {get_column_letter(min_col)}{min_row}:{get_column_letter(max_col)}{max_row}']
        shown = 0
        for r in range(min_row, max_row + 1):
            row = []
            for c in range(min_col, max_col + 1):
                raw = ws.cell(r, c).value
                value = vs.cell(r, c).value
                if raw is None and value is None:
                    continue
                ref = f"{get_column_letter(c)}{r}"
                if isinstance(raw, str) and raw.startswith("="):
                    row.append(f"{ref}: {_fmt(value)} ({raw})" if value is not None else f"{ref}: {raw}")
                else:
                    row.append(f"{ref}: {_fmt(value if value is not None else raw)}")
            if row:
                lines.append(" | ".join(row))
                shown += 1
                if shown >= limit:
                    lines.append(f"[Stopped at row {r}; ask for the next range.]")
                    break
        if shown == 0:
            lines.append("(these cells are empty)")
        return "\n".join(lines)
    finally:
        values.close()
        formulas.close()


def trace_cell(data: bytes, sheet: str, cell: str, *, depth: int = 2) -> str:
    """A cell's formula and the cells it depends on, ``depth`` levels down."""
    from openpyxl import load_workbook

    values = load_workbook(io.BytesIO(data), data_only=True)
    formulas = load_workbook(io.BytesIO(data), data_only=False)
    try:
        ws = _find_sheet(formulas, sheet)
        if ws is None:
            return f"No sheet called {sheet!r}. Sheets: " + ", ".join(f'"{s}"' for s in formulas.sheetnames)
        ref = (cell or "").replace("$", "").upper().strip()
        if not re.fullmatch(r"[A-Z]{1,3}\d+", ref):
            return f"{cell!r} isn't a single cell. Use something like C12."
        lines: list[str] = []
        _trace(formulas, values, ws.title, ref, depth, 0, lines, set())
        return "\n".join(lines)
    finally:
        values.close()
        formulas.close()


def _trace(formulas, values, sheet: str, ref: str, depth: int, level: int, lines: list[str], seen: set) -> None:
    from openpyxl.utils import range_boundaries, get_column_letter

    key = (sheet, ref)
    if key in seen or len(lines) > 80:
        return
    seen.add(key)
    raw = formulas[sheet][ref].value
    value = values[sheet][ref].value
    indent = "  " * level
    where = f"{sheet}!{ref}"
    if isinstance(raw, str) and raw.startswith("="):
        shown = f" = {_fmt(value)}" if value is not None else ""
        lines.append(f"{indent}{where}{shown} ← {raw}")
        if level >= depth:
            return
        for match in _FORMULA_REF.finditer(raw[1:]):
            target = (match.group(1) or match.group(2) or sheet).replace("''", "'")
            if target not in formulas.sheetnames:
                continue
            start = f"{match.group(3)}{match.group(4)}"
            if match.group(5):
                end = f"{match.group(5)}{match.group(6)}"
                min_col, min_row, max_col, max_row = range_boundaries(f"{start}:{end}")
                cells = [
                    f"{get_column_letter(c)}{r}"
                    for r in range(min_row, max_row + 1)
                    for c in range(min_col, max_col + 1)
                ]
                if len(cells) > 12:
                    total = [values[target][c].value for c in cells]
                    numbers = [v for v in total if isinstance(v, (int, float))]
                    lines.append(
                        f"{indent}  {target}!{start}:{end}: {len(cells)} cells"
                        + (f", {len(numbers)} numbers adding to {_fmt(sum(numbers))}" if numbers else "")
                    )
                    continue
                for c in cells:
                    _trace(formulas, values, target, c, depth, level + 1, lines, seen)
            else:
                _trace(formulas, values, target, start, depth, level + 1, lines, seen)
    else:
        lines.append(f"{indent}{where} = {_fmt(value if value is not None else raw) or '(empty)'}")


def _find_sheet(book, sheet: str):
    name = (sheet or "").strip().strip("'\"")
    if not name:
        return book.worksheets[0]
    for ws in book.worksheets:
        if ws.title.lower() == name.lower():
            return ws
    if name.isdigit() and 1 <= int(name) <= len(book.worksheets):
        return book.worksheets[int(name) - 1]
    for ws in book.worksheets:
        if name.lower() in ws.title.lower():
            return ws
    return None
