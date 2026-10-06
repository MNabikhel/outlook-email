"""Questions about a schedule answered by a query: the model writes it, CloseDesk runs it exactly.

``table_lookup`` reads the questions it has words for ("total", "over $30,000", "in 2027"). A question
put any other way ("everything not current", "which accrual went up the most", "Q2 for Cedar Valley")
needs the question understood, which is the model's strength, and the rows added up exactly, which is
not. So the file's tables are loaded into a database held in memory, the model is shown their columns
and asked for one SQLite SELECT, and the rows that query returns go into the prompt with the query
beside them, for the model to check against the question and answer from.

The database is built from the file's own text, read-only, and dropped after the question. A query
can only read it: anything else is refused, and one that runs too long is stopped.
"""

from __future__ import annotations

import itertools
import re
import sqlite3
import time
from dataclasses import dataclass, field
from decimal import Decimal

from controller_inbox import table_lookup
from controller_inbox.table_lookup import Table

MAX_TABLES = 6
MAX_RESULT_ROWS = 25
# Distinct names listed for a text column, so the model writes them as the sheet does.
MAX_NAMES = 30
QUERY_SECONDS = 2.0
ATTEMPTS = 3
WORKED_HEAD = "Worked out with a query over the table (check it is what was asked; the whole file follows):"

SYSTEM = """You answer questions about a spreadsheet by writing ONE SQLite query over its tables.
Write two lines:
Plan: which rows (all, or the ones the question names), what value each row gives, and how they combine (list, sum, count, average, largest...).
SQL: the query, on one line.
Rules:
- Use only the schema's tables and columns. Total and subtotal rows are left out of the tables, so add them up with SUM.
- A figure sits in a row and a column: pick the row with WHERE on the column that names the rows, and the column with SELECT. A name the schema lists as a column is never a value in WHERE.
- Filter only on rows the question picks out, never on the sheet's own subject (in an inventory sheet "inventory" is every row). Match a name with LIKE '%word%'.
- Dates are text 'YYYY-MM-DD' (or 'MM-DD' when the sheet shows no year). A blank or '-' figure is 0.
- Q1 is Jan-Mar, Q2 Apr-Jun, Q3 Jul-Sep, Q4 Oct-Dec; H1 is Jan-Jun, H2 Jul-Dec.
- A column the schema says is worked out from others ("= a + b on every row") already holds that result: use it, don't add its parts to it.
- Show the row's name column beside each figure.
- If no column holds what is asked (another period, or a figure the sheet doesn't show), write SQL: NONE. Never answer with a different column in its place.

Example schema:
CREATE TABLE t1 (  -- Inventory, 40 rows
  sku TEXT, description TEXT, category TEXT,  -- category: Raw, WIP, Finished
  qty_jun REAL, qty_sep REAL, unit_cost REAL, ext_value REAL,  -- ext_value = qty_sep * unit_cost on every row
  last_count TEXT  -- date YYYY-MM-DD
);
Question: what's in the WIP category
Plan: rows with category WIP; list each with its value.
SQL: SELECT sku, description, ext_value FROM t1 WHERE category LIKE '%wip%'
Question: which category is worth the most
Plan: all rows; ext_value; summed per category, largest first.
SQL: SELECT category, SUM(ext_value) AS value FROM t1 GROUP BY category ORDER BY value DESC LIMIT 1
Question: how many finished items weren't counted since march
Plan: rows with category Finished and last_count before 2026-03-01; count them.
SQL: SELECT COUNT(*) AS items FROM t1 WHERE category LIKE '%finished%' AND last_count < '2026-03-01'
Question: which sku dropped the most in quantity from june to sept
Plan: all rows; qty_sep - qty_jun; the smallest change.
SQL: SELECT sku, description, qty_jun, qty_sep, qty_sep - qty_jun AS change FROM t1 ORDER BY change ASC LIMIT 1
Question: how many more units did we hold in sept than june overall
Plan: all rows; qty_sep and qty_jun; the difference of their totals.
SQL: SELECT SUM(qty_sep) - SUM(qty_jun) AS more_units FROM t1
Question: unit cost of the blue widget vs the red one
Plan: the two named rows; unit_cost; their difference.
SQL: SELECT (SELECT unit_cost FROM t1 WHERE description LIKE '%blue widget%') - (SELECT unit_cost FROM t1 WHERE description LIKE '%red widget%') AS difference
Question: what was the inventory worth at the end of last year
Plan: no column holds a value at last year end (only jun and sep quantities); the tables cannot answer it.
SQL: NONE
Question: what's our inventory turnover
Plan: turnover needs cost of goods sold, which no column holds; the tables cannot answer it.
SQL: NONE"""

