"""The rows and columns of a file's tables that a question is about, read back out of the file's text.

CloseDesk writes a table one row per line, each cell named by its column:

    Vendor: Harbor Steel LLC | Vendor #: V1030 | Current: $48,500.00 | 31 - 60 Days: $22,150.00 | ...

A small model given a 40-row schedule often reads the row above, or the column beside, the one it
was asked about. So before it reads the file, the rows the question names (a vendor, an asset, a
date, payment 24) and the columns it names ("the 31-60 day bucket", "Q4 budget", "NBV") are picked
out by their words, and a question that names a column but no row ("which vendors are over 90
days", "the largest YTD depreciation") gets that column for every row. The model still gets the
whole file: this is a short index into it, copied word for word from the file's own lines.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from controller_inbox import tables
from controller_inbox.documents import MARKER_RE

MAX_ROWS = 6
MAX_CHARS = 1800

# Words that carry no meaning for matching a column or a row.
_STOP = frozenset(
    """
    a an and are as at be by did do does for from had has have how i in is it its me my of on or our per
    please show tell that the their them there these they this those to was we were what when where which
    who whom why will with would you your much many each every all any some us owe owed shown listed
    list give get find see look schedule table file pdf attachment row rows column columns
    """.split()
)
# Short forms a schedule's headings use for the words a question uses.
_SAME = {
    "pmt": "payment", "pymt": "payment", "no": "number", "#": "number", "num": "number", "amt": "amount",
    "acct": "account", "dept": "department", "qty": "quantity", "yrs": "years", "yr": "year", "mos": "months",
    "mo": "month", "bal": "balance", "beg": "beginning", "int": "interest", "prin": "principal",
    "exp": "expense", "rev": "revenue", "ytd": "ytd", "avg": "average", "pct": "percent", "%": "percent",
}
_MONTHS = {
    name: number
    for number, names in enumerate(
        [("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",), ("jun", "june"),
         ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
         ("dec", "december")],
        start=1,
    )
    for name in names
}
_SCAN = re.compile(
    r"\b(which|who|any|all|every|each|list|largest|biggest|highest|most|greatest|top|smallest|lowest|least|"
    r"fewest|over|above|under|below|more than|less than|greater than|total|sum|how many|rank|sort)\b",
    re.I,
)
_LARGEST = re.compile(r"\b(largest|biggest|highest|most|greatest|top|max(?:imum)?)\b", re.I)
_SMALLEST = re.compile(r"\b(smallest|lowest|least|fewest|min(?:imum)?)\b", re.I)
_TOTAL = re.compile(r"^(?:grand\s+|sub-?)?totals?\b", re.I)
_DATE = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?![\d/])")
_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_MONTH_DAY = re.compile(
    r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?"
    r"(?:\s*(?:-|–|to|and|&)\s*(\d{1,2})(?:st|nd|rd|th)?)?\b",
    re.I,
)
_DAY_MONTH = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\b", re.I)
_TOKEN = re.compile(r"[a-z0-9][a-z0-9&]*|#|%")


@dataclass
class Row:
    page: str
    group: str
    cells: list[tuple[str, str]]
    line: str

    # What the row is called (set from its table, see ``_name_rows``).
    name: str = ""


@dataclass
class Table:
    labels: tuple[str, ...]
    rows: list[Row] = field(default_factory=list)


def lookup(text: str, question: str, *, limit: int = MAX_CHARS) -> str:
    """The rows and columns of ``text``'s tables the question names, as lines copied from the file, or ""."""
    question = question or ""
    words = _question_words(question)
    if not words:
        return ""
    best: tuple[float, list[str]] | None = None
    for table in tables_in(text):
        found = _answer_lines(table, question, words)
        if found and (best is None or found[0] > best[0]):
            best = found
    if best is None:
        return ""
    lines = best[1]
    out = "Rows that match the question, copied from the table (the whole file follows):\n" + "\n".join(lines)
    if len(out) > limit:
        out = out[:limit].rsplit("\n", 1)[0] + "\n…"
    return out


def tables_in(text: str) -> list[Table]:
    """Every run of rows written as ``Label: value | Label: value`` with the same columns, with each row's page."""
    found: list[Table] = []
    page = ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        marker = MARKER_RE.match(line)
        if marker:
            page = marker.group(1)
            continue
        row = _row(line, page)
        if row is None:
            continue
        labels = tuple(label for label, _value in row.cells)
        if not found or found[-1].labels != labels:
            found.append(Table(labels))
        found[-1].rows.append(row)
    for table in found:
        _name_rows(table)
    return [table for table in found if table.rows]


