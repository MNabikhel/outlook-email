"""Questions answered by a query the model writes over a schedule's tables, run exactly by SQLite."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from controller_inbox import table_query
from controller_inbox.documents import extract_document
from controller_inbox.table_query import Tables, parse

MORE = Path(__file__).parent / "fixtures" / "more_schedules"
FIRST = Path(__file__).parent / "fixtures" / "excel_schedules"


def _tables(path: Path) -> Tables:
    return Tables([(path.name, extract_document(path.name, "application/pdf", path.read_bytes()))])


@pytest.fixture(scope="module")
def aging() -> Tables:
    return _tables(MORE / "AR Aging 9-30-26.pdf")


def test_the_schema_names_each_column_and_how_rows_work_out(aging):
    schema = aging.schema()
    assert "AR Aging 9-30-26.pdf / Accounts Receivable Aging Summary" in schema
    assert "c_61_90 REAL" in schema and "Halcyon Biotech LLC" in schema
    assert '"Total Balance" figure = current_col + c_1_30 + c_31_60 + c_61_90 + over_90 on every row' in schema
    assert "Total Accounts Receivable" not in schema  # the total row is left out, so SUM adds up the rows


def test_identities_read_as_the_sheet_works_them_out():
    rollforward = _tables(MORE / "Accrued Liabilities Rollforward Q3 2026.pdf").schema()
    assert "= beginning_balance_6_30_2026 + additions - payments - reversals on every row" in rollforward
    budget = _tables(FIRST / "Budget vs Actual Q3 2026.pdf").schema()
    assert '"Q3 2026 Variance $" figure = q3_2026_actual - q3_2026_budget on every row' in budget
    # Every month amortizes the same amount: "Oct = Jul - Aug + Sep" is a coincidence, not a formula.
    assert " = " not in _tables(FIRST / "Prepaid Amortization FY2026.pdf").schema()


def test_a_query_s_figures_are_written_the_way_the_sheet_writes_them(aging):
    names, rows, more = aging.run("SELECT customer, total_balance FROM t1 ORDER BY current_col DESC LIMIT 1")
    shown = aging.render(table_query.Result("", "q", names, rows, more))
    assert "Customer: Halcyon Biotech LLC | Total Balance: $56,300.00" in shown
    names, rows, more = aging.run("SELECT SUM(total_balance) - SUM(current_col) AS past_due, COUNT(*) AS n FROM t1 WHERE over_90 > 0")
    assert aging.render(table_query.Result("", "q", names, rows, more)).endswith("past_due: 51,858.61 | n: 4")


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM t1",
        "SELECT 1; DROP TABLE t1",
        "ATTACH DATABASE 'x.db' AS other",
        "SELECT load_extension('x')",
        "WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM r) SELECT COUNT(*) FROM r",
    ],
)
def test_a_query_can_only_read(aging, sql):
    with pytest.raises((ValueError, sqlite3.Error)):
        aging.run(sql)
    assert aging.run("SELECT COUNT(*) FROM t1")[1] == [(15,)]


def test_the_reply_s_query_is_read_from_its_sql_line():
    assert parse("Plan: the named row.\nSQL: SELECT a FROM t1;\n\nThat gives the answer.") == ("the named row.", "SELECT a FROM t1")
    assert parse("Plan: x\n```sql\nSELECT b\nFROM t1\n```") == ("x", "SELECT b\nFROM t1")
    assert parse("Plan: no column has 2025.\nSQL: NONE")[1] == ""


def test_a_failed_query_is_tried_once_more_with_the_error(aging):
    replies = iter([
        "Plan: the row.\nSQL: SELECT name FROM t1",
        "Plan: the row.\nSQL: SELECT customer, over_90 FROM t1 WHERE customer LIKE '%juniper%'",
    ])
    seen: list[list[dict]] = []

    def fake(settings, messages, *, max_tokens=400):
        seen.append(list(messages))
        return next(replies)

    found = table_query.ask(None, aging, "what's juniper's over 90 balance", complete=fake)
    assert found is not None and found.rows == [("Juniper Ridge Apartments", 7615.29)]
    assert "no such column: name" in seen[1][-1]["content"]


def test_none_is_no_answer(aging):
    def none(*_args, **_kwargs):
        return "Plan: no credit limits shown.\nSQL: NONE"

    assert table_query.ask(None, aging, "what's Dunmore's credit limit", complete=none) is None


@pytest.fixture(scope="module")
def recs() -> Tables:
    return _tables(MORE / "Bank Reconciliations 9-30-26.pdf")


def test_a_row_named_as_a_column_is_an_error_with_the_way_to_name_it(recs):
    # SQLite would read "Adjusted bank balance" as a string and find nothing; the model hears why instead.
    sql = """SELECT "Adjusted bank balance" FROM t1 WHERE line LIKE '%payroll harborview bank acct x0932%'"""
    with pytest.raises(ValueError, match="no such column"):
        recs.run(sql)
    hints = recs.hints(sql, 'no such column: "Adjusted bank balance"')
    assert "'payroll harborview bank acct x0932' is the column payroll_harborview_bank_acct_x0932 of t1" in hints[0]
    assert "use line LIKE '%adjusted bank balance%'" in hints[1]
    # An alias in quotes is a name the query gives itself.
    assert recs.run('SELECT SUM(amount) AS "Total Owed" FROM t2 ORDER BY "Total Owed"')[1] == [(143887.19,)]


def test_a_filter_on_what_every_row_is_is_named(recs):
    hints = recs.hints("SELECT payee, amount FROM t2 WHERE payee LIKE '%operating%'")
    assert hints == ["Every row of t2 is 'operating' (its title says so): don't filter on it."]


def test_a_worked_out_figure_takes_the_sheet_s_decimals():
    budget = _tables(FIRST / "Budget vs Actual Q3 2026.pdf")
    def shown(sql: str) -> str:
        names, rows, more = budget.run(sql)
        return budget.render(table_query.Result("", "q", names, rows, more)).splitlines()[-1]

    # Whole dollars like the sheet (not the "6.3%" column's one decimal); two decimals when not whole.
    assert shown("SELECT q4_2026_forecast - q3_2026_actual AS difference FROM t1 WHERE department LIKE '%marketing%'") == "difference: 45,780"
    assert shown("SELECT AVG(q3_2026_actual) AS mean FROM t1") == "mean: 1,320,707.50"


def test_a_plan_that_says_the_sheet_lacks_it_is_no_answer(aging):
    def reply(*_args, **_kwargs):
        return "Plan: sum q3 actual (since Q2 is not in the table).\nSQL: SELECT SUM(total_balance) FROM t1"

    assert table_query.ask(None, aging, "what were Q2 collections", complete=reply) is None
