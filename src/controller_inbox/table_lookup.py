"""Questions about a file's tables, answered from the table's own rows.

CloseDesk writes a table one row per line, each cell named by its column:

    Vendor: Harbor Steel LLC | Vendor #: V1030 | Current: $48,500.00 | 31 - 60 Days: $22,150.00 | ...

A small model given a 40-row schedule often reads the row above, or the column beside, the one it
was asked about, and adds a column up wrong. So the question is read against the table first:

* which columns it names ("the 31-60 day bucket", "Q4 budget", "NBV", "Jul-26 through Sep-26"),
* which rows (a vendor, an asset, a date, payment 24, "not started", "Priya's" in her column),
* what it limits them to (over $30,000, more than 7 years, in 2027, after October 5, over budget),
* and what it wants: the cell, the matching rows, a total, an average, a count, the largest or
  smallest, the first or last, a difference, or a total for each category.

The rows used, and anything worked out from them (exactly, with the subtotal rows left out and the
table's own total beside it), go at the top of the file text with how the question was read, so the
model can check that reading before it answers. The model still gets the whole file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from controller_inbox import tables
from controller_inbox.documents import MARKER_RE

MAX_ROWS = 6
MAX_LISTED = 25
MAX_CHARS = 1800

FOUND_HEAD = "Rows that match the question, copied from the table (the whole file follows):"
WORKED_HEAD = "Worked out from the table, exactly (check it is what was asked; the whole file follows):"

# Words that carry no meaning for matching a column or a row.
_STOP = frozenset(
    """
    a an and are as at be by did do does for from had has have how i in is it its me my of on or our per
    please show tell that the their them there these they this those to was we were what when where which
    who whom why will with would you your much many each every all any some us owe owed shown listed
    list give get find see look schedule table file pdf attachment row rows column columns
    """.split()
)
# Words that say what is wanted, not which row: they never pick out a row.
_ASKING = frozenset(
    """
    total sum add added combined altogether average mean count number largest biggest highest most greatest
    maximum max top smallest lowest least fewest minimum min first earliest last latest final difference
    compare compared versus vs more less higher lower over under above below than exceed exceeds exceeding
    between after before since until through during within left remaining still again paid due
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
_MONTH_NAMES = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DAYS = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)

# What the question wants, tried in this order ("how many ... in total" is a count).
_OPS = [
    ("count", re.compile(r"\bhow many\b|\bnumber of\b|\bcount\b", re.I)),
    ("average", re.compile(r"\baverage\b|\bmean\b", re.I)),
    ("difference", re.compile(r"\bdifference\b|\bhow much (?:more|less|higher|lower|bigger|smaller)\b|\bvs\.?\b|\bversus\b|\bcompared? (?:to|with)\b", re.I)),
    ("max", re.compile(r"\b(?:largest|biggest|highest|most|greatest|maximum|max)\b", re.I)),
    ("min", re.compile(r"\b(?:smallest|lowest|least|fewest|minimum|min)\b", re.I)),
    ("first", re.compile(r"\b(?:first|earliest)\b", re.I)),
    ("last", re.compile(r"\b(?:last|latest|final)\b", re.I)),
    ("sum", re.compile(r"\b(?:total|sum|add(?:ed)? up|combined|altogether|in all)\b", re.I)),
    ("list", re.compile(r"\b(?:which|who|whose|list|what are|show)\b", re.I)),
]
_HOW_MUCH = re.compile(r"\bhow much\b", re.I)
_NUM_COND = re.compile(
    r"(?:\b(more than|greater than|higher than|larger than|bigger than|over|above|exceed(?:s|ing)?|at least|no less than|"
    r"less than|lower than|smaller than|fewer than|under|below|at most|no more than|up to|equal to|exactly)|(>=|<=|>|<))"
    r"\s*(?:\$\s*)?(-?\d[\d,]*(?:\.\d+)?)(?:\s*(%)|\s*(k|m|mm|thousand|million)\b)?(?![\w/.-]*\d)",
    re.I,
)
_NUM_OPS = {
    "more than": ">", "greater than": ">", "higher than": ">", "larger than": ">", "bigger than": ">", "over": ">",
    "above": ">", "exceed": ">", "exceeds": ">", "exceeding": ">", "at least": ">=", "no less than": ">=",
    "less than": "<", "lower than": "<", "smaller than": "<", "fewer than": "<", "under": "<", "below": "<",
    "at most": "<=", "no more than": "<=", "up to": "<=", "equal to": "=", "exactly": "=",
    ">": ">", ">=": ">=", "<": "<", "<=": "<=",
}
# The words that join a limit to its figure or date ("more than", "after").
_CONNECTIVES = frozenset(
    "more greater higher larger bigger than over above exceed at least no less lower smaller fewer under below most "
    "up to equal exactly on or after before since from starting until till through by prior in during within the of "
    "between and month year k m mm thousand million".split()
)
_SCALE = {"k": 1000, "thousand": 1000, "m": 1_000_000, "mm": 1_000_000, "million": 1_000_000}
_DATE_TEXT = (
    rf"(?:\d{{4}}-\d{{1,2}}-\d{{1,2}}|\d{{1,2}}/\d{{1,2}}(?:/\d{{2,4}})?|"
    rf"(?:{_MONTH_NAMES})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?(?!\d)|"
    rf"\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTH_NAMES})(?:,?\s+\d{{4}})?|"
    rf"(?:{_MONTH_NAMES})\.?(?:\s+\d{{4}})?|(?:19|20)\d{{2}})"
)
_DATE_COND = re.compile(
    rf"\b(?:(between)\s+({_DATE_TEXT})\s+(?:and|to)\s+({_DATE_TEXT})"
    rf"|(on or after|on or before|after|since|from|starting|before|until|till|through|by|prior to|in|during|within)"
    rf"\s+(?:the\s+(?:month|year)\s+of\s+)?({_DATE_TEXT}))\b(?![-/]\d)",
    re.I,
)
_BUDGET = re.compile(r"\b(over|under|below|above|missed|beat|exceeded|within)\s+(?:\w+\s+){0,2}?budget\b", re.I)
_GROUP = re.compile(r"\b(?:by|per|for each|for every|each)\s+([a-z][a-z#&-]*)", re.I)
_RANGE = re.compile(r"^\s*(?:through|thru|to|until|-|–)\s*$", re.I)
_TOTAL = re.compile(r"^(?:grand\s+|sub-?)?totals?\b|^%", re.I)
# A row's own label can name its total last ("Finance Subtotal", "Company Total"); a description can't.
_TOTAL_LAST = re.compile(r"\b(?:sub-?)?totals?$", re.I)
_DATE = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?![\d/])")
_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_MONTH_DAY = re.compile(
    rf"\b({_MONTH_NAMES})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:\s*(?:-|–|to|and|&)\s*(\d{{1,2}})(?:st|nd|rd|th)?)?(?:,?\s+(\d{{4}}))?\b",
    re.I,
)
_DAY_MONTH = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_NAMES})\b(?:,?\s+(\d{{4}}))?", re.I)
_WEEKDAY = re.compile(r"^(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*\.?,?$", re.I)
_TOKEN = re.compile(r"[a-z0-9][a-z0-9&]*|#|%")
_CELL_REF = re.compile(r"^([A-Z]{1,3}\d{1,7})(?: \((.+)\))?$")
_DASHES = frozenset({"-", "–", "—", "$ -", "$-"})
_LEADING_FIGURE = re.compile(r"\s*([(\-−])?\s*([$€£¥])?\s*([(\-−])?\s*(\d[\d,]*(?:\.\d+)?|\.\d+)")

