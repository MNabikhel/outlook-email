"""The tables on a file's page and its key details (invoice number, dates, totals), each tied to where it is printed.

The Page tab marks a box over every piece of text read on the page (``page_view``). This finds which of those boxes
make up a table and which hold an invoice's key details, so the workspace can outline each table on the page, show it
as a grid, and point at the box an invoice number or total was read from.

Nothing here reads the page again: it works from the boxes already kept for the page and from what CloseDesk already
read from the file.

- A PDF with its own text: its tables are the ones CloseDesk reads from that text (``table_lookup``); each cell is
  the box holding the same text, row by row down the page, so a "1" in the Qty column is the one on its own row.
- A scan or a picture read by the vision model: its tables are the model's; each cell is the OCR box with the same
  text or figures, or, for a figure OCR read differently, the box in the cell's row and column.
- A scan the model hasn't read: a table only where at least three lines of OCR boxes line up in three or more
  columns, one of them figures. A page of prose, an address block beside a list of details is not a table.

Key details are found by their printed label ("Invoice no.", "Total due") and the box beside or below it; failing a
label, by the values CloseDesk extracted from the email and the file.
"""

from __future__ import annotations

import difflib
import re
import statistics
from typing import Iterable

from controller_inbox import table_lookup, tables, vision
from controller_inbox.page_view import _plain

# Room around a table's cells when it is outlined, as a fraction of the page.
PAD = 0.006
MAX_TABLES = 12
MAX_ROWS = 150
# Two texts are the same cell from this alike (difflib's ratio) on; OCR drops spaces and misreads a letter or two.
ALIKE = 0.75
# How many of a cell's lookalikes down the page are weighed when finding its row.
NEAREST = 4
# A table is shown when at least this share of its cells were found on the page.
FOUND_SHARE = 0.5


def _middle(region: dict) -> float:
    return region["y"] + region["h"] / 2


def _same_line(a: dict, b: dict) -> bool:
    return abs(_middle(a) - _middle(b)) <= 0.6 * max(a["h"], b["h"], 0.004)


def _figures(text: str) -> set:
    return {abs(value) for value in vision.figures(text)}


def _score(cell: str, text: str, *, heading: bool = False, plain: str | None = None, figures: set | None = None) -> float:
    """How surely a box's text is the cell's (0 to 1). A figure has to be the same figure: OCR's "1,868.40" is not the
    cell "1,886.40", however alike they look; that box is found by its place instead. ``plain`` and ``figures`` are
    the box's, worked out once."""
    a, b = _plain(cell), _plain(text) if plain is None else plain
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if heading and len(b) >= 3 and a.endswith(b):
        # A heading read run together with text printed above it ("Harbor Steel LLC Invoice no."): its own words
        # are the last ones.
        return 0.8
    if heading and len(b) >= 3 and a.startswith(b):
        return 0.7
    mine, theirs = _figures(cell), _figures(text) if figures is None else figures
    if mine or theirs:
        return 0.95 if mine and mine == theirs else 0.0
    if min(len(a), len(b)) < 3:
        return 0.0
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if matcher.real_quick_ratio() < ALIKE or matcher.quick_ratio() < ALIKE:
        return 0.0
    ratio = matcher.ratio()
    return ratio if ratio >= ALIKE else 0.0


