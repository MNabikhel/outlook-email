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
import io
import logging
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time

from controller_inbox import ocr, pdf_layout, tables

# pypdf and pdfminer warn on the small defects many real PDFs have ("EOF marker not found") and
# still read them; with no logging set up, those warnings land in the user's terminal.
logging.getLogger("pypdf").setLevel(logging.ERROR)
logging.getLogger("pdfminer").setLevel(logging.ERROR)

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
    scanned = []
    total = len(reader.pages)
    layouts = _layouts(data)
    previous: list[pdf_layout.Table] = []
    for number, page in enumerate(reader.pages[:MAX_PDF_PAGES], start=1):
        layout = next(layouts, None)
        text = ""
        if layout is not None:
            laid_out = pdf_layout.page_text(pdf_layout.glyphs_of(layout), previous)
            text, previous = laid_out.text, laid_out.tables
        if not text.strip():
            text = _pdf_page(page)
        if not text.strip() and len(scanned) < MAX_OCR_PAGES:
            scanned.append(number)
            text = _ocr_page_images(page)
        words += len(text.split())
        pages.append(f"[page {number}]\n{text.strip() or '(no text on this page)'}")
    if total > MAX_PDF_PAGES:
        pages.append(f"[CloseDesk read the first {MAX_PDF_PAGES} of {total} pages.]")
    if total and words < 5 * total:
        pages.insert(0, _SCANNED_NOTE if ocr.engine_name() else _SCANNED_NOTE + _OCR_HINT)
    elif scanned:
        pages.insert(0, f"[Scanned {'page' if len(scanned) == 1 else 'pages'} {_page_list(scanned)} read with OCR: check figures against the file.]")
    return "\n\n".join(pages)


def _page_list(numbers: list[int]) -> str:
    return ", ".join(map(str, numbers[:8])) + (f" and {len(numbers) - 8} more" if len(numbers) > 8 else "")


_SCANNED_NOTE = (
    "[This PDF looks scanned: its pages are pictures with little or no text layer. "
    "Text from it may be missing; open the file to read it.]"
)
_OCR_HINT = ' [To read scanned pages, install the OCR add-on: pip install -e ".[ocr]"]'
MAX_OCR_PAGES = 40


def _layouts(data: bytes):
    """pdfminer's pages, with every character's position; nothing once pdfminer can't go on."""
    try:
        from pdfminer.high_level import extract_pages

        yield from extract_pages(io.BytesIO(data), laparams=None, maxpages=MAX_PDF_PAGES)
    except Exception:
        return


def _pdf_page(page) -> str:
    """pypdf's text for a page, for the files pdfminer can't lay out."""
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
    """OCR a scanned page's pictures when an OCR engine is installed; otherwise nothing."""
    texts = []
    try:
        for image in page.images[:4]:
            texts.append(ocr.image_text(image.data))
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
    """Rows through ``tables``: a cell spanning columns is one cell, a vertically merged cell repeats
    the value above it, and the header row is the one Word marks as repeating, or a bold first row."""
    grid: list[list[str | None]] = []
    marked = False
    bold: list[bool] = []
    for number, row in enumerate(element.findall(f"{_W}tr")):
        if number == 0:
            marked = row.find(f"{_W}trPr/{_W}tblHeader") is not None
        cells: list[str | None] = []
        for cell in _docx_cells(row):
            text = " ".join(filter(None, (_docx_paragraph(p) for p in cell.iter(f"{_W}p")))).strip()
            properties = cell.find(f"{_W}tcPr")
            span = properties.find(f"{_W}gridSpan") if properties is not None else None
            merge = properties.find(f"{_W}vMerge") if properties is not None else None
            if merge is not None and merge.get(f"{_W}val", "continue") == "continue":
                above = grid[-1] if grid else []
                text = next((c for c in reversed(above[: len(cells) + 1]) if c is not None), "") if len(cells) < len(above) else ""
            cells.append(text)
            if span is not None and (span.get(f"{_W}val") or "1").isdigit():
                cells.extend([None] * (int(span.get(f"{_W}val")) - 1))
        grid.append(cells)
        if number == 0:
            runs = [r for r in row.iter(f"{_W}r") if "".join(t.text or "" for t in r.iter(f"{_W}t")).strip()]
            bold.append(bool(runs) and all(_docx_bold(r) for r in runs))
    header = tables.has_header(grid, marked=marked, bold_first=bool(bold and bold[0]))
    return tables.table_lines(grid, header=header)