When = tuple  # (year or None, month, day)


@dataclass
class Row:
    page: str
    group: str
    cells: list[tuple[str, str]]
    line: str
    # Each cell's reference in a workbook ("E10"), "" elsewhere.
    refs: list[str] = field(default_factory=list)
    # What the row is called, and whether it is a total or subtotal (set from its table, see ``_name_rows``).
    name: str = ""
    total: bool = False
    # Which line of the text it is.
    at: int = 0

    def value(self, label: str) -> str:
        for name, value in self.cells:
            if name == label:
                return "" if value == tables.BLANK else value
        return ""

    def cell(self, label: str) -> str:
        for index, (name, value) in enumerate(self.cells):
            if name == label:
                ref = self.refs[index] if index < len(self.refs) else ""
                return f"{ref} ({name}): {value}" if ref else f"{name}: {value}"
        return ""


@dataclass
class Table:
    labels: tuple[str, ...]
    rows: list[Row] = field(default_factory=list)
    kinds: dict[str, str] = field(default_factory=dict)

    @property
    def body(self) -> list[Row]:
        return [row for row in self.rows if not row.total]

    @property
    def where(self) -> str:
        return next((row.page for row in self.rows if row.page), "")


@dataclass
class Condition:
    """A limit the question puts on the rows: amounts ("over $30,000") or dates ("in 2027", "after October 5")."""

    kind: str  # "number" or "date"
    op: str  # >, >=, <, <=, =, or for dates also "in", "between"
    value: object  # Decimal, or a (low, high) pair of dates
    phrase: str
    at: int
    column: str = ""


@dataclass
class Question:
    text: str
    words: list[str]
    op: str
    conditions: list[Condition]
    budget: str = ""  # ">" over budget, "<" under budget
    group: str = ""  # the word after "by" / "per" / "for each"


@dataclass
class Answer:
    weight: float
    head: str
    lines: list[str]


def lookup(text: str, question: str, *, limit: int = MAX_CHARS) -> str:
    """The rows of ``text``'s tables that answer the question, and anything worked out from them, or ""."""
    found = answer(text, question)
    return render(found, limit) if found else ""


def answer(text: str, question: str) -> Answer | None:
    """The best answer any of ``text``'s tables gives, with how well it matched (``weight``), or None."""
    asked = read_question(question)
    if not asked.words and not asked.conditions:
        return None
    best: Answer | None = None
    for table in tables_in(text):
        found = _answer(table, asked)
        if found and (best is None or found.weight > best.weight):
            best = found
    return best


def render(found: Answer, limit: int = MAX_CHARS) -> str:
    out = found.head + "\n" + "\n".join(found.lines)
    if len(out) > limit:
        out = out[:limit].rsplit("\n", 1)[0] + "\n…"
    return out


# Tables ----------------------------------------------------------------------------------------


def tables_in(text: str) -> list[Table]:
    """Every run of rows written as ``Label: value | Label: value`` with the same columns, with each row's page."""
    parsed: list[Row] = []
    # The rows that come after a heading on their page: a list under its own heading is its own table, even
    # with the same columns as the one above ("Outstanding checks - Operating", then "... - Payroll").
    headed: set[int] = set()
    page = ""
    heading = False
    for at, raw in enumerate((text or "").splitlines()):
        line = raw.strip()
        marker = MARKER_RE.match(line)
        if marker:
            page = marker.group(1)
            heading = False
            continue
        if line == "[heading]":
            heading = True
            continue
        row = _row(line, page)
        if row is not None:
            row.at = at
            if heading:
                headed.add(at)
                heading = False
            parsed.append(row)
    _with_colon_labels(parsed)
    found: list[Table] = []
    for row in parsed:
        labels = tuple(label for label, _value in row.cells)
        # A PDF's table runs on over its pages; a workbook's sheets with the same columns are different tables.
        new_sheet = bool(found) and row.page.startswith("sheet") and row.page != found[-1].rows[-1].page
        new_list = bool(found) and row.at in headed and row.page == found[-1].rows[-1].page
        if not found or found[-1].labels != labels or new_sheet or new_list:
            found.append(Table(labels))
        found[-1].rows.append(row)
    for table in found:
        _with_row_labels(table)
    found = _joined_across_pages(found)
    for table in found:
        _name_rows(table)
        table.kinds = _kinds(table)
        if _settle_totals(table):
            table.kinds = _kinds(table)
    return found


@dataclass
class Verdict:
    """How a table's printed totals check out against the rows they add up."""

    matched: int = 0
    mismatched: list[str] = field(default_factory=list)

    @property
    def checked(self) -> int:
        return self.matched + len(self.mismatched)


def verify(table: Table) -> Verdict:
    """Check each printed total and subtotal of each figure column against the rows above it.

    A schedule carries its own checksums: a subtotal is the sum of the rows since the last one, a grand total
    the sum of every row or of the subtotals. A reading that put a figure in the wrong column or row, or lost
    a row, breaks them; a right reading keeps them. A percent row ("% of Total") is not a sum and is skipped."""
    verdict = Verdict()
    for label, kind in table.kinds.items():
        if kind != "figure":
            continue
        running = _Running()
        for row in table.rows:
            raw = row.value(label)
            value = _number(raw)
            if not row.total:
                running.add(value, raw)
                continue
            if value is None or "%" in raw or not running.started():
                continue
            if running.close(value):
                verdict.matched += 1
            else:
                verdict.mismatched.append(f"{row.name} ({label}): printed {raw}, the rows above add to {_plain(running.group_sum())}")
    return verdict


