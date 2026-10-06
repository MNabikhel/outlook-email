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

A sheet saved from a spreadsheet can put two cells closer together than two words of a sentence
(a right-aligned date against a left-aligned task, the accounting format's "$" against the figure
before it), so a table's column edges are found across its rows, not on one line: a strip no row
of figures writes across, a border drawn down the page, or text that lines up row after row.
Within those columns a heading wrapped onto two lines is one heading, a heading merged across
columns is matched to the run it is centered on, and names turned on their side join the upright
headings in their row. The accounting format's "$ -" is a zero.
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
    # A heading drawn on its side, set down at the foot of its column (see ``glyphs_of``).
    turned: bool = False


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
    words: list[Word] = field(default_factory=list)
    # A gap wider than this between two words starts a new piece (a table cell).
    piece_gap: float = 0.0
    # The column edges of the table region this line is in, outer bounds included (empty when none were found).
    grid: tuple[float, ...] = ()

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
        glyphs += [Glyph(word.text, word.x0, word.x1, foot + 0.35 * size, word.size, word.bold, True) for word in row]
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


@dataclass
class Rule:
    """A line drawn straight down the page (a cell border), from ``y0`` up to ``y1``."""

    x: float
    y0: float
    y1: float


def rules_of(layout) -> list[Rule]:
    """The vertical borders drawn on a pdfminer page: thin lines and rectangles running down it."""
    from pdfminer.layout import LTCurve

    found: list[Rule] = []

    def walk(item) -> None:
        if isinstance(item, LTCurve):
            width, height = item.x1 - item.x0, item.y1 - item.y0
            if width <= 2.0 and height >= 4.0:
                found.append(Rule((item.x0 + item.x1) / 2, item.y0, item.y1))
            return
        try:
            children = list(item)
        except TypeError:
            return
        for child in children:
            walk(child)

    walk(layout)
    return found


def page_text(glyphs: list[Glyph], previous: list[Table] | None = None, rules: list[Rule] | None = None) -> PageText:
    """The page as text: plain lines, and tables row by row. ``previous``: the tables on the page before.
    ``rules``: the cell borders drawn on the page, which mark column edges exactly."""
    if not glyphs:
        return PageText("", [])
    if sum(1 for g in glyphs if _CID.fullmatch(g.text)) > 0.2 * len(glyphs):
        return PageText("", [], unreadable=True)
    lines = _lines([g for g in glyphs if not _CID.fullmatch(g.text)])
    for line in lines:
        line.words, line.piece_gap = _words(line)
    lines = [line for line in lines if line.words]
    cuts = _column_cuts(lines, rules or [])
    for index, line in enumerate(lines):
        line.segments = _segments(line, cuts.get(index, ()))
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
    return _with_turned_headings(lines)


def _with_turned_headings(lines: list[Line]) -> list[Line]:
    """Names turned on their side and the upright headings beside them ("Date", "Day") are one header row.

    The turned names are set down at the foot of their column, a few points off the upright text's
    middle, so they would otherwise be a row of their own with no stub columns.
    """
    out: list[Line] = []
    for line in lines:
        pair = _turned_and_upright(out[-1], line) if out else None
        if pair is not None:
            turned, upright = pair
            out[-1] = Line(upright.mid, upright.size, sorted(upright.glyphs + turned.glyphs, key=lambda g: g.x0))
            continue
        out.append(line)
    return out


def _turned_and_upright(first: Line, second: Line) -> tuple[Line, Line] | None:
    """(turned names, upright headings) when one line is all turned names, the other has none, and they sit
    side by side within most of a letter's height."""
    def turned(line: Line) -> bool | None:
        kinds = {glyph.turned for glyph in line.glyphs}
        return kinds.pop() if len(kinds) == 1 else None

    kinds = (turned(first), turned(second))
    if kinds not in ((True, False), (False, True)):
        return None
    names, upright = (first, second) if kinds[0] else (second, first)
    if abs(first.mid - second.mid) > 0.8 * upright.size:
        return None
    if any(g.x0 < h.x1 and g.x1 > h.x0 for g in names.glyphs for h in upright.glyphs):
        return None
    return names, upright


def _dedupe(glyphs: list[Glyph]) -> list[Glyph]:
    """Drop the second copy of text drawn twice a hair apart (a common way to fake bold)."""
    out: list[Glyph] = []
    for glyph in glyphs:
        if any(other.text == glyph.text and abs(other.x0 - glyph.x0) < 0.2 * glyph.size for other in out[-3:]):
            continue
        out.append(glyph)
    return out


def _words(line: Line) -> tuple[list[Word], float]:
    """The line's words, and the gap wider than which two words are separate pieces (table cells)."""
    glyphs = [g for g in line.glyphs if g.text.strip()]
    if not glyphs:
        return [], 0.0
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
    words: list[Word] = []
    current = [glyphs[0]]
    for gap, glyph in zip(gaps, glyphs[1:]):
        if gap <= word_gap:
            current.append(glyph)
            continue
        words.append(_word(current))
        current = [glyph]
    words.append(_word(current))
    return _with_currency(words), piece_gap


def _segments(line: Line, cuts: list[float] | tuple[float, ...] = ()) -> list[list[Word]]:
    """The line's words, grouped into pieces that are separated by a wide gap or a column edge (table cells)."""
    words = line.words
    if not words:
        return []
    segments: list[list[Word]] = [[words[0]]]
    for before, word in zip(words, words[1:]):
        gap = word.x0 - before.x1
        if gap > line.piece_gap or any(before.x1 - 0.3 <= cut <= word.x0 + 0.3 for cut in cuts):
            segments.append([word])
        else:
            segments[-1].append(word)
    return segments


def _word(glyphs: list[Glyph]) -> Word:
    return Word("".join(g.text for g in glyphs).strip(), glyphs[0].x0, glyphs[-1].x1, all(g.bold for g in glyphs))


