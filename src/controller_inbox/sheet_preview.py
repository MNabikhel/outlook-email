"""A workbook, CSV or Word document as it looks, for the Preview tab: every sheet with its cells shown as Excel
shows them (number formats, merged cells, bold, fills, column widths), or a Word document's headings, paragraphs
and tables in order. Read from the file as it arrived; nothing here runs a macro or a formula.

The JSON is small on purpose: a sheet sends at most ``MAX_ROWS`` rows of ``MAX_COLS`` columns, and says how many
more there are.
"""

from __future__ import annotations

import csv
import io
import math
import re
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

MAX_ROWS = 1000
MAX_COLS = 52
MAX_SHEETS = 40
MAX_CELL = 2000  # characters shown in one cell
MAX_DOC_BLOCKS = 3000
MAX_BYTES = 25_000_000  # a larger workbook is downloaded, not drawn

SHEET_SUFFIXES = {".xlsx", ".xlsm", ".xltx", ".xltm", ".xls", ".csv", ".tsv"}
DOC_SUFFIXES = {".docx"}


def kind(filename: str) -> str:
    """"sheet", "doc" or "" for a file this module shows."""
    suffix = Path(filename or "").suffix.lower()
    if suffix in SHEET_SUFFIXES:
        return "sheet"
    if suffix in DOC_SUFFIXES:
        return "doc"
    return ""


def preview(data: bytes, filename: str, *, rows: int = MAX_ROWS, cols: int = MAX_COLS) -> dict:
    """{"kind": "sheet", "sheets": [...]} or {"kind": "doc", "blocks": [...]}."""
    which = kind(filename)
    if which == "doc":
        return {"kind": "doc", "blocks": docx_blocks(data)}
    if which != "sheet":
        raise ValueError("This kind of file has no preview.")
    rows, cols = max(1, min(rows, MAX_ROWS)), max(1, min(cols, MAX_COLS))
    suffix = Path(filename).suffix.lower()
    if len(data) > MAX_BYTES:
        raise ValueError("This file is too big to show here. Download it to open it.")
    if data[:2] == b"PK":
        sheets = _xlsx(data, rows, cols)
    elif data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        sheets = _xls(data, rows, cols)
    else:
        sheets = [_csv(data, filename, rows, cols, tab=suffix == ".tsv")]
    return {"kind": "sheet", "sheets": sheets}


# Excel workbooks ---------------------------------------------------------------------------------------


def _xlsx(data: bytes, max_rows: int, max_cols: int) -> list[dict]:
    from openpyxl import load_workbook

    values = load_workbook(io.BytesIO(data), data_only=True)
    formulas = load_workbook(io.BytesIO(data), data_only=False)
    sheets = []
    try:
        for ws in formulas.worksheets[:MAX_SHEETS]:
            sheets.append(_sheet(ws, values[ws.title], max_rows, max_cols))
    finally:
        values.close()
        formulas.close()
    return sheets