class _Running:
    """For one figure column, read down the table: the sums a total row may print there. A subtotal is the sum
    of the rows since the last total; a grand total the sum of every row, or of the subtotals."""

    def __init__(self) -> None:
        self.group: list[Decimal] = []
        self.body: list[Decimal] = []
        self.subtotals: list[Decimal] = []
        self.places = 0

    def add(self, value: Decimal | None, raw: str) -> None:
        if value is None:
            return
        if "." in raw:
            self.places = max(self.places, len(raw.split(".")[-1].rstrip(")% ")))
        self.group.append(value)
        self.body.append(value)

    def started(self) -> bool:
        return bool(self.group or self.subtotals)

    def group_sum(self) -> Decimal:
        return sum(self.group or self.body, Decimal(0))

    def holds(self, value: Decimal) -> str:
        """"group" when ``value`` is the sum of the rows since the last total, "all" when it is the sum of every
        row or of the subtotals, else "". Each part was rounded when it was printed, so a total can be off by
        half a unit of the last place per part."""
        slack = Decimal(1).scaleb(-self.places) * (Decimal("0.5") * (len(self.body) + 1))
        if self.group and abs(sum(self.group) - value) <= slack:
            return "group"
        if any(parts and abs(sum(parts) - value) <= slack for parts in (self.body, self.subtotals, self.subtotals + self.group)):
            return "all"
        return ""

    def close(self, value: Decimal) -> bool:
        """A total row printing ``value``: whether it holds; the next group starts after it either way."""
        held = self.holds(value)
        if held == "group":
            self.subtotals.append(value)
        elif held == "all":
            self.subtotals = []
        self.group = []
        return bool(held)


# A label that is only the word for a total: such a row is a total whatever its figures.
_TOTAL_WORD = re.compile(r"^(?:grand\s+|sub-?\s?)?totals?:?$|^%", re.I)


def _settle_totals(table: Table) -> bool:
    """Decide which rows are totals by their figures as well as their words. A row named "Total Quality
    Logistics" whose figures don't add up the rows above it is a vendor, not a total; a row with no name at all
    whose every figure adds up the rows above it is a total (a sheet often leaves its total row unlabeled).
    Returns whether any row changed."""
    figures = [label for label, kind in table.kinds.items() if kind == "figure"]
    if not figures:
        return False
    running = {label: _Running() for label in figures}
    changed = False
    seen = 0
    for row in table.rows:
        filled = {
            label: value
            for label in figures
            if (value := _number(row.value(label))) is not None and "%" not in row.value(label)
        }
        held = {label for label, value in filled.items() if running[label].started() and running[label].holds(value)}
        named = [value for _label, value in row.cells if value and value != tables.BLANK and not tables.is_value(value)]
        if row.total and seen and filled and not held and not _TOTAL_WORD.match(row.name.strip()):
            row.total, changed = False, True
        elif not row.total and seen >= 2 and not named and len(filled) >= min(2, len(figures)) and held == set(filled) and any(filled.values()):
            row.total, changed = True, True
            row.name = row.name if row.name and row.name != tables.BLANK else "Total"
        for label in figures:
            raw = row.value(label)
            if row.total:
                value = _number(raw)
                if value is not None and "%" not in raw and running[label].started():
                    running[label].close(value)
            else:
                running[label].add(_number(raw), raw)
        seen += not row.total
    return changed


def _plain(number: Decimal) -> str:
    return f"{number:,.2f}" if number != number.to_integral_value() else f"{number:,.0f}"


def _with_colon_labels(rows: list[Row]) -> None:
    """A bare row label with a colon in it ("Add: Deposits in transit | Operating: 86,412.50") reads as a
    cell named "Add". When the rows around it have bare labels and the same columns as the rest of it,
    it is a row label too."""
    for index, row in enumerate(rows):
        if row.group or row.refs and any(row.refs) or len(row.cells) < 3:
            continue
        label, value = row.cells[0]
        if len(label) > 25 or not value or tables.is_value(value):
            continue
        rest = [name for name, _value in row.cells[1:]]
        near = rows[max(0, index - 8) : index] + rows[index + 1 : index + 9]
        if any(other.group and other.page == row.page and [name for name, _value in other.cells] == rest for other in near):
            row.group = f"{label}: {value}"
            row.cells = row.cells[1:]
            row.refs = row.refs[1:]


# The heading a row-label column gets when the sheet left it blank.
ROW_LABEL = "Line"


def _with_row_labels(table: Table) -> None:
    """A row-label column without a heading ("Adjusted bank balance" down the side of a reconciliation)
    is written bare before the labelled cells, like a section name. A section repeats down its rows; a
    row label names one row, so it is made the table's first column."""
    leads = [row.group for row in table.rows]
    if len(leads) < 2 or not all(leads) or len(set(leads)) < 0.9 * len(leads):
        return
    table.labels = (ROW_LABEL, *table.labels)
    for row in table.rows:
        row.cells.insert(0, (ROW_LABEL, row.group))
        if row.refs:
            row.refs.insert(0, "")
        row.group = ""


def _joined_across_pages(found: list[Table]) -> list[Table]:
    """A sheet too wide for its page prints its last columns on the next page, the row-name columns
    repeated ("Customer", "Region" again before "Sep-26 ... FY Total"). Those columns are the same rows:
    they are joined onto them, so a question over the whole year sees every month."""
    out: list[Table] = []
    for table in found:
        before = out[-1] if out else None
        if before is not None and before.rows and table.rows and table.rows[0].page != before.rows[-1].page:
            shared = [label for label in table.labels if label in before.labels]
            names = [tuple(row.value(label) for label in shared) for row in table.rows]
            if shared and shared == list(table.labels[: len(shared)]) and names == [
                tuple(row.value(label) for label in shared) for row in before.rows
            ] and len(set(table.labels) - set(shared)) >= 1:
                added = [label for label in table.labels if label not in shared]
                before.labels = (*before.labels, *added)
                for mine, theirs in zip(before.rows, table.rows):
                    index = {label: position for position, (label, _value) in enumerate(theirs.cells)}
                    for label in added:
                        mine.cells.append((label, theirs.cells[index[label]][1]))
                        if mine.refs:
                            mine.refs.append(theirs.refs[index[label]] if index[label] < len(theirs.refs) else "")
                continue
        out.append(table)
    return out


