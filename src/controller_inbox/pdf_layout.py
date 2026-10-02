"""A PDF page's characters, by position, back into words, lines and table rows.

PDFs don't store words or tables, only where each character is drawn. Reading them as a
text stream gives two kinds of wrong text a model then repeats:

* spaced-out letters ("J o h n  S m i t h") when a heading is letter-spaced or each
  character is placed on its own, and
* shifted tables: a sheet saved as PDF has no separators, so a blank cell vanishes and the
  values after it slide one column left (the manager's name read as the department), and
* justified lines and short two-column pages, whose wide word spaces otherwise get read as columns.

Here each character keeps its box (pdfminer). A word ends where the gap is clearly wider than
the usual gap between letters on that line, so letter-spacing doesn't split words. Lines whose
pieces sit under the same columns as the lines around them are a table: every piece goes to
the column it sits under, blanks stay blank, and with a header row each row names its
columns (see ``tables``). A heading or a second table ends that run, and a page with more
than one kind of text is labeled ``[heading]``, ``[facts]``, ``[table]``, ``[notes]`` or
``[columns]`` so a small model can read the part that holds the figure. A label column
printed beside a table is peeled off into ``[facts]``. A bold group label stays inside its
table. The next page is extra columns only when it starts with a different first column.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from controller_inbox import tables

_CID = re.compile(r"\(cid:\d+\)")


@dataclass
class Glyph:
    text: str
    x0: float
    x1: float
    mid: float
    size: float
    bold: bool


@dataclass
class Word:
    text: str
    x0: float
    x1: float
    bold: bool


@dataclass
class Line:
    mid: float
    size: float
    glyphs: list[Glyph] = field(default_factory=list)
    segments: list[list[Word]] = field(default_factory=list)

    @property
    def bold(self) -> bool:
        words = [word for segment in self.segments for word in segment]
        return bool(words) and all(word.bold for word in words)

    def plain(self) -> str:
        """The line as prose: pieces far apart are kept apart with " | ", a label ending in ":" joins its value."""
        out = ""
        for index, segment in enumerate(self.segments):
            text = " ".join(word.text for word in segment)
            if index:
                gap = segment[0].x0 - self.segments[index - 1][-1].x1
                out += " " if out.endswith(":") or gap < 1.5 * self.size else " | "
            out += text
        return out


@dataclass
class Table:
    """A table's header and row heights, so a wide sheet printed across pages can be put back together."""

    header_mid: float | None
    rows: dict[int, str]
    row_label: str


@dataclass
class PageText:
    text: str
    tables: list[Table]
    unreadable: bool = False


def glyphs_of(layout) -> list[Glyph]:
    """Characters of a pdfminer page (``extract_pages(..., laparams=None)``).

    A name turned on its side (a schedule's column headings) is not upright. Those letters
    are gathered into the word and set down at the foot of the column, on one line with the
    other names, so the row is readable instead of dropped.
    """
    from pdfminer.layout import LTChar

    out: list[Glyph] = []
    turned: list[tuple[str, float, float, float, float, float, bool, bool]] = []

    def walk(item) -> None:
        if isinstance(item, LTChar):
            text = item.get_text()
            if text and item.x1 > item.x0 and item.y1 > item.y0:
                font = (item.fontname or "").lower()
                bold = any(mark in font for mark in ("bold", "black", "heavy", "semibold", "demi"))
                if item.upright:
                    out.append(Glyph(text, item.x0, item.x1, (item.y0 + item.y1) / 2, max(item.size, 1.0), bold))
                else:
                    _a, b, _c, _d, _e, _f = item.matrix
                    turned.append((text, item.x0, item.x1, item.y0, item.y1, max(item.size, 1.0), bold, b > 0))
            return
        try:
            children = list(item)
        except TypeError:
            return
        for child in children:
            walk(child)

    walk(layout)
    out.extend(_turned_words(turned))
    return out


