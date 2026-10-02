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
columns (see ``tables``). A heading drawn across several columns (a merged header) names each of
those columns, and a category heading names the rows under it, including when categories are
nested. A heading or a second table ends that run, and a page with more
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
    turned: list[_Turned] = []

    def walk(item) -> None:
        if isinstance(item, LTChar):
            text = item.get_text()
            if text and item.x1 > item.x0 and item.y1 > item.y0:
                font = (item.fontname or "").lower()
                bold = any(mark in font for mark in ("bold", "black", "heavy", "semibold", "demi"))
                if item.upright:
                    out.append(Glyph(text, item.x0, item.x1, (item.y0 + item.y1) / 2, max(item.size, 1.0), bold))
                else:
                    upward = item.matrix[1] > 0
                    turned.append(_Turned(text, item.x0, item.x1, item.y0, item.y1, max(item.size, 1.0), bold, upward))
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


@dataclass
class _Turned:
    """One character drawn on its side. ``y0``/``y1`` run along the word; ``upward`` is the reading direction."""

    text: str
    x0: float
    x1: float
    y0: float
    y1: float
    size: float
    bold: bool
    upward: bool

    @property
    def centre(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class _TurnedWord:
    text: str
    x0: float
    x1: float
    foot: float
    top: float
    size: float
    bold: bool
    upward: bool

    @property
    def start(self) -> float:
        """Where reading begins: the foot of an upward word, the top of a downward one."""
        return self.foot if self.upward else self.top


def _turned_words(chars: list[_Turned]) -> list[Glyph]:
    """Words drawn on their side, set down as one line of headings at the foot of the row.

    Letters stacked in one column are one word, read in the direction they were drawn. Words
    that start (or end) level with each other are one row of headings, whichever way they read,
    so a long name and a short one land on the same line.
    """
    words = [word for column in _turned_columns(chars) for word in _column_words(column)]
    glyphs: list[Glyph] = []
    for row in _turned_rows(words):
        foot = min(word.foot for word in row)
        size = statistics.median(word.size for word in row)
        glyphs += [Glyph(word.text, word.x0, word.x1, foot + 0.35 * size, word.size, word.bold) for word in row]
    return glyphs


def _turned_columns(chars: list[_Turned]) -> list[list[_Turned]]:
    columns: list[list[_Turned]] = []
    for char in sorted(chars, key=lambda item: item.centre):
        if columns and abs(char.centre - columns[-1][-1].centre) <= 0.8 * char.size:
            columns[-1].append(char)
        else:
            columns.append([char])
    return columns


def _column_words(column: list[_Turned]) -> list[_TurnedWord]:
    """Split a column where the gap is nearly two letters (the next name), then read each piece."""
    column = sorted(column, key=lambda item: item.y0)
    runs: list[list[_Turned]] = [[column[0]]]
    for char in column[1:]:
        if char.y0 - runs[-1][-1].y1 > 1.8 * char.size:
            runs.append([char])
        else:
            runs[-1].append(char)
    words = []
    for run in runs:
        letters = [char for char in run if not char.text.isspace()]
        # A lone sideways letter is a mark or one letter of a diagonal watermark, not a heading.
        if len(letters) < 2:
            continue
        upward = sum(char.upward for char in letters) >= len(letters) / 2
        text = _read_turned(letters, run, upward)
        words.append(
            _TurnedWord(
                text,
                min(char.x0 for char in letters),
                max(char.x1 for char in letters),
                min(char.y0 for char in letters),
                max(char.y1 for char in letters),
                statistics.median(char.size for char in letters),
                any(char.bold for char in letters),
                upward,
            )
        )
    return words


def _read_turned(letters: list[_Turned], run: list[_Turned], upward: bool) -> str:
    """The letters in reading order. A space is a space character, or a gap clearly wider than the usual one."""
    ordered = sorted(run, key=lambda char: char.y0 if upward else -char.y0)

    def gap(before: _Turned, after: _Turned) -> float:
        return after.y0 - before.y1 if upward else before.y0 - after.y1

    in_order = [char for char in ordered if not char.text.isspace()]
    gaps = [gap(a, b) for a, b in zip(in_order, in_order[1:])]
    usual = statistics.median(gaps) if gaps else 0.0
    out = ""
    previous = None
    for char in ordered:
        if char.text.isspace():
            out += " "
            continue
        if previous is not None and gap(previous, char) > usual + 0.2 * char.size:
            out += " "
        out += char.text
        previous = char
    return re.sub(r"\s+", " ", out).strip()


def _turned_rows(words: list[_TurnedWord]) -> list[list[_TurnedWord]]:
    """Group headings whose starts, or whose ends, are level: a row of names however long each is."""
    rows: list[list[_TurnedWord]] = []
    for word in sorted(words, key=lambda item: item.x0):
        tolerance = 0.6 * word.size

        def level(other: _TurnedWord) -> bool:
            if other.upward != word.upward:
                return False
            end, other_end = (word.top, other.top) if word.upward else (word.foot, other.foot)
            return abs(other.start - word.start) <= tolerance or abs(other_end - end) <= tolerance

        home = next((row for row in rows if any(level(other) for other in row)), None)
        if home is None:
            rows.append([word])
        else:
            home.append(word)
    return rows


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
        if _fits_columns(line, columns):
            return True
        # Group headings have fewer cells than the row under them. The row still belongs
        # when every heading already in the table sits on one of its columns.
        if len(line.segments) > max(len(ln.segments) for ln in block):
            spans = _spans(_columns([line]))
            return all(_owns(seg, spans) for ln in block for seg in ln.segments)
        return False
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
    """Column extents, from the rows with the most cells.

    A heading merged across columns is wider than the cells under it, and a short heading
    centered over a group can sit in the gap between them. Either one would glue two columns
    together or invent a column. The leaf rows already have a cell in each column, so the
    columns come from those rows only.
    """
    most = max(len(line.segments) for line in block)
    fullest = [line for line in block if len(line.segments) == most]
    merged: list[list[float]] = []
    for x0, x1 in sorted((seg[0].x0, seg[-1].x1) for line in fullest for seg in line.segments):
        # A right-aligned figure ends where its heading starts. Rounding leaves a hairline gap.
        if merged and x0 <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], x1)
        else:
            merged.append([x0, x1])
    # An indented label is drawn a little to the right of the heading, so the fullest rows
    # split one column in two. A word that crosses that small gap is the same column.
    # A heading that spans real columns crosses a much wider gap and does not join them.
    others = [line for line in block if line not in fullest]
    changed = True
    while changed and len(merged) > 1:
        changed = False
        centers = [(left + right) / 2 for left, right in merged]
        pitches = [b - a for a, b in zip(centers, centers[1:]) if b > a]
        typical = statistics.median(pitches) if pitches else 40.0
        for index in range(len(merged) - 1):
            if centers[index + 1] - centers[index] >= 0.55 * typical:
                continue
            if not any(_crosses(seg, merged[index], merged[index + 1]) for line in others for seg in line.segments):
                continue
            merged[index][0] = min(merged[index][0], merged[index + 1][0])
            merged[index][1] = max(merged[index][1], merged[index + 1][1])
            del merged[index + 1]
            changed = True
            break
    return [(a, b) for a, b in merged]