# SQLite's words, which a column can't be called without quotes the model would leave off.
_KEYWORDS = frozenset(
    """abort action add after all alter always analyze and as asc attach autoincrement before begin between by
    cascade case cast check collate column commit conflict constraint create cross current current_date current_time
    current_timestamp database default deferrable deferred delete desc detach distinct do drop each else end escape
    except exclude exclusive exists explain fail filter first following for foreign from full generated glob group
    groups having if ignore immediate in index indexed initially inner insert instead intersect into is isnull join
    key last left like limit match materialized natural no not nothing notnull null nulls of offset on or order
    others outer over partition plan pragma preceding primary query raise range recursive references regexp reindex
    release rename replace restrict returning right rollback row rows savepoint select set table temp temporary then
    ties to transaction trigger unbounded union unique update using vacuum values view virtual when where window
    with without""".split()
)
_ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, getattr(sqlite3, "SQLITE_RECURSIVE", 33)}
_RESULT_WORDS = re.compile(r"\b(?:total|net|ending|end|closing|variance|change|difference|balance)\b", re.I)
_CANNOT = re.compile(
    r"\bnot\s+(?:in|on|shown\s+in|listed\s+in|available\s+in|part\s+of)\s+the\s+(?:tables?|sheets?|schema|data)\b|"
    r"\b(?:tables?|sheets?|schema)\s+(?:does|do)\s*n[o']?t\s+(?:have|show|hold|include|contain|list)\b|"
    r"\bno\s+columns?\s+(?:for|holds?|has|have|shows?|gives?)\b|"
    r"\b(?:cannot|can't|can\s+not)\s+(?:be\s+)?answer",
    re.I,
)
_LITERAL = re.compile(r"'((?:[^']|'')*)'")
_QUOTED = re.compile(r'"([^"]+)"')
_ALIAS = re.compile(r'(?i)\bAS\s+"([^"]+)"')
_SQL_LINE = re.compile(r"(?is)\bSQL:\s*(.*)")
_PLAN_LINE = re.compile(r"(?i)\bPlan:\s*(.*)")
_FENCE = re.compile(r"```(?:sql)?\s*(.*?)```", re.S)


@dataclass
class Column:
    label: str  # as the sheet heads it
    name: str  # as the query calls it
    kind: str  # "figure", "date" or "text"
    samples: list[str] = field(default_factory=list)  # the cells as the sheet writes them
    formula: str = ""


@dataclass
class Sheet:
    name: str
    title: str
    note: str
    columns: list[Column]
    rows: int


@dataclass
class Result:
    plan: str
    sql: str
    names: list[str]
    rows: list[tuple]
    more: bool = False


