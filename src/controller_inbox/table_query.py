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
import math
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from controller_inbox import table_lookup, tables
from controller_inbox.table_lookup import Table

MAX_TABLES = 6
# Tables of one row (an aging's buckets, an invoice's header box) loaded beside those, at most.
MAX_ONE_ROW_TABLES = 4
# Words that can follow a table's name in FROM or JOIN without being an alias for it.
_SQL_WORDS = {"where", "group", "order", "limit", "join", "left", "right", "inner", "outer", "cross", "on", "using", "union", "having", "natural"}
MAX_RESULT_ROWS = 25
# Distinct names listed for a text column, so the model writes them as the sheet does.
MAX_NAMES = 30
# The schema shown to the model, in characters: fewer names listed, then fewer tables, past this.
SCHEMA_CHARS = 7000
QUERY_SECONDS = 2.0
# The longest text a query may build, so a query can't fill memory before the time limit stops it.
MAX_VALUE_BYTES = 100_000
# Rows checked for the columns a row works out from others.
FORMULA_ROWS = 200
# Goes at a query: one more when the first fails or finds nothing.
ATTEMPTS = 2
WORKED_HEAD = "Worked out with a query over the table (check it is what was asked; the file is above):"

SYSTEM = """You answer questions about a spreadsheet by writing ONE SQLite query over its tables.
Write four lines:
Table: which table holds the answer, and why (one named in the schema).
Rows: which rows the question picks (all, or the ones it names), and the column that names them.
Value: which column or expression gives the answer, and what its heading means.
SQL: the query, on one line.
Rules:
- Use only the schema's tables and columns. Total and subtotal rows are left out of the tables, so add them up with SUM.
- A figure sits in a row and a column: pick the row with WHERE on the column that names the rows, and the column with SELECT. A name the schema lists as a column is never a value in WHERE.
- A table ending _cells holds the same values as its table, one per row, with the heading each was under: use it to count, filter or add across those columns ("which entities owe X", "how many days is X on PTO").
- Read the headings: on a sheet of who owes whom, the row and column headings say which side owes.
- facts holds details printed beside the tables (who prepared it, the pay date, the period).
- Filter only on rows the question picks out, never on the sheet's own subject (in an inventory sheet "inventory" is every row). Match a name with LIKE '%word%'.
- Dates are text 'YYYY-MM-DD' (or 'MM-DD' when the sheet shows no year), months 'YYYY-MM'. A '-' figure is 0; a blank one is NULL.
- Q1 is Jan-Mar, Q2 Apr-Jun, Q3 Jul-Sep, Q4 Oct-Dec; H1 is Jan-Jun, H2 Jul-Dec.
- A column the schema says is worked out from others ("= a + b on every row") already holds that result: use it, don't add its parts to it.
- Show the row's name column beside each figure.
- If no column holds what is asked (another period, or a figure the sheet doesn't show), write SQL: NONE. Never answer with a different column in its place.

Example schema:
CREATE TABLE t1 (  -- Stock.xlsx / Stock by Warehouse: 40 rows, total rows left out
  sku TEXT,  -- "SKU": A-100, A-101, B-200, ...
  category TEXT,  -- "Category": Raw, WIP, Finished
  east REAL,  -- "East" figure, under "Warehouse"
  west REAL,  -- "West" figure, under "Warehouse"
  total_units REAL,  -- "Total Units" figure = east + west on every row
  unit_cost REAL,  -- "Unit Cost" figure
  last_count TEXT  -- "Last Count" date as text YYYY-MM-DD
);
CREATE TABLE t1_cells (  -- t1 again, one row per figure under "Warehouse" (east, west)
  sku TEXT,  -- as in t1
  category TEXT,  -- as in t1
  warehouse TEXT,  -- the heading the figure is under: East, West
  amount REAL
);
CREATE TABLE facts (file TEXT, name TEXT, value TEXT);  -- details printed beside the tables: Counted by: J. Ruiz; As of: 6/30/2026
Question: how many units of a-101 are in the west warehouse
Table: t1, one row per SKU.
Rows: the row whose sku is A-101.
Value: west, the West column under "Warehouse".
SQL: SELECT sku, west FROM t1 WHERE sku LIKE '%a-101%'
Question: which category holds the most units
Table: t1.
Rows: all, grouped by category.
Value: total_units, already the sum of the warehouses; summed per category, largest first.
SQL: SELECT category, SUM(total_units) AS units FROM t1 GROUP BY category ORDER BY units DESC LIMIT 1
Question: in how many warehouses is b-200 out of stock
Table: t1_cells, to count across the warehouse columns.
Rows: sku B-200 where the amount is 0.
Value: the number of such rows.
SQL: SELECT COUNT(*) AS warehouses FROM t1_cells WHERE sku LIKE '%b-200%' AND amount = 0
Question: whats in WIP
Table: t1.
Rows: category WIP; each one listed.
Value: total_units beside each sku.
SQL: SELECT sku, total_units FROM t1 WHERE category LIKE '%wip%'
Question: how many items weren't counted since march
Table: t1.
Rows: last_count before 2026-03-01.
Value: the number of such rows.
SQL: SELECT COUNT(*) AS items FROM t1 WHERE last_count < '2026-03-01'
Question: how many more units in east than west overall
Table: t1.
Rows: all.
Value: the total of east less the total of west.
SQL: SELECT SUM(east) - SUM(west) AS more_units FROM t1
Question: who counted the stock
Table: facts.
Rows: the fact named Counted by.
Value: its value.
SQL: SELECT value FROM facts WHERE name LIKE '%counted by%'
Question: what was the inventory worth at the end of last year
Table: none: no column holds a value at last year end; the tables cannot answer it.
Rows: none.
Value: none.
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
# The functions a question over a schedule needs: sums and counts, rounding, text and dates. Any other (one that
# reaches outside the database, builds large values, or exposes the engine's internals) is refused.
_FUNCTIONS = frozenset(
    """abs avg ceil ceiling coalesce count date datetime dense_rank first_value floor glob group_concat ifnull iif
    instr julianday lag last_value lead length like lower ltrim max min nullif printf format rank replace round
    row_number rtrim sign strftime substr substring sum time total trim trunc typeof upper""".split()
)
# The longest text kept in one cell: anything longer is not a cell a question is about.
MAX_CELL_CHARS = 10_000
_RESULT_WORDS = re.compile(r"\b(?:total|net|ending|end|closing|variance|change|difference|balance)\b", re.I)
_CANNOT = re.compile(
    r"\bnot\s+(?:in|on|shown\s+in|listed\s+in|available\s+in|part\s+of)\s+the\s+(?:tables?|sheets?|schema|data)\b|"
    r"\b(?:tables?|sheets?|schema)\s+(?:does|do)\s*n[o']?t\s+(?:have|show|hold|include|contain|list)\b|"
    r"\bno\s+columns?\s+(?:for|holds?|has|have|shows?|gives?)\b|"
    r"\b(?:cannot|can't|can\s+not)\s+(?:be\s+)?answer",
    re.I,
)
# "The sheet doesn't have a Q4 column, so Q4 is Oct-26 + Nov-26 + Dec-26": what is asked, worked out from columns it has.
_WORKED_FROM = re.compile(r"^\s*(?:so|but|instead)\b.*(?:\+|\bsum\s+of\b|\badd(?:ing|ed)?\b)", re.I)
# "Totals are not in the table, so SUM them" is about the left-out total rows, not the question.
_TOTAL_ROWS = re.compile(r"\b(?:sub)?total(?:s|\s+rows?)\b", re.I)
_LITERAL = re.compile(r"'((?:[^']|'')*)'")
_CROSS = re.compile(r"(?i)\bFROM\s+(?:\([^()]*\)|\w+)(?:\s+(?:AS\s+)?\w+)?\s*,")
_JOIN = re.compile(r"(?i)\bJOIN\s+(?:\([^()]*\)|\w+)((?:\s+\w+){0,4})")
_QUOTED = re.compile(r'"([^"]+)"')
_ALIAS = re.compile(r'(?i)\bAS\s+"([^"]+)"')
# "SQL:", "**SQL**:" or "**SQL:**".
_SQL_LINE = re.compile(r"(?is)\bSQL\**:\**\s*(.*)")
# The reasoning lines before the query (and "Plan:", which the model sometimes writes instead).
_REASONING = re.compile(r"(?im)^\s*\**(Plan|Table|Rows|Value)\**:\**\s*(.+?)\s*$")
# A fence tagged any way ("```sql", "```SQL", "```sqlite") or not at all, or on one line ("```sql SELECT …```").
_FENCE = re.compile(r"```(?:(?!(?i:select|with)\b)[\w+-]*[ \t]*\n|(?i:sql(?:ite)?|postgres(?:ql)?|mysql|duckdb)[ \t]+)?\s*(.*?)```", re.S)


@dataclass
class Column:
    label: str  # as the sheet heads it
    name: str  # as the query calls it
    kind: str  # "figure", "date" or "text"
    samples: list[str] = field(default_factory=list)  # the cells as the sheet writes them
    formula: str = ""
    parts: list[tuple[int, int]] = field(default_factory=list)  # (column index, sign) of the formula's terms
    written: dict[str, str] = field(default_factory=dict)  # a value as stored -> as the sheet writes it
    under: str = ""  # the heading merged over this column and its neighbours ("Due From (payable entity)")
    most: bool = False  # its formula holds on most rows, not every one (a footer row or two works otherwise)
    same_as: str = ""  # in a _cells table: the table whose column this repeats
    running: str = ""  # a running balance: what each row adds to the row above's ("+ charges - payments")


@dataclass
class Sheet:
    name: str
    title: str
    note: str
    columns: list[Column]
    rows: int
    of: str = ""  # a _cells table: the table it lays out one value per row
    across: str = ""  # its columns that were laid out (the first ... the last)
    caution: str = ""  # its printed totals that don't match its rows as read


@dataclass
class Result:
    plan: str
    sql: str
    names: list[str]
    rows: list[tuple]
    more: bool = False


class Tables:
    """A file's tables as a database the model's query runs on."""

    def __init__(self, files: list[tuple[str, str]], question: str = ""):
        """``files``: (file name, extracted text) for each file whose tables the question may be about. ``question``:
        when given, a table of one row is loaded only when the question names one of its columns ("past due", "over
        60 days", "terms"). Beside a statement's lines, its aging box led a small model to add up every payment on
        the statement for "how much did we pay them in September?"."""
        self.sheets: list[Sheet] = []
        # Each column name's column, or None when two tables use the name for different columns.
        self._labels: dict[str, Column | None] = {}
        self._names: set[str] = set()
        self._db = sqlite3.connect(":memory:", check_same_thread=False)
        self._db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
        # SQLite's LIKE ignores case for A-Z only: "Müller" would not match '%müller%'.
        # LIKE runs in Python, where SQLite's progress handler can't stop it: it watches the query's deadline itself.
        self._deadline = 0.0
        self._db.create_function("like", 2, lambda pattern, value: _like(pattern, value, deadline=self._deadline), deterministic=True)
        self._db.create_function("like", 3, lambda pattern, value, escape: _like(pattern, value, escape, deadline=self._deadline), deterministic=True)
        self.facts: list[tuple[str, str, str]] = []
        count = small = 0
        read = [(source, (text or "").splitlines(), table_lookup.tables_in(text)) for source, text in files]
        loaded: set[str] = set()
        for source, lines, found in read:
            for table in found:
                if len(table.body) >= 2 and count < MAX_TABLES:
                    count += 1
                    self._load(f"t{count}", table, lines, source)
                    loaded.add(source)
        # A table of one row is a table too (an aging's buckets, an invoice's box of number, dates and terms), in a
        # few places of its own, so it never takes the place of a table of rows.
        asked = [word for word in table_lookup.read_question(question).words if len(word) >= 3 or table_lookup._numeric(word)]
        for source, lines, found in read:
            for table in found:
                if len(table.body) == 1 and small < MAX_ONE_ROW_TABLES and (not question or _names_a_column(table, asked)):
                    small += 1
                    self._load(f"t{count + small}", table, lines, source)
                    loaded.add(source)
        for source, lines, found in read:
            if source in loaded:
                rows = {row.at for table in found if len(table.rows) >= 2 for row in table.rows}
                self.facts += [(source, name, value) for name, value in _facts_in(lines, rows)]
        if self.facts:
            self._db.execute("CREATE TABLE facts (file TEXT COLLATE NOCASE, name TEXT COLLATE NOCASE, value TEXT COLLATE NOCASE)")
            self._db.executemany("INSERT INTO facts VALUES (?, ?, ?)", self.facts)
            self._names |= {"facts", "file", "name", "value"}
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
        for _source, name, _value in self.facts:
            known.update(table_lookup._label_words(name))
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
            # By position: a sheet can head two columns alike ("Date | Amount | Date | Amount").
            for column, (_label, raw) in zip(columns[1 if section else 0 :], row.cells):
                raw = "" if raw == tables.BLANK else raw
                if raw:
                    column.samples.append(raw)
                stored = _stored(raw, column.kind)
                if column.kind == "date" and isinstance(stored, str):
                    column.written.setdefault(stored, raw)
                cells.append(stored)
            values.append(cells)
        if section:
            columns[0].samples = [row.group for row in table.body if row.group]
        _formulas(columns, values)
        _running_balances(columns, values)
        self._db.execute(
            f"CREATE TABLE {name} ({', '.join(f'{c.name} {_sql_type(c.kind)}' for c in columns)})"
        )
        self._db.executemany(f"INSERT INTO {name} VALUES ({', '.join('?' * len(columns))})", values)
        # A workbook's sheets are told apart by name ("Aug 2026", "Sep 2026"), a PDF's tables by their headings.
        sheet = table.where if table.where.startswith("sheet") else ""
        headings = _headings_above(lines, table.rows[0].at)
        title = " / ".join(part for part in (source, sheet, " / ".join(headings[:3])) if part)
        verdict = table_lookup.verify(table)
        caution = f"{len(verdict.mismatched)} of its {verdict.checked} printed totals don't match its rows as read" if verdict.mismatched else ""
        self._register(Sheet(name, title, _notes(lines), columns, len(values), caution=caution))
        family = _family(columns)
        if family:
            self._load_cells(self.sheets[-1], family, values, _over(headings, columns, family))

    def _load_cells(self, sheet: Sheet, family: list[int], values: list[list], over: str) -> None:
        """The same values one per row, with the heading each was under: a sheet that lays one quantity across
        its columns (entities owed, months, accounts, each person's shift) asked "which", "how many" or "how
        much in all" across them, as a list the query filters and counts like any other."""
        columns = sheet.columns
        months = [_month(columns[index].label) for index in family]
        if not all(months) or len(set(months)) < len(months):
            # By month only when each column is its own month: "Budget Jul-26 | Actual Jul-26" are two headings
            # (one month would add them together), and beside "Adjustments" the schema lists them as headed.
            months = [""] * len(family)
        keys = [index for index, column in enumerate(columns) if index not in family and column.kind != "figure"]
        if not keys:
            # Nothing would tell its rows apart.
            return
        taken = {columns[index].name for index in keys}
        figures = columns[family[0]].kind == "figure"
        by_month = all(months)
        axis = Column(over or ("Month" if by_month else "heading"), _ident(over or ("month" if by_month else "heading"), taken), "text")
        axis.samples = [columns[index].label for index in family]
        if by_month:
            axis.written = {month: columns[index].label for month, index in zip(months, family)}
        value = Column("Amount" if figures else "Value", _ident("amount" if figures else "value", taken), "figure" if figures else "text")
        value.samples = [sample for index in family for sample in columns[index].samples]
        cells_columns = [
            Column(columns[index].label, columns[index].name, columns[index].kind, columns[index].samples, written=columns[index].written, same_as=sheet.name)
            for index in keys
        ] + [axis, value]
        rows = [
            [row[index] for index in keys] + [month or columns[index].label, row[index]]
            for row in values
            for index, month in zip(family, months)
        ]
        name = f"{sheet.name}_cells"
        self._db.execute(f"CREATE TABLE {name} ({', '.join(f'{c.name} {_sql_type(c.kind)}' for c in cells_columns)})")
        self._db.executemany(f"INSERT INTO {name} VALUES ({', '.join('?' * len(cells_columns))})", rows)
        across = f"{columns[family[0]].name} ... {columns[family[-1]].name}"
        if over:
            for index in family:
                columns[index].under = over
        self._register(Sheet(name, sheet.title, "", cells_columns, len(rows), of=sheet.name, across=across))

    def _register(self, sheet: Sheet) -> None:
        self.sheets.append(sheet)
        for column in sheet.columns:
            known = self._labels.get(column.name, column)
            same = known is not None and (known.label, _style(known)) == (column.label, _style(column))
            self._labels[column.name] = known if same else None
        self._names |= {sheet.name, *(column.name for column in sheet.columns)}

    def schema(self) -> str:
        """The tables as the model sees them: each column's name, what it holds, and the names it lists. A
        long one lists fewer names, then leaves out the last tables, to stay within ``SCHEMA_CHARS``."""
        facts = self._facts_line()
        room = SCHEMA_CHARS - len(facts)
        for names in (MAX_NAMES, 12, 4):
            text = "\n".join(self._create(sheet, names) for sheet in self.sheets)
            if len(text) <= room:
                return "\n".join(part for part in (text, facts) if part)
        kept: list[str] = []
        for sheet in self.sheets:
            create = self._create(sheet, 4)
            if kept and len("\n".join(kept)) + len(create) > room:
                break
            kept.append(create)
        return "\n".join(kept + ([facts] if facts else []))

    def _facts_line(self) -> str:
        if not self.facts:
            return ""
        listed = "; ".join(f"{name}: {value}" for _source, name, value in self.facts)
        return f"CREATE TABLE facts (file TEXT, name TEXT, value TEXT);  -- details printed beside the tables: {listed[:500]}"

    def _create(self, sheet: Sheet, names: int) -> str:
        if sheet.of:
            under = f'under "{sheet.columns[-2].label}" ' if sheet.columns[-2].label not in ("heading", "Month") else ""
            head = f"CREATE TABLE {sheet.name} (  -- {sheet.of} again, one row per value {under}(its columns {sheet.across})"
        else:
            head = f"CREATE TABLE {sheet.name} (  -- {sheet.title + ': ' if sheet.title else ''}{sheet.rows} rows, total rows left out"
        lines = [head]
        for index, column in enumerate(sheet.columns):
            comma = "," if index < len(sheet.columns) - 1 else ""
            lines.append(f"  {column.name} {_sql_type(column.kind)}{comma}  -- {_describe(column, names)}")
        notes = [f"caution: {sheet.caution}, so its figures may be misread"] if sheet.caution else []
        notes += [f"note: {sheet.note}"] if sheet.note else []
        lines.append(");" + (f"  -- {'; '.join(notes)}" if notes else ""))
        return "\n".join(lines)

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
        joins = _JOIN.findall(bare)
        if (
            _CROSS.search(bare)
            or any(not re.match(r"(?i)\s*(?:(?:AS\s+)?\w+\s+)?(?:ON|USING)\b", rest) for rest in joins)
            or (joins and re.search(r"(?i)\bON\s+(?:1|TRUE)\b", bare))
        ):
            # Every row of one table against every row of another: its sums are many times too large.
            raise ValueError("the tables are separate lists: query one at a time, or JOIN them ON a column they share")
        for sheet, column in self._added_up_running(bare):
            raise ValueError(
                f"{column.name} in {sheet.name} is a running balance: each row's already includes every row above it, "
                f"so adding it up means nothing. Take the last row's {column.name}, or add up the columns it moves by "
                f"({column.running})"
            )
        started = time.monotonic()
        self._deadline = started + QUERY_SECONDS
        self._db.set_progress_handler(lambda: int(time.monotonic() - started > QUERY_SECONDS), 10_000)
        try:
            cursor = self._db.execute(sql)
            names = [item[0] for item in cursor.description or []]
            rows = cursor.fetchmany(MAX_RESULT_ROWS + 1)
        except MemoryError:
            raise sqlite3.OperationalError("the query needs too much memory: add up or filter the rows instead") from None
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
        used = set(re.findall(r"\b(t\d+)(?:_cells)?\b", _LITERAL.sub("''", found.sql)))
        for sheet in self.sheets:
            if sheet.name in used and sheet.caution:
                lines.append(f"Caution: {sheet.caution} ({sheet.title}); its figures may be misread, so say so if you use them.")
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
            if not math.isfinite(value) or abs(value) >= 1e15:
                return f"{value:g}"
            try:
                number = Decimal(repr(value)).quantize(Decimal("0.0001")).normalize()
            except InvalidOperation:
                return f"{value:g}"
            column = self._labels.get(name.lower())
            if column and column.kind == "figure":
                # A figure the query worked out under the column's name ("AVG(days) AS days"): its decimals
                # too, when it has more than the column writes (45.67, not 46).
                if -number.as_tuple().exponent > _style(column)[3]:
                    finer = ("0.0000" if abs(number) < 1 else "0.00") + ("%" if column.samples and _style(column)[1] else "")
                    return table_lookup._format(number, [*column.samples, finer])
                return table_lookup._format(number, column.samples)
            # A figure the query worked out (a sum, an average): the sheet's decimals, two when it isn't whole,
            # four for a ratio under one ("0.0347", not "0.03").
            samples = self._figure_samples()
            if number != number.to_integral_value():
                samples.append("0.0000" if abs(number) < 1 else "0.00")
            return table_lookup._format(number, samples)
        column = self._labels.get(name.lower())
        if column and column.written:
            # Stored as 2026-10-08 to compare; shown as the sheet writes it ("Thu 10/08", "Apr-26").
            return column.written.get(str(value), str(value))
        return str(value)[:300]

    def _figure_samples(self) -> list[str]:
        """How the sheet writes its amounts, for a result the query worked out: their decimals, without a
        currency sign that might not apply, and not a percent column's."""
        samples = [s for sheet in self.sheets for c in sheet.columns if c.kind == "figure" for s in c.samples[:5]]
        return [s.replace("$", "") for s in samples if "%" not in s]

    def _added_up_running(self, sql: str) -> list[tuple[Sheet, Column]]:
        """The running balances the query adds up (SUM, TOTAL or AVG): by their table's name or alias, or unprefixed
        in a query that reads their table. A column of the same name in another table is another column."""
        read = {name.lower() for name in re.findall(r"(?i)\b(?:from|join)\s+(\w+)", sql)}
        aliases: dict[str, str] = {}
        for table, alias in re.findall(r"(?i)\b(?:from|join)\s+(\w+)\s+(?:as\s+)?(\w+)", sql):
            if alias.lower() not in _SQL_WORDS:
                aliases[alias.lower()] = table.lower()
        found = []
        for sheet in self.sheets:
            for column in sheet.columns:
                if not column.running:
                    continue
                for prefix in re.findall(rf'(?i)\b(?:sum|total|avg)\s*\(\s*(?:distinct\s+|all\s+)?(?:"?(\w+)"?\.)?"?{column.name}\b', sql):
                    owner = aliases.get(prefix.lower(), prefix.lower()) if prefix else ""
                    if owner == sheet.name.lower() or (not owner and sheet.name.lower() in read):
                        found.append((sheet, column))
                        break
        return found

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


def ask(settings, tables: Tables, question: str, *, complete=None, think: bool = False) -> Result | None:
    """The model's query for the question, run on ``tables``; None when it says the tables can't answer it
    or no query it writes runs and finds something. ``complete``: the model call (``local_llm.complete_text``);
    ``think``: let a model that can think do so first. Thinking is what most helps a small model read which
    column and row a question means (on held-out questions a 4B model got twice as many of the hard ones),
    at some 600 tokens a question; a second, independent query that had to agree did not help, as its
    mistakes are the same ones."""
    if complete is None:
        from controller_inbox.local_llm import complete_text as complete

    outcome = _candidate(settings, tables, question, complete, think=think)
    return None if outcome is _NO_QUERY else outcome


_NO_QUERY = object()


def _candidate(settings, tables: Tables, question: str, complete, *, think: bool):
    """A Result, None when the model says the tables can't answer it, or ``_NO_QUERY`` when none of its queries
    ran and found something. A query that fails or finds nothing is tried again with what was wrong."""
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"Schema:\n{tables.schema()}\n\nQuestion: {question}"},
    ]
    for attempt in range(ATTEMPTS):
        reply = complete(settings, messages, max_tokens=500, think=think)
        plan, sql = parse(reply)
        if not sql or _cannot(plan):
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
            break
        hints = " ".join(tables.hints(sql, error))
        messages += [
            {"role": "assistant", "content": reply},
            {"role": "user", "content": f"{problem} {hints} Write the Table, Rows, Value and SQL lines again.".replace("  ", " ")},
        ]
    return _NO_QUERY