def _crosses(segment: list[Word], left: list[float], right: list[float]) -> bool:
    """The piece overlaps both boxes, so they are one cell split by an indent."""
    x0, x1 = segment[0].x0, segment[-1].x1
    return x0 < left[1] and x1 > right[0]


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


_YEAR = re.compile(r"^(?:19|20)\d{2}$")


def _amount_cell(cell: str) -> bool:
    """A figure. A year is a column heading, not an amount."""
    text = (cell or "").strip()
    return bool(text) and tables.is_value(text) and not _YEAR.fullmatch(text)


def _label_row(row: list[str]) -> bool:
    """A header row: several labels, and no amounts. Years count as labels."""
    filled = [cell for cell in row if cell.strip()]
    return len(filled) >= 2 and not any(_amount_cell(cell) for cell in filled)


def _span_labels(header_rows: list[list[tuple[float, float, str]]], columns: list[tuple[float, float]]) -> list[str]:
    """Each column's heading, including a heading that is merged across several columns.

    A PDF does not record the merge. A heading covers the columns its letters actually sit
    over. When the letters are short of that (the word is only as wide as the first
    sub-column, or it is centered on the middle one), it covers the run of matching
    sub-headings below it ("Actual, Budget, Actual, Budget"), or an equal run when the
    counts divide and the heading sits at the left or the middle of that run.
    """
    labels = [""] * len(columns)
    placed = [_placed_texts(segs, columns) for segs in header_rows]
    for index, segs in enumerate(header_rows):
        below = placed[index + 1] if index + 1 < len(placed) else None
        for (_x0, _x1, text), cols in zip(sorted(segs, key=lambda seg: seg[0]), _covers(sorted(segs, key=lambda seg: seg[0]), columns, below)):
            for column in cols:
                if text and text not in labels[column].split():
                    labels[column] = f"{labels[column]} {text}".strip()
    return _unique_labels(labels)