def _map_rows(
    rows: list[list[str]], found: list[dict], *, below: float = -1.0, above: float = 2.0, heading: bool = False, by_place: bool = True
) -> list[list[int | None]]:
    """For each cell, the index of the box it was read from, or None. Rows go down the page in order: each row is
    the line holding most of its cells, below the row before it, so a value printed on several rows is taken from
    its own. ``below``/``above`` keep the rows between two heights; ``heading`` takes the line nearest ``above``.
    ``by_place``: a cell whose text no box has (a figure OCR misread) takes the box in its row and column."""
    plains = [_plain(region["text"]) for region in found]
    numbers = [_figures(region["text"]) for region in found]
    exact: dict[str, list[int]] = {}
    for index, key in enumerate(plains):
        exact.setdefault(key, []).append(index)
    used: set[int] = set()
    out: list[list[int | None]] = []
    anchors: list[int | None] = []
    choices: list[list[list[tuple[float, int]]]] = []
    floor = below
    for cells in rows:
        options: list[list[tuple[float, int]]] = []
        for cell in cells:
            key = _plain(cell)
            if not key:
                options.append([])
                continue
            hits = [(1.0, index) for index in exact.get(key, [])]
            if not hits or heading:
                hits += [
                    (score, index)
                    for index, region in enumerate(found)
                    if plains[index] != key and (score := _score(cell, region["text"], heading=heading, plain=plains[index], figures=numbers[index]))
                ]
            hits = sorted(((score, index) for score, index in hits if index not in used and floor < _middle(found[index]) < above), key=lambda hit: _middle(found[hit[1]]))
            # The row is the next one down the page (the last one up, for a heading): a figure repeated all down a
            # wide sheet need only be looked for on the next few lines.
            options.append(hits[-NEAREST:] if heading else hits[:NEAREST])
        best: tuple[tuple[float, float], int] | None = None
        for option in options:
            for _likeness, index in option:
                line = found[index]
                total = sum(max((score for score, other in each if _same_line(line, found[other])), default=0.0) for each in options)
                rank = (round(total, 3), _middle(line) if heading else -_middle(line))
                if best is None or rank > best[0]:
                    best = (rank, index)
        choices.append(options)
        if best is None:
            anchors.append(None)
            out.append([None] * len(cells))
            continue
        anchor = found[best[1]]
        anchors.append(best[1])
        picked: list[int | None] = []
        for option in options:
            on = sorted(((score, index) for score, index in option if _same_line(anchor, found[index]) and index not in used), reverse=True)
            # Two boxes as like the cell on one line ("185.00" as unit price and amount) wait for the columns.
            if on and (len(on) == 1 or on[0][0] > on[1][0]):
                picked.append(on[0][1])
                used.add(on[0][1])
            else:
                picked.append(None)
        out.append(picked)
        floor = _middle(anchor) + 0.4 * anchor["h"]
    # Where each column is on the page, from the cells placed so far.
    columns: dict[int, float] = {}
    for column in range(max((len(cells) for cells in rows), default=0)):
        placed = [found[row[column]] for row in out if column < len(row) and row[column] is not None]
        if placed:
            columns[column] = statistics.median(region["x"] + region["w"] / 2 for region in placed)
    widths = [region["w"] for row in out for region in (found[i] for i in row if i is not None)]
    reach = max(statistics.median(widths) if widths else 0.05, 0.03)
    for cells, options, picked, anchor_index in zip(rows, choices, out, anchors):
        if anchor_index is None:
            continue
        anchor = found[anchor_index]
        for column, cell in enumerate(cells):
            if picked[column] is not None or not _plain(cell) or column not in columns:
                continue
            spot = columns[column]
            near = [index for _likeness, index in options[column] if index not in used and _same_line(anchor, found[index])]
            if not near and by_place and not heading:
                # A figure read differently by OCR: the box in this row and this column.
                near = [
                    index
                    for index, region in enumerate(found)
                    if index not in used and _same_line(anchor, region) and region["x"] - reach <= spot <= region["x"] + region["w"] + reach
                ]
            if near:
                index = min(near, key=lambda i: abs(found[i]["x"] + found[i]["w"] / 2 - spot))
                picked[column] = index
                used.add(index)
    return out


def _part_of(whole: str, part: str) -> bool:
    a, b = _plain(whole), _plain(part)
    return a != b and len(b) >= 3 and (a.startswith(b) or a.endswith(b))


def _box(found: list[dict], indices: Iterable[int | None]) -> dict | None:
    boxes = [found[index] for index in indices if index is not None]
    if not boxes:
        return None
    x0 = max(0.0, min(region["x"] for region in boxes) - PAD)
    y0 = max(0.0, min(region["y"] for region in boxes) - PAD)
    x1 = min(1.0, max(region["x"] + region["w"] for region in boxes) + PAD)
    y1 = min(1.0, max(region["y"] + region["h"] for region in boxes) + PAD)
    return {"x": round(x0, 5), "y": round(y0, 5), "w": round(x1 - x0, 5), "h": round(y1 - y0, 5)}