def _cannot(plan: str) -> bool:
    """The plan says the sheet doesn't hold what was asked ("Q2 is not in the table", "Table: none")."""
    if re.search(r"(?i)\btable:\s*none\b", plan):
        return True
    parts = re.split(r"[;,.()]", plan)
    return any(
        _CANNOT.search(part) and not _TOTAL_ROWS.search(part) and not _WORKED_FROM.search(" ".join(parts[at + 1 : at + 2]))
        for at, part in enumerate(parts)
    )


def parse(reply: str) -> tuple[str, str]:
    """(plan, sql) from the model's reply: its Table, Rows and Value lines as one plan, and the query; sql is
    "" when it wrote NONE or no query."""
    text = reply or ""
    fenced = _FENCE.search(text)
    sql_line = _SQL_LINE.search(text)
    before = text[: sql_line.start()] if sql_line else text[: fenced.start()] if fenced else text
    plan = " ".join(f"{m.group(1)}: {m.group(2)}" for m in _REASONING.finditer(before))
    sql = fenced.group(1) if fenced else sql_line.group(1) if sql_line else text
    # Anything the model wrote after the query, past a blank line, is not part of it.
    sql = sql.strip().split("\n\n")[0].strip("`*").strip().rstrip(";").strip()
    if not re.match(r"(?is)^(select|with)\b", sql):
        sql = ""
    return plan, sql