def _docx_cells(row):
    """A row's own cells, including those inside content controls, not those of a table nested in one."""
    for child in row:
        if child.tag == f"{_W}tc":
            yield child
        elif child.tag == f"{_W}sdt":
            content = child.find(f"{_W}sdtContent")
            if content is not None:
                yield from _docx_cells(content)


def _docx_bold(run) -> bool:
    mark = run.find(f"{_W}rPr/{_W}b")
    return mark is not None and mark.get(f"{_W}val", "true") not in ("0", "false", "off")


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


_HEADER_SEARCH = 10


@dataclass
class SheetRow:
    number: int
    cells: dict[int, str]
    bold: bool = False


def sheet_row_lines(rows: list[SheetRow]) -> list[str]:
    """``A5: value | C5: value`` for each row. Under a header row each cell also names its column,
    ``C5 (Department): Finance``, and a blank between filled cells is written ``C5 (Department): (blank)``,
    so a row about one person or account reads on its own."""
    from openpyxl.utils import get_column_letter

    header = _sheet_header(rows)
    labels = {c: v.replace("|", "/") for c, v in rows[header].cells.items()} if header is not None else {}
    out = []
    for index, row in enumerate(rows):
        named = header is not None and index > header
        columns = sorted(row.cells)
        if named and len(columns) >= 2:
            columns = sorted(set(columns) | {c for c in labels if columns[0] < c < columns[-1]})
        cells = []
        for column in columns:
            ref = f"{get_column_letter(column)}{row.number}"
            label = f" ({labels[column]})" if named and column in labels else ""
            cells.append(f"{ref}{label}: {row.cells.get(column) or tables.BLANK}")
        out.append(" | ".join(cells))
    return out


def _sheet_header(rows: list[SheetRow]) -> int | None:
    """The index of the row naming the columns: one of the first rows, with two or more distinct labels,
    bold over rows that aren't, or over a column of figures."""
    for index, row in enumerate(rows[:_HEADER_SEARCH]):
        if len(row.cells) < 2:
            continue
        below = rows[index + 1 : index + 31]
        columns = range(min(row.cells), max(max(r.cells) for r in [row, *below]) + 1)
        grid = [[r.cells.get(c, "") for c in columns] for r in [row, *below]]
        bold = row.bold and not all(r.bold for r in below)
        if tables.has_header(grid, bold_first=bold):
            return index
    return None


def _sheet_lines(formula_sheet, value_sheet) -> list[str]:
    from openpyxl.utils import get_column_letter

    rows: list[SheetRow] = []
    more = 0
    formula_count = 0
    last_col = 0
    for index, (frow, vrow) in enumerate(
        zip(
            formula_sheet.iter_rows(max_col=MAX_COLUMNS),
            value_sheet.iter_rows(max_col=MAX_COLUMNS, values_only=True),
        )
    ):
        cells: dict[int, str] = {}
        bold = len(rows) < _HEADER_SEARCH
        for column, cell in enumerate(frow):
            raw = getattr(cell, "value", None)
            value = vrow[column] if column < len(vrow) else None
            if raw is None and value is None:
                continue
            last_col = max(last_col, column + 1)
            if isinstance(raw, str) and raw.startswith("="):
                formula_count += 1
                shown = f"{_fmt(value)} ({raw})" if value is not None else raw
            else:
                shown = _fmt(value if value is not None else raw)
            if shown:
                cells[column + 1] = shown
                bold = bold and bool(getattr(getattr(cell, "font", None), "b", False))
        if not cells:
            continue
        if len(rows) >= MAX_ROWS:
            more += 1
            continue
        number = next((cell.row for cell in frow if getattr(cell, "row", None)), index + 1)
        rows.append(SheetRow(number, cells, bold))
    rows_text = sheet_row_lines(rows)
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
    out = [head, note, *rows_text]
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
        rows: list[SheetRow] = []
        for r in range(sheet.nrows):
            cells: dict[int, str] = {}
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
                if _fmt(value):
                    cells[c + 1] = _fmt(value)
            if cells:
                if len(rows) >= MAX_ROWS:
                    rows_left = sheet.nrows - r
                    break
                rows.append(SheetRow(r + 1, cells))
        else:
            rows_left = 0
        lines.extend(sheet_row_lines(rows))
        if rows_left:
            lines.append(f"[{rows_left} more rows not shown.]")
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
    sheet = [
        SheetRow(number, {c + 1: v.strip() for c, v in enumerate(row[:MAX_COLUMNS]) if v.strip()})
        for number, row in enumerate(rows[:MAX_ROWS], start=1)
    ]
    lines.extend(sheet_row_lines([row for row in sheet if row.cells]))
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
                lines.extend(["", *_pptx_table(shape.table), ""])
            elif getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                text = shape.text_frame.text.strip()
                if text:
                    lines.append(text)
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip() if slide.notes_slide.notes_text_frame else ""
            if notes:
                lines.append(f"Speaker notes: {notes}")
        parts.append(re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip())
    return "\n\n".join(parts)