_ITEMS = re.compile(r"\b(description|items?|qty|quantity|unit|price|rate|amount|hours|product|service|sku|part)\b", re.I)
_TOTALS = re.compile(r"^\s*(sub-?\s?total|total|(sales\s*)?tax|vat|gst|hst|amount\s*due|balance(\s*due)?|shipping|freight|discount|grand\s*total)", re.I)


def _label(columns: list[str], rows: list[list[str]], heading: str, number: int) -> str:
    """What to call a table: "Line items", "Totals", its printed heading, "Details" for a list of labels and values."""
    if sum(1 for column in columns if _ITEMS.search(column)) >= 2:
        return "Line items"
    firsts = [next((cell for cell in row if cell.strip()), "") for row in rows]
    if firsts and all(_TOTALS.match(first) for first in firsts):
        return "Totals"
    if heading:
        return heading
    if len(columns) == 2 and sum(1 for first in firsts if first and not re.search(r"\d", first)) >= 0.7 * len(firsts):
        return "Details"
    return f"Table {number}"


def _table(number: int, columns: list[str], rows: list[list[str]], found: list[dict], heading: str = "", *, by_place: bool = True) -> dict | None:
    """A table with each cell tied to its box; None when too few of its cells are on the page to show it there. A
    PDF's own text is exact, so there a cell is only ever tied to a box with its text (``by_place`` False)."""
    rows = rows[:MAX_ROWS]
    width = max([len(columns), *(len(row) for row in rows)], default=0)
    rows = [[*row, *[""] * (width - len(row))] for row in rows]
    columns = [*columns, *[""] * (width - len(columns))]
    placed = _map_rows(rows, found, by_place=by_place)
    filled = sum(1 for row in rows for cell in row if _plain(cell))
    hits = sum(1 for row, where in zip(rows, placed) for cell, index in zip(row, where) if _plain(cell) and index is not None)
    if len(rows) < 2 or not filled or hits / filled < FOUND_SHARE or sum(1 for where in placed if any(i is not None for i in where)) < 2:
        return None
    top = min((_middle(found[i]) - found[i]["h"] / 2 for where in placed for i in where if i is not None), default=0.0)
    header: list[int | None] = [None] * width
    if any(_plain(column) for column in columns):
        header = _map_rows([columns], found, above=top + 0.001, heading=True)[0]
        # Only a line close above the first row is its heading.
        header = [index if index is not None and top - _middle(found[index]) < 0.08 else None for index in header]
        # A heading read run together with what is printed above it shows as the box's own words.
        columns = [found[index]["text"] if index is not None and _part_of(column, found[index]["text"]) else column for column, index in zip(columns, header)]
    box = _box(found, [*header, *(index for where in placed for index in where)])
    return {
        "id": f"t{number}",
        "label": _label(columns, rows, heading, number),
        "box": box,
        "columns": columns,
        "column_regions": header,
        "rows": [[{"text": cell, "region": index} for cell, index in zip(row, where)] for row, where in zip(rows, placed)],
    }


def _heading_before(lines: list[str], at: int) -> str:
    """The heading printed just above a table read from a file's text ("[heading]" then its words), or ""."""
    index = at - 1
    skipped = 0
    while index > 0 and skipped < 3:
        line = lines[index].strip()
        if line.startswith("[heading]"):
            return ""
        if line and not line.startswith("[") and " | " not in line:
            return line[:60] if lines[index - 1].strip() == "[heading]" else ""
        skipped += 1
        index -= 1
    return ""