# Building the tables ----------------------------------------------------------------------------


def _names_a_column(table: Table, asked: list[str]) -> bool:
    """One of the question's words is in a column name of the table ("due" in "Due date", "60" in "31-60")."""
    labels = [word for label in table.labels for word in table_lookup._label_words(label)]
    return any(table_lookup._matches(word, labels) for word in asked)


def _authorize(action: int, _table, function, _db, _trigger) -> int:
    """Reading and the functions in ``_FUNCTIONS`` only; for a function call SQLite passes its name third."""
    if action in _ALLOWED and (action != sqlite3.SQLITE_FUNCTION or str(function).lower() in _FUNCTIONS):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


_ANY = object()
_STAR = object()


def _like(pattern, value, escape=None, *, deadline: float = 0.0):
    """SQL's LIKE with case folded for every letter: % any run of characters, _ any one. Past ``deadline`` (a
    time.monotonic() value; 0 for none) it gives up with an error, as SQLite does for a query that runs too long."""
    if pattern is None or value is None:
        return None
    tokens: list = []
    # Folded a character at a time, so "_" still matches the one letter "ß" (folding the whole word makes it "ss").
    text = str(pattern)
    index = 0
    while index < len(text):
        char = text[index]
        if escape is not None and char == str(escape) and index + 1 < len(text):
            tokens.append(text[index + 1].casefold())
            index += 2
            continue
        if char == "%":
            if not tokens or tokens[-1] is not _STAR:
                tokens.append(_STAR)
        else:
            tokens.append(_ANY if char == "_" else char.casefold())
        index += 1
    return _wildcard(tokens, [char.casefold() for char in str(value)], deadline)