# A spreadsheet's accounting format draws the currency sign at the cell's left edge and the figure at
# its right, with a dash for zero: "$       1,845.20" and "$      -".
_CURRENCY = {"$", "US$", "C$", "A$", "€", "£", "¥"}
_FIGURE = re.compile(r"^\(?-?[\d.,]*\d[\d.,]*\)?%?$|^[-–—]$")
# A zero in the accounting format, alone or with its currency sign (see ``_with_currency``).
_ZERO_DASHES = {"-", "–", "—", *(f"{sign}0" for sign in _CURRENCY)}


def _with_currency(words: list[Word]) -> list[Word]:
    """A currency sign drawn apart from its figure is one word with it, so the sign is not read as the
    previous cell's. "$ -" is the accounting format's zero."""
    out: list[Word] = []
    index = 0
    while index < len(words):
        word = words[index]
        nxt = words[index + 1] if index + 1 < len(words) else None
        if word.text in _CURRENCY and nxt is not None and _FIGURE.match(nxt.text):
            figure = "0" if nxt.text in {"-", "–", "—"} else nxt.text
            out.append(Word(f"{word.text}{figure}", word.x0, nxt.x1, word.bold and nxt.bold))
            index += 2
            continue
        out.append(word)
        index += 1
    return out


# Column edges ----------------------------------------------------------------------------------


_PROSE_WORD = re.compile(r"^[a-z][a-z'’-]*[,.;:!?]?$")


def _row_like(line: Line) -> bool:
    """Words in cells: at least one gap on the line is a whole letter-height wide, and the line is not a
    sentence (justified text stretches its word spaces, but its words are mostly lowercase)."""
    words = line.words
    if len(words) < 2 or max(b.x0 - a.x1 for a, b in zip(words, words[1:])) < line.size:
        return False
    return len(words) < 5 or sum(bool(_PROSE_WORD.match(word.text)) for word in words) < 0.6 * len(words)


def _column_cuts(lines: list[Line], rules: list[Rule]) -> dict[int, list[float]]:
    """Where each table line crosses from one column to the next, by line index.

    A sheet saved as PDF has the same columns on every row, but its cells can sit closer together than
    two words of a sentence: a date pushed to the right of its cell runs into the task left-aligned in
    the next, and an accounting "$" sits just after the previous cell's figure. So a column edge is
    found across the rows, not on one line: a strip of the page that no row writes across. A wide strip
    is an edge. A strip narrower than a letter is an edge only when a border is drawn there, or when
    the text beside it lines up row after row and no heading is written across it (so "Mon 09/28" under
    "Date" stays one cell).
    """
    cuts: dict[int, list[float]] = {}
    for region in _regions(lines):
        rows = [index for index in region if _row_like(lines[index])]
        if len(rows) < 3:
            continue
        em = statistics.median(lines[index].size for index in rows)
        top = max(lines[index].mid for index in region) + em
        bottom = min(lines[index].mid for index in region) - em
        drawn = [rule.x for rule in rules if rule.y1 >= bottom and rule.y0 <= top]
        # Headings are often merged across columns, so the gaps come from the rows from the first figure
        # down. The heading lines above only veto a narrow gap one of their words is written across.
        first = next((pos for pos, index in enumerate(rows) if any(_amount_cell(w.text) for w in lines[index].words)), None)
        # Fewer than three rows of figures is too little to find columns from (each line's own gaps do);
        # a table with no figures at all (a task list) is read from all its rows.
        body = rows if first is None else rows[first:] if len(rows) - first >= 3 else []
        heads = [word for index in rows if index not in body for word in lines[index].words]
        tolerance = 0 if len(body) < 6 else max(1, round(0.08 * len(body)))
        edges: list[float] = []
        for lo, hi, strict, at in _strips(lines, body, tolerance):
            border = [x for x in drawn if lo - 0.5 <= x <= hi + 0.5]
            if border:
                edges.append(border[0])
            elif hi - lo >= 0.9 * em:
                edges.append(_edge_at(lines, body, lo, hi, at, heads, 0.4 * em))
            elif strict and _lined_up(lines, body, lo, hi):
                cut = _edge_at(lines, body, lo, hi, at, heads, 0.6)
                if not any(word.x0 < cut < word.x1 for word in heads):
                    edges.append(cut)
        # A drawn border is a column edge even where a long label runs across it into an empty cell
        # ("Total Machinery & Equipment"), as long as most rows stay on their side of it.
        for x in drawn:
            crossing = sum(1 for index in rows if any(w.x0 < x - 0.5 and w.x1 > x + 0.5 for w in lines[index].words))
            if crossing <= 0.3 * len(rows):
                edges.append(x)
        edges = _distinct(edges)
        if not edges:
            continue
        lo = min(lines[index].words[0].x0 for index in rows)
        hi = max(lines[index].words[-1].x1 for index in rows)
        grid = (lo, *sorted(edge for edge in set(edges) if lo < edge < hi), hi)
        # A short line between the rows, or right after the last at the rows' spacing, is a row with blank
        # cells ("10/10 Sat"); a title above them is not.
        pitch = statistics.median(lines[a].mid - lines[b].mid for a, b in zip(rows, rows[1:]))
        last = rows[-1]
        while last + 1 in region and lines[last].mid - lines[last + 1].mid <= 1.5 * pitch and len(lines[last + 1].words) <= 4:
            last += 1
        for index in region:
            line = lines[index]
            if index in rows or (rows[0] < index <= last and len(line.words) <= 4):
                cuts[index] = edges
                line.grid = grid
    return cuts