def _name_rows(table: Table) -> None:
    """Name each row by its first text cell that no other row repeats (the asset or vendor, not the category
    merged down the rows), with the next one when that is a short code ("V-302 · Ford F-150 pickup")."""
    counts: dict[tuple[str, str], int] = {}
    for row in table.rows:
        for cell in row.cells:
            counts[cell] = counts.get(cell, 0) + 1
    for row in table.rows:
        first = row.cells[0]
        if first[1] and first[1] != tables.BLANK and counts[first] == 1 and _figure(first[1]):
            # A row keyed by a date or a number ("Date: 10/14", "Pmt #: 24") is named by it.
            row.name = f"{first[0]} {first[1]}"
            continue
        texts = [(label, value) for label, value in row.cells if value and value != tables.BLANK and not _figure(value)]
        unique = [cell for cell in texts if counts[cell] == 1] or texts or row.cells[:1]
        name = unique[0][1]
        rest = unique[1:2]
        if rest and len(name) <= 8 and any(ch.isdigit() for ch in name):
            name = f"{name} · {rest[0][1]}"
        row.name = name


def _row(line: str, page: str) -> Row | None:
    parts = [part.strip() for part in line.split(" | ")]
    group: list[str] = []
    cells: list[tuple[str, str]] = []
    for part in parts:
        label, sep, value = part.partition(": ")
        if sep and 0 < len(label) <= 60 and not _figure(label):
            cells.append((label.strip(), value.strip()))
        elif not cells:
            group.append(part)
        else:
            return None
    if len(cells) < 2:
        return None
    return Row(page, " > ".join(group), cells, line)


def _answer_lines(table: Table, question: str, words: list[str]) -> tuple[float, list[str]] | None:
    columns = _columns_named(table, words)
    used = {word for label in columns for word in _label_words(label) if word in words and _numeric(word)}
    row_words = [word for word in words if word not in used]
    scored = [(score, index) for index, row in enumerate(table.rows) if (score := _row_score(row, row_words, question)) > 0]
    lines: list[str] = []
    if scored:
        top = max(score for score, _index in scored)
        rows = [table.rows[index] for score, index in sorted(scored, key=lambda item: (-item[0], item[1])) if score >= top][:MAX_ROWS]
        for row in rows:
            where = f"{row.page} · " if row.page else ""
            picked = [f"{label}: {value}" for label, value in row.cells if label in columns and value != tables.BLANK]
            head = f"- {where}{row.name}" + (f" → {' | '.join(picked)}" if picked else "")
            lines.append(head if picked and len(row.cells) <= len(picked) + 1 else f"{head}\n  row: {row.line}")
        weight = top + (1 if columns else 0)
        return weight, lines
    if columns and _SCAN.search(question):
        view = _column_view(table, columns, question)
        return (0.5 + len(columns), view) if view else None
    return None


def _columns_named(table: Table, words: list[str]) -> list[str]:
    """The columns whose heading the question uses: every word of the heading, leaving out a date or number the
    question doesn't give ("net book value" for "NBV 9/30/26"), or most of them. A column is left out when
    another one matches the same words and more ("Q4 budget": "Q4 2026 Budget", not "Q3 2026 Budget")."""
    found: dict[str, set[str]] = {}
    scores: dict[str, float] = {}
    for label in table.labels:
        own = _label_words(label)
        hits = {word for word in own if _matches(word, words)}
        if not hits or (all(_numeric(word) for word in hits) and len(hits) < len(own)):
            continue
        named = [word for word in own if not _numeric(word)]
        # A date or number in the heading that the question doesn't give doesn't count against it.
        score = len(hits) / max(1, len(named) + sum(1 for word in hits if _numeric(word)))
        if score >= 0.5:
            found[label], scores[label] = hits, score
    if not found:
        return []
    best = max(scores.values())
    return [
        label for label in table.labels
        if label in found and scores[label] >= best - 0.34 and not any(found[label] < found[other] for other in found)
    ]


def _row_score(row: Row, words: list[str], question: str) -> float:
    """How many of the question's words name this row: its text cells, its first cell, its group, a date in it."""
    own: set[str] = set()
    for index, (_label, value) in enumerate(row.cells):
        if value == tables.BLANK:
            continue
        if not _figure(value) or (index == 0 and re.fullmatch(r"\d{1,4}", value)):
            own.update(_words(value))
        own.update(_dates(value))
    own.update(_words(row.group))
    score = 0.0
    for word in words:
        if word in own or (
            len(word) >= 4 and word.isalpha() and any(len(mine) >= 4 and mine.isalpha() and (mine.startswith(word) or word.startswith(mine)) for mine in own)
        ):
            score += 2 if _numeric(word) or "/" in word else 1
    if _TOTAL.match(row.name) and not re.search(r"\btotal", question, re.I):
        score -= 0.5
    return score