def _wildcard(tokens: list, text, deadline: float = 0.0) -> bool:
    """Whether ``tokens`` match all of ``text``, going back only to the last % (no runaway backtracking)."""
    at = position = 0
    star, mark = -1, 0
    steps = 0
    while position < len(text):
        steps += 1
        if deadline and not steps % 4096 and time.monotonic() > deadline:
            raise sqlite3.OperationalError("interrupted")
        if at < len(tokens) and tokens[at] is not _STAR and (tokens[at] is _ANY or tokens[at] == text[position]):
            at += 1
            position += 1
        elif at < len(tokens) and tokens[at] is _STAR:
            star, mark = at, position
            at += 1
        elif star != -1:
            at, mark = star + 1, mark + 1
            position = mark
        else:
            return False
    while at < len(tokens) and tokens[at] is _STAR:
        at += 1
    return at == len(tokens)


def _ident(label: str, taken: set[str]) -> str:
    """A column name the model can write bare: "31 - 60 Days" is c_31_60_days, "Check #" check_no."""
    plain = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode()
    name = re.sub(r"[^a-z0-9]+", "_", plain.lower().replace("#", " no ").replace("%", " pct ")).strip("_") or "col"
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
    raw = raw[:MAX_CELL_CHARS]
    if kind == "figure":
        # A "-" is the accounting format's zero; a blank cell is no figure at all (MIN and AVG skip it).
        number = table_lookup._number(raw)
        return float(number) if number is not None else None
    if kind == "date":
        when = table_lookup._when(raw)
        if when:
            year, month, day = when
            return f"{year:04d}-{month:02d}-{day:02d}" if year else f"{month:02d}-{day:02d}"
    return raw or None