def _sheet(ws, vs, max_rows: int, max_cols: int) -> dict:
    from openpyxl.utils import get_column_letter

    # Only the cells the sheet holds are looked at: asking openpyxl for any other cell makes it, and a sheet
    # formatted down to row 1,048,576 would make a million of them.
    held = {key: cell for key, cell in ws._cells.items() if cell.value is not None}
    values = vs._cells
    merged: dict[tuple[int, int], tuple[int, int]] = {}
    covered: set[tuple[int, int]] = set()
    for rng in ws.merged_cells.ranges:
        merged[(rng.min_row, rng.min_col)] = (rng.max_row - rng.min_row + 1, rng.max_col - rng.min_col + 1)
        for r in range(rng.min_row, min(rng.max_row, rng.min_row + max_rows) + 1):
            for c in range(rng.min_col, min(rng.max_col, max_cols) + 1):
                if (r, c) != (rng.min_row, rng.min_col):
                    covered.add((r, c))
    used = set(held) | set(merged)
    last_row = max((r for r, _c in used), default=0)
    last_col = max((c for _r, c in used), default=0)
    shown_cols = min(last_col, max_cols)
    data_rows = sorted({r for r, _c in used})
    out_rows = []
    shown_last = 0
    for r in range(1, last_row + 1):
        if len(out_rows) >= max_rows:
            break
        dim = ws.row_dimensions.get(r) if r in ws.row_dimensions else None
        if dim is not None and dim.hidden:
            continue
        shown_last = r
        cells = []
        for c in range(1, shown_cols + 1):
            if (r, c) in covered:
                continue
            span = merged.get((r, c))
            cell = held.get((r, c)) or (ws._cells.get((r, c)) if span else None)
            if cell is None:
                continue
            raw = cell.value
            value_cell = values.get((r, c))
            value = value_cell.value if value_cell is not None else None
            formula = raw if isinstance(raw, str) and raw.startswith("=") else ""
            if not formula and type(raw).__name__ == "ArrayFormula":
                formula = str(getattr(raw, "text", "") or "")
            shown_value = value if formula else raw
            text, red = display(shown_value, cell.number_format)
            entry: dict = {"c": c, "v": text[:MAX_CELL]}
            if formula:
                entry["f"] = formula[:500]
                if shown_value is None:
                    # Saved without its result (a file written by a program, not Excel): the formula, marked as such.
                    entry["v"], entry["nocalc"] = formula[:MAX_CELL], True
            if isinstance(shown_value, (int, float, Decimal)) and not isinstance(shown_value, bool):
                entry["num"] = True
            if red:
                entry["red"] = True
            if span:
                entry["rs"], entry["cs"] = span[0], min(span[1], shown_cols - c + 1)
            entry.update(_style(cell))
            cells.append(entry)
        # An empty row still takes its place in the grid, so row numbers read as in Excel.
        out_rows.append({"n": r, "cells": cells})
    while out_rows and not out_rows[-1]["cells"]:
        out_rows.pop()
    widths = []
    for c in range(1, shown_cols + 1):
        letter = get_column_letter(c)
        dim = ws.column_dimensions.get(letter) if letter in ws.column_dimensions else None
        hidden = bool(dim is not None and dim.hidden)
        width = dim.width if dim is not None and dim.width else None
        widths.append({"n": c, "letter": letter, "w": _px(width), "hidden": hidden})
    freeze = ws.freeze_panes if isinstance(ws.freeze_panes, str) else ""
    return {
        "name": ws.title,
        "hidden": ws.sheet_state != "visible",
        "cols": widths,
        "rows": out_rows,
        "more_rows": sum(1 for r in data_rows if r > shown_last),
        "more_cols": max(0, last_col - shown_cols),
        "freeze": freeze,
    }


def _px(width) -> int:
    """Excel's column width (characters of the default font) in pixels, as Excel draws it."""
    if not width:
        return 64
    return max(16, min(480, int(round(float(width) * 7 + 5))))


_THEMELESS = re.compile(r"^[0-9A-Fa-f]{8}$")


def _rgb(color) -> str:
    try:
        if color is None or getattr(color, "type", "") != "rgb":
            return ""
        value = str(color.rgb or "")
    except Exception:
        return ""
    if not _THEMELESS.match(value):
        return ""
    return "#" + value[2:].lower()


def _style(cell) -> dict:
    out: dict = {}
    font = getattr(cell, "font", None)
    if font is not None:
        if font.b:
            out["b"] = True
        if font.i:
            out["i"] = True
        if font.u and font.u != "none":
            out["u"] = True
        fg = _rgb(font.color)
        if fg and fg not in ("#000000",):
            out["fg"] = fg
    fill = getattr(cell, "fill", None)
    if fill is not None and getattr(fill, "fill_type", None) == "solid":
        bg = _rgb(fill.fgColor) or _rgb(fill.start_color)
        if bg and bg not in ("#ffffff",):
            out["bg"] = bg
    alignment = getattr(cell, "alignment", None)
    if alignment is not None:
        if alignment.horizontal in ("center", "right", "left", "centerContinuous"):
            out["al"] = "center" if alignment.horizontal == "centerContinuous" else alignment.horizontal
        if alignment.wrap_text:
            out["wrap"] = True
        if alignment.indent:
            out["ind"] = int(alignment.indent)
    border = getattr(cell, "border", None)
    if border is not None:
        lines = "".join(side[0] for side in ("top", "bottom", "left", "right") if getattr(getattr(border, side, None), "style", None))
        if lines:
            out["bd"] = lines
    return out