def _pptx_table(table) -> list[str]:
    """Rows through ``tables``; the header is the first row when the table is styled with one."""
    grid: list[list[str | None]] = []
    for row in table.rows:
        cells: list[str | None] = []
        for column, cell in enumerate(row.cells):
            if cell._tc.get("hMerge") in ("1", "true"):
                cells.append(None)
            elif cell._tc.get("vMerge") in ("1", "true"):
                cells.append(grid[-1][column] if grid and column < len(grid[-1]) else "")
            else:
                cells.append(cell.text.strip())
        grid.append(cells)
    marked = bool(getattr(table, "first_row", False))
    return tables.table_lines(grid, header=tables.has_header(grid, marked=marked))


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
        refs = re.findall(r"(?m)^[A-Z]{1,3}(\d+)(?::| \()", piece)
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


def excerpt(text: str, query: str, size: int) -> str:
    """At most ``size`` characters: the whole text if it fits, else its section list, its opening, and the matching sections."""
    text = (text or "").strip()
    if len(text) <= size:
        return text
    parts = split_parts(text)
    lines = []
    if len(parts) > 1:
        labels = [part.label for part in parts[:12]] + ([f"… {len(parts) - 12} more"] if len(parts) > 12 else [])
        lines.append("Sections: " + " / ".join(labels))
    room = size - sum(len(line) + 1 for line in lines)
    matches = [part for part in search_parts(text, query, limit=3) if part.label != parts[0].label]
    opening_cap = room // 3 if matches else room
    shown: dict[str, str] = {}
    for part in matches:
        shown[part.label] = _clip(part, (room - opening_cap) // len(matches))
    used = sum(len(block) + 1 for block in shown.values())
    shown[parts[0].label] = _clip(parts[0], room - used)
    order = {part.label: index for index, part in enumerate(parts)}
    lines += [block for label, block in sorted(shown.items(), key=lambda item: order[item[0]]) if block]
    return "\n".join(lines)


def skim(parts: list[Part], room: int, *, tag: str = "", line_size: int = 240) -> str:
    """The lines that stand out from ``parts``, labelled by section, within ``room`` characters.

    Lines whose shape repeats (page headers, boilerplate, "Section 3.2: no exceptions…") are dropped, so what is
    left is what differs from page to page: findings, totals, names, dates. Sections take turns so a long file is
    covered end to end instead of only its first pages.
    """

    def shape(line: str) -> str:
        return re.sub(r"\d+", "#", line.lower()).strip()

    counts: dict[str, int] = {}
    for part in parts:
        for line in part.text.splitlines():
            counts[shape(line)] = counts.get(shape(line), 0) + 1
    standout = [
        [line.strip()[:line_size] for line in part.text.splitlines() if len(line.split()) >= 3 and counts[shape(line)] < 3]
        for part in parts
    ]
    chosen: list[list[str]] = [[] for _ in parts]
    used = 0
    for depth in range(max((len(lines) for lines in standout), default=0)):
        for index, lines in enumerate(standout):
            if depth >= len(lines):
                continue
            cost = len(lines[depth]) + 1 + (0 if chosen[index] else len(tag) + len(parts[index].label) + 3)
            if used + cost > room:
                return _skim_text(parts, chosen, tag)
            chosen[index].append(lines[depth])
            used += cost
    return _skim_text(parts, chosen, tag)


def _skim_text(parts: list[Part], chosen: list[list[str]], tag: str) -> str:
    return "\n".join(f"[{tag}{part.label}]\n" + "\n".join(lines) for part, lines in zip(parts, chosen) if lines)


def _clip(part: Part, room: int) -> str:
    head = f"[{part.label}]\n"
    if room - len(head) < 100:
        return ""
    if len(head) + len(part.text) <= room:
        return head + part.text
    return head + part.text[: room - len(head) - 2].rsplit(" ", 1)[0] + " …"


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


def compare_columns(data: bytes, sheet: str, first: str, second: str, *, limit: int = 40) -> str:
    """How each row changes from column ``first`` to ``second`` (letters or header text), biggest increase first."""
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter

    book = load_workbook(io.BytesIO(data), data_only=True)
    try:
        ws = _find_sheet(book, sheet)
        if ws is None:
            return f"No sheet called {sheet!r}. Sheets: " + ", ".join(f'"{s}"' for s in book.sheetnames)
        found = _columns(ws, first, second)
        if isinstance(found, str):
            return found
        header, a, b = found
        rows, totals = [], []
        for r in range(header + 1, ws.max_row + 1):
            old, new = ws.cell(r, a).value, ws.cell(r, b).value
            if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (old, new)):
                continue
            words = [str(ws.cell(r, c).value).strip() for c in range(1, min(a, b)) if isinstance(ws.cell(r, c).value, str)]
            label = " · ".join(word for word in words if word) or f"row {r}"
            (totals if re.search(r"\btotal\b", label, re.I) else rows).append((new - old, r, label, old, new))
        if not rows and not totals:
            return (
                f"No rows on \"{ws.title}\" have numbers in both {get_column_letter(a)} and {get_column_letter(b)}. "
                "If they are formulas, the file may have been saved without values: open and save it in Excel."
            )
        def name(col: int) -> str:
            head = ws.cell(header, col).value if header else None
            return f"{get_column_letter(col)} “{_fmt(head)}”" if head is not None else get_column_letter(col)

        rows.sort(key=lambda row: row[0], reverse=True)
        lines = [f'Sheet "{ws.title}": {name(a)} → {name(b)}, {len(rows)} rows, biggest increase first']
        lines += [_change_line(*row) for row in rows[:limit]]
        if len(rows) > limit:
            lines.append(f"… {len(rows) - limit} more rows")
        if rows:
            up = f"Biggest increase: {rows[0][2]}." if rows[0][0] > 0 else "Nothing went up."
            down = f"Biggest decrease: {rows[-1][2]}." if rows[-1][0] < 0 else "Nothing went down."
            lines.append(f"{up} {down}")
        if totals:
            lines += ["Total rows:"] + [_change_line(*row) for row in totals]
        return "\n".join(lines)
    finally:
        book.close()