def _distinct(edges: list[float]) -> list[float]:
    """Edges less than a point and a half apart are one edge (a border drawn twice, or a strip and its border)."""
    out: list[float] = []
    for edge in sorted(edges):
        if not out or edge - out[-1] > 1.5:
            out.append(edge)
    return out


def _regions(lines: list[Line]) -> list[list[int]]:
    """Runs of lines close enough together to be one sheet's rows (a blank row or two included)."""
    regions: list[list[int]] = []
    for index, line in enumerate(lines):
        if regions and lines[index - 1].mid - line.mid <= 3.2 * max(line.size, lines[index - 1].size):
            regions[-1].append(index)
        else:
            regions.append([index])
    return regions


def _strips(lines: list[Line], rows: list[int], tolerance: int) -> list[tuple[float, float, bool, float]]:
    """Strips of the page between the rows' first and last words that at most ``tolerance`` rows write
    across: (left, right, whether no row does, where to cut). The cut is the middle of the widest
    stretch the fewest rows cross, so a heading centered over the next column is not cut through."""
    events: list[tuple[float, int]] = []
    for index in rows:
        spans: list[list[float]] = []
        for word in lines[index].words:
            if spans and word.x0 <= spans[-1][1] + 0.05:
                spans[-1][1] = max(spans[-1][1], word.x1)
            else:
                spans.append([word.x0, word.x1])
        for x0, x1 in spans:
            events += [(x0, 1), (x1, -1)]
    if not events:
        return []
    events.sort(key=lambda event: (event[0], -event[1]))
    left = events[0][0]
    right = max(x for x, _delta in events)
    strips: list[tuple[float, float, bool, float]] = []
    depth = 0
    start: float | None = None
    stretches: list[tuple[int, float, float]] = []  # (depth, from, to) inside the open strip
    for x, delta in events:
        before = depth
        depth += delta
        if start is not None and stretches and x > stretches[-1][1]:
            stretches[-1] = (stretches[-1][0], stretches[-1][1], x)
        if before > tolerance >= depth and left < x < right:
            start = x
            stretches = [(depth, x, x)]
        elif start is not None and depth <= tolerance:
            stretches.append((depth, x, x))
        elif start is not None and before <= tolerance < depth:
            if x > start:
                low = min(stretch[0] for stretch in stretches)
                best = max((stretch for stretch in stretches if stretch[0] == low), key=lambda s: s[2] - s[1])
                strips.append((start, x, low == 0, (best[1] + best[2]) / 2))
            start = None
            stretches = []
    return strips


def _lined_up(lines: list[Line], rows: list[int], lo: float, hi: float) -> bool:
    """Most rows with text on both sides of a narrow strip end flush against it, or start flush after it."""
    beside = sum(
        1
        for index in rows
        if any(word.x1 <= lo + 0.05 for word in lines[index].words) and any(word.x0 >= hi - 0.05 for word in lines[index].words)
    )
    (ends, _left), (starts, _right) = _flush(lines, rows, lo, hi, 0.6)
    return beside >= 3 and max(ends, starts) >= max(3, 0.6 * beside)


def _flush(lines: list[Line], rows: list[int], lo: float, hi: float, slack: float) -> tuple[tuple[int, int], tuple[int, int]]:
    """(rows ending within ``slack`` of the strip's left side, rows with text there) and (rows starting within
    ``slack`` of its right side, rows with text there): whether a right-aligned column ends at it and a
    left-aligned one starts after it. A figure may end short of it by a parenthesis, a zero dash by a couple of
    digits more: the accounting format pads them so they line up with "(1,215.00)"."""
    ends = starts = left_rows = right_rows = 0
    for index in rows:
        words = lines[index].words
        left = [word for word in words if word.x1 <= lo + 0.05]
        right = [word for word in words if word.x0 >= hi - 0.05]
        if left:
            left_rows += 1
            # The accounting format ends a positive figure a parenthesis short of where "(1,215.00)" ends,
            # and its zero dash a couple of digits shorter again.
            last = left[-1].text
            room = slack
            if last in _ZERO_DASHES:
                room = max(slack, 0.9 * lines[index].size)
            elif _amount_cell(last):
                room = max(slack, 0.4 * lines[index].size)
            ends += lo - left[-1].x1 <= room
        if right:
            right_rows += 1
            # Figures are drawn from the right of their cell; equal widths start level by chance.
            starts += right[0].x0 - hi <= slack and not _amount_cell(right[0].text)
    return (ends, left_rows), (starts, right_rows)


def _edge_at(lines: list[Line], rows: list[int], lo: float, hi: float, at: float, heads: list[Word], slack: float) -> float:
    """Where the cell border is in a strip: just past a right-aligned column's figures, just before a
    left-aligned column's text, midway when both line up. Otherwise in the stretch the fewest rows cross,
    off any heading word when the strip has room beside it ("Variance $ | Variance %")."""
    (ends, left_rows), (starts, right_rows) = _flush(lines, rows, lo, hi, slack)
    flush_left = ends >= 3 and ends >= 0.8 * left_rows
    flush_right = starts >= 3 and starts >= 0.8 * right_rows
    pad = min(2.5, (hi - lo) / 2)
    if flush_left and flush_right:
        return (lo + hi) / 2
    if flush_left:
        return lo + pad
    if flush_right:
        return hi - pad
    if not any(word.x0 < at < word.x1 for word in heads):
        return at
    covered: list[list[float]] = []
    for word in sorted(heads, key=lambda w: w.x0):
        if word.x1 <= lo or word.x0 >= hi:
            continue
        if covered and word.x0 <= covered[-1][1]:
            covered[-1][1] = max(covered[-1][1], word.x1)
        else:
            covered.append([word.x0, word.x1])
    bounds = [lo, *(x for span in covered for x in span), hi]
    gaps = [(a, b) for a, b in zip(bounds[::2], bounds[1::2]) if b - a > 1.0]
    if not gaps:
        return at
    a, b = min(gaps, key=lambda gap: abs((gap[0] + gap[1]) / 2 - at))
    return (a + b) / 2