# Excel 97-2003 ----------------------------------------------------------------------------------------


def _xls(data: bytes, max_rows: int, max_cols: int) -> list[dict]:
    import xlrd

    book = xlrd.open_workbook(file_contents=data, formatting_info=False, on_demand=True)
    sheets = []
    try:
        for index in range(min(book.nsheets, MAX_SHEETS)):
            sheet = book.sheet_by_index(index)
            shown_cols = min(sheet.ncols, max_cols)
            out_rows = []
            for r in range(min(sheet.nrows, max_rows)):
                cells = []
                for c in range(shown_cols):
                    cell = sheet.cell(r, c)
                    if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                        continue
                    value = cell.value
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        try:
                            value = xlrd.xldate.xldate_as_datetime(value, book.datemode)
                        except Exception:
                            pass
                    elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                        value = bool(value)
                    elif cell.ctype == xlrd.XL_CELL_ERROR:
                        value = xlrd.error_text_from_code.get(value, "#ERROR")
                    text, red = display(value, "General")
                    entry = {"c": c + 1, "v": text[:MAX_CELL]}
                    if isinstance(value, float):
                        entry["num"] = True
                    cells.append(entry)
                out_rows.append({"n": r + 1, "cells": cells})
            while out_rows and not out_rows[-1]["cells"]:
                out_rows.pop()
            sheets.append(_plain(sheet.name, out_rows, shown_cols, max(0, sheet.nrows - max_rows), max(0, sheet.ncols - shown_cols), hidden=bool(getattr(sheet, "visibility", 0))))
    finally:
        book.release_resources()
    return sheets


def _plain(name: str, rows: list[dict], ncols: int, more_rows: int, more_cols: int, *, hidden: bool = False) -> dict:
    from openpyxl.utils import get_column_letter

    return {
        "name": name,
        "hidden": hidden,
        "cols": [{"n": c, "letter": get_column_letter(c), "w": 96, "hidden": False} for c in range(1, ncols + 1)],
        "rows": rows,
        "more_rows": more_rows,
        "more_cols": more_cols,
        "freeze": "",
    }


# CSV --------------------------------------------------------------------------------------------------