def _placed_texts(segs: list[tuple[float, float, str]], columns: list[tuple[float, float]]) -> list[str]:
    """Where each piece lands when it belongs to one column: the text under each column."""
    texts = [""] * len(columns)
    centers = [(left + right) / 2 for left, right in columns]
    for x0, x1, text in segs:
        if not text:
            continue
        column = min(range(len(centers)), key=lambda i: abs(centers[i] - (x0 + x1) / 2))
        texts[column] = f"{texts[column]} {text}".strip()
    return texts


def _covers(
    segs: list[tuple[float, float, str]], columns: list[tuple[float, float]], below: list[str] | None
) -> list[list[int]]:
    segs = sorted(segs, key=lambda seg: seg[0])
    centers = [(left + right) / 2 for left, right in columns]
    if not below:
        # One heading row. A heading names the columns its letters cover, or the nearest column.
        # Reaching into the neighbour would rename a right-aligned amount with the label beside it.
        covers = []
        for x0, x1, _text in segs:
            under = [column for column, center in enumerate(centers) if x0 - 4 <= center <= x1 + 4]
            if not under:
                mid = (x0 + x1) / 2
                under = [min(range(len(centers)), key=lambda i: abs(centers[i] - mid))]
            covers.append(under)
        return covers
    covers: list[list[int] | None] = [None] * len(segs)
    stub = _stub_segment(segs, centers, below)
    if stub is not None:
        covers[stub] = [min(range(len(centers)), key=lambda i: abs(centers[i] - (segs[stub][0] + segs[stub][1]) / 2))]
    groups = [index for index in range(len(segs)) if index != stub]
    if len(groups) >= 2 and below:
        repeated = _repeated_runs(below, len(groups))
        if repeated and not (stub is not None and any(0 in cols for cols in repeated)):
            for index, columns_ in zip(groups, repeated):
                covers[index] = columns_
            return [covered or [] for covered in covers]
    for index in groups:
        x0, x1, _text = segs[index]
        under = [column for column, center in enumerate(centers) if x0 - 4 <= center <= x1 + 4]
        if len(under) >= 2:
            covers[index] = under
    rest = [index for index in groups if covers[index] is None]
    taken = {column for covered in covers if covered for column in covered}
    free = [column for column in range(len(columns)) if column not in taken]
    if len(rest) >= 2 and free and len(free) % len(rest) == 0:
        width = len(free) // len(rest)
        chunks = [free[start : start + width] for start in range(0, len(free), width)]
        if all(_heading_fits(segs[index], chunk, columns) for index, chunk in zip(rest, chunks)):
            for index, chunk in zip(rest, chunks):
                covers[index] = chunk
            return [covered or [] for covered in covers]
    pitches = [b - a for a, b in zip(centers, centers[1:]) if b > a]
    pitch = statistics.median(pitches) if pitches else 40.0
    claimed: dict[int, list[int]] = {}
    for position, index in enumerate(rest):
        x0, _x1, _text = segs[index]
        previous = segs[rest[position - 1]][1] if position else -10**6
        nxt = segs[rest[position + 1]][0] if position + 1 < len(rest) else 10**6
        for column in free:
            if previous < centers[column] < nxt and centers[column] >= x0 - 1.35 * pitch and not _sole_stub(column, below):
                claimed.setdefault(column, []).append(index)
    for column, owners in claimed.items():
        started = [index for index in owners if segs[index][0] <= centers[column] + 4]
        owner = max(started, key=lambda i: segs[i][0]) if started else owners[0]
        covers[owner] = sorted({*(covers[owner] or []), column})
    for index in rest:
        if not covers[index]:
            mid = (segs[index][0] + segs[index][1]) / 2
            covers[index] = [min(range(len(centers)), key=lambda i: abs(centers[i] - mid))]
    return [covered or [] for covered in covers]


