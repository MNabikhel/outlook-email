"""A PDF page's characters, by position, back into words, lines and table rows.

PDFs don't store words or tables, only where each character is drawn. Reading them as a
text stream gives two kinds of wrong text a model then repeats:

* spaced-out letters ("J o h n  S m i t h") when a heading is letter-spaced or each
  character is placed on its own, and
* shifted tables: a sheet saved as PDF has no separators, so a blank cell vanishes and the
  values after it slide one column left (the manager's name read as the department).

Here each character keeps its box (pdfminer). A word ends where the gap is clearly wider than
the usual gap between letters on that line, so letter-spacing doesn't split words. Lines whose
pieces sit under the same columns as the lines around them are a table: every piece goes to
the column it sits under, blanks stay blank, and with a header row each row names its
columns (see ``tables``).
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
    """Characters of a pdfminer page (``extract_pages(..., laparams=None)``)."""
    from pdfminer.layout import LTChar

    out: list[Glyph] = []

    def walk(item) -> None:
        if isinstance(item, LTChar):
            text = item.get_text()
            if item.upright and text and item.x1 > item.x0:
                font = (item.fontname or "").lower()
                bold = any(mark in font for mark in ("bold", "black", "heavy", "semibold", "demi"))
                out.append(Glyph(text, item.x0, item.x1, (item.y0 + item.y1) / 2, max(item.size, 1.0), bold))
            return
        try:
            children = list(item)
        except TypeError:
            return
        for child in children:
            walk(child)

    walk(layout)
    return out


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
    out: list[str] = []
    found: list[Table] = []
    index = 0
    for start, end in _table_blocks(lines):
        out.extend(line.plain() for line in lines[index:start])
        text, table = _table(lines[start:end], None if found else previous)
        out.extend(["", *text, ""])
        if table:
            found.append(table)
        index = end
    out.extend(line.plain() for line in lines[index:])
    return PageText(re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip(), found)


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
    # The usual gap between letters on this line: about zero, or the tracking of letter-spaced text.
    # Too few gaps to tell (a row of short numbers) means ordinary spacing.
    small = sorted(max(0.0, gap) for gap in gaps if gap < 0.6 * em)
    letter = min(small[len(small) // 4], 0.4 * em) if len(small) >= 4 else 0.0
    word_gap = letter + 0.15 * em
    piece_gap = max(word_gap + 0.35 * em, 2 * letter + 0.55 * em)
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
            blocks.append((i, end))
            i = end
        else:
            i += 1
    return blocks


def _continues(lines: list[Line], start: int, index: int) -> bool:
    """Whether line ``index`` is another row of the table that starts at ``start``."""
    line, before = lines[index], lines[index - 1]
    pitches = [lines[k - 1].mid - lines[k].mid for k in range(start + 1, index)]
    usual = statistics.median(pitches) if pitches else 2.0 * line.size
    gap = before.mid - line.mid
    if len(line.segments) >= 2:
        return gap <= max(2.2 * usual, 3.0 * line.size)
    # A row with one filled cell: it has to sit under a single column, at the table's row spacing.
    if gap > 1.6 * usual + 1:
        return False
    x0, x1 = line.segments[0][0].x0, line.segments[0][-1].x1
    under = [c for c in _columns(lines[start:index]) if x0 < c[1] and x1 > c[0]]
    return len(under) == 1


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


def _table(block: list[Line], previous: list[Table] | None) -> tuple[list[str], Table | None]:
    columns = _columns(block)
    grid: list[list[str]] = []
    for line in block:
        row = [""] * len(columns)
        for segment in line.segments:
            column = _place(segment, columns)
            row[column] = f"{row[column]} {' '.join(word.text for word in segment)}".strip()
        grid.append(row)
    mids = [line.mid for line in block]

    if _prose(grid):
        return [" ".join(row[c] for row in grid if row[c]) for c in range(len(columns))], None

    bold_first = block[0].bold and not all(line.bold for line in block[1:])
    heads = 1
    while bold_first and heads < min(3, len(block) - 2) and block[heads].bold and not any(tables.is_value(c) for c in grid[heads] if c):
        heads += 1
    if heads > 1:
        grid = [[" ".join(row[c] for row in grid[:heads] if row[c]) for c in range(len(columns))]] + grid[heads:]
        mids = [mids[0]] + mids[heads:]

    header = tables.has_header(grid, bold_first=bold_first)
    grid, mids = _join_wrapped(grid, mids, first=1 if header else 0)
    labels = grid[0] if header else None
    body, body_mids = (grid[1:], mids[1:]) if header else (grid, mids)

    def render(row: list[str]) -> str:
        return tables.labelled_row(labels, row) if labels else tables.table_lines([row], header=False)[0]

    carried = _carried_over(body_mids, mids[0] if header else None, previous or [])
    if carried:
        prior, names = carried
        lines = [f"[These columns continue the table on the page before; each row starts with its {prior.row_label}.]"]
        if labels:
            lines.append(" | ".join(label for label in labels if label))
        for row, name in zip(body, names):
            line = render(row)
            lines.append(f"{prior.row_label}: {name} | {line}" if name else line)
        return lines, Table(mids[0] if header else None, prior.rows, prior.row_label)

    lines = ([" | ".join(label for label in labels if label)] if labels else []) + [render(row) for row in body]
    rows = {round(mid): row[0] for mid, row in zip(body_mids, body) if row[0]}
    return lines, Table(mids[0] if header else None, rows, (labels[0] if labels else "") or "")


def _prose(grid: list[list[str]]) -> bool:
    """Two or three columns of running text (a newsletter layout), not a table."""
    if len(grid) < 5 or len(grid[0]) > 3:
        return False
    words = [len(cell.split()) for row in grid for cell in row if cell]
    return bool(words) and statistics.mean(words) >= 6


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


def _carried_over(body_mids: list[float], header_mid: float | None, previous: list[Table]) -> tuple[Table, list[str]] | None:
    """A sheet too wide for one page prints its other columns on the next page, row for row at the
    same heights. Then each row is named by the first column on the page before."""
    if not previous or not body_mids:
        return None
    prior = previous[-1]
    if not prior.rows or not prior.row_label:
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
