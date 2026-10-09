"""A second reader for the tables in a PDF's text: Camelot, used beside CloseDesk's own.

CloseDesk's reader (pdf_layout.py) rebuilds a page's rows from where each letter sits. It reads the finance
schedules programs print (aging reports, registers, statements from Excel and accounting systems) better than the
general tools, but misses many tables in real-world published PDFs: annual reports and filings, where columns are
far apart, headings span several columns and a figure's "$" is set apart from it. Camelot's "stream" reader
(MIT-licensed, run on this computer) finds those tables by the gaps between columns of words.

Each page with figures on it is read by both. CloseDesk's own tables always stay; each of Camelot's tables is
added beside them when it reads what they don't:

- at least two figures not read yet, and a tenth of its own (a table CloseDesk missed, or read only in part);
- or rows without figures not read yet (a table of contents, a list by date);
- or rows whose figures were read under other names (``_adds_named_rows``: a row's label read into the row above,
  or two columns run together), unless CloseDesk's table of those figures adds up. A row of a register (a date,
  a merchant and a purpose) read by both but with its words split differently counts as read.

Replacing CloseDesk's tables with Camelot's, under every rule tried, made some of the finance schedules CloseDesk
reads well worse (a wrapped row read as two, columns run together); adding never did. A table read by both is
then written twice, CloseDesk's first; each is a table of its own, so no total adds a row twice.

Each Camelot table is written the way CloseDesk writes tables (``as_text``): dot leaders dropped, a "$" set apart
joined to its figure, its heading lines kept as printed above it and joined into the column names. A table is only
kept when it is one (``is_data``): a column of amounts, a table of contents, a list by date, a grid of numbers or
times. A page of prose read as a table is not. See README, "Tables in real-world PDFs", for how this was measured.

Camelot is optional: the launchers install it (``pip install -e ".[tables]"``), and where it can't be installed (an
Intel Mac older than macOS 12 has no OpenCV for it) or fails on a file, PDFs are read as before.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import warnings
from collections import Counter

from controller_inbox import tables, vision

log = logging.getLogger(__name__)

# A page is worth a second reading when its text has at least this many figures.
MIN_FIGURES = 4
# Pages read by Camelot in one file: it takes about half a second a page.
MAX_PAGES = 60
# Camelot's table is added when it reads at least ADD_MIN figures not read yet, and ADD_SHARE of its own; or when
# it has at least MIN_NEW rows not read yet and at most MAX_SEEN of its rows read already.
ADD_MIN = 2
ADD_SHARE = 0.1
MIN_NEW = 2
MAX_SEEN = 0.2
# Or when at least NAMED_MIN of its named rows, and NAMED_SHARE of them, have figures read under other names.
NAMED_MIN = 2
NAMED_SHARE = 0.3
_CURRENCY = {"$", "€", "£", "¥", "US$"}


def available() -> bool:
    try:
        import camelot  # noqa: F401
    except Exception:  # noqa: BLE001 - a broken install is the same as none
        return False
    return True


def read(data: bytes, pages: list[int]) -> dict[int, list[list[list[str]]]]:
    """Camelot's tables on these pages of the PDF, cleaned (``clean``) and only those that hold figures
    (``is_data``): page -> grids. Empty when Camelot isn't installed or can't read the file."""
    pages = sorted(set(pages))[:MAX_PAGES]
    if not pages or not available():
        return {}
    import camelot

    handle, path = tempfile.mkstemp(suffix=".pdf")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            found = camelot.read_pdf(path, pages=",".join(map(str, pages)), flavor="stream")
    except Exception as exc:  # noqa: BLE001 - the page keeps CloseDesk's own reading
        log.info("Camelot couldn't read the PDF's tables: %s", exc)
        return {}
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    out: dict[int, list[list[list[str]]]] = {}
    for table in found:
        try:
            page = int(table.page)
            grid = clean(table.df.values.tolist())
        except Exception:  # noqa: BLE001
            continue
        if is_data(grid):
            out.setdefault(page, []).append(grid)
    return out


_LEADER = re.compile(r"[.·…_\s]*[.·…_][.·…_\s]*")
_TRAILING_LEADER = re.compile(r"(?:\s*[.·…_]){2,}\s*$")
# A row of running text caught in a table's area: one cell this long, nothing beside it.
PROSE = 70


def _cell(cell) -> str:
    text = " ".join(str(cell or "").split())
    if _LEADER.fullmatch(text):
        return ""  # dot leaders printed between a row's name and its figures
    return _TRAILING_LEADER.sub("", text)


def clean(grid: list[list]) -> list[list[str]]:
    """Cells as one line each, without dot leaders; a column of currency signs set apart from its figures joined to
    them; rows of running text, empty columns and empty rows dropped."""
    rows = [[_cell(cell) for cell in row] for row in grid]
    rows = [row for row in rows if not (sum(1 for cell in row if cell) == 1 and max(map(len, row)) > PROSE)]
    if not rows:
        return []
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    for column in range(width - 1):
        signs = [row[column] for row in rows if row[column]]
        if signs and all(sign in _CURRENCY for sign in signs):
            for row in rows:
                if row[column] and row[column + 1]:
                    row[column + 1] = f"{row[column]}{row[column + 1]}"
                row[column] = ""
    keep = [column for column in range(width) if any(row[column] for row in rows)]
    rows = [[row[column] for column in keep] for row in rows]
    rows = [row for row in rows if any(row)]
    # A row's name printed in one of two columns (indented rows): one column, while no row fills both.
    while rows and len(rows[0]) > 2 and not any(row[0] and row[1] for row in rows) and not any(_figure(row[1]) for row in rows):
        rows = [[row[0] or row[1], *row[2:]] for row in rows]
    return rows


def _figure(cell: str) -> bool:
    return bool(cell) and bool(vision.figures(cell)) and tables.is_value(cell.replace(" ", ""))


# An amount, however small, or two set in one cell, or a time: "$ 40", "(3)", "8,162 7,962", "12.5%", "—", "12:04".
_AMOUNTS = re.compile(r"[\s$€£¥(),.\-–—%]*\d[\d\s$€£¥(),.:\-–—%]*|[-–—]")


_MONEY_IN = re.compile(r"[$€£¥]\s?\d|\d\s?%")


def _amount(cell: str) -> bool:
    return bool(cell) and bool(_AMOUNTS.fullmatch(cell))


# A page number in a table of contents: "12", "F-2", "A-14", "ii".
_PAGE_REF = re.compile(r"(?:[A-Z]{1,2}-?)?\d{1,4}|[ivxlc]{1,6}", re.I)


def _number(cell: str) -> bool:
    """A figure, an amount, a time, a date, or a short number such as a page number or a count."""
    return _figure(cell) or _amount(cell) or tables.is_value(cell) or bool(_PAGE_REF.fullmatch(cell.replace(" ", "")))


def is_data(grid: list[list[str]]) -> bool:
    """A table of figures: two or more columns and a column that is mostly amounts (with a figure among them, or
    beside named rows); or a table of contents, or a list by date. A page of prose read as a table has none of
    these."""
    if len(grid) < 2 or max((len(row) for row in grid), default=0) < 2:
        return False
    named = sum(1 for row in grid if _named(row[0]))
    for column in range(1, max(len(row) for row in grid)):
        filled = [row[column] for row in grid if column < len(row) and row[column]]
        amounts = sum(map(_amount, filled))
        if len(filled) >= 2 and amounts >= 2 and amounts >= 0.6 * len(filled) and any(map(_figure, filled)) or (
            len(filled) >= 3 and amounts >= 0.6 * len(filled) and named >= 2
        ):
            return True
        # A price list: most cells of the column hold money or a percentage among words ("$10 copay").
        if len(filled) >= 3 and named >= 3 and sum(1 for cell in filled if _MONEY_IN.search(cell)) >= 0.6 * len(filled):
            return True
        # A table of contents (named rows, each with its page number), or a list by date.
        if len(filled) >= 4 and named >= 4 and sum(map(_number, filled)) >= 0.8 * len(filled):
            return True
    cells = [cell for row in grid for cell in row if cell]
    # A grid of numbers or times (a timetable, a rate matrix): most of its cells, over four rows and three columns.
    if len(grid) >= 4 and max(len(row) for row in grid) >= 3 and sum(map(_amount, cells)) >= 0.7 * len(cells):
        return True
    first = [row[0] for row in grid if row and row[0]]
    # Dates down the first column, beside named rows: a schedule or a list of transactions.
    return len(first) >= 4 and sum(1 for cell in first if tables.is_value(cell) and not _figure(cell)) >= 0.8 * len(first) and sum(
        1 for row in grid if any(_named(cell) for cell in row[1:])) >= 4


def row_key(cells: list[str], *, loose: bool = False) -> tuple:
    """A row by its figures, as written in either reader's text: what tells two readings of one row apart.
    ``loose``: a row with no figures is known by its page numbers or dates and its words, for a table of contents
    or a list by date."""
    figures = tuple(sorted(str(abs(value)) for value in vision.figures(" ".join(cells))))
    if figures or not loose:
        return figures
    filled = [cell for cell in cells if cell and cell != tables.BLANK]
    values = [cell.replace(" ", "") for cell in filled if _number(cell)]
    if values and (any(_named(cell) for cell in filled) or len(values) >= 3):
        # By the values in its text, not its cells: one reader can run two cells into one ("10/1 Thu").
        joined = " ".join(filled)
        return ("values", *re.findall(r"\d[\d/:.,-]*", joined), re.sub(r"[^a-z]", "", joined.lower())[:40])
    return ()


def _keys(rows: list[list[str]]) -> Counter:
    """A table's rows by their figures; a table with fewer than two rows of figures by its rows' values and words."""
    strict = Counter(key for key in (row_key(row) for row in rows) if key)
    if strict.total() >= 2:
        return strict
    return Counter(key for key in (row_key(row, loose=True) for row in rows) if key)


def grid_keys(grid: list[list[str]]) -> Counter:
    return _keys(grid)


def _named(cell: str) -> bool:
    return bool(re.search(r"[A-Za-z]{2}", cell or "")) and not tables.is_value(cell)


def as_text(grid: list[list[str]]) -> str:
    """The table as CloseDesk writes tables (``[table]``, a line of column names, ``Label: value`` rows): the
    lines above the first row of figures are its headings."""
    first = next((index for index, row in enumerate(grid) if any(_figure(cell) for cell in row[1:])), 0)
    # Headings are the lines over the figure columns: something past the first column, nothing long. A line of its
    # own in the first column just above the rows names the section under it and stays a row; a title above is
    # left out (it is in the page's text).
    heads: list[list[str]] = []
    above: list[list[str]] = []
    for row in grid[:first]:
        filled = [cell for cell in row if cell]
        if any(row[1:]) and not any(len(cell) > PROSE // 2 for cell in filled):
            heads.append(row)
            above = []
        else:
            above.append(row)
    body = [row for row in above if len(row[0]) <= PROSE // 2][-1:] + grid[first:]
    width = max(len(row) for row in grid)
    header = [""] * width
    # The last two heading lines name the columns ("Year ended December 31" over "2009 | 2008"); lines above them
    # are the table's title.
    lines = [line + [""] * (width - len(line)) for line in heads[-2:]]
    if len(lines) == 2 and sum(1 for cell in lines[0][1:] if cell) * 2 <= sum(1 for cell in lines[1][1:] if cell):
        # A sparse top line spans the columns under it ("December 31" over "2009 | 2008"): carried across them.
        header = vision._joined_headings(lines[0], lines[1])
    elif lines:
        # Each column's own heading lines ("Total" over "Paid"), nothing carried from the column beside it.
        header = [" ".join(line[column] for line in lines if line[column]) for column in range(width)]
    if heads and any(len(cell) > 60 for cell in header):
        header = heads[-1] + [""] * (width - len(heads[-1]))
    header = [re.sub(r"\s+", " ", cell).strip() for cell in header]
    table = "[table]\n" + "\n".join(vision._table_lines(header, body))
    # The heading lines as printed, above the table: the column names join them ("December 31 2009"), and what
    # they say about all the columns ("Millions of dollars", "Year ended December 31") stays in view.
    printed = [" | ".join(cell for cell in line if cell) for line in heads[-2:]]
    printed = [line for line in printed if line]
    return f"[heading]\n{chr(10).join(printed)}\n\n{table}" if printed else table


_BLOCK = re.compile(r"(?m)^\[table\]\n(?:(?!\n\n).)*", re.S)


# Numbers that are part of an ID, not figures: a masked tax ID or account ("**-***4821"), a code ("R-01", "V1001").
_ID = re.compile(r"\S*[*#]\S*|\b[A-Za-z]+[-/]?\d+[\w-]*")


def _figures_of(rows: list[list[str]]) -> set:
    """The figures in rows (by size), leaving out numbers that are part of an ID."""
    return {abs(value) for value in vision.figures(_ID.sub(" ", " ".join(" ".join(row) for row in rows)))}


def _our_rows(block: str) -> tuple[list[list[str]], int]:
    """A table of ours as rows of values, and how many columns it has."""
    from controller_inbox.table_lookup import tables_in

    found = tables_in(block)
    lines = block.splitlines()

    def values(row) -> list[str]:
        # Each cell's value, and an amount written without a column name ("| $48,250.00") when the row has a few
        # of them: a row with more amounts without names than with is a table read into the wrong columns.
        out = [value for _label, value in row.cells]
        if row.at is not None and 0 <= row.at < len(lines):
            unnamed = [piece.strip() for piece in lines[row.at].split(" | ") if ": " not in piece and _amount(piece.strip())]
            if len(unnamed) <= max(1, len(row.cells) // 2):
                out += unnamed
        return out

    rows = [values(row) for table in found for row in table.rows]
    return rows, max((len(table.labels) for table in found), default=0)


def combine(page_text: str, grids: list[list[list[str]]]) -> str:
    """The page's text with Camelot's tables added where they read what CloseDesk's didn't (see the module's
    docstring). CloseDesk's own tables always stay: replacing one, however the two were compared, made some of the
    finance schedules CloseDesk reads well worse. Camelot's tables, fullest first, are each added when they read
    figures not read yet (at least ``ADD_MIN``, and ``ADD_SHARE`` of their own), or are mostly rows not read yet (a
    table of contents, a list by date)."""
    if not grids:
        return page_text
    blocks = [match.group(0) for match in _BLOCK.finditer(page_text)]
    # A page that is one table is written without a [table] line: its rows come before any marked part.
    unmarked = re.split(r"(?m)^\[[a-z]+\]$", page_text, maxsplit=1)[0]
    if unmarked.strip():
        blocks.insert(0, unmarked)
    ours = [_our_rows(block)[0] for block in blocks]
    # Figures in a table of ours whose printed totals add up as read: read right, names and all.
    checked = set().union(*(_figures_of(rows) for rows, block in zip(ours, blocks) if _adds_up(block)))
    read = [row for rows in ours for row in rows]
    added: list[int] = []
    for index in sorted(range(len(grids)), key=lambda i: -grid_keys(grids[i]).total()):
        grid = grids[index]
        # Renamed rows count in a table of several columns; two (a label and a value) are a block of details.
        renames = len(grid[0]) >= 3 and len(_figures_of(grid) & checked) < 2 and _adds_named_rows(grid, read)
        if _adds_figures(grid, read) or _mostly_new(grid, read) or renames:
            added.append(index)
            read += grid
    text = page_text.strip()
    return "\n\n".join(part for part in [text, *(as_text(grids[index]) for index in sorted(added))] if part.strip())


def _adds_up(block: str) -> bool:
    """Whether a table's printed totals add up as read (at least one checked, none off)."""
    from controller_inbox.table_lookup import tables_in, verify

    verdicts = [verify(table) for table in tables_in(block)]
    return any(v.matched for v in verdicts) and not any(v.mismatched for v in verdicts)