def _style(column: Column) -> tuple:
    """How a column writes its cells: its kind, and for figures a percent, a currency sign, the decimals."""
    if column.kind != "figure":
        return (column.kind,)
    places = max((len(m.group(1)) for sample in column.samples for m in [re.search(r"\.(\d+)", sample)] if m), default=0)
    return ("figure", all("%" in s for s in column.samples), any("$" in s for s in column.samples), places)


def _describe(column: Column, names: int = MAX_NAMES) -> str:
    if column.same_as:
        return f"as in {column.same_as}"
    if column.written and all(len(stored) == 7 for stored in column.written):
        months = list(column.written.items())
        return f'"{column.label}" as text YYYY-MM: {months[0][0]} ({months[0][1]}) to {months[-1][0]} ({months[-1][1]})'
    if column.kind == "figure":
        note = f'"{column.label}" figure' + (f', under "{column.under}"' if column.under else "")
        if column.samples and all("%" in sample for sample in column.samples):
            note += ", a percent (6.3 means 6.3%)"
        if column.running:
            return f"{note}, a running balance: each row's is the row above's {column.running}; the last row's is the balance, never add it up"
        return f"{note} = {column.formula} on {'most rows' if column.most else 'every row'}" if column.formula else note
    if column.kind == "date":
        stored = next((_stored(s, "date") for s in column.samples if table_lookup._when(s)), "")
        shape = "YYYY-MM-DD" if len(stored or "") == 10 else "MM-DD"
        return f'"{column.label}" date as text {shape}' + (f", e.g. {stored}" if stored else "")
    listed = list(dict.fromkeys(column.samples))
    shown = ", ".join(name[:48] for name in listed[:names]) + (", …" if len(listed) > names else "")
    return f'"{column.label}": {shown}'