def _text_tables(text: str, page: int, found: list[dict]) -> list[dict]:
    """The tables CloseDesk reads from a PDF's own text that are on this page, tied to its boxes."""
    wanted = {f"page {page}"} | ({""} if page == 1 else set())
    lines = (text or "").splitlines()
    out: list[dict] = []
    for table in table_lookup.tables_in(text) if (text or "").strip() else []:
        rows = [row for row in table.rows if row.page in wanted]
        if not rows:
            continue
        values = [["" if value == tables.BLANK else value for _label_, value in row.cells] for row in rows]
        heading = _heading_before(lines, rows[0].at - 1) if rows[0].at else ""
        made = _table(len(out) + 1, list(table.labels), values, found, heading, by_place=False)
        if made:
            out.append(made)
        if len(out) >= MAX_TABLES:
            break
    return out


def _model_tables(model_text: str, found: list[dict]) -> list[dict]:
    """The vision model's tables on the page, tied to the OCR boxes."""
    out: list[dict] = []
    for block in vision.markdown_blocks(model_text or ""):
        if block["kind"] != "table":
            continue
        header = block["header"]
        # A table printed without headings gets made-up ones ("Column 2"), which aren't on the page.
        made_up = all(not cell or re.fullmatch(r"Column \d+", cell) for cell in header)
        columns = [""] * len(header) if made_up else header
        made = _table(len(out) + 1, columns, [list(row) for row in block["rows"]], found)
        if made:
            out.append(made)
        if len(out) >= MAX_TABLES:
            break
    return out


_NUMBER = re.compile(r"^[-(]?\s*[$€£]?\s*\d[\d,]*(?:\.\d+)?\)?%?$")


def _lines(found: list[dict]) -> list[list[int]]:
    """The boxes grouped into lines down the page, each line left to right."""
    out: list[list[int]] = []
    for index in sorted(range(len(found)), key=lambda i: _middle(found[i])):
        if out and _same_line(found[out[-1][0]], found[index]):
            out[-1].append(index)
        else:
            out.append([index])
    return [sorted(line, key=lambda i: found[i]["x"]) for line in out]


def _ocr_tables(found: list[dict]) -> list[dict]:
    """Tables in OCR's boxes alone: at least three lines in a row whose boxes line up in three or more columns, one
    of them a column of figures."""
    lines = _lines(found)
    heights = [region["h"] for region in found]
    step = 3 * (statistics.median(heights) if heights else 0.02)
    runs: list[list[list[int]]] = []
    for line in lines:
        close = runs and runs[-1] and _middle(found[line[0]]) - _middle(found[runs[-1][-1][0]]) <= step
        if len(line) >= 2 and close:
            runs[-1].append(line)
        else:
            runs.append([line] if len(line) >= 2 else [])
    out: list[dict] = []
    for run in runs:
        core = [line for line in run if len(line) >= 3]
        if len(core) < 3:
            continue
        # Columns: where the boxes of the fuller lines overlap across lines.
        spans = sorted((found[i]["x"], found[i]["x"] + found[i]["w"]) for line in core for i in line)
        bands: list[list[float]] = []
        for x0, x1 in spans:
            if bands and x0 <= bands[-1][1]:
                bands[-1][1] = max(bands[-1][1], x1)
            else:
                bands.append([x0, x1])
        if len(bands) < 3:
            continue

        def band_of(index: int) -> int:
            region = found[index]
            overlap = [min(region["x"] + region["w"], b1) - max(region["x"], b0) for b0, b1 in bands]
            best = max(range(len(bands)), key=lambda b: overlap[b])
            return best if overlap[best] > -0.01 else -1

        grid: list[list[int | None]] = []
        for line in run:
            cells: list[int | None] = [None] * len(bands)
            fits = True
            for index in line:
                band = band_of(index)
                if band < 0 or cells[band] is not None:
                    fits = False
                    break
                cells[band] = index
            if not fits:
                if len([row for row in grid if sum(i is not None for i in row) >= 3]) >= 3:
                    break
                grid = []
                continue
            grid.append(cells)
        full = [row for row in grid if sum(i is not None for i in row) >= 3]
        shared = sum(1 for band in range(len(bands)) if sum(1 for row in full if row[band] is not None) >= 3)
        # And a column of figures: an address printed beside a list of details lines up too, but holds none.
        counted = max(sum(1 for row in full if row[band] is not None and _NUMBER.match(found[row[band]]["text"].strip())) for band in range(len(bands)))
        if len(full) < 3 or shared < 3 or counted < 3:
            continue
        # Drop lines before the first full one and after the last, unless they are totals under it.
        first = grid.index(full[0])
        grid = grid[first:]
        texts = [[found[i]["text"] if i is not None else "" for i in row] for row in grid]
        columns = [""] * len(bands)
        header = [None] * len(bands)
        if not any(_figures(cell) for cell in texts[0]):
            columns, header = texts[0], grid[0]
            texts, grid = texts[1:], grid[1:]
        box = _box(found, [*header, *(i for row in grid for i in row)])
        out.append(
            {
                "id": f"t{len(out) + 1}",
                "label": _label(columns, texts, "", len(out) + 1),
                "box": box,
                "columns": columns,
                "column_regions": header,
                "rows": [[{"text": text, "region": index} for text, index in zip(row, where)] for row, where in zip(texts, grid)],
            }
        )
        if len(out) >= MAX_TABLES:
            break
    return out