class Tables:
    """A file's tables as a database the model's query runs on."""

    def __init__(self, files: list[tuple[str, str]]):
        """``files``: (file name, extracted text) for each file whose tables the question may be about."""
        self.sheets: list[Sheet] = []
        self._labels: dict[str, Column] = {}
        self._names: set[str] = set()
        self._db = sqlite3.connect(":memory:", check_same_thread=False)
        for source, text in files:
            lines = (text or "").splitlines()
            for table in table_lookup.tables_in(text):
                if len(table.body) >= 2 and len(self.sheets) < MAX_TABLES:
                    self._load(f"t{len(self.sheets) + 1}", table, lines, source)
        self._db.execute("PRAGMA query_only = ON")
        self._db.set_authorizer(_authorize)

    def __bool__(self) -> bool:
        return bool(self.sheets)

    def about(self, question: str) -> bool:
        """Whether the question is for these tables: it asks for something worked out or limited (a total, a
        count, "over $30k", "in 2027"), or names a heading, a row or a sheet. "Where is the offsite?" is not."""
        asked = table_lookup.read_question(question)
        if asked.op != "find" or asked.conditions or asked.group or asked.budget:
            return True
        known: set[str] = set()
        for sheet in self.sheets:
            known.update(table_lookup._words(sheet.title))
            for column in sheet.columns:
                known.update(table_lookup._label_words(column.label))
                if column.kind == "text":
                    for sample in column.samples:
                        known.update(table_lookup._cell_words(sample))
        listed = list(known)
        return any(table_lookup._matches(word, listed) for word in asked.words if len(word) >= 3 or table_lookup._numeric(word))

    def close(self) -> None:
        self._db.close()

    def _load(self, name: str, table: Table, lines: list[str], source: str) -> None:
        taken: set[str] = set()
        section = any(row.group for row in table.body)
        columns = [Column("Section", _ident("section", taken), "text")] if section else []
        columns += [Column(label, _ident(label, taken), table.kinds.get(label, "text")) for label in table.labels]
        values: list[list] = []
        for row in table.body:
            cells = [row.group or None] if section else []
            for column in columns[1 if section else 0 :]:
                raw = row.value(column.label)
                if raw:
                    column.samples.append(raw)
                cells.append(_stored(raw, column.kind))
            values.append(cells)
        if section:
            columns[0].samples = [row.group for row in table.body if row.group]
        _formulas(columns, values)
        self._db.execute(
            f"CREATE TABLE {name} ({', '.join(f'{c.name} {_sql_type(c.kind)}' for c in columns)})"
        )
        self._db.executemany(f"INSERT INTO {name} VALUES ({', '.join('?' * len(columns))})", values)
        title = " / ".join(part for part in (source, _heading_above(lines, table.rows[0].at)) if part)
        self.sheets.append(Sheet(name, title, _notes(lines), columns, len(values)))
        for column in columns:
            self._labels.setdefault(column.name, column)
        self._names |= {name, *(column.name for column in columns)}

    def schema(self) -> str:
        """The tables as the model sees them: each column's name, what it holds, and the names it lists."""
        out = []
        for sheet in self.sheets:
            head = f"CREATE TABLE {sheet.name} (  -- {sheet.title + ': ' if sheet.title else ''}{sheet.rows} rows, total rows left out"
            lines = [head]
            for column in sheet.columns:
                lines.append(f"  {column.name} {_sql_type(column.kind)},  -- {_describe(column)}")
            lines.append(");" + (f"  -- note: {sheet.note}" if sheet.note else ""))
            out.append("\n".join(lines))
        return "\n".join(out)

    def run(self, sql: str) -> tuple[list[str], list[tuple], bool]:
        """Run one SELECT: (column names, rows, whether there were more). Raises ``ValueError`` or
        ``sqlite3.Error`` with a message the model can act on."""
        sql = (sql or "").strip().rstrip(";").strip()
        if not re.match(r"(?is)^(select|with)\b", sql):
            raise ValueError("write one SELECT statement")
        # SQLite reads "Adjusted bank balance" as a string when no column has that name, so a query that names
        # a row as if it were a column runs and finds nothing. It is an error here, so the model hears why.
        bare = _LITERAL.sub("''", sql)
        aliases = {alias.lower() for alias in _ALIAS.findall(bare)}
        for quoted in _QUOTED.findall(bare):
            if quoted.lower() not in self._names | aliases:
                raise ValueError(f'no such column: "{quoted}"')
        started = time.monotonic()
        self._db.set_progress_handler(lambda: int(time.monotonic() - started > QUERY_SECONDS), 10_000)
        try:
            cursor = self._db.execute(sql)
            names = [item[0] for item in cursor.description or []]
            rows = cursor.fetchmany(MAX_RESULT_ROWS + 1)
        finally:
            self._db.set_progress_handler(None, 0)
        return names, rows[:MAX_RESULT_ROWS], len(rows) > MAX_RESULT_ROWS

    def render(self, found: Result) -> str:
        """The query and what it returned, figures written the way the sheet writes them."""
        lines = [WORKED_HEAD]
        if found.plan:
            lines.append(f"Plan: {found.plan}")
        lines.append(f"Query: {found.sql}")
        if not found.rows:
            lines.append("It returned no rows.")
            return "\n".join(lines)
        lines.append(f"Result ({len(found.rows)} row{'s' if len(found.rows) != 1 else ''}{', more not shown' if found.more else ''}):")
        for row in found.rows:
            cells = [f"{self._heading(name)}: {self._shown(name, value)}" for name, value in zip(found.names, row)]
            lines.append(" | ".join(cells))
        return "\n".join(lines)

    def _heading(self, name: str) -> str:
        column = self._labels.get(name.lower())
        return column.label if column else name

    def _shown(self, name: str, value) -> str:
        if value is None:
            return "blank"
        if isinstance(value, float):
            number = Decimal(repr(value)).quantize(Decimal("0.0001")).normalize()
            column = self._labels.get(name.lower())
            if column and column.kind == "figure":
                return table_lookup._format(number, column.samples)
            # A figure the query worked out (a sum, an average): the sheet's decimals, two when it isn't whole.
            samples = self._figure_samples() + ([] if number == number.to_integral_value() else ["0.00"])
            return table_lookup._format(number, samples)
        return str(value)

    def _figure_samples(self) -> list[str]:
        """How the sheet writes its amounts, for a result the query worked out: their decimals, without a
        currency sign that might not apply, and not a percent column's."""
        samples = [s for sheet in self.sheets for c in sheet.columns if c.kind == "figure" for s in c.samples[:5]]
        return [s.replace("$", "") for s in samples if "%" not in s]

    def hints(self, sql: str, error: str = "") -> list[str]:
        """What a query that failed or found nothing got wrong, in words the model can act on: a column
        named as if it were a row ("payroll" in WHERE line LIKE ...), a row named as if it were a column
        ("Adjusted bank balance"), or a filter on what every row of the table is (its title)."""
        found: list[str] = []
        used = set(re.findall(r"\b(t\d+)\b", _LITERAL.sub("''", sql)))
        sheets = [sheet for sheet in self.sheets if sheet.name in used] or self.sheets
        for literal in _LITERAL.findall(sql):
            text = literal.replace("''", "'").strip("% ").lower()
            words = set(table_lookup._words(text))
            if len(text) < 3 or not words:
                continue
            for sheet in sheets:
                in_rows = any(text in sample.lower() for c in sheet.columns if c.kind == "text" for sample in c.samples)
                if in_rows:
                    continue
                column = next((c for c in sheet.columns if c.kind != "text" and words <= set(table_lookup._words(c.label)) | {c.name}), None)
                if column is not None:
                    found.append(f"'{text}' is the column {column.name} of {sheet.name}, not a name in a row: use {column.name} itself.")
                    break
                if words <= set(table_lookup._words(sheet.title)):
                    found.append(f"Every row of {sheet.name} is '{text}' (its title says so): don't filter on it.")
                    break
        missing = re.search(r'no such column: "?([^"]+)"?', error or "")
        if missing:
            name = missing.group(1).split(".")[-1].strip().lower()
            for sheet in sheets:
                column = next((c for c in sheet.columns if c.kind == "text" and any(name in sample.lower() for sample in c.samples)), None)
                if column is not None:
                    found.append(f"'{missing.group(1)}' is a row of {sheet.name}, named in {column.name}: use {column.name} LIKE '%{name}%'.")
                    break
        return found