def _row(line: str, page: str) -> Row | None:
    """A table row, or None. A workbook row names each cell with its reference ("E10 (31 - 60 Days): 22,150");
    a workbook line of bare references ("A5: Vendor | B5: Vendor #") is its heading row, not data."""
    parts = [part.strip() for part in line.split(" | ")]
    # Every cell of a workbook row is named by its reference, all on one row ("A5", "B5"). Headings that only
    # look like references ("US01", "CA02" entity codes, "Q1" ... "Q4") are names.
    named = [_CELL_REF.match(part.partition(": ")[0].strip()) for part in parts if ": " in part]
    workbook = bool(named) and all(named) and len({re.sub(r"^[A-Z]+", "", ref.group(1)) for ref in named}) == 1
    group: list[str] = []
    cells: list[tuple[str, str]] = []
    refs: list[str] = []
    for part in parts:
        label, sep, value = part.partition(": ")
        label = label.strip()
        if sep and 0 < len(label) <= 60 and not tables.is_value(label):
            ref = ""
            if workbook and (cell := _CELL_REF.match(label)):
                if not cell.group(2):
                    return None
                ref, label = cell.group(1), cell.group(2)
            cells.append((label, value.strip()))
            refs.append(ref)
        elif not cells:
            group.append(part)
        else:
            return None
    if len(cells) < 2:
        return None
    return Row(page, " > ".join(group), cells, line, refs)


def _name_rows(table: Table) -> None:
    """Name each row by its first text cell that no other row repeats (the asset or vendor, not the category
    merged down the rows), with the next one when that is a short code ("V-302 · Ford F-150 pickup"). A row
    named "Total ...", "Grand Total", "% of ..." or "... Subtotal" is a total (see ``_is_total``)."""
    counts: dict[tuple[str, str], int] = {}
    for row in table.rows:
        for cell in row.cells:
            counts[cell] = counts.get(cell, 0) + 1
    key = _key_column(table)
    for row in table.rows:
        texts = [(label, value) for label, value in row.cells if value and value != tables.BLANK and not tables.is_value(value)]
        row.total = _is_total(row)
        first = row.cells[0]
        if first[1] and first[1] != tables.BLANK and counts[first] == 1 and tables.is_value(first[1]):
            # A row keyed by a date or a number ("Date: 10/14", "Pmt #: 24") is named by it.
            row.name = f"{first[0]} {first[1]}"
            continue
        keyed = [cell for cell in texts if cell[0] == key]
        unique = keyed + [cell for cell in texts if counts[cell] == 1 and cell not in keyed] or texts or row.cells[:1]
        name = unique[0][1]
        # A short code takes the text beside it: "V-302 · Ford F-150 pickup".
        after = [cell for cell in texts[texts.index(unique[0]) + 1:] if counts[cell] == 1] if unique[0] in texts else []
        if after and len(name) <= 8 and any(ch.isdigit() for ch in name):
            name = f"{name} · {after[0][1]}"
        row.name = name


def _is_total(row: Row) -> bool:
    """A total or subtotal row: a cell starts "Total", "Grand Total", "Subtotal" or "%", or the row's label
    (its first text) ends with "Total" or "Subtotal"."""
    texts = [value for _label, value in row.cells if value and value != tables.BLANK and not tables.is_value(value)]
    return any(_TOTAL.match(value) for value in texts) or bool(texts and _TOTAL_LAST.search(texts[0]))


def _key_column(table: Table) -> str:
    """The column that tells the rows apart (Asset ID, Vendor, Task): the first one of text whose values
    nearly every row has and no two rows share. A category merged down the rows repeats, so it is not."""
    rows = [row for row in table.rows if not _is_total(row)]
    for label in table.labels:
        values = [row.value(label) for row in rows if row.value(label)]
        if len(values) >= max(2, 0.8 * len(rows)) and len(set(values)) == len(values) and not all(tables.is_value(v) for v in values):
            return label
    return ""


def _kinds(table: Table) -> dict[str, str]:
    """Each column's kind from the cells of its ordinary rows: "date", "figure" or "text"."""
    kinds = {}
    for label in table.labels:
        cells = [row.value(label) for row in table.body if row.value(label)]
        # A "-" reads as the accounting format's zero, but a column of text can have dashes for "none" too:
        # the other cells decide.
        values = [value for value in cells if value.strip() not in _DASHES] or cells
        if not values:
            kinds[label] = "text"
            continue
        dates = sum(_when(value) is not None for value in values)
        figures = sum(_number(value) is not None for value in values)
        kinds[label] = "date" if dates >= 0.6 * len(values) else "figure" if figures >= 0.6 * len(values) else "text"
    return kinds


# The question ----------------------------------------------------------------------------------


def read_question(question: str) -> Question:
    """What the question asks for, its words, and the limits it puts on the rows."""
    text = (question or "").strip()
    found_op = next((name for name, pattern in _OPS if pattern.search(text)), "find")
    conditions: list[Condition] = []
    for match in _NUM_COND.finditer(text):
        word = (match.group(1) or match.group(2)).lower()
        number = Decimal(match.group(3).replace(",", ""))
        number *= _SCALE.get((match.group(5) or "").lower(), 1)
        conditions.append(Condition("number", _NUM_OPS.get(word, ">"), number, match.group(0).strip(), match.start()))
    for match in _DATE_COND.finditer(text):
        if match.group(1):
            low, high = _span(match.group(2)), _span(match.group(3))
            if low and high:
                conditions.append(Condition("date", "between", (low[0], high[1]), match.group(0).strip(), match.start()))
            continue
        word, span = match.group(4).lower(), _span(match.group(5))
        if span is None or (word in {"in", "during", "within", "by"} and _bare_number(match.group(5))):
            continue
        op = {"after": ">", "since": ">=", "from": ">=", "starting": ">=", "on or after": ">=", "before": "<",
              "prior to": "<", "until": "<=", "till": "<=", "through": "<=", "by": "<=", "on or before": "<="}.get(word, "in")
        conditions.append(Condition("date", op, span, match.group(0).strip(), match.start()))
    budget = _BUDGET.search(text)
    group = _GROUP.search(text)
    words = [word for word in _words(text) if word not in _STOP]
    return Question(
        text,
        list(dict.fromkeys(words + _dates(text))),
        _op_for(found_op, text),
        conditions,
        budget=">" if budget and budget.group(1).lower() in {"over", "above", "exceeded"} else "<" if budget else "",
        group=group.group(1).lower() if group else "",
    )


def _op_for(op: str, text: str) -> str:
    """"Which vendors have a total balance over $30,000" lists rows: there "total" names the column."""
    if op == "sum" and re.search(r"\b(?:which|who|whose|list)\b", text, re.I):
        return "list"
    return op


def _bare_number(text: str) -> bool:
    """A year alone after "in" is a year only when it looks like one ("in 2027"), not "in 30" days."""
    return text.strip().isdigit() and not re.fullmatch(r"(?:19|20)\d{2}", text.strip())