def _formulas(columns: list[Column], values: list[list]) -> None:
    """Note each figure column every row works out from the columns beside it ("ending = beginning +
    additions - payments - reversals", "total = current + 1-30 + ..."), so the model uses it rather than
    adding its parts to it again. Of an identity read both ways ("total = current + over 90", "over 90 =
    total - current"), the result is the column headed like one (a total, net, ending or variance), else
    the one further right."""
    figures = [index for index, column in enumerate(columns) if column.kind == "figure"]
    values = values[:FORMULA_ROWS]
    found: dict[int, list[tuple[int, int]]] = {}
    for target in figures:
        position = figures.index(target)
        runs: list[tuple[list[int], bool]] = []
        for width in range(2, min(13, len(figures))):
            if position - width >= 0:
                runs.append((figures[position - width : position], False))
            if position + 1 + width <= len(figures):
                runs.append((figures[position + 1 : position + 1 + width], False))
            # Every other column: "Total HC = Chicago HC + Austin HC + Remote HC" beside their Salary columns.
            if width >= 3 and position - 2 * width >= 0:
                runs.append((figures[position - 2 * width : position : 2], True))
        # A formula that holds on every row comes first: "total = current + 1-30" holds on most rows of an aging
        # (the later buckets are mostly empty) but only the one over every bucket holds on all of them.
        for share in (1.0, 0.9):
            for run, interleaved in runs:
                signs = [(1,) * len(run)] if len(run) > 5 or interleaved else [(1, *rest) for rest in itertools.product((1, -1), repeat=len(run) - 1)]
                terms = next((list(zip(run, pattern)) for pattern in signs if _holds(values, target, list(zip(run, pattern)), share)), None)
                if terms:
                    found[target] = terms
                    columns[target].most = share < 1
                    break
            if target in found:
                break
    def kept(index: int) -> tuple[bool, int]:
        return bool(_RESULT_WORDS.search(columns[index].label)), index

    for target, terms in list(found.items()):
        for other, _sign in terms:
            if other in found and target in found and any(index == target for index, _s in found[other]):
                found.pop(min(target, other, key=kept))
    for target, terms in found.items():
        columns[target].parts = terms
        columns[target].formula = " ".join(("+ " if sign > 0 else "- ") + columns[index].name for index, sign in terms).removeprefix("+ ")