def _inside(region: dict, box: dict) -> bool:
    cx, cy = region["x"] + region["w"] / 2, region["y"] + region["h"] / 2
    return box["x"] <= cx <= box["x"] + box["w"] and box["y"] <= cy <= box["y"] + box["h"]


def _overlap(a: dict, b: dict) -> float:
    """How much two boxes cover the same place: shared area over their combined area, 0 to 1."""
    x0, y0 = max(a["x"], b["x"]), max(a["y"], b["y"])
    x1, y1 = min(a["x"] + a["w"], b["x"] + b["w"]), min(a["y"] + a["h"], b["y"] + b["h"])
    shared = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    whole = a["w"] * a["h"] + b["w"] * b["h"] - shared
    return shared / whole if whole > 0 else 0.0


def _within(small: dict, big: dict) -> float:
    """How much of ``small`` lies inside ``big``, 0 to 1."""
    x0, y0 = max(small["x"], big["x"]), max(small["y"], big["y"])
    x1, y1 = min(small["x"] + small["w"], big["x"] + big["w"]), min(small["y"] + small["h"], big["y"] + big["h"])
    area = small["w"] * small["h"]
    return max(0.0, x1 - x0) * max(0.0, y1 - y0) / area if area > 0 else 0.0


def table_from_box(found: list[dict], box: dict, number: int, label: str = "") -> dict | None:
    """A table made from the boxes inside ``box`` (a table the layout model saw, or one drawn by the user): lines
    down the page, columns where the boxes line up across the lines, the first line its headings when it holds no
    figures. Two boxes in one column of a line are one cell, pointing at the first. None when nothing was read
    inside."""
    inside = [index for index, region in enumerate(found) if _inside(region, box)]
    if not inside:
        return None
    lines = [[inside[i] for i in line] for line in _lines([found[index] for index in inside])]
    # Columns come from the fullest lines (the rows): a totals label printed across two columns would join them.
    most = max(len(line) for line in lines)
    core = [line for line in lines if len(line) == most]
    if len(core) < 2:
        core = [line for line in lines if len(line) >= most - 1]
    spans = sorted((found[i]["x"], found[i]["x"] + found[i]["w"]) for line in core for i in line)
    bands: list[list[float]] = []
    for x0, x1 in spans:
        if bands and x0 <= bands[-1][1]:
            bands[-1][1] = max(bands[-1][1], x1)
        else:
            bands.append([x0, x1])

    def band_of(index: int) -> int:
        """The column a box is in: the one it overlaps most, else (between columns) the nearest one."""
        region = found[index]
        left, right = region["x"], region["x"] + region["w"]
        return max(range(len(bands)), key=lambda b: min(right, bands[b][1]) - max(left, bands[b][0]))

    grid: list[list[int | None]] = []
    texts: list[list[str]] = []
    for line in lines:
        cells: list[int | None] = [None] * len(bands)
        words = [""] * len(bands)
        for index in line:
            band = band_of(index)
            cells[band] = index if cells[band] is None else cells[band]
            words[band] = f"{words[band]} {found[index]['text']}".strip()
        grid.append(cells)
        texts.append(words)
    columns, header = [""] * len(bands), [None] * len(bands)
    if len(grid) > 1 and not any(_figures(cell) for cell in texts[0]):
        columns, header = texts[0], grid[0]
        texts, grid = texts[1:], grid[1:]
    drawn = _box(found, [*header, *(i for row in grid for i in row)]) or {key: round(box[key], 5) for key in ("x", "y", "w", "h")}
    return {
        "id": f"t{number}",
        "label": label or _label(columns, texts, "", number),
        "box": drawn,
        "columns": columns,
        "column_regions": header,
        "rows": [[{"text": text, "region": index} for text, index in zip(row, where)] for row, where in zip(texts, grid)],
    }