def _csv(data: bytes, filename: str, max_rows: int, max_cols: int, *, tab: bool = False) -> dict:
    from controller_inbox.documents import decode_text

    text = decode_text(data)
    if tab:
        dialect: type[csv.Dialect] | csv.Dialect = csv.excel_tab
    else:
        try:
            dialect = csv.Sniffer().sniff(text[:20000], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = []
    widest = 0
    more = 0
    for number, record in enumerate(reader, start=1):
        if number > max_rows:
            more += 1
            continue
        widest = max(widest, len(record))
        cells = []
        for c, value in enumerate(record[:max_cols], start=1):
            if value == "":
                continue
            entry = {"c": c, "v": value[:MAX_CELL]}
            if _looks_numeric(value):
                entry["num"] = True
            cells.append(entry)
        rows.append({"n": number, "cells": cells})
    shown = min(widest, max_cols)
    return _plain(Path(filename).stem or "Sheet1", rows, shown, more, max(0, widest - shown))


_NUMERIC = re.compile(r"^\s*[-+(]?\s*[$€£¥]?\s*[\d,]*\.?\d+\s*%?\)?\s*$")


def _looks_numeric(value: str) -> bool:
    return bool(_NUMERIC.match(value))


# How Excel shows a value ------------------------------------------------------------------------------

_COLOR = re.compile(r"\[(black|blue|cyan|green|magenta|red|white|yellow|color\s*\d+)\]", re.IGNORECASE)
_CURRENCY = re.compile(r"\[\$([^\]\-]*)(?:-[^\]]*)?\]")
_CONDITION = re.compile(r"\[[<>=][^\]]*\]")
_DATE_TOKENS = re.compile(r'(?i)"[^"]*"|\\.|yyyy|yy|mmmmm|mmmm|mmm|mm|m|dddd|ddd|dd|d|hh|h|ss|s|am/pm|a/p|\.0+|[^"\\]')
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")
_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_EPOCH = datetime(1899, 12, 30)


def display(value, number_format: str | None) -> tuple[str, bool]:
    """``value`` as Excel shows it with ``number_format``, and whether Excel shows it in red."""
    fmt = number_format if isinstance(number_format, str) else "General"
    if value is None:
        return "", False
    if isinstance(value, bool):
        return ("TRUE" if value else "FALSE"), False
    if isinstance(value, str):
        sections = _sections(fmt)
        if len(sections) >= 4 and "@" in sections[3]:
            return _literal(sections[3]).replace("@", value), False
        return value, False
    if isinstance(value, time):
        value = datetime.combine(date(1899, 12, 30), value)
    if isinstance(value, (datetime, date)):
        if not isinstance(value, datetime):
            value = datetime.combine(value, time(0))
        return _date(value, fmt), False
    if isinstance(value, timedelta):
        value = value.total_seconds() / 86400
    if isinstance(value, Decimal):
        value = float(value)
    if not isinstance(value, (int, float)):
        return str(value), False
    if isinstance(value, float) and not math.isfinite(value):
        return "#NUM!", False
    if fmt.strip().lower() in ("general", ""):
        return _general(value), False
    sections = _sections(fmt)
    if _is_date_format(sections[0]):
        try:
            return _date(_EPOCH + timedelta(days=float(value)), fmt), False
        except (OverflowError, ValueError):
            return _general(value), False
    if value < 0 and len(sections) >= 2:
        section, number, signed = sections[1], -value, False
    elif value == 0 and len(sections) >= 3:
        section, number, signed = sections[2], value, False
    else:
        section, number, signed = sections[0], value, value < 0
    red = bool(_COLOR.search(section)) and "red" in _COLOR.search(section).group(1).lower()
    text = _number(abs(number), section)
    if signed and text and any(ch.isdigit() and ch != "0" for ch in text):
        text = "-" + text
    return text, red


def _sections(fmt: str) -> list[str]:
    out, current, quoted, escaped = [], [], False, False
    for ch in fmt:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\":
            current.append(ch)
            escaped = True
        elif ch == '"':
            current.append(ch)
            quoted = not quoted
        elif ch == ";" and not quoted:
            out.append("".join(current))
            current = []
        else:
            current.append(ch)
    out.append("".join(current))
    return out


def _general(value) -> str:
    """Excel's General format: whole numbers as they are, others to about ten significant digits."""
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer() and abs(value) < 1e15):
        return str(int(value))
    if abs(value) >= 1e11 or abs(value) < 1e-9:
        return f"{value:.5E}".replace("E+0", "E+").replace("E-0", "E-")
    text = f"{value:.10g}"
    return text


def _literal(section: str) -> str:
    """The section's text with Excel's markup taken out: quotes, escapes, spacing and fill characters, colours."""
    section = _CURRENCY.sub(lambda m: m.group(1), section)
    section = _COLOR.sub("", section)
    section = _CONDITION.sub("", section)
    out = []
    i = 0
    while i < len(section):
        ch = section[i]
        if ch == '"':
            end = section.find('"', i + 1)
            end = len(section) if end < 0 else end
            out.append(section[i + 1 : end])
            i = end + 1
            continue
        if ch == "\\" and i + 1 < len(section):
            out.append(section[i + 1])
            i += 2
            continue
        if ch == "_" and i + 1 < len(section):
            out.append(" ")
            i += 2
            continue
        if ch == "*" and i + 1 < len(section):
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