def _turned_words(chars: list[tuple[str, float, float, float, float, float, bool, bool]]) -> list[Glyph]:
    """Letters stacked in one column, read in the direction they were drawn, as one word."""
    if not chars:
        return []
    chars = sorted(chars, key=lambda item: (item[1] + item[2]) / 2)
    columns: list[list[tuple]] = [[chars[0]]]
    for char in chars[1:]:
        size = max(char[5], 1.0)
        centre = (char[1] + char[2]) / 2
        previous = (columns[-1][-1][1] + columns[-1][-1][2]) / 2
        if abs(centre - previous) <= 0.8 * size:
            columns[-1].append(char)
        else:
            columns.append([char])
    words: list[Glyph] = []
    for column in columns:
        column.sort(key=lambda item: item[3])
        runs: list[list[tuple]] = [[column[0]]]
        for char in column[1:]:
            # A short letter such as "i" has a small box. The gap that ends a word is about
            # a whole letter, measured from the wider of the two boxes, not from that short one.
            previous = runs[-1][-1]
            scale = max(char[2] - char[1], char[4] - char[3], previous[2] - previous[1], previous[4] - previous[3], 1.0)
            # A space inside a name is about one letter. A new name in the same column
            # leaves a clearly larger gap, so "Maya Chen" stays one heading.
            if char[3] - previous[4] > 1.8 * scale:
                runs.append([char])
            else:
                runs[-1].append(char)
        for run in runs:
            if len(run) < 2:
                continue
            upward = sum(1 for item in run if item[7]) >= len(run) / 2
            ordered = sorted(run, key=lambda item: item[3] if upward else -item[3])
            pieces = [ordered[0][0]]
            for previous, item in zip(ordered, ordered[1:]):
                gap = (item[3] - previous[4]) if upward else (previous[3] - item[4])
                scale = max(item[2] - item[1], item[4] - item[3], previous[2] - previous[1], previous[4] - previous[3], 1.0)
                if gap > 0.55 * scale:
                    pieces.append(" ")
                pieces.append(item[0])
            name = "".join(pieces).strip()
            if not name:
                continue
            x0, x1 = min(item[1] for item in run), max(item[2] for item in run)
            size = statistics.median(item[5] for item in run)
            foot = min(item[3] for item in run)
            words.append(Glyph(name, x0, x1, foot + size * 0.35, max(size, 1.0), any(item[6] for item in run)))
    return words


def page_text(glyphs: list[Glyph], previous: list[Table] | None = None) -> PageText:
    """The page as text: plain lines, and tables row by row. ``previous``: the tables on the page before."""
    if not glyphs:
        return PageText("", [])
    if sum(1 for g in glyphs if _CID.fullmatch(g.text)) > 0.2 * len(glyphs):
        return PageText("", [], unreadable=True)
    lines = _lines([g for g in glyphs if not _CID.fullmatch(g.text)])
    for line in lines:
        line.segments = _segments(line)
    lines = [line for line in lines if line.segments]
    chunks: list[tuple[str, list[str]]] = []
    found: list[Table] = []
    index = 0
    for start, end in _table_blocks(lines):
        chunks.extend(_prose_chunks(lines[index:start]))
        text, table, facts = _table(lines[start:end], None if found else previous)
        if facts:
            chunks.append(("facts", facts))
        if table:
            found.append(table)
        if text:
            chunks.append((_chunk_role(text, table), text))
        index = end
    chunks.extend(_prose_chunks(lines[index:]))
    return PageText(_render_sections(chunks), found)


# Lines and words -------------------------------------------------------------------------------


def _lines(glyphs: list[Glyph]) -> list[Line]:
    lines: list[Line] = []
    for glyph in sorted(glyphs, key=lambda g: -g.mid):
        for line in reversed(lines[-3:]):
            if abs(line.mid - glyph.mid) <= 0.45 * min(line.size, glyph.size):
                line.glyphs.append(glyph)
                break
        else:
            lines.append(Line(glyph.mid, glyph.size, [glyph]))
    for line in lines:
        line.size = statistics.median(g.size for g in line.glyphs)
        line.glyphs = _dedupe(sorted(line.glyphs, key=lambda g: g.x0))
    return lines


def _dedupe(glyphs: list[Glyph]) -> list[Glyph]:
    """Drop the second copy of text drawn twice a hair apart (a common way to fake bold)."""
    out: list[Glyph] = []
    for glyph in glyphs:
        if any(other.text == glyph.text and abs(other.x0 - glyph.x0) < 0.2 * glyph.size for other in out[-3:]):
            continue
        out.append(glyph)
    return out