# Answering -------------------------------------------------------------------------------------


def _answer(table: Table, asked: Question) -> Answer | None:
    columns = _columns_named(table, asked)
    conditions = _placed(table, asked, columns)
    # The question's words that named a column ("review" for "Reviewer") don't also pick out rows.
    label_words = {word for word in asked.words for label in columns for own in _label_words(label) if _matches(own, [word])}
    held = {word for condition in conditions for word in _words(condition.phrase) + _dates(condition.phrase)}
    words = [word for word in asked.words if word not in label_words and word not in held and word not in _ASKING]

    # A word found in a named column of text limits that column ("Priya Raman" on PTO: PTO in her column).
    by_column: dict[str, list[str]] = {}
    for word in list(words):
        for label in columns:
            if table.kinds.get(label) == "text" and any(word in _cell_words(row.value(label)) for row in table.rows):
                by_column.setdefault(label, []).append(word)
                words.remove(word)
                break

    budget = _budget_pair(table, asked) if asked.budget else None
    rows = [
        row for row in table.rows
        if all(_meets(row, condition, table) for condition in conditions)
        and all(all(word in _cell_words(row.value(label)) for word in found) for label, found in by_column.items())
        and (budget is None or _over_under(row, budget, asked.budget))
    ]
    scores = {id(row): _row_score(row, words, asked.text) for row in rows}
    named = [row for row in rows if scores[id(row)][0] > 0]
    if named:
        top = max(scores[id(row)][0] for row in named)
        named = [row for row in named if scores[id(row)][0] >= top]
    limited = bool(conditions or by_column or budget or named)
    chosen = named or (rows if limited else [])
    figures = [label for label in columns if table.kinds.get(label) == "figure"]
    op = asked.op
    if op == "count" and not limited and figures:
        # "How many vendors have a balance over 90 days": the rows with a figure in that column.
        chosen = [row for row in table.body if (_number(row.value(figures[0])) or 0) != 0]
        limited = True
        reading_extra = f", where {figures[0]} is not zero"
    else:
        reading_extra = ""
    # The table's own total rows for these rows ("Total Vehicles"), kept to check a sum against.
    totals = [row for row in (chosen if limited else table.rows) if row.total and not row.name.startswith("%")]
    if op != "find":
        chosen = [row for row in chosen if not row.total] or chosen
    if op == "find" and limited and not named:
        op = "sum" if _HOW_MUCH.search(asked.text) and figures else "list"
    reading = _reading(conditions, by_column, budget, asked.budget, named, words, scores) or reading_extra
    # How much of the question this table answered: the columns it names, the rows its words pick out, and the
    # limits it could apply. An email's other tables are weighed against it, so a table that only fell back on
    # its "Total" column doesn't win over one that has the column and the rows asked about.
    quality = (
        2 * sum(1 for hits in columns.values() if hits)
        + (max(scores[id(row)][0] for row in named) if named else 0)
        + len(conditions) + len(by_column) + (2 if budget else 0)
    )

    def done(found: Answer | None) -> Answer | None:
        if found is not None:
            found.weight = quality + found.weight / 100
        return found

    if op != "find" and not columns and not limited:
        # "Is this the latest aging?" names no column and no rows: it is not a question about the table.
        return None
    if op in {"sum", "average", "max", "min"} and not figures:
        figures = [label for label in [_default_column(table)] if label]
    if op == "difference":
        found = _difference(table, asked, columns, figures, chosen, rows, words)
        if found:
            return done(found)
        op = "find"
    if asked.group and op in {"sum", "average", "count"}:
        found = _per_group(table, asked, figures, chosen if limited else table.body, op, reading)
        if found:
            return done(found)
    if op in {"sum", "average", "count", "max", "min", "first", "last"}:
        found = _worked(table, op, figures, columns, chosen if limited else table.body, totals, reading)
        if found:
            return done(found)
        op = "list" if limited else "find"
    if not chosen:
        if columns and figures and (op == "list" or asked.op != "find"):
            return done(_column_view(table, figures, asked))
        return None
    return done(_found(table, chosen, columns, scores, figures, reading, budget))


def _columns_named(table: Table, asked: Question) -> dict[str, set[str]]:
    """The columns the question names, with the words that name them: every word of the heading, leaving out
    a date or number the question doesn't give ("net book value" for "NBV 9/30/26"), or most of them. A column
    is left out when another matches the same words and more ("Q4 budget": "Q4 2026 Budget", not "Q3 2026
    Budget"). "Jul-26 through Sep-26" names the columns between them too."""
    found: dict[str, set[str]] = {}
    scores: dict[str, float] = {}
    for label in table.labels:
        own = _label_words(label)
        hits = {word for word in own if _matches(word, asked.words)}
        if not hits or (all(_numeric(word) for word in hits) and len(hits) < len(own)):
            continue
        named = [word for word in own if not _numeric(word)]
        # A date or number in the heading that the question doesn't give doesn't count against it.
        score = len(hits) / max(1, len(named) + sum(1 for word in hits if _numeric(word)))
        if score >= 0.5:
            found[label], scores[label] = hits, score
    if not found:
        return {}
    best = max(scores.values())
    keep = {
        label: found[label] for label in table.labels
        if label in found and scores[label] >= best - 0.34 and not any(found[label] < found[other] for other in found)
    }
    return _with_ranges(table, asked.text, keep)


def _with_ranges(table: Table, question: str, columns: dict[str, set[str]]) -> dict[str, set[str]]:
    lower = question.lower()
    spots = {label: lower.find(label.lower()) for label in columns}
    named = sorted((at, label) for label, at in spots.items() if at >= 0)
    for (at_a, a), (at_b, b) in zip(named, named[1:]):
        between = lower[at_a + len(a): at_b]
        if _RANGE.match(between):
            i, j = table.labels.index(a), table.labels.index(b)
            for label in table.labels[min(i, j): max(i, j) + 1]:
                columns.setdefault(label, set())
    return {label: columns[label] for label in table.labels if label in columns}