# Tables ----------------------------------------------------------------------------------------


def _table_blocks(lines: list[Line]) -> list[tuple[int, int]]:
    """Runs of lines that line up in two or more columns: [start, end) into ``lines``.

    Three lines is the usual table. Two lines still count when one is a header and the other
    is a row of figures, so a section with a single data row keeps its column names.
    """
    blocks: list[tuple[int, int]] = []
    i = 0
    while i < len(lines):
        if len(lines[i].segments) < 2:
            i += 1
            continue
        end = i + 1
        while end < len(lines) and _continues(lines, i, end):
            end += 1
        if _is_table(lines[i:end]) and len(_columns(lines[i:end])) >= 2:
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
    # Wrapped heading lines sit closer than rows do, so they don't set how far apart rows are.
    usual = max(statistics.median(pitches), 1.2 * line.size) if pitches else 2.0 * line.size
    gap = before.mid - line.mid
    if gap > max(2.2 * usual, 3.0 * line.size):
        return False
    block = lines[start:index]
    median = statistics.median(ln.size for ln in block)
    # A noticeably larger line is a section heading, not another row. A one-word category can be a
    # little larger than the rows it names and still belong, unless the next line is a new header.
    limit = 1.15
    if len(line.segments) == 1 and line.size <= median * 1.35:
        nxt = lines[index + 1] if index + 1 < len(lines) else None
        if nxt is not None and not _new_header(nxt):
            limit = 1.35
    if line.size > median * limit:
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
    x0, x1 = line.segments[0][0].x0, line.segments[0][-1].x1
    # A label may run a little into the empty cell beside it ("Operating Expenses" past a narrow column);
    # a title written across the table covers whole columns.
    under = [c for c in columns if x0 < c[1] and x1 > c[0] and (c[0] <= x0 or min(x1, c[1]) - c[0] >= 0.3 * (c[1] - c[0]))]
    if len(under) != 1:
        return False
    if gap <= 1.6 * usual + 1:
        return True
    # A section label after a blank row ("Operating Expenses") still belongs when rows on the same
    # columns follow it.
    nxt = lines[index + 1] if index + 1 < len(lines) else None
    return (
        nxt is not None
        and len(nxt.segments) >= 2
        and line.mid - nxt.mid <= 1.6 * usual + 1
        and _fits_columns(nxt, columns)
        and any(_amount_cell(word.text) for segment in nxt.segments for word in segment)
    )


def _new_header(line: Line) -> bool:
    """A row of column names. Years and dates count as names; a row that already has amounts does not."""
    if len(line.segments) < 2:
        return False
    words = [word.text for segment in line.segments for word in segment]
    if not words or any(word.endswith(":") for word in words):
        return False
    return not any(_amount_cell(word) for word in words)


def _labels_above(line: Line, block: list[Line]) -> bool:
    """A row of names sitting on the columns, even when it has no cell for the stub column.

    Schedules often print the people sideways in the top row and the districts down the side.
    The districts make one more column than the name row has, so the names would otherwise
    be left off the table.
    """
    columns = _columns(block)
    if len(columns) < 2 or not line.segments:
        return False
    if len(line.segments) == 1:
        return _merged_heading_above(line, block, columns)
    words = [word.text for segment in line.segments for word in segment]
    if not words or sum(map(tables.is_value, words)) >= 0.5 * len(words):
        return False
    if line.segments[0][-1].text.endswith(":"):
        # "Pay Group:  Hourly & Salaried": a fact about the sheet above it, not its column names.
        return False
    gap = line.mid - block[0].mid
    usual = statistics.median(block[i].mid - block[i + 1].mid for i in range(len(block) - 1)) if len(block) > 1 else line.size
    if gap <= 0 or gap > max(2.2 * usual, 3.0 * line.size):
        return False
    return all(_owns(segment, _spans(columns)) for segment in line.segments)


def _merged_heading_above(line: Line, block: list[Line], columns: list[tuple[float, float]]) -> bool:
    """A heading merged down two heading rows ("Total" beside "Chicago | Austin | Remote", over its own "HC |
    Salary") is printed halfway between them, on a line of its own just above the table: it is part of the
    heading row when it sits over columns that row leaves empty, closer than a row apart."""
    segment = line.segments[0]
    first = block[0]
    words = [word.text for word in segment]
    if len(block) < 3 or len(first.segments) < 2 or any(tables.is_value(word) for word in words) or words[-1].endswith(":"):
        return False
    gap = line.mid - first.mid
    if gap <= 0 or gap > 0.8 * (first.mid - block[1].mid) or gap > 1.2 * line.size:
        return False
    taken = {_place(other, columns) for other in first.segments}
    return _place(segment, columns) not in taken and _owns(segment, _spans(columns))