def _column_view(table: Table, columns: list[str], question: str) -> list[str]:
    """Each named column for every row: the ones with a figure first (largest first when asked), then a count of the rest."""
    lines = []
    # Only columns of figures: "Vendor" for every row is the table itself.
    columns = [label for label in columns if sum(_figure(dict(row.cells).get(label, "")) for row in table.rows) >= 0.5 * len(table.rows)]
    for label in columns[:2]:
        values = []
        for row in table.rows:
            value = dict(row.cells).get(label, "")
            values.append((row, value))
        filled = [(row, value) for row, value in values if value and value != tables.BLANK and _figure_value(value) not in (None, 0.0)]
        empty = [row for row, value in values if (row, value) not in filled]
        order = "in the table's order"
        if _LARGEST.search(question) or _SMALLEST.search(question):
            body = [item for item in filled if not _TOTAL.match(item[0].name)]
            totals = [item for item in filled if _TOTAL.match(item[0].name)]
            body.sort(key=lambda item: _figure_value(item[1]) or 0.0, reverse=bool(_LARGEST.search(question)))
            filled = body + totals
            order = "largest first" if _LARGEST.search(question) else "smallest first"
        page = next((row.page for row in table.rows if row.page), "")
        where = f" ({page})" if page else ""
        shown = "; ".join(f"{_named(row)}: {value}" for row, value in filled[:40])
        line = f'- "{label}" for every row{where}, {order}: {shown or "none"}.'
        if empty:
            names = ", ".join(_named(row) for row in empty[:12]) + (" …" if len(empty) > 12 else "")
            line += f" Zero or blank: {names}."
        lines.append(line)
    return lines


def _named(row: Row) -> str:
    return f"{row.group} > {row.name}" if row.group else row.name


def _question_words(question: str) -> list[str]:
    words = [word for word in _words(question) if word not in _STOP]
    return list(dict.fromkeys(words + _dates(question)))


def _label_words(label: str) -> list[str]:
    words = [word for word in _words(label) if word not in _STOP or word in {"over", "under"}]
    return list(dict.fromkeys(words + _dates(label)))


def _words(text: str) -> list[str]:
    out = []
    for word in _TOKEN.findall((text or "").lower().replace("'s", "")):
        word = _SAME.get(word, word)
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        if len(word) == 1 and not word.isdigit() and word not in "#%":
            continue
        out.append(word)
    return out


def _dates(text: str) -> list[str]:
    """Month/day keys for the dates written in ``text``: 10/14, "October 14", "Oct 3-4", 2026-10-14."""
    keys: list[str] = []
    for match in _DATE.finditer(text or ""):
        month, day = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            keys.append(f"{month}/{day}")
    for match in _ISO.finditer(text or ""):
        keys.append(f"{int(match.group(2))}/{int(match.group(3))}")
    for match in _MONTH_DAY.finditer(text or ""):
        month = _MONTHS[match.group(1).lower()]
        first, last = int(match.group(2)), int(match.group(3) or match.group(2))
        if 1 <= first <= last <= 31 and last - first <= 7:
            keys += [f"{month}/{day}" for day in range(first, last + 1)]
    for match in _DAY_MONTH.finditer(text or ""):
        keys.append(f"{_MONTHS[match.group(2).lower()]}/{int(match.group(1))}")
    return keys


def _matches(word: str, words: list[str]) -> bool:
    """A heading word in the question: the same word, or one a short form of the other ("Amort." and
    "amortization"), or an acronym of the question's words ("NBV" for net book value)."""
    if word in words:
        return True
    if len(word) >= 3 and not _numeric(word):
        if any(len(other) >= 3 and not _numeric(other) and (other.startswith(word) or word.startswith(other)) for other in words):
            return True
        if 2 <= len(word) <= 4 and word.isalpha():
            initials = "".join(other[0] for other in words if other[:1].isalpha())
            return word in initials
    return False


def _numeric(word: str) -> bool:
    return any(ch.isdigit() for ch in word)


def _figure(value: str) -> bool:
    return tables.is_value(value)


def _figure_value(value: str) -> float | None:
    text = (value or "").strip()
    negative = text.startswith("(") and text.endswith(")") or text.startswith("-")
    digits = re.sub(r"[^\d.]", "", text)
    if not digits or digits.count(".") > 1:
        return None
    try:
        number = float(digits)
    except ValueError:
        return None
    return -number if negative else number