def _placed(table: Table, asked: Question, columns: dict[str, set[str]]) -> list[Condition]:
    """The question's limits that this table can apply, each on its column. A limit that is part of a heading
    ("over 90 days" for "Over 90 Days", "in Mar-26") names the column instead."""
    placed = []
    lower = asked.text.lower()
    for condition in asked.conditions:
        if _in_heading(condition, table):
            continue
        kind = "figure" if condition.kind == "number" else "date"
        candidates = [label for label in columns if table.kinds.get(label) == kind]
        if not candidates:
            candidates = [label for label in table.labels if table.kinds.get(label) == kind]
            if kind == "figure":
                candidates = [label for label in [_default_column(table)] if label]
        if not candidates:
            continue

        def distance(label: str) -> int:
            spots = [m.start() for word in columns.get(label, set()) if not _numeric(word) for m in re.finditer(re.escape(word[:5]), lower)]
            return min((abs(spot - condition.at) for spot in spots), default=10_000)

        column = min(candidates, key=lambda label: (distance(label), table.labels.index(label)))
        if condition.kind == "date" and _whole_years(condition.value) and not _dated_with_years(table, column):
            # "In 2027" says nothing about dates written without a year ("10/14").
            continue
        placed.append(Condition(condition.kind, condition.op, condition.value, condition.phrase, condition.at, column))
    return placed


def _whole_years(span: tuple[When, When]) -> bool:
    low, high = span
    return low[0] is not None and (low[1], low[2]) == (1, 1) and (high[1], high[2]) == (12, 31)


def _dated_with_years(table: Table, label: str) -> bool:
    return any((when := _when(row.value(label))) is not None and when[0] is not None for row in table.rows)


def _in_heading(condition: Condition, table: Table) -> bool:
    """The limit's words are a column's heading: "over 90" in "Over 90 Days" (its "over" too), "in 2026" in
    "FY 2026 Forecast"."""
    words = _words(condition.phrase) + _dates(condition.phrase)
    content = {word for word in words if word not in _CONNECTIVES}
    if not content:
        return False
    for label in table.labels:
        own = set(_label_words(label))
        if content <= own and (condition.kind == "date" or set(words) - content <= own):
            return True
    return False


def _meets(row: Row, condition: Condition, table: Table) -> bool:
    value = row.value(condition.column)
    if condition.kind == "number":
        number = _number(value)
        if number is None:
            return False
        target = condition.value
        if "%" in condition.phrase and "%" not in value and abs(number) <= 1:
            number *= 100
        return {
            ">": number > target, ">=": number >= target, "<": number < target, "<=": number <= target, "=": number == target,
        }[condition.op]
    when = _when(value)
    if when is None:
        return False
    low, high = condition.value
    if condition.op in {"in", "between"}:
        return _before_or_on(low, when) and _before_or_on(when, high)
    if condition.op == ">":
        return not _before_or_on(when, high)
    if condition.op == ">=":
        return _before_or_on(low, when)
    if condition.op == "<":
        return not _before_or_on(low, when)
    return _before_or_on(when, high)


def _budget_pair(table: Table, asked: Question) -> tuple[str, str] | None:
    """The actual (or forecast) and budget columns the question compares, by the words they share with it."""
    figures = [label for label in table.labels if table.kinds.get(label) == "figure"]
    budgets = [label for label in figures if "budget" in label.lower()]
    actuals = [label for label in figures if re.search(r"actual|forecast|spent|spend", label, re.I)]
    best = None
    for actual in actuals:
        rest = set(_label_words(actual)) - {"actual", "forecast", "spent", "spend"}
        for budget in budgets:
            if set(_label_words(budget)) - {"budget"} != rest:
                continue
            score = len(rest & set(asked.words)) + (0.5 if "actual" in actual.lower() else 0)
            if best is None or score > best[0]:
                best = (score, actual, budget)
    return (best[1], best[2]) if best else None


def _over_under(row: Row, pair: tuple[str, str], way: str) -> bool:
    actual, budget = _number(row.value(pair[0])), _number(row.value(pair[1]))
    if actual is None or budget is None or row.total:
        return False
    return actual > budget if way == ">" else actual < budget


def _default_column(table: Table) -> str:
    """The column a question means by "balance" or "amount" when it names none: a Total, Balance, Amount,
    Net or Cost column, else the only column of figures."""
    figures = [label for label in table.labels if table.kinds.get(label) == "figure"]
    for pattern in (r"^total$", r"\btotal\b", r"balance|amount|net\b|cost|value"):
        hits = [label for label in figures if re.search(pattern, label, re.I)]
        if hits:
            return hits[-1]
    return figures[0] if len(figures) == 1 else ""


# Working it out --------------------------------------------------------------------------------


def _worked(
    table: Table, op: str, figures: list[str], columns: dict[str, set[str]], rows: list[Row], totals: list[Row], reading: str
) -> Answer | None:
    body = [row for row in rows if not row.total]
    where = _where(table)
    if op == "count":
        if not body:
            return None
        lines = [f"- Count{where}: {len(body)} row{'s' if len(body) != 1 else ''}{reading}."]
        lines += _listed(body, [label for label in columns if label in table.labels])
        return Answer(4 + bool(columns), WORKED_HEAD, lines)
    if op in {"first", "last"}:
        if not body:
            return None
        dated = [label for label in columns if table.kinds.get(label) == "date"] or [
            label for label in table.labels if table.kinds.get(label) == "date"
        ]
        order = sorted(body, key=lambda row: _date_key(_when(row.value(dated[0])))) if dated else body
        row = order[0] if op == "first" else order[-1]
        shown = [label for label in columns] or figures
        lines = [f"- The {op} row{where}{reading}" + (f", by {dated[0]}" if dated else "") + ":"]
        lines += _listed([row], shown, full=True)
        return Answer(4 + bool(columns), WORKED_HEAD, lines)
    if not figures:
        return None
    lines: list[str] = []
    for label in figures[:3]:
        values = [(row, _number(row.value(label))) for row in body]
        values = [(row, value) for row, value in values if value is not None]
        if not values:
            continue
        samples = [row.value(label) for row, _value in values]
        if op in {"sum", "average"}:
            total = sum((value for _row, value in values), Decimal(0))
            result = total / len(values) if op == "average" else total
            what = "Average" if op == "average" else "Total"
            lines.append(f'- {what} of "{label}"{where} over {len(values)} row{"s" if len(values) != 1 else ""}{reading}: {_format(result, samples)}')
            shown = "; ".join(f"{_short(row)}: {row.value(label)}" for row, _value in values[:MAX_LISTED])
            lines.append(f"  rows: {shown}" + (f"; …{len(values) - MAX_LISTED} more" if len(values) > MAX_LISTED else ""))
            for row in totals[:2]:
                if row.value(label):
                    lines.append(f'  the table\'s own "{row.name}" row says {label}: {row.value(label)}')
        else:
            ranked = sorted(values, key=lambda item: item[1], reverse=op == "max")
            what = "Largest" if op == "max" else "Smallest"
            lines.append(f'- {what} "{label}"{where}{reading}: {_short(ranked[0][0])} — {ranked[0][0].value(label)}')
            if len(ranked) > 1:
                lines.append("  next: " + "; ".join(f"{_short(row)}: {row.value(label)}" for row, _value in ranked[1:4]))
    if not lines:
        return None
    if len(figures) > 1 and op == "sum" and len(body) and _all_ranged(figures, columns):
        grand = sum((_number(row.value(label)) or Decimal(0) for row in body for label in figures), Decimal(0))
        samples = [row.value(label) for row in body for label in figures if row.value(label)]
        lines.append(f'- All of {figures[0]} through {figures[-1]} together: {_format(grand, samples)}')
    return Answer(4 + len(figures), WORKED_HEAD, lines)