def _fits_columns(line: Line, columns: list[tuple[float, float]]) -> bool:
    """Every piece of the line sits in a column this table already has.

    A right-aligned figure can sit well to the right of a short heading. It still belongs
    when it lands in that heading's span or just past the last one.
    """
    if len(columns) < 2:
        return False
    spans = _spans(columns)
    last = columns[-1][1]
    for segment in line.segments:
        if _owns(segment, spans):
            continue
        if segment[0].x0 + 1 >= last and segment[0].x0 - last <= 180:
            continue
        return False
    return True


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
    """Column extents from one full row, widened by the cells that belong to those columns.

    A heading merged across columns is wider than the cells under it, and a short heading
    centered over a group can sit in the gap between them. Either one would glue two columns
    together or invent a column, so the columns come from a single full row. A right-aligned
    figure sits just to the right of its heading and still belongs to that column.

    When the rows' column edges were found across the page (``Line.grid``), those are the columns.
    """
    grid = block[0].grid
    most = max(len(line.segments) for line in block)
    # The page's columns, unless they are fewer than a row's cells (a lone border, not the whole grid).
    if len(grid) - 1 >= most and all(line.grid == grid for line in block):
        lo = min(grid[0], *(segment[0].x0 for line in block for segment in line.segments))
        hi = max(grid[-1], *(segment[-1].x1 for line in block for segment in line.segments))
        bounds = [lo, *grid[1:-1], hi]
        return list(zip(bounds, bounds[1:]))
    skeleton = next(line for line in block if len(line.segments) == most)
    merged: list[list[float]] = []
    for segment in skeleton.segments:
        x0, x1 = segment[0].x0, segment[-1].x1
        # A right-aligned figure ends where its heading starts. Rounding leaves a hairline gap.
        if merged and x0 <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], x1)
        else:
            merged.append([x0, x1])
    for line in block:
        if line is skeleton:
            continue
        for segment in line.segments:
            _absorb_column(merged, segment[0].x0, segment[-1].x1, " ".join(word.text for word in segment))
    # An indented label is drawn a little to the right of the heading, so two boxes are one
    # column when a word crosses that small gap. A heading that spans real columns crosses a
    # much wider gap and does not join them.
    changed = True
    while changed and len(merged) > 1:
        changed = False
        centers = [(left + right) / 2 for left, right in merged]
        pitches = [b - a for a, b in zip(centers, centers[1:]) if b > a]
        typical = statistics.median(pitches) if pitches else 40.0
        for index in range(len(merged) - 1):
            if centers[index + 1] - centers[index] >= 0.55 * typical:
                continue
            if not any(_crosses(seg, merged[index], merged[index + 1]) for line in block if line is not skeleton for seg in line.segments):
                continue
            merged[index][0] = min(merged[index][0], merged[index + 1][0])
            merged[index][1] = max(merged[index][1], merged[index + 1][1])
            del merged[index + 1]
            changed = True
            break
    return [(a, b) for a, b in merged]


def _absorb_column(columns: list[list[float]], x0: float, x1: float, text: str = "") -> None:
    """Widen the one column this piece belongs to.

    A piece that covers two columns is a merged heading and is left out. A figure that only
    reaches the next heading (a right-aligned amount ending where that heading starts) belongs
    to that heading, not the column on its left. A figure sitting in the gap before the next
    heading belongs to the heading on its left, even when the column is wide.
    """
    real = [index for index, (left, right) in enumerate(columns) if x0 <= right and x1 >= left]
    if len(real) > 1:
        return
    if len(real) == 1:
        _widen(columns, real[0], x0, x1)
        return
    # Less than a point is rounding. A real gap, even a small one, is the space before the next column.
    hair = [index for index, (left, right) in enumerate(columns) if x0 <= right + 0.75 and x1 >= left - 0.75]
    if len(hair) == 1:
        _widen(columns, hair[0], x0, x1)
        return
    for index, (left, right) in enumerate(columns):
        nxt = columns[index + 1][0] if index + 1 < len(columns) else 10**6
        prev = columns[index - 1][1] if index else -10**6
        if right < x0 and x1 <= nxt + 1:
            gap_left = x0 - right
            gap_right = nxt - x1
            room = max(nxt - right, 1.0)
            if gap_right <= 0.75 and index + 1 < len(columns):
                _widen(columns, index + 1, x0, x1)
            elif _amount_cell(text) or gap_left < min(0.55 * room, 60):
                _widen(columns, index, x0, x1)
            return
        if x1 < left and x0 >= prev - 1:
            # A heading drawn just to the left of its figures. A wide gap is a different column.
            room = max(left - prev, 1.0)
            if left - x1 <= 1.5 or left - x1 < min(0.55 * room, 60):
                _widen(columns, index, x0, x1)
            return