_PLACES = re.compile(r"[0#?][0#?,]*(?:\.[0#?]*)?(?:[eE][+-][0#]+)?|\.[0#?]+")


def _number(value: float, section: str) -> str:
    text = _literal(section)
    if "%" in text:
        value *= 100 * (text.count("%"))
    match = _PLACES.search(text)
    if not match:
        return text.replace("@", "").strip() if text.strip() else _general(value)
    spec = match.group(0)
    mantissa, _, exponent = spec.partition("E") if "E" in spec else spec.partition("e")
    whole, _, decimals_spec = mantissa.partition(".")
    # Commas after the last digit placeholder scale by a thousand each.
    trailing = len(whole) - len(whole.rstrip(","))
    scaled = value / (1000**trailing)
    whole = whole.rstrip(",")
    grouped = "," in whole
    places = len(decimals_spec)
    min_whole = whole.count("0")
    if exponent:
        digits = f"{scaled:.{places}E}"
        number, _, power = digits.partition("E")
        power_digits = len(exponent) - 1
        sign = "-" if power.startswith("-") else ("+" if exponent[0] == "+" else "")
        shown = f"{number}E{sign}{abs(int(power)):0{power_digits}d}"
    else:
        rounded = Decimal(str(scaled)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        shown = f"{rounded:,.{places}f}" if grouped else f"{rounded:.{places}f}"
        integer, dot, fraction = shown.partition(".")
        bare_integer = integer.replace(",", "")
        if bare_integer == "0" and min_whole == 0:
            integer = ""
        elif len(bare_integer) < min_whole:
            padded = bare_integer.zfill(min_whole)
            integer = f"{int(padded):,}".zfill(len(padded)) if grouped else padded
        # "#.##" drops trailing zeros past the "0" places.
        optional = len(decimals_spec) - len(decimals_spec.rstrip("#?"))
        if optional and fraction:
            keep = len(decimals_spec) - optional
            fraction = fraction[:keep] + fraction[keep:].rstrip("0")
        shown = integer + (dot + fraction if fraction else ("." if dot and places and not optional else ""))
        if shown.endswith(".") and optional == places:
            shown = shown[:-1] if not decimals_spec.startswith("0") else shown
    return (text[: match.start()] + shown + text[match.end() :]).strip()


def _is_date_format(section: str) -> bool:
    bare = _CONDITION.sub("", _COLOR.sub("", _CURRENCY.sub("", section)))
    bare = re.sub(r'"[^"]*"|\\.', "", bare).lower()
    return bool(re.search(r"[dy]|m{1,5}|h|ss", bare)) and not re.search(r"[0#?]", bare)


def _date(value: datetime, fmt: str) -> str:
    section = _sections(fmt)[0] if fmt else ""
    if not _is_date_format(section):
        # A date in a General or number cell: shown as Excel does by default.
        if value.time() == time(0):
            return f"{value.month}/{value.day}/{value.year}"
        return f"{value.month}/{value.day}/{value.year} {value.hour}:{value.minute:02d}"
    section = _CONDITION.sub("", _COLOR.sub("", _CURRENCY.sub(lambda m: m.group(1), section)))
    tokens = [m.group(0) for m in _DATE_TOKENS.finditer(section)]
    twelve = any(t.lower() in ("am/pm", "a/p") for t in tokens)
    out = []
    for index, token in enumerate(tokens):
        low = token.lower()
        prev = next((t.lower() for t in reversed(tokens[:index]) if t.strip() and t[0] not in '"\\' and t.lower() not in ("/", "-", ".", ",", ":", " ")), "")
        nxt = next((t.lower() for t in tokens[index + 1 :] if t.strip() and t[0] not in '"\\' and t.lower() not in ("/", "-", ".", ",", ":", " ")), "")
        minute = low in ("m", "mm") and (prev.startswith("h") or nxt.startswith("s"))
        hour = value.hour % 12 or 12 if twelve else value.hour
        if token.startswith('"'):
            out.append(token[1:-1])
        elif token.startswith("\\"):
            out.append(token[1:])
        elif low == "yyyy":
            out.append(f"{value.year:04d}")
        elif low == "yy":
            out.append(f"{value.year % 100:02d}")
        elif minute:
            out.append(f"{value.minute:02d}" if low == "mm" else str(value.minute))
        elif low == "mmmmm":
            out.append(_MONTHS[value.month - 1][0])
        elif low == "mmmm":
            out.append(_MONTHS[value.month - 1])
        elif low == "mmm":
            out.append(_MONTHS[value.month - 1][:3])
        elif low == "mm":
            out.append(f"{value.month:02d}")
        elif low == "m":
            out.append(str(value.month))
        elif low == "dddd":
            out.append(_DAYS[value.weekday()])
        elif low == "ddd":
            out.append(_DAYS[value.weekday()][:3])
        elif low == "dd":
            out.append(f"{value.day:02d}")
        elif low == "d":
            out.append(str(value.day))
        elif low == "hh":
            out.append(f"{hour:02d}")
        elif low == "h":
            out.append(str(hour))
        elif low == "ss":
            out.append(f"{value.second:02d}")
        elif low == "s":
            out.append(str(value.second))
        elif low == "am/pm":
            out.append("AM" if value.hour < 12 else "PM")
        elif low == "a/p":
            out.append("A" if value.hour < 12 else "P")
        elif low.startswith(".0"):
            out.append("." + f"{value.microsecond:06d}"[: len(token) - 1])
        elif token in ("_", "*"):
            continue
        else:
            out.append(token)
    return "".join(out).strip()


# Word -------------------------------------------------------------------------------------------------


def docx_blocks(data: bytes) -> list[dict]:
    """The document's headings, paragraphs, list items and tables in order: {"t": "h1".."h4" | "p" | "li" |
    "table", ...}. Tracked insertions are shown as they read now; text and table cells only, no pictures."""
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(io.BytesIO(data))
    body = document.element.body
    blocks: list[dict] = []
    for child in body.iterchildren():
        if len(blocks) >= MAX_DOC_BLOCKS:
            blocks.append({"t": "p", "text": "[The rest of the document isn't shown here; it's in the Text tab.]"})
            break
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            paragraph = Paragraph(child, document)
            text = _paragraph_text(paragraph)
            if not text.strip():
                continue
            style = (paragraph.style.name if paragraph.style is not None else "") or ""
            level = re.match(r"(?i)heading (\d)", style)
            if style.lower() == "title":
                blocks.append({"t": "h1", "text": text})
            elif level:
                blocks.append({"t": f"h{min(4, int(level.group(1)) + 1)}", "text": text})
            elif "list" in style.lower() or child.find(".//{*}numPr") is not None:
                blocks.append({"t": "li", "text": text})
            else:
                runs = paragraph.runs
                bold = bool(runs) and all(run.bold for run in runs if run.text.strip())
                blocks.append({"t": "p", "text": text, **({"b": True} if bold else {})})
        elif tag == "tbl":
            table = Table(child, document)
            grid = []
            for row in table.rows[:500]:
                cells = []
                seen = set()
                for cell in row.cells[:40]:
                    if id(cell._tc) in seen:
                        continue  # a merged cell repeats in python-docx
                    seen.add(id(cell._tc))
                    cells.append("\n".join(_paragraph_text(p) for p in cell.paragraphs).strip()[:MAX_CELL])
                grid.append(cells)
            if grid:
                blocks.append({"t": "table", "rows": grid})
    return blocks


def _paragraph_text(paragraph) -> str:
    """The paragraph as it reads with tracked changes accepted: inserted text in, deleted text out."""
    parts = []
    for node in paragraph._p.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag == "t" and not _inside(node, "del"):
            parts.append(node.text or "")
        elif tag == "tab":
            parts.append("\t")
        elif tag in ("br", "cr"):
            parts.append("\n")
    return "".join(parts)


def _inside(node, name: str) -> bool:
    parent = node.getparent()
    while parent is not None:
        if parent.tag.rsplit("}", 1)[-1] == name:
            return True
        parent = parent.getparent()
    return False