def ask(settings, tables: Tables, question: str, *, complete=None) -> Result | None:
    """The model's query for the question, run on ``tables``; None when it says the tables can't answer it
    or no query it writes will run. A query that fails or finds nothing is tried again with what was wrong. ``complete``:
    the model call (``local_llm.complete_text``)."""
    if complete is None:
        from controller_inbox.local_llm import complete_text as complete

    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"Schema:\n{tables.schema()}\n\nQuestion: {question}"},
    ]
    for attempt in range(ATTEMPTS):
        reply = complete(settings, messages, max_tokens=400)
        plan, sql = parse(reply)
        if not sql or _CANNOT.search(plan):
            # "Sum q3 actual (since Q2 is not in the table)" answers another question under this one's name.
            return None
        error = ""
        try:
            names, rows, more = tables.run(sql)
        except (ValueError, sqlite3.Error) as exc:
            error = str(exc)[:200]
            problem = f"That query failed: {error}."
        else:
            if rows and any(value is not None for value in rows[0]):
                return Result(plan, sql, names, rows, more)
            # Nothing found, after another try, is a filter the sheet doesn't have, not an answer.
            problem = "That query found nothing. Check each filter against the names the schema lists."
        if attempt == ATTEMPTS - 1:
            return None
        hints = " ".join(tables.hints(sql, error))
        messages += [
            {"role": "assistant", "content": reply},
            {"role": "user", "content": f"{problem} {hints} Write the Plan and SQL lines again.".replace("  ", " ")},
        ]
    return None