def _widen(columns: list[list[float]], index: int, x0: float, x1: float) -> None:
    """Grow a column toward this piece, stopping at the neighbouring column."""
    prev = columns[index - 1][1] if index else x0
    nxt = columns[index + 1][0] if index + 1 < len(columns) else x1
    columns[index][0] = min(columns[index][0], max(x0, prev))
    columns[index][1] = max(columns[index][1], min(x1, nxt))


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
_DATE_CELL = re.compile(r"\d{1,4}[-/.]\d{1,2}(?:[-/.]\d{1,4})?")
_MONTH_WORD = re.compile(r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*", re.I)


def _amount_cell(cell: str) -> bool:
    """A figure. A year or a date is a column heading on a statement, not an amount."""
    text = (cell or "").strip()
    if not text or not tables.is_value(text) or _YEAR.fullmatch(text):
        return False
    return _MONTH_WORD.search(text) is None


def _is_table(rows: list[Line]) -> bool:
    """Whether these aligned lines are a table. Two lines qualify when the first names columns and the second has figures."""
    if len(rows) >= 3:
        return True
    if len(rows) != 2 or len(rows[0].segments) < 2 or len(rows[1].segments) < 2:
        return False
    top = [word.text for segment in rows[0].segments for word in segment]
    bottom = [word.text for segment in rows[1].segments for word in segment]
    # A year or a date in the top row is a column name. An amount there means this is not a header.
    if not top or any(word.endswith(":") for word in top) or any(_amount_cell(word) for word in top):
        return False
    return any(tables.is_value(word) for word in bottom)


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
    if stub is None and below and not below[0].strip() and len(columns) > 2 and segs[0][1] <= columns[1][0] + 2:
        # A first heading inside the first column with nothing under it is that column's own name, merged down
        # the heading rows ("Department" beside "Chicago" over "HC | Salary"), not a group.
        stub = 0
    if stub is not None:
        covers[stub] = [min(range(len(centers)), key=lambda i: abs(centers[i] - (segs[stub][0] + segs[stub][1]) / 2))]
    groups = [index for index in range(len(segs)) if index != stub]
    if len(groups) >= 2 and below:
        repeated = _repeated_runs(below, len(groups))
        if repeated and not (stub is not None and any(0 in cols for cols in repeated)):
            for index, columns_ in zip(groups, repeated):
                covers[index] = columns_
            return [covered or [] for covered in covers]
    if len(groups) >= 2 and below and len(columns) > 2:
        centred = list(covers)
        heads, first = list(groups), 1 if stub is not None else 0
        # A first heading inside the first column with nothing under it is that column's own name,
        # merged down the heading rows ("Department" beside "Q3 2026" and "Actual").
        if stub is None and segs[heads[0]][1] <= columns[1][0] + 2 and not below[0].strip():
            centred[heads[0]] = [0]
            heads, first = heads[1:], 1
        runs = _centred_runs([segs[index] for index in heads], columns, first)
        if runs:
            for index, run in zip(heads, runs):
                centred[index] = run
            return [covered or [] for covered in centred]
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


def _centred_runs(heads: list[tuple[float, float, str]], columns: list[tuple[float, float]], first: int) -> list[list[int]] | None:
    """Columns ``first`` onward split into one run per heading, side by side, each heading centered over
    its run or starting at its left: how a spreadsheet draws a heading merged across columns. None when
    some heading doesn't sit that way over any run, so the other readings get their turn."""
    count, n = len(heads), len(columns)
    if not count or n - first < count:
        return None

    def cost(head: tuple[float, float, str], a: int, b: int) -> float:
        left, right = columns[a][0], columns[b][1]
        if head[0] < left - 3 or head[1] > right + 3:
            return float("inf")
        off = min(abs((head[0] + head[1]) / 2 - (left + right) / 2), abs(head[0] - left))
        return off if off <= max(6.0, 0.12 * (right - left)) else float("inf")

    inf = float("inf")
    # best[i][end]: headings[:i] placed with the i-th run ending just before column ``end``.
    best = [[inf] * (n + 1) for _ in range(count + 1)]
    back = [[0] * (n + 1) for _ in range(count + 1)]
    for end in range(first + 1, n + 1):
        best[1][end] = cost(heads[0], first, end - 1)
        back[1][end] = first
    for i in range(2, count + 1):
        for end in range(first + i, n + 1):
            for start in range(first + i - 1, end):
                total = best[i - 1][start] + cost(heads[i - 1], start, end - 1)
                if total < best[i][end]:
                    best[i][end], back[i][end] = total, start
    if best[count][n] == inf:
        return None
    runs: list[list[int]] = []
    end = n
    for i in range(count, 0, -1):
        start = back[i][end]
        runs.append(list(range(start, end)))
        end = start
    return runs[::-1]


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
    # A subtotal ends its category: rows on either side of one never share a category, and the subtotal
    # row itself ("Total Vehicles") already names what it adds up.
    block, blocks = 0, []
    for row in rows:
        blocks.append(block)
        block += _total_row(row)
    for column in columns:
        anchors = [
            index for index, row in enumerate(rows)
            if column < len(row) and row[column].strip() and not _TOTAL.match(row[column].strip())
        ]
        if not anchors:
            continue
        for index, row in enumerate(rows):
            if column >= len(row) or row[column].strip() or not _has_value(row, value_cols) or _total_row(row):
                continue
            near = [anchor for anchor in anchors if blocks[anchor] == blocks[index]] or anchors
            nearest = min(near, key=lambda anchor: (abs(mids[anchor] - mids[index]), anchor > index, anchor))
            row[column] = rows[nearest][column]
        _whole_names(rows, column, blocks)
    return columns


def _whole_names(rows: list[list[str]], column: int, blocks: list[int]) -> None:
    """A two-line category merged over two rows prints one line beside each ("Computer", "Equipment"). The
    subtotal under them names it whole ("Total Computer Equipment"): when the pieces read that name in
    order, every row of the block takes it."""
    for index, row in enumerate(rows):
        if not _total_row(row):
            continue
        name = _TOTAL.sub("", next(cell.strip() for cell in row if cell.strip())).strip()
        members = [k for k in range(index) if blocks[k] == blocks[index] and column < len(rows[k]) and not _total_row(rows[k])]
        pieces = list(dict.fromkeys(rows[k][column].strip() for k in members if rows[k][column].strip()))
        if len(pieces) >= 2 and " ".join(" ".join(pieces).split()).lower() == " ".join(name.split()).lower():
            for k in members:
                rows[k][column] = name


_TOTAL = re.compile(r"^(?:grand\s+|sub-?)?totals?\b", re.I)
_TOTAL_LAST = re.compile(r"\b(?:sub-?)?totals?$", re.I)


def _total_row(row: list[str]) -> bool:
    """A subtotal or total row: its label starts "Total"/"Subtotal" or ends with it ("Finance Subtotal")."""
    first = next((cell.strip() for cell in row if cell.strip()), "")
    return bool(_TOTAL.match(first) or _TOTAL_LAST.search(first))


def _join_label_lines(
    rows: list[list[str]], mids: list[float], spans: list[list[tuple[float, float]]], extra: list[list]
) -> tuple[list[list[str]], list[float]]:
    """A category name wrapped onto two lines in a cell merged down several rows ("Computer" over
    "Equipment") is one name. Lines holding only that cell, in the same column, drawn at the same left
    edge or the same centre, closer together than rows are, are joined top to bottom. (An indented label
    under another is a nested category, not the rest of its name.) ``spans``: each cell's extent on the
    page. ``extra`` lists lose the same lines."""
    pitch = _row_pitch(rows, mids, 0)
    if not pitch:
        return rows, mids
    rows, mids = [list(row) for row in rows], list(mids)

    def only(row: list[str]) -> int | None:
        filled = [c for c, cell in enumerate(row) if cell.strip()]
        return filled[0] if len(filled) == 1 and not tables.is_value(row[filled[0]]) else None

    def aligned(a: tuple[float, float], b: tuple[float, float]) -> bool:
        return abs(a[0] - b[0]) <= 1.5 or abs((a[0] + a[1]) / 2 - (b[0] + b[1]) / 2) <= 1.5

    k = 0
    while k + 1 < len(rows):
        column = only(rows[k])
        if (
            column is not None
            and only(rows[k + 1]) == column
            and mids[k] - mids[k + 1] < 0.8 * pitch
            and aligned(spans[k][column], spans[k + 1][column])
        ):
            rows[k][column] = f"{rows[k][column]} {rows[k + 1][column]}"
            mids[k] = (mids[k] + mids[k + 1]) / 2
            spans[k][column] = (min(spans[k][column][0], spans[k + 1][column][0]), max(spans[k][column][1], spans[k + 1][column][1]))
            del rows[k + 1], mids[k + 1], spans[k + 1]
            for sequence in extra:
                del sequence[k + 1]
            continue
        k += 1
    return rows, mids


def _row_pitch(rows: list[list[str]], mids: list[float], start: int, figures=None) -> float:
    """How far apart rows are: two rows of figures one after the other, else any two lines from ``start``."""
    figures = figures or (lambda row: any(_amount_cell(cell) for cell in row))
    pitches = [mids[k] - mids[k + 1] for k in range(start, len(rows) - 1) if figures(rows[k]) and figures(rows[k + 1])]
    pitches = pitches or [mids[k] - mids[k + 1] for k in range(start, len(rows) - 1)]
    return statistics.median(pitches) if pitches else 0.0


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
    # Columns found across the page can be empty in this block (another table's columns).
    used = sorted({_place(segment, columns) for line in block for segment in line.segments})
    columns = [columns[index] for index in used]
    grid: list[list[str]] = []
    bolds: list[bool] = []
    seg_rows: list[list[tuple[float, float, str]]] = []
    row_x: list[float] = []
    cell_x: list[list[float]] = []
    cell_end: list[list[float]] = []
    for line in block:
        row = [""] * len(columns)
        xs = [0.0] * len(columns)
        ends = [0.0] * len(columns)
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
                ends[column] = x1
        grid.append(row)
        cell_x.append(xs)
        cell_end.append(ends)
        seg_rows.append(segs)
        row_x.append(first_x)
        bolds.append(line.bold)
    mids = [line.mid for line in block]
    sizes = [line.size for line in block]
    groups = _wrapped_headings(grid, mids)
    if groups:
        grid = [_stack_cells([grid[k] for k in group]) for group in groups]
        seg_rows = [_stack_segments([seg_rows[k] for k in group], columns) for group in groups]
        cell_x = [[min((cell_x[k][c] for k in group if cell_x[k][c]), default=0.0) for c in range(len(columns))] for group in groups]
        cell_end = [[max(cell_end[k][c] for k in group) for c in range(len(columns))] for group in groups]
        bolds = [all(bolds[k] for k in group) for group in groups]
        row_x = [min(row_x[k] for k in group) for group in groups]
        sizes = [max(sizes[k] for k in group) for group in groups]
        mids = [mids[group[0]] for group in groups]

    peeled = _peel_side_facts(grid)
    facts: list[str] = []
    if peeled:
        facts, grid = peeled
        bolds = [False] * len(grid)
        cell_x = [row[2:] for row in cell_x]
        cell_end = [row[2:] for row in cell_end]

    if _prose(grid):
        width = len(grid[0]) if grid else 0
        return _dehyphenate([" ".join(row[c] for row in grid if c < len(row) and row[c]) for c in range(width)]), None, facts

    bold_first = bolds[0] and not all(bolds[1:])
    heads = _header_depth(grid) if not peeled else 1
    spanned = _span_labels(seg_rows[:heads], columns) if not peeled else None
    header_mid = mids[0]
    if spanned and (_usable_header(spanned, grid[heads:], bold_first) or heads > 1):
        labels = spanned
        grid, bolds, mids, row_x, cell_x, cell_end, sizes = (
            grid[heads:], bolds[heads:], mids[heads:], row_x[heads:], cell_x[heads:], cell_end[heads:], sizes[heads:]
        )
    else:
        labels = None
        header = tables.has_header(grid, bold_first=bold_first)
        if header:
            labels = [_clean_label(cell) for cell in grid[0]]
            grid, bolds, mids, row_x, cell_x, cell_end, sizes = (
                grid[1:], bolds[1:], mids[1:], row_x[1:], cell_x[1:], cell_end[1:], sizes[1:]
            )
    group_cols: list[int] = []
    if labels:
        spans = [list(zip(starts, ends)) for starts, ends in zip(cell_x, cell_end)]
        grid, mids = _join_label_lines(grid, mids, spans, [row_x, bolds, cell_x, sizes])
        grid, mids, row_x, bolds, sizes = _join_body(grid, mids, row_x, bolds, cell_x, sizes)
        group_cols = _fill_grouping_columns(grid, mids)
        kept = [index for index in range(len(grid)) if not _absorbed(grid, index)]
        grid = [grid[index] for index in kept]
        mids = [mids[index] for index in kept]
        row_x = [row_x[index] for index in kept]
        bolds = [bolds[index] for index in kept]
        cell_x = [cell_x[index] for index in kept]
        sizes = [sizes[index] for index in kept]

    def render(row: list[str]) -> str:
        return tables.labelled_row(labels, row) if labels else tables.table_lines([row], header=False)[0]

    body_lines = _grouped_lines(grid, row_x, bolds, render, group_cols, cell_x, sizes) if labels else [render(row) for row in grid]
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


def _wrapped_headings(grid: list[list[str]], mids: list[float]) -> list[list[int]] | None:
    """Heading lines that are one sheet row, as groups of row indexes (every other row on its own).

    A heading cell's text wraps onto a second line, and a one-line heading beside it is centered
    between the two, so one row of headings is printed as two or three lines. Those lines sit closer
    together than the rows of figures under them, and each fills the same cells as the lines above
    it or cells none of them filled, or the cells above and fewer new ones (a one-line "Total" set halfway
    down three wrapped headings). Excel's wrapped lines sit under 0.9 of a row apart. A group heading over
    several columns ("Q3 2026" over "Actual", "Budget") is a row of its own: the row under it fills more new
    cells than it shares.
    """
    def figures_in(row: list[str]) -> bool:
        # A date on a heading line is part of a column name ("Accum. Depr." over "12/31/25").
        return any(_amount_cell(cell) and not _DATE_CELL.fullmatch(cell.strip()) for cell in row)

    lead = 0
    while lead < min(len(grid), 6) and not figures_in(grid[lead]):
        lead += 1
    if lead < 2 or lead >= len(grid):
        return None
    pitch = _row_pitch(grid, mids, lead, figures_in)
    if not pitch:
        return None
    groups = [[0]]
    filled = {c for c, cell in enumerate(grid[0]) if cell}
    for k in range(1, lead):
        mine = {c for c, cell in enumerate(grid[k]) if cell}
        # A line that fills the cells above and a few more is the same row: a one-line heading ("Total")
        # sits halfway down the wrapped ones beside it. A group heading has more columns under it than
        # it fills itself.
        wider = filled < mine and len(mine - filled) < len(filled)
        if mids[k - 1] - mids[k] < 0.9 * pitch and (mine <= filled or not mine & filled or wider):
            groups[-1].append(k)
            filled |= mine
        else:
            groups.append([k])
            filled = mine
    if all(len(group) == 1 for group in groups):
        return None
    return groups + [[k] for k in range(lead, len(grid))]


def _stack_cells(rows: list[list[str]]) -> list[str]:
    """Lines of one row, each column's text read top to bottom."""
    return [" ".join(row[c] for row in rows if row[c]).strip() for c in range(len(rows[0]))]


def _stack_segments(rows: list[list[tuple[float, float, str]]], columns: list[tuple[float, float]]) -> list[tuple[float, float, str]]:
    """The pieces of one row's lines: pieces that start in the same column are one, read top to bottom."""
    by_column: dict[int, list[tuple[float, float, str]]] = {}
    for segs in rows:
        for seg in segs:
            if seg[2]:
                by_column.setdefault(_place_span(seg, columns), []).append(seg)
    stacked = [
        (min(seg[0] for seg in segs), max(seg[1] for seg in segs), " ".join(seg[2] for seg in segs))
        for _column, segs in sorted(by_column.items())
    ]
    return stacked


def _place_span(seg: tuple[float, float, str], columns: list[tuple[float, float]]) -> int:
    return _place([Word(seg[2], seg[0], seg[1], False)], columns)


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
    filled = [label.strip() for label in labels if label and label.strip()]
    if len(filled) < 2 or any(label.endswith(":") or _amount_cell(label) for label in filled):
        return False
    # "2024" and "31 March 2024" are column names. has_header treats both as figures.
    if all(_YEAR.fullmatch(label) for label in filled):
        return True
    return any(_amount_cell(cell) for row in body for cell in row)


def _clean_label(cell: str) -> str:
    return cell.strip()


def _join_body(
    rows: list[list[str]],
    mids: list[float],
    xs: list[float],
    bolds: list[bool],
    cell_x: list[list[float]],
    sizes: list[float],
) -> tuple[list[list[str]], list[float], list[float], list[bool], list[float]]:
    rows, mids = _join_wrapped(rows, mids, first=0, extra=[xs, bolds, cell_x, sizes], sizes=sizes)
    return rows, mids, xs, bolds, sizes


def _grouped_lines(
    rows: list[list[str]],
    xs: list[float],
    bolds: list[bool],
    render,
    group_cols: list[int],
    cell_x: list[list[float]],
    sizes: list[float],
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
        label = _one_label(row) if bold or _indents_next(rows, cell_x, index, sizes) else ""
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


def _indents_next(rows: list[list[str]], cell_x: list[list[float]], index: int, sizes: list[float] | None = None) -> bool:
    """A category row that is not bold still names the rows under it when they are indented or smaller."""
    if not _one_label(rows[index]) or index + 1 >= len(rows):
        return False
    this = _stub_x(rows[index], cell_x[index] if index < len(cell_x) else [])
    nxt = _stub_x(rows[index + 1], cell_x[index + 1] if index + 1 < len(cell_x) else [])
    if this is None:
        return False
    if nxt is not None and nxt > this + 4:
        return True
    return bool(sizes) and index + 1 < len(sizes) and sizes[index] > sizes[index + 1] + 0.5


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
_PAGE_MARK = re.compile(r"^page\s+\d{1,4}(?:\s+of\s+\d{1,4})?$", re.I)
# "2/14" alone on a line is a page count; in a row it is a date ("10/1").
_PAGE_COUNT = re.compile(r"^\d{1,4}\s*/\s*\d{1,4}$")


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
        if _PAGE_COUNT.match(line.strip()):
            continue
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
    grid: list[list[str]],
    mids: list[float],
    *,
    first: int,
    extra: list[list] | None = None,
    sizes: list[float] | None = None,
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
            if gap < 0.95 * usual
            and sum(1 for cell in rows[target] if cell) > filled
            and (sizes is None or abs(sizes[k] - sizes[target]) <= 0.6)
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