def _grown(found: list[dict], box: dict) -> dict:
    """The layout model's box, taken down over the lines printed close under it inside its width that hold a figure:
    a table's last total, which its box can stop just short of."""
    inside = [region for region in found if _inside(region, box)]
    if not inside:
        return box
    heights = sorted(region["h"] for region in inside)
    step = 2.2 * heights[len(heights) // 2]
    bottom = max(region["y"] + region["h"] for region in inside)
    grown = dict(box)
    below = sorted((region for region in found if not _inside(region, box) and region["y"] > bottom - 0.002), key=_middle)
    while below:
        first = below[0]
        line = [region for region in below if _same_line(first, region)]
        within = all(box["x"] - 0.01 <= region["x"] and region["x"] + region["w"] <= box["x"] + box["w"] + 0.01 for region in line)
        if first["y"] - bottom > step or not within or not any(_figures(region["text"]) for region in line):
            break
        bottom = max(region["y"] + region["h"] for region in line)
        grown["h"] = min(bottom + 0.004, 1.0) - grown["y"]
        below = [region for region in below if region not in line]
    return grown


def _seen_tables(found: list[dict], seen: list[dict], already: list[dict]) -> list[dict]:
    """Tables the layout model saw that aren't one of ``already``: each made from the boxes inside it, when it has
    at least two lines of at least two cells (a heading or a paragraph it took for a table isn't shown)."""
    out: list[dict] = []
    for box in seen:
        if any(table.get("box") and (_overlap(table["box"], box) >= 0.3 or _within(table["box"], box) >= 0.6 or _within(box, table["box"]) >= 0.6) for table in [*already, *out]):
            continue
        made = table_from_box(found, _grown(found, box), len(already) + len(out) + 1)
        if made is None:
            continue
        full = [row for row in made["rows"] if sum(1 for cell in row if cell["text"]) >= 2]
        if len(full) + (1 if any(made["columns"]) else 0) < 2 or len(made["columns"]) < 2:
            continue
        made["found_by"] = "layout"
        out.append(made)
        if len(already) + len(out) >= MAX_TABLES:
            break
    return out


def page_tables(found: list[dict], source: str, text: str, page: int, model_text: str | None, seen: list[dict] | None = None) -> list[dict]:
    """The page's tables: [{id, label, box, columns, column_regions, rows: [[{text, region}]]}], ``region`` being the
    index of the cell's box in ``found`` (or None), ``box`` the table's outline (fractions of the page). ``seen``:
    where the layout model saw tables (``table_finder``), or None without it. A table read from the PDF's own text or
    by the vision model comes first; one the layout model saw that none of those is added from the boxes inside it.
    On a scan the vision model hasn't read, without the layout model, a table is only where OCR's boxes line up."""
    if not found:
        return []
    if source == "text":
        tables_ = _text_tables(text, page, found)
    elif model_text:
        tables_ = _model_tables(model_text, found)
    elif seen is None:
        return _ocr_tables(found)
    else:
        tables_ = []
    if seen:
        tables_ += _seen_tables(found, seen, tables_)
    return tables_


# Key details ------------------------------------------------------------------------------------

_END = r"(?![a-z])"
# Each detail's printed labels, the most telling first.
DETAILS: list[tuple[str, list[str], str]] = [
    ("Invoice no.", [r"invoice\s*(?:no\.?|number|num\.?|#)", r"inv\.?\s*(?:no\.?|#)"], "id"),
    ("Invoice date", [r"invoice\s*date", r"date\s*of\s*invoice", r"(?:issue|bill(?:ing)?)\s*date", r"date"], "date"),
    ("Due date", [r"(?:payment\s*)?due\s*date", r"date\s*due", r"payment\s*due", r"due\s*(?:on|by)?"], "date"),
    ("PO number", [r"p\.?\s*o\.?\s*(?:no\.?|number|num\.?|#)", r"purchase\s*order(?:\s*(?:no\.?|number|#))?", r"p\.?o\.?(?!\s*box)"], "id"),
    ("Vendor", [], "name"),
    ("Subtotal", [r"sub-?\s*total"], "money"),
    ("Tax", [r"(?:sales\s*)?tax(?:\s*\(?\s*\d+(?:\.\d+)?\s*%\s*\)?)?", r"(?:vat|gst|hst)(?:\s*\(?\s*\d+(?:\.\d+)?\s*%\s*\)?)?"], "money"),
    ("Total due", [r"(?:total|amount|balance)\s*due", r"(?:invoice|grand)\s*total", r"total\s*amount", r"amount\s*payable", r"total"], "money"),
]


def _value_ok(kind: str, value: str) -> bool:
    value = value.strip()
    if not value or len(value) > 60:
        return False
    if kind == "id":
        return bool(re.search(r"\d", value)) and len(value) <= 30 and not table_lookup._dates(value)
    if kind == "date":
        return bool(table_lookup._dates(value))
    if kind == "money":
        return bool(re.match(r"[-(]?\s*[$€£]?\s*\d", value)) and bool(_figures(value)) and "%" not in value
    return True


def _labelled(text: str, patterns: list[str]) -> tuple[int, str] | None:
    """Which of the labels the text starts with (its rank) and what follows it, or None."""
    for rank, pattern in enumerate(patterns):
        match = re.match(rf"\s*(?:{pattern}){_END}\s*[:#.]?\s*(.*)$", text, re.I | re.S)
        if match:
            return rank, match.group(1).strip()
    return None


def _beside(found: list[dict], label: int, kind: str) -> int | None:
    """The box holding a label's value: the nearest on its line to its right, else the one just below it."""
    me = found[label]
    right = [
        index
        for index, region in enumerate(found)
        if index != label and _same_line(me, region) and region["x"] >= me["x"] + me["w"] - 0.005 and region["x"] - (me["x"] + me["w"]) < 0.5
    ]
    for index in sorted(right, key=lambda i: found[i]["x"]):
        if _value_ok(kind, found[index]["text"]):
            return index
        break
    bottom = me["y"] + me["h"]
    under = [
        index
        for index, region in enumerate(found)
        if index != label and bottom - 0.002 <= region["y"] <= bottom + 2.5 * me["h"] and abs(region["x"] - me["x"]) < 0.06
    ]
    for index in sorted(under, key=lambda i: found[i]["y"]):
        if _value_ok(kind, found[index]["text"]):
            return index
        break
    return None


def _model_facts(model_text: str) -> dict[str, str]:
    """The details the vision model read: "Label: value" lines, and a table row's label and its last value."""
    pairs: list[tuple[str, str]] = []
    for block in vision.markdown_blocks(model_text or ""):
        if block["kind"] == "text":
            label, colon, value = block["text"].partition(":")
            if colon:
                pairs.append((label.strip(), value.strip()))
            continue
        for row in [block["header"], *block["rows"]]:
            filled = [cell.strip() for cell in row if cell and cell.strip()]
            if len(filled) >= 2:
                pairs.append((filled[0], filled[-1]))
    out: dict[str, str] = {}
    for name, patterns, kind in DETAILS:
        best: tuple[int, str] | None = None
        for label, value in pairs:
            found = _labelled(label, patterns)
            if found and found[1] == "" and _value_ok(kind, value) and (best is None or found[0] < best[0]):
                best = (found[0], value)
        if best:
            out[name] = best[1]
    return out


def _find_value(found: list[dict], value: str, kind: str) -> int | None:
    """The box that holds a value (the same text, figure or date), the first down the page."""
    key = _plain(value)
    if not key:
        return None
    order = sorted(range(len(found)), key=lambda i: (found[i]["y"], found[i]["x"]))
    if kind == "money":
        wanted = _figures(value)
        return next((i for i in order if wanted and wanted <= _figures(found[i]["text"])), None)
    if kind == "date":
        wanted = set(table_lookup._dates(value))
        return next((i for i in order if wanted & set(table_lookup._dates(found[i]["text"]))), None)
    same = next((i for i in order if key == _plain(found[i]["text"])), None)
    return same if same is not None else next((i for i in order if len(key) >= 4 and key in _plain(found[i]["text"])), None)


_SUFFIXES = frozenset("ar a/r billing accounts receivable invoices invoice team dept department llc inc ltd co corp company the payments finance".split())


def _vendor(found: list[dict], names: list[str]) -> int | None:
    """The box where the vendor's name is printed: the highest whose words start with a known name's own words
    ("Harbor Steel AR" is "Harbor Steel LLC")."""
    for name in names:
        words = [word for word in re.findall(r"[a-z0-9&'-]+", name.lower()) if word not in _SUFFIXES]
        if not words or len("".join(words)) < 4:
            continue
        key = "".join(words[:3])
        order = sorted(range(len(found)), key=lambda i: found[i]["y"])
        hit = next((i for i in order if _plain(found[i]["text"]).startswith(key) and found[i]["y"] < 0.5), None)
        if hit is not None:
            return hit
    return None


def key_details(found: list[dict], source: str, model_text: str | None, extracted: dict[str, list[str]]) -> list[dict]:
    """The invoice's key details on this page: [{label, value, region}], only those found here. ``extracted`` holds
    what CloseDesk read from the email and file: {"invoices", "pos", "due_dates", "vendors"}."""
    if not found:
        return []
    facts = _model_facts(model_text) if source == "ocr" and model_text else {}
    out: list[dict] = []
    for name, patterns, kind in DETAILS:
        region: int | None = None
        value = ""
        if patterns:
            labels = []
            for index, box in enumerate(found):
                hit = _labelled(box["text"], patterns)
                if hit is not None:
                    labels.append((hit[0], index, hit[1]))
            # The most telling label; of two the same, the highest on the page (the last for a total).
            labels.sort(key=lambda item: (item[0], -found[item[1]]["y"] if name == "Total due" else found[item[1]]["y"]))
            for _rank, index, rest in labels:
                if rest:
                    if _value_ok(kind, rest):
                        region, value = index, rest
                        break
                    continue
                beside = _beside(found, index, kind)
                if beside is not None:
                    region, value = beside, found[beside]["text"]
                    break
        if name in facts:
            if region is None:
                region = _find_value(found, facts[name], kind)
            if region is not None:
                value = facts[name]
        if region is None:
            if name == "Vendor":
                region = _vendor(found, extracted.get("vendors", []))
                value = found[region]["text"] if region is not None else ""
            else:
                known = {"Invoice no.": "invoices", "PO number": "pos", "Due date": "due_dates"}.get(name)
                for candidate in extracted.get(known, []) if known else []:
                    region = _find_value(found, _us_date(candidate) if kind == "date" else candidate, kind)
                    if region is not None:
                        value = found[region]["text"] if kind == "date" else candidate
                        break
        if region is not None and value:
            out.append({"label": name, "value": value, "region": region})
    return out


def _us_date(iso: str) -> str:
    """2026-10-30 as 10/30/2026, the way it is compared with the page."""
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", iso or "")
    return f"{int(match.group(2))}/{int(match.group(3))}/{match.group(1)}" if match else iso