def _named_key(row: list[str]) -> tuple:
    """A row by its figures and its name's letters: two readings of a row agree when both are the same."""
    figures = tuple(sorted(str(abs(value)) for value in vision.figures(_ID.sub(" ", " ".join(row)))))
    words = re.sub(r"[^a-z]", "", " ".join(cell for cell in row if not _amount(cell)).lower())
    return (figures, words) if figures and words else ()


def _adds_named_rows(grid: list[list[str]], read: list[list[str]]) -> bool:
    """Whether a table names rows of figures differently from how they were read: at least ``NAMED_MIN`` of its
    named rows, and ``NAMED_SHARE`` of them, aren't among the rows read (their figures read under other names, or
    in rows run together)."""
    seen = {_named_key(row) for row in read}
    named: dict[tuple, list[str]] = {}
    for row in read:
        key = _named_key(row)
        if key:
            named.setdefault(key[0], []).append(key[1])

    def new_row(row: list[str]) -> bool:
        # Read already when the same figures were read under the same name; a row of several words (a register's
        # merchant and purpose, an item and its description) also when read under a name holding its first words,
        # as its other words can be split differently.
        key = _named_key(row)
        if key in seen:
            return False
        names = [cell for cell in row if _named(cell)]
        if len(names) < 2:
            return True
        first = re.sub(r"[^a-z]", "", names[0].lower())
        return not (first and any(first in words for words in named.get(key[0], [])))

    mine = [row for row in grid if _named_key(row)]
    new = sum(1 for row in mine if new_row(row))
    return new >= NAMED_MIN and new >= NAMED_SHARE * len(mine)


def _adds_figures(grid: list[list[str]], read: list[list[str]]) -> bool:
    """Whether a table reads figures not read yet: at least ``ADD_MIN`` of them, and ``ADD_SHARE`` of its own."""
    figures = _figures_of(grid)
    new = figures - _figures_of(read)
    return len(new) >= ADD_MIN and len(new) >= ADD_SHARE * len(figures)


def _mostly_new(grid: list[list[str]], read: list[list[str]]) -> bool:
    """Whether a table is mostly rows not read yet: at least ``MIN_NEW`` new rows, at most ``MAX_SEEN`` of its rows
    read already. A row of figures is read when all its figures are among those read; a row without figures (a
    table of contents, a list by date) when the same row was read."""
    figures = _figures_of(read)
    keys = {row_key(row, loose=True) for row in read}
    rows = [row for row in grid if row_key(row, loose=True)]
    if not rows:
        return False
    new = 0
    for row in rows:
        mine = _figures_of([row])
        new += not (mine <= figures if mine else row_key(row, loose=True) in keys)
    return new >= MIN_NEW and (len(rows) - new) / len(rows) <= MAX_SEEN
