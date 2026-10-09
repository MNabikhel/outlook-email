"""Table rows as text a small model reads correctly, whatever file the table came from.

A row written as ``Maya Chen | Finance | 98,500`` only works while every cell is filled:
one blank cell and every value after it looks like it belongs to the column before.
So when a table has a header row, each row names its columns and says which are blank:

    Employee: Jonathan Alvarez | ID: E-1002 | Department: not listed | Manager: Priya Raman

and a question about one person needs nothing but that line. "not listed" rather than a
marker, because a small model repeats what it reads: "no department is listed" is right.
Without a header, empty cells are still written, as (empty), so the columns stay in place.
"""

from __future__ import annotations

import re

BLANK = "not listed"
EMPTY = "(empty)"

_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
# A negative figure can be "(1,250.00)", "$(1,250.00)" (a currency sign before its parentheses), "-1,250.00",
# "–1,250.00" (a typeset minus), or "1,250.00-" with its minus last, as SAP and Oracle reports print it.
_VALUE_RE = re.compile(
    r"[(\-+−–]?\s*[$€£¥]?\s*[(\-+−–]?\d[\d,.' ]*(?:%|k|m|bn|x)?[-−]?\)?"
    r"|\d{1,4}[-/.]\d{1,2}(?:[-/.]\d{1,4})?"
    r"|\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTH + r"(?:,?\s+\d{2,4})?"
    r"|" + _MONTH + r"\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{2,4})?"
    r"|" + _MONTH + r"\s+\d{4}"
    r"|(?:yes|no|true|false|n/?a|-|—|–)",
    re.I,
)

# A year, a month or a date: a value in a row, but a column's name in a row of headings.
_PERIOD_RE = re.compile(
    r"(?:fy\s?)?(?:19|20)\d{2}"
    r"|\d{1,4}[-/.]\d{1,2}(?:[-/.]\d{1,4})?"
    r"|\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTH + r"(?:,?\s+\d{2,4})?"
    r"|" + _MONTH + r"(?:\s+\d{1,2}(?:st|nd|rd|th)?)?(?:,?\s+\d{2,4})?",
    re.I,
)


def is_value(cell: str) -> bool:
    """A number, amount, percentage, date or yes/no, the kind of cell a header row doesn't have."""
    return bool(_VALUE_RE.fullmatch((cell or "").strip()))


def has_header(rows: list[list[str | None]], *, marked: bool = False, bold_first: bool = False) -> bool:
    """Whether the first row names the columns.

    ``marked``: the file says so (a Word header row, PowerPoint's first-row style).
    ``bold_first``: the first row is bold and the rows under it aren't.
    Otherwise it takes a first row of distinct labels over at least one column of figures, with three or more
    columns, so a two-column "Amount due | $12,480.00" list isn't mistaken for one; with two columns, only labels
    over four or more rows of a name and an amount (``_names_over_amounts``).
    """
    if len(rows) < 2:
        return False
    first = [cell.strip() for cell in rows[0] if cell is not None]
    filled = [cell for cell in first if cell]
    if len(filled) < 2 or len(set(filled)) != len(filled):
        return False
    # A year or a month over each column ("2026", "Oct 2026") names it when the file marks the row as headings.
    periods = marked or bold_first
    if any((is_value(cell) and not (periods and _PERIOD_RE.fullmatch(cell))) or cell.endswith(":") for cell in filled):
        return False
    if marked or bold_first:
        return True
    if len(first) == 2 and len(filled) == 2:
        return _names_over_amounts(rows)
    if len(first) < 3 or len(rows) < 3:
        return False
    for column, label in enumerate(rows[0]):
        if not label:
            continue
        below = [row[column].strip() for row in rows[1:] if column < len(row) and row[column] and row[column].strip()]
        if len(below) >= 2 and sum(map(is_value, below)) >= 0.6 * len(below):
            return True
    return False


_AMOUNT_RE = re.compile(r"[(\-−–]?\s*[$€£¥]?\s*[(\-−–]?\d[\d,]*(?:\.\d+)?%?\)?")
_MONEY_RE = re.compile(r"[$€£¥]|\d,\d{3}|\.\d\d\b")
# A field a form fills in with a number that is not an amount: "Invoice No.", "PO Number", "Vendor #", "Tax ID".
_ID_LABEL_RE = re.compile(r"(?i)(?:\b(?:no|nos|number|id|code|ref|reference)\.?|#)\s*:?$")


def _names_over_amounts(rows: list[list[str | None]]) -> bool:
    """Two columns headed by two labels over a name and an amount on each of four or more rows ("Department | Q3
    Budget" over each department's budget). A list of labels and values keeps its first row as a row: it is
    shorter ("Customer | Northwind", "Subtotal | 1,200"), or has dates, words or numbers not written as money
    among its values, or a field for one ("Invoice No. | 58213")."""
    body = [row for row in rows[1:] if any(cell and cell.strip() for cell in row)]
    if len(body) < 4 or any(len(row) < 2 for row in body):
        return False
    names = [(row[0] or "").strip() for row in body]
    amounts = [(row[1] or "").strip() for row in body]
    return (
        all(names) and len(set(names)) == len(names) and not any(map(is_value, names))
        and not any(_ID_LABEL_RE.search(name) for name in names)
        and all(_AMOUNT_RE.fullmatch(amount) and _MONEY_RE.search(amount) for amount in amounts)
    )


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
    """``Label: value`` for each column, ``Label: not listed`` where the cell is empty."""
    labels = [_clean(label) or "" for label in labels]
    row = [_clean(cell) for cell in row] + [""] * (len(labels) - len(row))
    filled = [cell for cell in row if cell]
    # A lone heading ("Contractors") reads on its own; a lone figure, often a total, needs its column.
    if len(filled) == 1 and len(labels) > 2 and not is_value(filled[0]):
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
    for index, cell in enumerate(cells):
        if not cell and out and out[-1].endswith(":") and _value_later(cells[index + 1 :]):
            continue  # "Total due:", a blank, "$1,344.00": the label's value is further along
        if out and out[-1].endswith(":"):
            out[-1] = f"{out[-1]} {cell or BLANK}"
        else:
            out.append(cell or EMPTY)
    return " | ".join(out)


def _value_later(cells: list[str]) -> bool:
    """The next filled cell is a value, not another label."""
    later = next((cell for cell in cells if cell), "")
    return bool(later) and not later.endswith(":")


def _clean(cell: str | None) -> str | None:
    if cell is None:
        return None
    return re.sub(r"\s+", " ", cell).strip()