def _stub_segment(
    segs: list[tuple[float, float, str]], centers: list[float], below: list[str] | None
) -> int | None:
    """The row-name heading (repeated down the header rows), which is not a merged group."""
    if not segs or not below or not below[0].strip():
        return None
    x0, x1, text = segs[0]
    column = min(range(len(centers)), key=lambda i: abs(centers[i] - (x0 + x1) / 2))
    if column != 0 or text.strip().lower() != below[0].strip().lower():
        return None
    return 0


def _repeated_runs(texts: list[str], groups: int) -> list[list[int]] | None:
    """``Actual, Budget, Actual, Budget`` as two runs of ``Actual, Budget``. The stub column is skipped."""
    cells = [text.strip().lower() for text in texts]
    for start in range(min(3, len(cells))):
        rest = cells[start:]
        if groups < 2 or len(rest) % groups:
            continue
        width = len(rest) // groups
        pattern = rest[:width]
        if width < 1 or not any(pattern):
            continue
        if all(rest[begin : begin + width] == pattern for begin in range(0, len(rest), width)):
            return [list(range(start + begin, start + begin + width)) for begin in range(0, len(rest), width)]
    return None


def _heading_fits(seg: tuple[float, float, str], cols: list[int], columns: list[tuple[float, float]]) -> bool:
    """The heading sits at the left of this run or at its middle, so the run is really its merge."""
    left, right = columns[cols[0]][0], columns[cols[-1]][1]
    span = max(right - left, 1.0)
    mid = (seg[0] + seg[1]) / 2
    if mid < left - 0.35 * span or mid > right + 0.35 * span:
        return False
    from_left = (mid - left) / span
    from_center = abs(mid - (left + right) / 2) / span
    return from_left <= 0.6 or from_center <= 0.28


def _sole_stub(column: int, below: list[str] | None) -> bool:
    """A label that appears once at the left (the row-name column) is not part of a merged heading."""
    if not below or column >= len(below) or not below[column].strip():
        return False
    label = below[column].strip().lower()
    if tables.is_value(below[column]):
        return False
    return sum(text.strip().lower() == label for text in below) == 1 and column == 0


