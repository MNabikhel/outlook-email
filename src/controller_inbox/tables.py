"""Table rows as text a small model reads correctly, whatever file the table came from.

A row written as ``Maya Chen | Finance | 98,500`` only works while every cell is filled:
one blank cell and every value after it looks like it belongs to the column before.
So when a table has a header row, each row names its columns and says which are blank:

    Employee: Jonathan Alvarez | ID: E-1002 | Department: (blank) | Manager: Priya Raman

and a question about one person needs nothing but that line. Without a header, the
blank cells are still written out so the columns stay in place.
"""

from __future__ import annotations

import re

BLANK = "(blank)"

_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_VALUE_RE = re.compile(
    r"[(\-+−]?\s*[$€£¥]?\s*[-+−]?\d[\d,.' ]*(?:%|k|m|bn|x)?\)?"
    r"|\d{1,4}[-/.]\d{1,2}(?:[-/.]\d{1,4})?"
    r"|\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTH + r"(?:,?\s+\d{2,4})?"
    r"|" + _MONTH + r"\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{2,4})?"
    r"|" + _MONTH + r"\s+\d{4}"
    r"|(?:yes|no|true|false|n/?a|-|—|–)",
    re.I,
)


def is_value(cell: str) -> bool:
    """A number, amount, percentage, date or yes/no, the kind of cell a header row doesn't have."""
    return bool(_VALUE_RE.fullmatch((cell or "").strip()))


def has_header(rows: list[list[str | None]], *, marked: bool = False, bold_first: bool = False) -> bool:
    """Whether the first row names the columns.

    ``marked``: the file says so (a Word header row, PowerPoint's first-row style).
    ``bold_first``: the first row is bold and the rows under it aren't.
    Otherwise it takes a first row of distinct labels over at least one column of figures,
    with three or more columns, so a two-column "Amount due | $12,480.00" list isn't mistaken for one.
    """
    if len(rows) < 2:
        return False
    first = [cell.strip() for cell in rows[0] if cell is not None]
    filled = [cell for cell in first if cell]
    if len(filled) < 2 or len(set(filled)) != len(filled):
        return False
    if any(is_value(cell) or cell.endswith(":") for cell in filled):
        return False
    if marked or bold_first:
        return True
    if len(first) < 3 or len(rows) < 3:
        return False
    for column, label in enumerate(rows[0]):
        if not label:
            continue
        below = [row[column].strip() for row in rows[1:] if column < len(row) and row[column] and row[column].strip()]
        if len(below) >= 2 and sum(map(is_value, below)) >= 0.6 * len(below):
            return True
    return False


def table_lines(rows: list[list[str | None]], *, header: bool) -> list[str]:
    """One line per row. ``None`` marks a merged cell's continuation (not a blank); ``""`` is blank."""
    rows = [row for row in rows if any(cell and cell.strip() for cell in row)]
    if not rows:
        return []
    width = max(len(row) for row in rows)
    rows = [[_clean(cell) for cell in row] + [""] * (width - len(row)) for row in rows]
    if not header:
        return [_plain(row) for row in rows]
    labels = rows[0]
    return [" | ".join(label for label in labels if label)] + [labelled_row(labels, row) for row in rows[1:]]


def labelled_row(labels: list[str | None], row: list[str | None]) -> str:
    """``Label: value`` for each column, ``(blank)`` where the cell is empty."""
    labels = [_clean(label) or "" for label in labels]
    row = [_clean(cell) for cell in row] + [""] * (len(labels) - len(row))
    filled = [cell for cell in row if cell]
    if len(filled) == 1 and len(labels) > 2:
        return filled[0]
    cells = []
    for label, cell in zip(labels, row):
        if cell is None:
            continue
        if not label:
            if cell:
                cells.append(cell)
            continue
        cells.append(f"{label}: {cell or BLANK}")
    return " | ".join(cells)


def _plain(row: list[str | None]) -> str:
    cells = [cell for cell in row if cell is not None]
    while cells and not cells[-1]:
        cells.pop()
    out: list[str] = []
    for cell in cells:
        if out and out[-1].endswith(":"):
            out[-1] = f"{out[-1]} {cell or BLANK}"
        else:
            out.append(cell or BLANK)
    return " | ".join(out)


def _clean(cell: str | None) -> str | None:
    if cell is None:
        return None
    return re.sub(r"\s+", " ", cell).strip()
