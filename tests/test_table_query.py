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
    assert parse("Table: t1.\nRows: the named row.\nValue: a.\nSQL: SELECT a FROM t1;\n\nThat gives the answer.") == (
        "Table: t1. Rows: the named row. Value: a.",
        "SELECT a FROM t1",
    )
    assert parse("Plan: x\n```sql\nSELECT b\nFROM t1\n```") == ("Plan: x", "SELECT b\nFROM t1")
    assert parse("Plan: no column has 2025.\nSQL: NONE")[1] == ""


def _replies(*replies: str):
    """A fake model call answering ``replies`` in turn, keeping each call's messages and options."""
    queue = iter(replies)
    calls: list[tuple[list[dict], dict]] = []

    def complete(_settings, messages, **options):
        calls.append((list(messages), options))
        return next(queue)

    complete.calls = calls
    return complete


def test_a_failed_query_is_tried_once_more_with_the_error(aging):
    right = "Table: t1.\nRows: Juniper.\nValue: over_90.\nSQL: SELECT customer, over_90 FROM t1 WHERE customer LIKE '%juniper%'"
    complete = _replies("Table: t1.\nRows: the row.\nValue: x.\nSQL: SELECT name FROM t1", right)
    found = table_query.ask(None, aging, "what's juniper's over 90 balance", complete=complete, think=True)
    assert found is not None and found.rows == [("Juniper Ridge Apartments", 7615.29)]
    assert "no such column: name" in complete.calls[1][0][-1]["content"]
    assert all(options["think"] for _messages, options in complete.calls), "each go may think"


def test_a_query_that_never_runs_is_no_answer(aging):
    wrong = "Table: t1.\nRows: the row.\nValue: x.\nSQL: SELECT name FROM t1"
    assert table_query.ask(None, aging, "what's juniper's over 90 balance", complete=_replies(wrong, wrong)) is None


def test_none_is_no_answer(aging):
    def none(*_args, **_kwargs):
        return "Plan: no credit limits shown.\nSQL: NONE"

    assert table_query.ask(None, aging, "what's Dunmore's credit limit", complete=none) is None


def test_a_matrix_is_also_a_list_of_who_owes_whom():
    matrix = _tables(MORE / "Intercompany Matrix 9-30-26.pdf")
    schema = matrix.schema()
    assert '"MX04" figure, under "Due From (payable entity)"' in schema
    assert "CREATE TABLE t1_cells (  -- t1 again, one row per value under \"Due From (payable entity)\" (its columns us01 ... sg06)" in schema
    owed = matrix.run("SELECT due_from_payable_entity FROM t1_cells WHERE due_to_receivable_entity LIKE 'sg06%' AND amount > 0")[1]
    assert owed == [("US01",), ("UK03",), ("MX04",)]


def test_people_across_the_columns_can_be_counted():
    coverage = _tables(FIRST / "October Close Coverage.pdf")
    rows = coverage.run("SELECT heading, COUNT(*) FROM t1_cells WHERE value = 'PTO' GROUP BY heading ORDER BY heading")[1]
    assert rows == [("Grace Kim", 1), ("Priya Raman", 2), ("Sam Ortiz", 1), ("Tom Becker", 2)]


def test_month_columns_are_months_to_pick_a_quarter():
    revenue = _tables(MORE / "Revenue by Customer FY2026.pdf")
    assert '"Month" as text YYYY-MM: 2026-01 (Jan-26) to 2026-12 (Dec-26)' in revenue.schema()
    names, rows, more = revenue.run(
        "SELECT month, amount FROM t1_cells WHERE customer LIKE '%cedar%' AND month BETWEEN '2026-04' AND '2026-06' ORDER BY month"
    )
    assert rows[0] == ("2026-04", 22527.27) and len(rows) == 3
    assert revenue.render(table_query.Result("", "q", names, rows, more)).splitlines()[3] == "Month: Apr-26 | Amount: 22,527.27"


def test_details_beside_a_table_are_facts():
    payroll = _tables(MORE / "Payroll Register 10-15-26.pdf")
    assert "Pay Date: Oct 15, 2026" in payroll.schema()
    assert payroll.run("SELECT value FROM facts WHERE name LIKE '%pay date%'")[1] == [("Oct 15, 2026",)]
    assert payroll.about("what's the pay date on this register")


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


def _text_tables(text: str) -> Tables:
    return Tables([("book.xlsx", text)])


def test_sheets_with_the_same_columns_are_separate_tables():
    book = _text_tables(
        '[sheet "Aug 2026"]\nCustomer: Acme | Balance: 1,000\nCustomer: Bolt | Balance: 2,150\n'
        '[sheet "Sep 2026"]\nCustomer: Acme | Balance: 1,150\nCustomer: Bolt | Balance: 1,000\n'
    )
    assert [sheet.title for sheet in book.sheets] == ['book.xlsx / sheet "Aug 2026"', 'book.xlsx / sheet "Sep 2026"']
    assert book.run("SELECT SUM(balance) FROM t2")[1] == [(2150.0,)]