def _running_balances(columns: list[Column], values: list[list]) -> None:
    """Note each figure column that is a running balance: every row's is the row above's plus one column and minus
    another ("Balance" on a statement of account: + charges - payments), or plus or minus one (a loan's balance less
    its principal). Each row's already holds every row above it, so adding the column up means nothing; its last
    row is the balance."""
    figures = [index for index, column in enumerate(columns) if column.kind == "figure" and not column.formula]
    values = values[:FORMULA_ROWS]
    for target in figures:
        others = [index for index in figures if index != target]
        # A pair of columns it moves by is looked for among the eight nearest it: on a sheet of many figure columns
        # (36 months) every pair is a million rows read for each question.
        near = sorted(sorted(others, key=lambda index: abs(index - target))[:8])
        options = [[(a, sign)] for a in others for sign in (1, -1)] + [[(a, 1), (b, -1)] for a in near for b in near if a != b]
        for terms in options:
            if _runs(values, target, terms):
                columns[target].running = " ".join(("+ " if sign > 0 else "- ") + columns[index].name for index, sign in terms)
                break


def _runs(values: list[list], target: int, terms: list[tuple[int, int]]) -> bool:
    """Whether ``target`` is the row above's plus the signed ``terms`` (a blank one adding nothing) on at least
    nine in ten of the rows after the first, three or more of them, with something added on most of them."""
    steps = good = moved = 0
    # More rows off than one in ten of them all can't hold: a column that isn't a running balance stops early.
    allowed = 0.1 * (len(values) - 1)
    for above, row in zip(values, values[1:]):
        if above[target] is None or row[target] is None:
            continue
        added = sum(sign * (row[index] or 0.0) for index, sign in terms)
        steps += 1
        good += abs(above[target] + added - row[target]) <= 0.015
        moved += abs(added) > 0.005
        if steps - good > allowed:
            return False
    return steps >= 3 and good >= 0.9 * steps and moved >= 0.5 * steps


def _holds(values: list[list], target: int, terms: list[tuple[int, int]], share: float = 1.0) -> bool:
    """Whether ``target`` is the signed sum of ``terms`` on at least ``share`` of the rows that have it."""
    used = good = steady = 0
    copies = [0] * len(terms)
    # The columns in the sheet's order, to tell a straight-line schedule (each column the same step from the last).
    in_order = sorted([target, *(index for index, _sign in terms)])
    # More rows off than ``share`` allows of them all can't hold: a formula that doesn't stops early.
    allowed = (1 - share) * len(values)
    for row in values:
        # A blank part adds nothing, as in the sheet's SUM.
        parts = [row[index] or 0.0 for index, _sign in terms]
        # A row with none of the parts ("Balance per trial balance, GL" under a rollforward) isn't worked out
        # from them at all.
        if row[target] is None or all(row[index] is None for index, _sign in terms):
            continue
        used += 1
        good += abs(sum(sign * part for (_index, sign), part in zip(terms, parts)) - row[target]) <= 0.015 * len(terms)
        if used - good > allowed:
            return False
        for position, part in enumerate(parts):
            copies[position] += abs(part - row[target]) < 0.005
        cells = [row[index] or 0.0 for index in in_order]
        steps = [after - before for before, after in zip(cells, cells[1:])]
        steady += max(steps) - min(steps) <= 0.015 * len(terms)
    nonzero = sum(1 for row in values if row[target])
    # Equal months ("Oct = Jul - Aug + Sep" when every month is the same) only look like a formula, and so do equal
    # steps: a balance amortized straight-line ("Mar 31 = Jun 30 + Sep 30 - Dec 31") fits a + d = b + c every time.
    return used >= 3 and nonzero >= 2 and good >= share * used and max(copies) < 0.8 * used and steady < 0.8 * used