def parse(reply: str) -> tuple[str, str]:
    """(plan, sql) from the model's reply; sql is "" when it wrote NONE or no query."""
    text = reply or ""
    fenced = _FENCE.search(text)
    plan = _PLAN_LINE.search(text)
    sql_line = _SQL_LINE.search(text)
    sql = fenced.group(1) if fenced else sql_line.group(1) if sql_line else text
    # Anything the model wrote after the query, past a blank line, is not part of it.
    sql = sql.strip().split("\n\n")[0].strip("`").strip().rstrip(";").strip()
    if not re.match(r"(?is)^(select|with)\b", sql):
        sql = ""
    return (plan.group(1).strip() if plan else ""), sql


# Building the tables ----------------------------------------------------------------------------


def _authorize(action: int, _arg1, _arg2, _db, _trigger) -> int:
    if action in _ALLOWED and not (action == sqlite3.SQLITE_FUNCTION and str(_arg2).lower() == "load_extension"):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _ident(label: str, taken: set[str]) -> str:
    """A column name the model can write bare: "31 - 60 Days" is c_31_60_days, "Check #" check_no."""
    name = re.sub(r"[^a-z0-9]+", "_", label.lower().replace("#", " no ").replace("%", " pct ")).strip("_") or "col"
    if name[0].isdigit():
        name = f"c_{name}"
    if name in _KEYWORDS:
        name = f"{name}_col"
    base, count = name, 2
    while name in taken:
        name, count = f"{base}_{count}", count + 1
    taken.add(name)
    return name


def _sql_type(kind: str) -> str:
    return "REAL" if kind == "figure" else "TEXT COLLATE NOCASE"


def _stored(raw: str, kind: str):
    if kind == "figure":
        number = table_lookup._number(raw)
        # A blank cell in a column of figures is nothing there: 0.
        return float(number) if number is not None else 0.0 if not raw else None
    if kind == "date":
        when = table_lookup._when(raw)
        if when:
            year, month, day = when
            return f"{year:04d}-{month:02d}-{day:02d}" if year else f"{month:02d}-{day:02d}"
    return raw or None