def _unique_labels(labels: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for label in labels:
        if not label:
            out.append("")
            continue
        seen[label] = seen.get(label, 0) + 1
        out.append(label if seen[label] == 1 else f"{label} ({seen[label]})")
    return out


def _value_columns(rows: list[list[str]]) -> list[bool]:
    flags = []
    width = max((len(row) for row in rows), default=0)
    for column in range(width):
        cells = [row[column] for row in rows if column < len(row) and row[column].strip()]
        values = sum(map(tables.is_value, cells))
        flags.append(values >= 2 or bool(cells) and values >= 0.6 * len(cells))
    return flags


def _grouping_columns(rows: list[list[str]]) -> list[int]:
    """Leading columns that are blank at least half the time: a category merged down the rows.

    A column filled on nearly every row (a department, a manager) is a missing cell, not a merge.
    """
    if len(rows) < 2:
        return []
    value_cols = _value_columns(rows)
    first_value = next((index for index, flag in enumerate(value_cols) if flag), None)
    if not first_value:
        return []
    columns = []
    for column in range(first_value):
        blanks = sum(1 for row in rows if column < len(row) and not row[column].strip())
        # One blank is a missing cell (a note left empty). A merge leaves at least two blanks.
        if blanks >= 2 and blanks * 2 >= len(rows):
            columns.append(column)
    return columns


def _fill_grouping_columns(rows: list[list[str]], mids: list[float]) -> list[int]:
    """Copy a merged category onto the figure rows it covers.

    The name may sit on the first row, or be drawn once in the vertical middle of the rows.
    Each blank figure row takes the name closest to it on the page. Returns those columns.
    """
    columns = _grouping_columns(rows)
    value_cols = _value_columns(rows)
    for column in columns:
        anchors = [index for index, row in enumerate(rows) if column < len(row) and row[column].strip()]
        if not anchors:
            continue
        for index, row in enumerate(rows):
            if column >= len(row) or row[column].strip() or not _has_value(row, value_cols):
                continue
            nearest = min(anchors, key=lambda anchor: (abs(mids[anchor] - mids[index]), anchor > index, anchor))
            row[column] = rows[nearest][column]
    return columns


def _has_value(row: list[str], value_cols: list[bool]) -> bool:
    return any(flag and index < len(row) and _amount_cell(row[index]) for index, flag in enumerate(value_cols))


def _one_label(row: list[str]) -> str:
    """The single non-figure cell of a category row, or "" when the row is ordinary data."""
    filled = [cell.strip() for cell in row if cell.strip()]
    if len(filled) == 1 and not tables.is_value(filled[0]):
        return filled[0]
    return ""


def _absorbed(rows: list[list[str]], index: int) -> bool:
    """A category row whose name was copied onto the data rows beside it."""
    text = _one_label(rows[index])
    if not text:
        return False
    column = next(col for col, cell in enumerate(rows[index]) if cell.strip())
    for neighbor in (index - 1, index + 1):
        if 0 <= neighbor < len(rows) and _has_value(rows[neighbor], _value_columns(rows)) and rows[neighbor][column].strip() == text:
            return True
    return False


def _table(block: list[Line], previous: list[Table] | None) -> tuple[list[str], Table | None, list[str]]:
    columns = _columns(block)
    grid: list[list[str]] = []
    bolds: list[bool] = []
    seg_rows: list[list[tuple[float, float, str]]] = []
    row_x: list[float] = []
    cell_x: list[list[float]] = []
    for line in block:
        row = [""] * len(columns)
        xs = [0.0] * len(columns)
        segs: list[tuple[float, float, str]] = []
        first_x = 0.0
        for segment in line.segments:
            text = " ".join(word.text for word in segment).strip()
            x0, x1 = segment[0].x0, segment[-1].x1
            if text and not segs:
                first_x = x0
            segs.append((x0, x1, text))
            column = _place(segment, columns)
            if text:
                row[column] = f"{row[column]} {text}".strip()
                xs[column] = x0
        grid.append(row)
        cell_x.append(xs)
        seg_rows.append(segs)
        row_x.append(first_x)
        bolds.append(line.bold)
    mids = [line.mid for line in block]

    peeled = _peel_side_facts(grid)
    facts: list[str] = []
    if peeled:
        facts, grid = peeled
        bolds = [False] * len(grid)
        cell_x = [row[2:] for row in cell_x]

    if _prose(grid):
        width = len(grid[0]) if grid else 0
        return _dehyphenate([" ".join(row[c] for row in grid if c < len(row) and row[c]) for c in range(width)]), None, facts

    bold_first = bolds[0] and not all(bolds[1:])
    heads = _header_depth(grid) if not peeled else 1
    spanned = _span_labels(seg_rows[:heads], columns) if not peeled else None
    header_mid = mids[0]
    if spanned and (_usable_header(spanned, grid[heads:], bold_first) or heads > 1):
        labels = spanned
        grid, bolds, mids, row_x, cell_x = grid[heads:], bolds[heads:], mids[heads:], row_x[heads:], cell_x[heads:]
    else:
        labels = None
        header = tables.has_header(grid, bold_first=bold_first)
        if header:
            labels = [_clean_label(cell) for cell in grid[0]]
            grid, bolds, mids, row_x, cell_x = grid[1:], bolds[1:], mids[1:], row_x[1:], cell_x[1:]
    group_cols: list[int] = []
    if labels:
        grid, mids, row_x, bolds = _join_body(grid, mids, row_x, bolds, cell_x)
        group_cols = _fill_grouping_columns(grid, mids)
        kept = [index for index in range(len(grid)) if not _absorbed(grid, index)]
        grid = [grid[index] for index in kept]
        mids = [mids[index] for index in kept]
        row_x = [row_x[index] for index in kept]
        bolds = [bolds[index] for index in kept]
        cell_x = [cell_x[index] for index in kept]

    def render(row: list[str]) -> str:
        return tables.labelled_row(labels, row) if labels else tables.table_lines([row], header=False)[0]

    body_lines = _grouped_lines(grid, row_x, bolds, render, group_cols, cell_x) if labels else [render(row) for row in grid]
    first_label = (labels[0] if labels else "") or ""
    carried = _carried_over(mids, header_mid if labels else None, previous or [], first_label)
    if carried and labels:
        prior, names = carried
        lines = [f"[These columns continue the table on the page before; each row starts with its {prior.row_label}.]"]
        lines.append(" | ".join(label for label in labels if label))
        for line, name in zip(body_lines, names):
            lines.append(f"{prior.row_label}: {name} | {line}" if name else line)
        return lines, Table(header_mid, prior.rows, prior.row_label), facts

    lines = ([" | ".join(label for label in labels if label)] if labels else []) + body_lines
    rows = {round(mid): row[0] for mid, row in zip(mids, grid) if row and row[0]}
    return lines, Table(header_mid if labels else None, rows, first_label), facts


def _header_depth(grid: list[list[str]]) -> int:
    """How many leading rows are headings. Stops at the first row of amounts, and never eats a table that has no amounts."""
    if len(grid) < 2 or not _label_row(grid[0]):
        return 1
    if not any(_amount_cell(cell) for row in grid[1:] for cell in row):
        return 1
    heads = 1
    while heads < min(4, len(grid) - 1) and _label_row(grid[heads]):
        heads += 1
    return heads


def _usable_header(labels: list[str], body: list[list[str]], bold_first: bool) -> bool:
    if tables.has_header([labels, *body], bold_first=bold_first):
        return True
    filled = [label for label in labels if label.strip()]
    return len(filled) >= 2 and all(_YEAR.fullmatch(label.strip()) for label in filled)


def _clean_label(cell: str) -> str:
    return cell.strip()


def _join_body(
    rows: list[list[str]],
    mids: list[float],
    xs: list[float],
    bolds: list[bool],
    cell_x: list[list[float]],
) -> tuple[list[list[str]], list[float], list[float], list[bool]]:
    rows, mids = _join_wrapped(rows, mids, first=0, extra=[xs, bolds, cell_x])
    return rows, mids, xs, bolds


def _grouped_lines(
    rows: list[list[str]],
    xs: list[float],
    bolds: list[bool],
    render,
    group_cols: list[int],
    cell_x: list[list[float]],
) -> list[str]:
    """Category rows name the data rows under them. Indent nests: Operating > Revenue > Product.

    A category merged down a column has already been copied into those rows. A bold category row
    with no figures of its own stays as ``Group:``. A row that also has a total stays on its own
    line and is named on the indented rows under it. A blank first cell is a missing value, not
    an indent.
    """
    lines = []
    groups: list[tuple[float, str, str]] = []
    grouped = set(group_cols)
    for index, (row, x) in enumerate(zip(rows, xs)):
        bold = bolds[index] if index < len(bolds) else False
        label = _one_label(row) if bold else ""
        if label:
            while groups and x <= groups[-1][0] + 1:
                groups.pop()
            groups.append((x, label, "group"))
            lines.append(f"Group: {label}")
            continue
        stub = _stub_x(row, cell_x[index] if index < len(cell_x) else [])
        if stub is None:
            while groups and groups[-1][2] == "data":
                groups.pop()
            prefix = " > ".join(group for _at, group, _kind in groups)
            line = render(row)
            lines.append(f"{prefix} | {line}" if prefix else line)
            continue
        while groups and groups[-1][2] == "data" and stub <= groups[-1][0] + 1:
            groups.pop()
        while groups and groups[-1][2] == "group" and stub < groups[-1][0] - 1:
            groups.pop()
        prefix = " > ".join(group for _at, group, _kind in groups)
        line = render(row)
        lines.append(f"{prefix} | {line}" if prefix else line)
        name, _name_x = _detail(row, cell_x[index] if index < len(cell_x) else [], grouped, stub)
        if name:
            groups.append((stub, name, "data"))
    return lines


def _stub_x(row: list[str], xs: list[float]) -> float | None:
    """Where the first column's text is drawn. Empty when that cell was blank on the page."""
    if not row or not row[0].strip() or not xs or not xs[0]:
        return None
    return xs[0]


def _detail(row: list[str], xs: list[float], group_cols: set[int], fallback: float) -> tuple[str, float]:
    """The row's own name and where it is drawn, skipping a category copied down a column."""
    for column, cell in enumerate(row):
        if column in group_cols or not cell.strip() or tables.is_value(cell):
            continue
        return cell.strip(), xs[column] if column < len(xs) and xs[column] else fallback
    return "", fallback


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


def _join_wrapped(
    grid: list[list[str]], mids: list[float], *, first: int, extra: list[list] | None = None
) -> tuple[list[list[str]], list[float]]:
    """Fold a cell's wrapped line into its row: a line with few cells filled that sits closer to a
    fuller neighbouring row than rows usually are to each other.

    ``extra`` lists (row indents, bold flags) lose the same row, so they stay aligned with the grid.
    """
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
        for sequence in extra or []:
            del sequence[k]
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