def _headings_above(lines: list[str], at: int) -> list[str]:
    """The heading lines nearest above a table ("Accrued Liabilities Rollforward", "Quarter Ended ...")."""
    for index in range(min(at, len(lines)) - 1, -1, -1):
        if lines[index].strip() != "[heading]":
            continue
        heads = []
        for line in lines[index + 1 :]:
            if not line.strip() or line.startswith("["):
                break
            heads.append(line.strip()[:80])
        return heads
    return []


def _family(columns: list[Column]) -> list[int]:
    """The columns a sheet lays one quantity across: the three or more its total adds up ("Total Due To = US01
    + CA02 + ..."), its month columns, or three or more text columns holding the same few values (each
    person's shift: "8-5", "OFF", "PTO")."""
    sums = [
        column.parts
        for column in columns
        if len(column.parts) >= 3
        and all(sign > 0 for _index, sign in column.parts)
        and all(b - a == 1 for (a, _s), (b, _t) in zip(column.parts, column.parts[1:]))
    ]
    if sums:
        return [index for index, _sign in max(sums, key=len)]
    months = [index for index, column in enumerate(columns) if column.kind == "figure" and _month(column.label)]
    if len(months) >= 3:
        return months
    texts = [index for index, column in enumerate(columns) if column.kind == "text" and len(column.samples) >= 3 and len({s.casefold() for s in column.samples}) <= 12]
    best: list[int] = []
    for index in texts:
        group = [other for other in texts if _overlap(columns[index], columns[other]) >= 0.4]
        if len(group) > len(best):
            best = group
    return best if len(best) >= 3 else []


def _overlap(a: Column, b: Column) -> float:
    first, second = {s.casefold() for s in a.samples}, {s.casefold() for s in b.samples}
    return len(first & second) / max(1, len(first | second))


_MONTH_LABEL = re.compile(r"(?:^|\s)([a-z]{3,9})\.?([\s\-'/]*)(\d{2}|\d{4})$", re.I)


def _month(label: str) -> str:
    """ "Apr-26", "April 2026", "Tax Collected Jul-26" as 2026-04 (2026-07), else "". A month's last day after a
    space ("Mar 31", "Jun 30": balances at each quarter end) is that day, not the year 2031."""
    match = _MONTH_LABEL.search(label.strip())
    month = table_lookup._MONTHS.get(match.group(1).lower()) if match else None
    if not month:
        return ""
    year = int(match.group(3))
    if len(match.group(3)) == 2 and not match.group(2).strip() and year in ({28, 29} if month == 2 else {table_lookup._DAYS[month - 1]}):
        return ""
    return f"{year + 2000 if year < 100 else year:04d}-{month:02d}"


def _over(headings: list[str], columns: list[Column], family: list[int]) -> str:
    """The heading merged over a family of columns ("Due From (payable entity)", "Days Past Due"): the last
    heading line above the table, when it is short, isn't the title and isn't a date."""
    if len(headings) < 2:
        return ""
    last = headings[-1]
    if len(last.split()) > 5 or any(ch.isdigit() for ch in last) or last in {columns[index].label for index in family}:
        return ""
    return last


_FACT = re.compile(r"^([A-Z][\w .&/()#'-]{1,40}?):\s+(\S.{0,100})$")


def _facts_in(lines: list[str], rows: set[int] = frozenset()) -> list[tuple[str, str]]:
    """Details printed beside a file's tables as "Name: value" on a line of their own ("Pay Date: Oct 15,
    2026", "Prepared by: L. Wei 10/2/2026"); a table row has several of them and is left alone. ``rows``: the
    lines that are rows of the file's tables, which a page holding only its table doesn't mark as one."""
    found: list[tuple[str, str]] = []
    block = ""
    for at, line in enumerate(lines):
        if len(found) >= 30:
            break
        if at in rows:
            continue
        if line.startswith("["):
            block = line.strip()
            continue
        pieces = [piece.strip() for piece in line.strip().split(" | ")]
        named = [piece for piece in pieces if ": " in piece]
        # In a table, a line of several "Name: value" pieces is a row; elsewhere it is several details
        # ("Prepared by: R. Delgado | Date: 10/12/2026").
        if not named or (block == "[table]" and len(named) != 1):
            continue
        for piece in named:
            match = _FACT.match(piece)
            # "Group: Payroll" is how a table's section heading is written out (pdf_layout), not a detail.
            if match and match.group(1) != "Group" and (match.group(1).strip(), match.group(2).strip()) not in found:
                found.append((match.group(1).strip(), match.group(2).strip()))
    return found[:30]


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