def _describe(column: Column) -> str:
    if column.kind == "figure":
        note = f'"{column.label}" figure'
        return f"{note} = {column.formula} on every row" if column.formula else note
    if column.kind == "date":
        stored = next((_stored(s, "date") for s in column.samples if table_lookup._when(s)), "")
        shape = "YYYY-MM-DD" if len(stored or "") == 10 else "MM-DD"
        return f'"{column.label}" date as text {shape}' + (f", e.g. {stored}" if stored else "")
    names = list(dict.fromkeys(column.samples))
    shown = ", ".join(name[:48] for name in names[:MAX_NAMES]) + (", …" if len(names) > MAX_NAMES else "")
    return f'"{column.label}": {shown}'


def _formulas(columns: list[Column], values: list[list]) -> None:
    """Note each figure column every row works out from the columns beside it ("ending = beginning +
    additions - payments - reversals", "total = current + 1-30 + ..."), so the model uses it rather than
    adding its parts to it again. Of an identity read both ways ("total = current + over 90", "over 90 =
    total - current"), the result is the column headed like one (a total, net, ending or variance), else
    the one further right."""
    figures = [index for index, column in enumerate(columns) if column.kind == "figure"]
    found: dict[int, list[tuple[int, int]]] = {}
    for target in figures:
        position = figures.index(target)
        runs = []
        for width in range(2, min(13, len(figures))):
            if position - width >= 0:
                runs.append(figures[position - width : position])
            if position + 1 + width <= len(figures):
                runs.append(figures[position + 1 : position + 1 + width])
        for run in runs:
            signs = [(1,) * len(run)] if len(run) > 5 else [(1, *rest) for rest in itertools.product((1, -1), repeat=len(run) - 1)]
            terms = next((list(zip(run, pattern)) for pattern in signs if _holds(values, target, list(zip(run, pattern)))), None)
            if terms:
                found[target] = terms
                break
    def kept(index: int) -> tuple[bool, int]:
        return bool(_RESULT_WORDS.search(columns[index].label)), index

    for target, terms in list(found.items()):
        for other, _sign in terms:
            if other in found and target in found and any(index == target for index, _s in found[other]):
                found.pop(min(target, other, key=kept))
    for target, terms in found.items():
        columns[target].formula = " ".join(("+ " if sign > 0 else "- ") + columns[index].name for index, sign in terms).removeprefix("+ ")


def _holds(values: list[list], target: int, terms: list[tuple[int, int]]) -> bool:
    used = good = 0
    copies = [0] * len(terms)
    for row in values:
        parts = [row[index] for index, _sign in terms]
        if row[target] is None or any(part is None for part in parts):
            continue
        used += 1
        good += abs(sum(sign * part for (_index, sign), part in zip(terms, parts)) - row[target]) <= 0.015 * len(terms)
        for position, part in enumerate(parts):
            copies[position] += abs(part - row[target]) < 0.005
    nonzero = sum(1 for row in values if row[target])
    # Equal months ("Oct = Jul - Aug + Sep" when every month is the same) only look like a formula.
    return used >= 3 and nonzero >= 2 and good >= 0.9 * used and max(copies) < 0.8 * used


def _heading_above(lines: list[str], at: int) -> str:
    """The heading lines nearest above a table ("Accrued Liabilities Rollforward / Quarter Ended ...")."""
    for index in range(min(at, len(lines)) - 1, -1, -1):
        if lines[index].strip() != "[heading]":
            continue
        heads = []
        for line in lines[index + 1 :]:
            if not line.strip() or line.startswith("["):
                break
            heads.append(line.strip())
        return " / ".join(heads[:3])[:160]
    return ""


def _notes(lines: list[str]) -> str:
    """The notes printed in the file ("Read across: each cell is owed TO the row entity BY the column entity"),
    which explain its tables wherever they sit: a note under the last table often reads the one above."""
    notes: list[str] = []
    inside = False
    for line in lines:
        text = line.strip()
        if text.startswith("["):
            inside = text == "[notes]"
            continue
        if inside and text and text not in notes:
            notes.append(text)
    return " ".join(notes)[:300]