def _all_ranged(figures: list[str], columns: dict[str, set[str]]) -> bool:
    """The figures came from a range of columns ("Jul-26 through Sep-26"): some were named by no word."""
    return any(not columns.get(label) for label in figures)


def _per_group(table: Table, asked: Question, figures: list[str], rows: list[Row], op: str, reading: str) -> Answer | None:
    """A total (or average, or count) for each value of the column the question groups by ("by category")."""
    label = next((label for label in table.labels if table.kinds.get(label) == "text" and _matches(asked.group.rstrip("s"), _label_words(label))), "")
    if not label or (op != "count" and not figures):
        return None
    groups: dict[str, list[Row]] = {}
    current = ""
    for row in rows:
        if row.total:
            continue
        current = row.value(label) or current
        if current:
            groups.setdefault(current, []).append(row)
    # "Per vendor" over one row per vendor is not a grouping: the question wants the rows themselves.
    if len(groups) < 2 or all(len(members) == 1 for members in groups.values()):
        return None
    lines = []
    for name, members in groups.items():
        if op == "count":
            lines.append(f"- {name}: {len(members)} row{'s' if len(members) != 1 else ''}")
            continue
        parts = []
        for figure in figures[:2]:
            values = [_number(row.value(figure)) for row in members]
            values = [value for value in values if value is not None]
            if values:
                total = sum(values, Decimal(0))
                result = total / len(values) if op == "average" else total
                parts.append(f"{figure} {_format(result, [row.value(figure) for row in members])}")
        lines.append(f"- {name} ({len(members)} rows): " + "; ".join(parts))
    what = {"sum": "Total", "average": "Average", "count": "Count"}[op]
    return Answer(5, WORKED_HEAD, [f"{what} for each {label}{_where(table)}{reading}:"] + lines)


def _difference(
    table: Table, asked: Question, columns: dict[str, set[str]], figures: list[str], chosen: list[Row], rows: list[Row], words: list[str]
) -> Answer | None:
    """One column minus another on the same rows, or one row minus another in the same column, in the order
    the question names them."""
    lower = asked.text.lower()

    def spot(words_: set[str]) -> int:
        places = [lower.find(word[:5]) for word in words_ if not _numeric(word) and lower.find(word[:5]) >= 0]
        return min(places, default=10_000)

    if len(figures) >= 2 and 0 < len(chosen) <= MAX_ROWS:
        a, b = sorted(figures[:2], key=lambda label: spot(columns.get(label, set())))
        lines = []
        for row in chosen:
            x, y = _number(row.value(a)), _number(row.value(b))
            if x is None or y is None:
                continue
            samples = [row.value(a), row.value(b)]
            lines.append(f"- {_short(row)}: {a} {row.value(a)} − {b} {row.value(b)} = {_format(x - y, samples)}")
        if lines:
            return Answer(5, WORKED_HEAD, lines)
    figure = (figures or [_default_column(table)])[0] if (figures or _default_column(table)) else ""
    if not figure:
        return None
    scored = [(_row_score(row, words, asked.text), row) for row in rows if not row.total]
    picked = []
    for word in words:
        hits = [row for (_score, hit_words), row in scored if word in hit_words and row not in picked]
        if len(hits) == 1:
            picked.append(hits[0])
    if len(picked) != 2:
        return None
    first, second = sorted(picked, key=lambda row: spot(set(_cell_words(row.name))))
    x, y = _number(first.value(figure)), _number(second.value(figure))
    if x is None or y is None:
        return None
    samples = [first.value(figure), second.value(figure)]
    line = f"- {figure}: {_short(first)} {first.value(figure)} − {_short(second)} {second.value(figure)} = {_format(x - y, samples)}"
    return Answer(5, WORKED_HEAD, [line])


def _found(
    table: Table, chosen: list[Row], columns: dict[str, set[str]], scores: dict, figures: list[str], reading: str, budget
) -> Answer:
    shown = [label for label in columns]
    if budget:
        shown = list(dict.fromkeys([*budget, *shown, *[label for label in table.labels if "variance" in label.lower()]]))
    lines = []
    if len(chosen) > 1:
        # A count keeps a small model from adding the row next to them ("Priya has three tasks").
        lines.append(f"{len(chosen)} of the table's {len(table.rows)} rows match{reading}:")
    elif reading:
        lines.append(f"Matching{reading}:")
    lines += _listed(chosen, shown, full=len(chosen) <= MAX_ROWS)
    top = max((scores[id(row)][0] for row in chosen), default=0)
    return Answer(top + (1 if columns else 0) + (1 if reading else 0), FOUND_HEAD, lines)


def _column_view(table: Table, figures: list[str], asked: Question) -> Answer | None:
    """A named column for every row, those with a figure first, then the rows with none."""
    lines = []
    for label in figures[:2]:
        values = [(row, row.value(label)) for row in table.rows]
        filled = [(row, value) for row, value in values if (_number(value) or 0) != 0]
        empty = [row for row, value in values if (_number(value) or 0) == 0]
        shown = "; ".join(f"{_short(row)}: {_with_ref(row, label)}" for row, value in filled[:40])
        line = f'- "{label}" for every row{_where(table)}: {shown or "none"}.'
        if empty:
            names = ", ".join(_short(row) for row in empty[:12]) + (" …" if len(empty) > 12 else "")
            line += f" Zero or blank: {names}."
        lines.append(line)
    return Answer(0.5 + len(figures), FOUND_HEAD, lines) if lines else None


def _listed(rows: list[Row], shown: list[str], *, full: bool = True) -> list[str]:
    lines = []
    for row in rows[:MAX_LISTED]:
        picked = [row.cell(label) for label in shown if row.value(label)]
        where = f"{row.page} · " if row.page and full else ""
        head = f"- {where}{_short(row)}" + (f" → {' | '.join(picked)}" if picked else "")
        if full and not (picked and len(row.cells) <= len(picked) + 1) and len(rows) <= MAX_ROWS:
            head += f"\n  row: {row.line}"
        lines.append(head)
    if len(rows) > MAX_LISTED:
        lines.append(f"- …and {len(rows) - MAX_LISTED} more")
    return lines