def test_columns_headed_alike_keep_their_own_cells():
    book = _text_tables(
        "Customer: Acme | Date: 9/1/2026 | Amount: 1,000 | Date: 9/20/2026 | Amount: 600\n"
        "Customer: Bolt | Date: 9/3/2026 | Amount: 2,000 | Date: 9/25/2026 | Amount: 2,000\n"
    )
    assert book.run("SELECT amount, amount_2, date_2 FROM t1 WHERE customer LIKE '%acme%'")[1] == [(1000.0, 600.0, "2026-09-20")]


def test_a_blank_figure_is_no_figure_and_a_dash_is_zero():
    book = _text_tables(
        "Employee: Ava | Hours: 80.00 | Rate: 52.40\nEmployee: Marcus | Hours: not listed | Rate: not listed\n"
        "Employee: Nina | Hours: 72.50 | Rate: -\n"
    )
    assert book.run("SELECT MIN(hours), AVG(hours), MIN(rate) FROM t1")[1] == [(72.5, 76.25, 0.0)]


def test_a_description_ending_in_total_is_not_a_total_row():
    book = _text_tables(
        "Dept: Ops | Basis: Share of revenue total | Allocated: 20,000\nDept: IT | Basis: Headcount | Allocated: 10,000\n"
        "Dept: HR | Basis: Headcount | Allocated: 20,000\nDept: Company Total | Basis: not listed | Allocated: 50,000\n"
    )
    assert book.run("SELECT COUNT(*), SUM(allocated) FROM t1")[1] == [(3, 50000.0)]


def test_dashes_in_a_column_of_references_leave_it_text():
    book = _text_tables("Ref: - | Amount: 10\nRef: - | Amount: 20\nRef: INV-2231 | Amount: 30\nRef: WIRE-88 | Amount: 40\nRef: - | Amount: 50\n")
    assert book.run("SELECT amount FROM t1 WHERE ref LIKE '%inv-2231%'")[1] == [(30.0,)]


def test_headings_like_q1_to_q4_are_headings():
    book = _text_tables("Revenue | Q1: 1,200 | Q2: 1,300 | Q3: 1,400 | Q4: 1,500\nExpenses | Q1: 900 | Q2: 950 | Q3: 980 | Q4: 1,000\n")
    assert book.run("SELECT q2 FROM t1 WHERE line LIKE '%revenue%'")[1] == [(1300.0,)]


def test_names_match_whatever_their_case_in_any_alphabet():
    book = _text_tables("Vendor: Müller GmbH | Total: 1,500.00\nVendor: Électricité de France | Total: 2,250.00\n")
    assert book.run("SELECT total FROM t1 WHERE vendor LIKE '%MÜLLER%' OR vendor LIKE '%électricité%'")[1] == [(1500.0,), (2250.0,)]


def test_a_ratio_keeps_its_decimals_and_a_column_shared_by_two_tables_its_own_format():
    book = _text_tables(
        "Line: Ads | Change: 4.1%\nLine: Travel | Change: 6.3%\n\n"
        "Vendor: Acme | Change: $2,500.00\nVendor: Bolt | Change: $1,200.00\n"
    )
    names, rows, more = book.run("SELECT change, 1.0 / 6 AS share FROM t2 WHERE vendor LIKE '%acme%'")
    assert book.render(table_query.Result("", "q", names, rows, more)).endswith("change: 2,500.00 | share: 0.1667")


def test_a_plan_about_the_left_out_total_rows_is_still_an_answer(aging):
    def reply(*_args, **_kwargs):
        return "Plan: all rows (totals are not in the table, so SUM them); sum total_balance.\nSQL: SELECT SUM(total_balance) FROM t1"

    found = table_query.ask(None, aging, "total AR", complete=reply)
    assert found is not None and found.rows == [(323110.0,)]


def test_a_query_block_without_room_for_its_result_is_left_out():
    from controller_inbox.agent import _worked_block

    block = "Worked out with a query over the table (check it is what was asked; the whole file follows):\nQuery: SELECT x\nResult (1 row):\nx: 12,345.67"
    assert _worked_block(block, 2000) == block
    assert _worked_block(block, 60) == ""


def test_tables_are_not_crossed_without_a_column_they_share():
    matrix = _tables(MORE / "Intercompany Matrix 9-30-26.pdf")
    for sql in ["SELECT SUM(t1.total_due_to) + SUM(t2.net_receivable_payable) FROM t1, t2", "SELECT * FROM t1 JOIN t2"]:
        with pytest.raises(ValueError, match="JOIN them ON"):
            matrix.run(sql)
    assert matrix.run("SELECT SUM(total_due_to) FROM t1")[1][0][0] == pytest.approx(7185625.98)


def test_a_date_is_shown_as_the_sheet_writes_it():
    calendar = _tables(FIRST / "Sept Close Calendar.pdf")
    names, rows, more = calendar.run("SELECT task, date FROM t1 WHERE date > '10-06' ORDER BY date DESC LIMIT 1")
    assert calendar.render(table_query.Result("", "q", names, rows, more)).endswith("Task: Close package to CFO | Date: Thu 10/08")