def _segments(line: Line) -> list[list[Word]]:
    """The line's words, grouped into pieces that are separated by a wide gap (table cells)."""
    glyphs = [g for g in line.glyphs if g.text.strip()]
    if not glyphs:
        return []
    em = line.size
    gaps = [b.x0 - a.x1 for a, b in zip(glyphs, glyphs[1:])]
    # Letter-spacing and kerning sit well under half an em. A word space is wider than that, and a
    # column break is wider again. Justified prose stretches every word space, so the column break
    # has to sit above the line's own word spacing or the sentence is read as a table.
    tight = sorted(gap for gap in gaps if gap < 0.5 * em)
    letter = min(max(tight[len(tight) // 4], 0.0), 0.45 * em) if len(tight) >= 4 else 0.0
    word_gap = max(letter + 0.12 * em, 0.16 * em)
    wordish = [gap for gap in gaps if word_gap < gap <= 2.2 * em]
    typical = statistics.median(wordish) if len(wordish) >= 2 else max(0.25 * em, letter + 0.22 * em)
    piece_gap = max(1.35 * em, typical * 2.4, letter + 0.9 * em)
    segments: list[list[Word]] = [[]]
    current = [glyphs[0]]
    for gap, glyph in zip(gaps, glyphs[1:]):
        if gap <= word_gap:
            current.append(glyph)
            continue
        segments[-1].append(_word(current))
        if gap > piece_gap:
            segments.append([])
        current = [glyph]
    segments[-1].append(_word(current))
    return segments


def _word(glyphs: list[Glyph]) -> Word:
    return Word("".join(g.text for g in glyphs).strip(), glyphs[0].x0, glyphs[-1].x1, all(g.bold for g in glyphs))


# Tables ----------------------------------------------------------------------------------------


def _table_blocks(lines: list[Line]) -> list[tuple[int, int]]:
    """Runs of three or more lines that line up in two or more columns: [start, end) into ``lines``."""
    blocks: list[tuple[int, int]] = []
    i = 0
    while i < len(lines):
        if len(lines[i].segments) < 2:
            i += 1
            continue
        end = i + 1
        while end < len(lines) and _continues(lines, i, end):
            end += 1
        if end - i >= 3 and len(_columns(lines[i:end])) >= 2:
            start = i - 1 if i > 0 and _labels_above(lines[i - 1], lines[i:end]) else i
            blocks.append((start, end))
            i = end
        else:
            i += 1
    return blocks


def _continues(lines: list[Line], start: int, index: int) -> bool:
    """Whether line ``index`` is another row of the table that starts at ``start``.

    A line joins only when it sits on the same columns. A bold label after the data, or a
    larger heading, ends the table, so the next section is not read as another row.
    """
    line, before = lines[index], lines[index - 1]
    pitches = [lines[k - 1].mid - lines[k].mid for k in range(start + 1, index)]
    usual = statistics.median(pitches) if pitches else 2.0 * line.size
    gap = before.mid - line.mid
    if gap > max(2.2 * usual, 3.0 * line.size):
        return False
    block = lines[start:index]
    # A noticeably larger line is a section heading, not another row. A group label
    # inside the table is the same size as the rows around it.
    if line.size > statistics.median(ln.size for ln in block) * 1.15:
        return False
    data_started = any(not ln.bold for ln in block)
    words = [word.text for seg in line.segments for word in seg]
    # A new bold header (several cells, no figures) starts a new table. A single bold label
    # is a group row inside this table ("Employees", "Contractors").
    if (
        data_started
        and line.bold
        and len(line.segments) >= 2
        and words
        and not any(tables.is_value(word) for word in words)
    ):
        return False
    columns = _columns(block)
    if len(line.segments) >= 2:
        return _fits_columns(line, columns)
    if gap > 1.6 * usual + 1:
        return False
    x0, x1 = line.segments[0][0].x0, line.segments[0][-1].x1
    under = [c for c in columns if x0 < c[1] and x1 > c[0]]
    return len(under) == 1


def _labels_above(line: Line, block: list[Line]) -> bool:
    """A row of names sitting on the columns, even when it has no cell for the stub column.

    Schedules often print the people sideways in the top row and the districts down the side.
    The districts make one more column than the name row has, so the names would otherwise
    be left off the table.
    """
    columns = _columns(block)
    if len(line.segments) < 2 or len(columns) < 2:
        return False
    words = [word.text for segment in line.segments for word in segment]
    if not words or sum(map(tables.is_value, words)) >= 0.5 * len(words):
        return False
    gap = line.mid - block[0].mid
    usual = statistics.median(block[i].mid - block[i + 1].mid for i in range(len(block) - 1)) if len(block) > 1 else line.size
    if gap <= 0 or gap > max(2.2 * usual, 3.0 * line.size):
        return False
    return all(_owns(segment, _spans(columns)) for segment in line.segments)


def _fits_columns(line: Line, columns: list[tuple[float, float]]) -> bool:
    """Every piece of the line sits in a column this table already has.

    Columns are widened to the gap on either side, so a right-aligned number still belongs
    to the heading drawn at the right edge of the same column.
    """
    if len(columns) < 2:
        return False
    spans = _spans(columns)
    return all(_owns(segment, spans) for segment in line.segments)


def _spans(columns: list[tuple[float, float]]) -> list[tuple[float, float]]:
    spans = []
    for index, (left, right) in enumerate(columns):
        lo = (columns[index - 1][1] + left) / 2 if index else left - 80
        hi = (right + columns[index + 1][0]) / 2 if index + 1 < len(columns) else right + 80
        spans.append((lo, hi))
    return spans


def _owns(segment: list[Word], spans: list[tuple[float, float]]) -> bool:
    centre = (segment[0].x0 + segment[-1].x1) / 2
    return any(lo <= centre <= hi for lo, hi in spans)


def _columns(block: list[Line]) -> list[tuple[float, float]]:
    """Column extents: where the rows' pieces overlap, from all rows, or from the fullest ones
    when a piece running into the next column (overflowing text) joins two columns."""
    most = max(len(line.segments) for line in block)
    merged: list[list[float]] = []
    for rows in (block, [line for line in block if len(line.segments) == most]):
        merged = []
        for x0, x1 in sorted((seg[0].x0, seg[-1].x1) for line in rows for seg in line.segments):
            if merged and x0 <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], x1)
            else:
                merged.append([x0, x1])
        if len(merged) >= most:
            break
    return [(a, b) for a, b in merged]


def _place(segment: list[Word], columns: list[tuple[float, float]]) -> int:
    """The column a piece belongs to: the one it starts in, else the one it overlaps most, else the nearest."""
    x0, x1 = segment[0].x0, segment[-1].x1
    for index, (left, right) in enumerate(columns):
        if left - 1 <= x0 <= right:
            return index
    overlaps = [max(0.0, min(x1, right) - max(x0, left)) for left, right in columns]
    if max(overlaps) > 0:
        return overlaps.index(max(overlaps))
    centre = (x0 + x1) / 2
    return min(range(len(columns)), key=lambda i: min(abs(centre - columns[i][0]), abs(centre - columns[i][1])))


def _table(block: list[Line], previous: list[Table] | None) -> tuple[list[str], Table | None, list[str]]:
    columns = _columns(block)
    grid: list[list[str]] = []
    bolds: list[bool] = []
    for line in block:
        row = [""] * len(columns)
        for segment in line.segments:
            column = _place(segment, columns)
            row[column] = f"{row[column]} {' '.join(word.text for word in segment)}".strip()
        grid.append(row)
        bolds.append(line.bold)
    mids = [line.mid for line in block]

    peeled = _peel_side_facts(grid)
    facts: list[str] = []
    if peeled:
        facts, grid = peeled
        bolds = [False] * len(grid)

    if _prose(grid):
        width = len(grid[0]) if grid else 0
        return _dehyphenate([" ".join(row[c] for row in grid if c < len(row) and row[c]) for c in range(width)]), None, facts

    bold_first = bolds[0] and not all(bolds[1:])
    heads = 1
    while (
        bold_first
        and heads < min(3, len(grid) - 2)
        and bolds[heads]
        and sum(1 for cell in grid[heads] if cell) >= 2
        and not any(tables.is_value(cell) for cell in grid[heads] if cell)
    ):
        heads += 1
    if heads > 1:
        grid = [[" ".join(row[c] for row in grid[:heads] if row[c]) for c in range(len(grid[0]))]] + grid[heads:]
        bolds = [True] + bolds[heads:]
        mids = [mids[0]] + mids[heads:]

    header = tables.has_header(grid, bold_first=bold_first)
    grid, mids = _join_wrapped(grid, mids, first=1 if header else 0)
    labels = grid[0] if header else None
    body, body_mids = (grid[1:], mids[1:]) if header else (grid, mids)

    body_bold = bolds[1:] if header else bolds

    def render(row: list[str], bold: bool = False) -> str:
        filled = [cell for cell in row if cell]
        if bold and len(filled) == 1:
            return f"Group: {filled[0]}"
        return tables.labelled_row(labels, row) if labels else tables.table_lines([row], header=False)[0]

    first_label = (labels[0] if labels else "") or ""
    carried = _carried_over(body_mids, mids[0] if header else None, previous or [], first_label)
    if carried:
        prior, names = carried
        lines = [f"[These columns continue the table on the page before; each row starts with its {prior.row_label}.]"]
        if labels:
            lines.append(" | ".join(label for label in labels if label))
        for row, name, bold in zip(body, names, body_bold):
            line = render(row, bold)
            lines.append(f"{prior.row_label}: {name} | {line}" if name else line)
        return lines, Table(mids[0] if header else None, prior.rows, prior.row_label), facts

    lines = ([" | ".join(label for label in labels if label)] if labels else []) + [
        render(row, bold) for row, bold in zip(body, body_bold)
    ]
    rows = {round(mid): row[0] for mid, row in zip(body_mids, body) if row[0]}
    return lines, Table(mids[0] if header else None, rows, first_label), facts


def _peel_side_facts(grid: list[list[str]]) -> tuple[list[str], list[list[str]]] | None:
    """A label column printed beside a table ("Invoice:" | INV-4471 | Item | Qty | Amount).

    The labels become their own fact lines and the remaining columns stay the table, so the
    two regions are not read as one row.
    """
    if len(grid) < 3 or not grid[0] or len(grid[0]) < 4:
        return None
    if sum(1 for row in grid if row[0].rstrip().endswith(":")) < 0.7 * len(grid):
        return None
    facts = [f"{row[0]} {row[1]}".strip() if row[0].endswith(":") else row[0] for row in grid]
    rest = [row[2:] for row in grid]
    if max((len(row) for row in rest), default=0) < 2:
        return None
    return facts, rest


def _prose(grid: list[list[str]]) -> bool:
    """Two or three columns of running text (a newsletter layout), not a table.

    A short page of two text columns is still prose. A column of amounts, dates or yes/no
    keeps the block a table, so a description sitting next to a figure is not read down the page.
    """
    columns = len(grid[0]) if grid else 0
    if columns < 2 or columns > 3:
        return False
    if len(grid) < (3 if columns == 2 else 5):
        return False
    words = [len(cell.split()) for row in grid for cell in row if cell]
    # Median, so one short wrapped leftover ("twelfth.") does not hide a page of sentences.
    if not words or statistics.median(words) < 5:
        return False
    return not any(_mostly_values(column) for column in zip(*grid))


def _mostly_values(cells: tuple[str, ...]) -> bool:
    filled = [cell for cell in cells if cell.strip()]
    return len(filled) >= 2 and sum(map(tables.is_value, filled)) >= 0.6 * len(filled)


def _chunk_role(text: list[str], table: Table | None) -> str:
    body = [line for line in text if line.strip() and not line.startswith("[")]
    if table is None and _fact_lines(body):
        return "facts"
    if table is None and body and statistics.median(len(line.split()) for line in body) >= 5:
        return "columns"
    if table is None:
        return "notes"
    return "table"


def _fact_lines(lines: list[str]) -> bool:
    if len(lines) < 2:
        return False
    hits = [line for line in lines if "|" not in line and len(line) <= 80 and _FACT.match(line)]
    return len(hits) >= 0.7 * len(lines)


_FACT = re.compile(r"^[A-Za-z][^:]{0,40}: \S")
_PAGE_MARK = re.compile(r"^(?:page\s+\d{1,4}(?:\s+of\s+\d{1,4})?|\d{1,4}\s*/\s*\d{1,4})$", re.I)


def _prose_chunks(lines: list[Line]) -> list[tuple[str, list[str]]]:
    """Headings, key facts and ordinary lines, kept apart so a title is not a table row."""
    if not lines:
        return []
    med = statistics.median(line.size for line in lines)
    groups: list[tuple[str, list[str]]] = []
    role = ""
    buf: list[Line] = []

    def flush() -> None:
        nonlocal buf
        if buf:
            groups.append((role, _dehyphenate([line.plain() for line in buf])))
            buf = []

    for line in lines:
        nxt = _prose_role(line, med)
        if buf and nxt != role:
            flush()
        role = nxt
        buf.append(line)
    flush()
    return groups


def _prose_role(line: Line, med: float) -> str:
    text = line.plain().strip()
    words = text.split()
    if "|" in text:
        return "notes"
    if (line.bold or line.size >= med * 1.2) and 0 < len(words) <= 10 and not text.endswith("."):
        return "heading"
    if "|" not in text and len(text) <= 80 and len(words) <= 6 and _FACT.match(text):
        return "facts"
    return "notes"


def _separate_table(role: str, previous: list[str], new: list[str]) -> bool:
    """A second table with its own header is not more rows of the table above it."""
    if role != "table" or not new or "|" not in new[0]:
        return False
    return new[0] not in previous


def _without_page_marks(lines: list[str]) -> list[str]:
    """Drop a running "Page 3" (or "2/14"). The real page is the [page N] marker around this text."""
    kept = []
    for line in lines:
        parts = [part.strip() for part in line.split("|")]
        parts = [part for part in parts if part and not _PAGE_MARK.match(part)]
        if parts:
            kept.append(" | ".join(parts))
    return kept


def _render_sections(chunks: list[tuple[str, list[str]]]) -> str:
    """One block is plain text. Several blocks are labeled so a model can read the part it needs."""
    merged: list[tuple[str, list[str]]] = []
    for role, lines in chunks:
        lines = _without_page_marks([line for line in lines if line.strip()])
        if not lines:
            continue
        if merged and merged[-1][0] == role and not _separate_table(role, merged[-1][1], lines):
            merged[-1][1].extend(lines)
        else:
            merged.append((role, list(lines)))
    if not merged:
        return ""
    if len(merged) == 1:
        return "\n".join(merged[0][1]).strip()
    return "\n\n".join(f"[{role}]\n" + "\n".join(lines) for role, lines in merged)


def _dehyphenate(lines: list[str]) -> list[str]:
    """A word split at the end of a line ("non-" / "payment") is one word again."""
    out: list[str] = []
    for line in lines:
        if out and out[-1].endswith("-") and not out[-1].endswith(" -") and line[:1].islower():
            out[-1] = out[-1] + line
        else:
            out.append(line)
    return out


def _join_wrapped(grid: list[list[str]], mids: list[float], *, first: int) -> tuple[list[list[str]], list[float]]:
    """Fold a cell's wrapped line into its row: a line with few cells filled that sits closer to a
    fuller neighbouring row than rows usually are to each other."""
    if len(grid) - first < 3:
        return grid, mids
    usual = statistics.median(mids[k - 1] - mids[k] for k in range(first + 1, len(mids)))
    rows, row_mids = [list(row) for row in grid], list(mids)
    k = first
    while k < len(rows):
        row = rows[k]
        filled = sum(1 for cell in row if cell)
        if filled > max(1, len(row) // 2):
            k += 1
            continue
        near = [
            (row_mids[k - 1] - row_mids[k], k - 1) if k - 1 >= first else None,
            (row_mids[k] - row_mids[k + 1], k + 1) if k + 1 < len(rows) else None,
        ]
        near = [
            (gap, target)
            for gap, target in filter(None, near)
            if gap < 0.9 * usual and sum(1 for cell in rows[target] if cell) > filled
        ]
        if not near:
            k += 1
            continue
        target = min(near)[1]
        for c, cell in enumerate(row):
            if cell:
                rows[target][c] = f"{rows[target][c]} {cell}".strip() if target < k else f"{cell} {rows[target][c]}".strip()
        del rows[k], row_mids[k]
        k = max(first, k - 1)
    return rows, row_mids


def _carried_over(
    body_mids: list[float], header_mid: float | None, previous: list[Table], first_label: str = ""
) -> tuple[Table, list[str]] | None:
    """A sheet too wide for one page prints its other columns on the next page, row for row at the
    same heights. Then each row is named by the first column on the page before.

    The same table printed again (the next page starts with the same first column) is not a
    continuation, so its rows are not named twice.
    """
    if not previous or not body_mids:
        return None
    prior = previous[-1]
    if not prior.rows or not prior.row_label:
        return None
    if first_label.strip().lower() == prior.row_label.strip().lower():
        return None
    if (header_mid is None) != (prior.header_mid is None):
        return None
    if header_mid is not None and abs(header_mid - prior.header_mid) > 1.5:
        return None

    def name(mid: float) -> str:
        for key in (round(mid), round(mid) + 1, round(mid) - 1):
            if key in prior.rows:
                return prior.rows[key]
        return ""

    names = [name(mid) for mid in body_mids]
    if sum(1 for n in names if n) < max(2, 0.8 * len(names)):
        return None
    return prior, names