def _reading(conditions: list[Condition], by_column: dict[str, list[str]], budget, way: str, named: list[Row], words: list[str], scores) -> str:
    """How the question limited the rows, for the model (and the person) to check: ", where Payment Date is in 2027"."""
    parts = []
    for condition in conditions:
        parts.append(f"{condition.column} {condition.phrase}")
    for label, found in by_column.items():
        parts.append(f'{label} says "{" ".join(found)}"')
    if budget:
        parts.append(f"{budget[0]} {'over' if way == '>' else 'under'} {budget[1]}")
    if named:
        hit = list(dict.fromkeys(word for row in named for word in scores[id(row)][1]))
        if hit:
            parts.append("the row names " + ", ".join(hit))
    return f", where {' and '.join(parts)}" if parts else ""


def _where(table: Table) -> str:
    return f" ({table.where})" if table.where else ""


def _short(row: Row) -> str:
    return f"{row.group} > {row.name}" if row.group else row.name


def _with_ref(row: Row, label: str) -> str:
    index = next((i for i, (name, _value) in enumerate(row.cells) if name == label), None)
    ref = row.refs[index] if index is not None and index < len(row.refs) else ""
    value = row.value(label)
    return f"{value} ({ref})" if ref else value


def _row_score(row: Row, words: list[str], question: str) -> tuple[float, list[str]]:
    """How many of the question's words name this row (its text cells, a code or number in its first cell, an
    account code, its group, a date in it), and which."""
    own: set[str] = set()
    for index, (_label, value) in enumerate(row.cells):
        if value == tables.BLANK:
            continue
        if not tables.is_value(value) or re.fullmatch(r"\d{1,6}", value) and (index == 0 or len(value) >= 3):
            own.update(_words(value))
        own.update(_dates(value))
    own.update(_words(row.group))
    score = 0.0
    hits = []
    for word in words:
        if word in own or (
            len(word) >= 4 and word.isalpha() and any(len(mine) >= 4 and mine.isalpha() and (mine.startswith(word) or word.startswith(mine)) for mine in own)
        ):
            score += 2 if _numeric(word) or "/" in word else 1
            hits.append(word)
    if row.total and score and not re.search(r"\btotal", question, re.I):
        score -= 0.5
    return score, hits


# Words, figures and dates ------------------------------------------------------------------------


def _label_words(label: str) -> list[str]:
    words = [word for word in _words(label) if word not in _STOP or word in {"over", "under"}]
    return list(dict.fromkeys(words + _dates(label)))


def _cell_words(value: str) -> set[str]:
    return set(_words(value)) | set(_dates(value))


def _words(text: str) -> list[str]:
    out = []
    for word in _TOKEN.findall((text or "").lower().replace("'s", "").replace("’s", "")):
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


def _number(value: str) -> Decimal | None:
    """The figure in a cell ("(87,550)", "$(1,250.00)", "4.1%", "1,800 (=SUM(C2:C3))"), or None for a date,
    a code like "8-5" or "V-302", or text."""
    text = (value or "").strip()
    if text in _DASHES:
        # The accounting format's zero.
        return Decimal(0)
    if not text or _when(text) is not None:
        return None
    match = _LEADING_FIGURE.match(text)
    if not match:
        return None
    rest = text[match.end():].strip()
    if rest not in {"", "%", ")", "%)"} and not rest.startswith("(="):
        return None
    try:
        number = Decimal(match.group(4).replace(",", ""))
    except InvalidOperation:
        return None
    return -number if match.group(1) or match.group(3) else number


def _format(number: Decimal, samples: list[str]) -> str:
    """``number`` written the way the column writes its figures: "$" when they have one, their decimals, "%"."""
    places = max((len(m.group(1)) for sample in samples for m in [re.search(r"\.(\d+)", sample)] if m), default=0)
    money = any("$" in sample for sample in samples)
    percent = bool(samples) and all("%" in sample for sample in samples)
    text = f"{abs(number):,.{min(places, 4)}f}"
    text = f"${text}" if money else text
    text = f"{text}%" if percent else text
    return f"-{text}" if number < 0 else text


def _when(value: str) -> When | None:
    """The date a cell holds ("01/15/2026", "Mon 09/28", "10/14", "2026-10-14", "Oct 14, 2026"), or None."""
    text = (value or "").strip()
    if not text or len(text) > 40:
        return None
    for pattern in (_ISO, _DATE, _MONTH_DAY, _DAY_MONTH):
        match = pattern.search(text)
        if not match:
            continue
        rest = (text[: match.start()] + text[match.end():]).strip()
        if rest and not _WEEKDAY.match(rest):
            return None
        if pattern is _ISO:
            year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        elif pattern is _DATE:
            month, day = int(match.group(1)), int(match.group(2))
            year = _year(match.group(3))
        elif pattern is _MONTH_DAY:
            if match.group(3):
                return None
            month, day, year = _MONTHS[match.group(1).lower()], int(match.group(2)), _year(match.group(4))
        else:
            day, month, year = int(match.group(1)), _MONTHS[match.group(2).lower()], _year(match.group(3))
        if 1 <= month <= 12 and 1 <= day <= _DAYS[month - 1]:
            return (year, month, day)
        return None
    return None


def _year(text: str | None) -> int | None:
    if not text:
        return None
    year = int(text)
    return year + 2000 if year < 100 else year


def _span(text: str) -> tuple[When, When] | None:
    """The first and last day a date in a question covers: "2027" is the year, "October" the month,
    "October 5" or "10/5" the day."""
    text = text.strip().rstrip(".,")
    if re.fullmatch(r"(?:19|20)\d{2}", text):
        year = int(text)
        return (year, 1, 1), (year, 12, 31)
    month_only = re.fullmatch(rf"({_MONTH_NAMES})\.?(?:\s+(\d{{4}}))?", text, re.I)
    if month_only:
        month, year = _MONTHS[month_only.group(1).lower()], _year(month_only.group(2))
        return (year, month, 1), (year, month, _DAYS[month - 1])
    when = _when(text)
    return (when, when) if when else None


def _before_or_on(a: When, b: When) -> bool:
    """``a`` is on or before ``b``. A date without a year is compared by month and day only."""
    if a[0] is None or b[0] is None:
        return (a[1], a[2]) <= (b[1], b[2])
    return a <= b


def _date_key(when: When | None) -> tuple:
    if when is None:
        return (9999, 12, 31)
    return (when[0] or 0, when[1], when[2])