def _columns(ws, first: str, second: str):
    """(header row, first column, second column) from letters or header text, or a message saying what's there."""
    from openpyxl.utils import column_index_from_string

    headers: dict[str, tuple[int, int]] = {}
    for r in range(1, min(ws.max_row, 10) + 1):
        for c in range(1, min(ws.max_column, 60) + 1):
            value = ws.cell(r, c).value
            if isinstance(value, str) and value.strip():
                headers.setdefault(value.strip().lower(), (r, c))
    picked = []
    for wanted in (first, second):
        text = (wanted or "").strip().strip("'\"")
        if re.fullmatch(r"[A-Za-z]{1,3}", text):
            picked.append((None, column_index_from_string(text.upper())))
            continue
        hit = headers.get(text.lower()) or next((spot for head, spot in headers.items() if text and text.lower() in head), None)
        if hit is None:
            shown = ", ".join(f'"{head}"' for head in list(headers)[:20])
            return f"No column called {wanted!r}. Give column letters like C and D, or one of these headers: {shown}"
        picked.append(hit)
    rows = [r for r, _c in picked if r is not None]
    if rows:
        header = max(rows)
    else:
        header = next((r for r in range(1, min(ws.max_row, 10) + 1) if all(isinstance(ws.cell(r, c).value, str) for _r, c in picked)), 0)
    return header, picked[0][1], picked[1][1]


def _change_line(diff, row: int, label: str, old, new) -> str:
    sign = "+" if diff >= 0 else "−"
    pct = f" ({sign}{abs(diff / old * 100):.1f}%)" if old else ""
    return f"{label} (row {row}): {_fmt(old)} → {_fmt(new)}, {sign}{_fmt(abs(diff))}{pct}"


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
