"""Questions answered by a query the model writes over a schedule's tables, run exactly by SQLite."""

from __future__ import annotations

import sqlite3
import time
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

    block = "Worked out with a query over the table (check it is what was asked; the file is above):\nQuery: SELECT x\nResult (1 row):\nx: 12,345.67"
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


def test_only_the_functions_a_schedule_question_needs_are_allowed():
    tables = Tables([("book.xlsx", "Customer: Acme | Balance: 1,000\nCustomer: Bolt | Balance: 2,150\nCustomer: Cato | Balance: 3,000\n")])
    assert tables.run("SELECT ROUND(SUM(balance) / COUNT(*), 2), MAX(balance) FROM t1")[1] == [(2050.0, 3000.0)]
    assert tables.run("SELECT UPPER(customer) FROM t1 WHERE customer LIKE 'ac%'")[1] == [("ACME",)]
    for sql in ("SELECT fts3_tokenizer('simple')", "SELECT hex(customer) FROM t1", "SELECT sqlite_source_id()"):
        with pytest.raises(sqlite3.Error):
            tables.run(sql)


def test_tables_listed_side_by_side_are_refused_and_joins_on_a_column_run():
    tables = Tables([("book.xlsx", "Customer: Acme | Balance: 1,000\nCustomer: Bolt | Balance: 2,150\nCustomer: Cato | Balance: 3,000\n")])
    for sql in ("SELECT SUM(balance) FROM t1, t1 AS b", "SELECT SUM(balance) FROM (SELECT * FROM t1) x, t1", "SELECT COUNT(*) FROM t1 JOIN t1 AS b ON 1"):
        with pytest.raises(ValueError, match="separate lists"):
            tables.run(sql)
    assert tables.run("SELECT COUNT(*) FROM t1 AS a JOIN t1 AS b ON a.customer = b.customer")[1] == [(3,)]


def test_a_like_that_runs_too_long_is_stopped():
    # LIKE runs in Python, where SQLite's own time limit can't reach: it keeps to the limit itself.
    tables = Tables([("book.xlsx", "Customer: Acme | Balance: 1,000\nCustomer: Bolt | Balance: 2,150\nCustomer: Cato | Balance: 3,000\n")])
    started = time.monotonic()
    with pytest.raises(sqlite3.Error):
        tables.run("SELECT customer FROM t1 WHERE printf('%12000s', '') LIKE '%' || printf('%6000s', '') || 'x%'")
    assert time.monotonic() - started < table_query.QUERY_SECONDS + 1.5


def test_a_formula_is_said_to_hold_on_every_row_only_when_it_does():
    def aged(name, *buckets):
        cells = [f"{label}: {value:,.2f}" if value else f"{label}: -" for label, value in zip(("Current", "1 - 30", "31 - 60", "61 - 90", "Over 90"), buckets)]
        return " | ".join([f"Customer: {name}", f"Total Balance: {sum(buckets):,.2f}", *cells])

    text = "\n".join(aged(f"Customer {n}", 1000.0 * n, 100.0 * n, 0, 0, 0) for n in range(1, 10))
    text += "\n" + aged("Dunmore Precision", 8100.0, 2400.0, 4418.9, 2960.15, 11304.62)
    schema = Tables([("AR.pdf", text)]).schema()
    assert '"Total Balance" figure = current_col + c_1_30 + c_31_60 + c_61_90 + over_90 on every row' in schema


def test_a_very_long_cell_does_not_stop_the_table_loading():
    text = "Name: " + "x" * 120_000 + " | Amount: 1\nName: B | Amount: 2\nName: C | Amount: 3\n"
    assert Tables([("f.pdf", text)]).run("SELECT SUM(amount) FROM t1")[1] == [(6.0,)]


QUARTER_ENDS = """[page 1]
[heading]
Prepaid Balances 2026
[table]
Account | Mar 31 | Jun 30 | Sep 30 | Dec 31
Account: Insurance | Mar 31: 1,000.00 | Jun 30: 750.00 | Sep 30: 500.00 | Dec 31: 250.00
Account: Software | Mar 31: 2,000.00 | Jun 30: 1,500.00 | Sep 30: 1,000.00 | Dec 31: 500.00
Account: Rent | Mar 31: 3,000.00 | Jun 30: 3,000.00 | Sep 30: 3,000.00 | Dec 31: 3,000.00
"""


def test_quarter_end_columns_are_days_not_years():
    # "Mar 31" is the balance at March 31st, not March 2031.
    assert [table_query._month(label) for label in ("Mar 31", "Jun 30", "Feb 28", "Apr-26", "Apr 26", "Dec 2031")] == [
        "", "", "", "2026-04", "2026-04", "2031-12",
    ]
    assert "2031" not in Tables([("Prepaids.pdf", QUARTER_ENDS)]).schema()


def test_a_straight_line_schedule_is_not_read_as_a_formula():
    # Equal steps fit "Mar 31 = Jun 30 + Sep 30 - Dec 31" on every row; that is amortization, not how the sheet adds up.
    schema = Tables([("Prepaids.pdf", QUARTER_ENDS)]).schema()
    assert '"Mar 31" figure\n' in schema + "\n" and " = " not in schema


def test_the_rows_of_a_page_s_only_table_are_not_facts():
    # A page holding nothing but its table isn't marked [table]; its rows' cells are still not details beside it.
    text = """[page 1]
Vendor | Invoice | Amount
Vendor: Harbor Steel | Invoice: INV-1 | Amount: 4,500.00
Vendor: Acme Supply | Invoice: INV-3 | Amount: 800.00
Vendor: Blue Freight | Invoice: INV-4 | Amount: 300.00
Prepared by: L. Wei | Date: 10/2/2026
"""
    tables = Tables([("AP.pdf", text)])
    assert [(name, value) for _file, name, value in tables.facts] == [("Prepared by", "L. Wei"), ("Date", "10/2/2026")]


STATEMENT = """[page 1]
[table]
Date | Type | Reference | Charges | Payments | Balance
Date: 06/09/2026 | Type: Invoice | Reference: TF-22268 | Charges: 2,130.75 | Payments: not listed | Balance: 2,130.75
Date: 06/18/2026 | Type: Invoice | Reference: TF-21671 | Charges: 2,019.02 | Payments: not listed | Balance: 4,149.77
Date: 07/07/2026 | Type: Payment | Reference: ACH 380837 | Charges: not listed | Payments: 3,721.19 | Balance: 428.58
Date: 07/16/2026 | Type: Invoice | Reference: TF-25845 | Charges: 1,219.23 | Payments: not listed | Balance: 1,647.81
Date: 07/27/2026 | Type: Invoice | Reference: TF-26084 | Charges: 697.35 | Payments: not listed | Balance: 2,345.16

[table]
Current | 1-30 | 31-60 | 61-90 | Over 90 | Amount due
Current: 697.35 | 1-30: 1,219.23 | 31-60: 0.00 | 61-90: 428.58 | Over 90: 0.00 | Amount due: 2,345.16
"""


def test_a_running_balance_is_said_to_be_one_and_never_added_up():
    tables = Tables([("statement.pdf", STATEMENT)])
    schema = tables.schema()
    assert "a running balance: each row's is the row above's + charges - payments; the last row's is the balance, never add it up" in schema
    with pytest.raises(ValueError, match="running balance"):
        tables.run("SELECT SUM(balance) FROM t1 WHERE balance > 0")
    with pytest.raises(ValueError, match="running balance"):
        tables.run("SELECT avg(t1.balance) FROM t1")
    assert tables.run("SELECT SUM(charges) FROM t1")[1] == [(6066.35,)], "its parts still add up"
    assert tables.run("SELECT balance FROM t1 ORDER BY date DESC LIMIT 1")[1] == [(2345.16,)]


def test_a_table_of_one_row_is_queried_too():
    tables = Tables([("statement.pdf", STATEMENT)])
    assert '"Over 90" figure' in tables.schema() and "1 rows" in tables.schema()
    assert tables.run("SELECT c_61_90 + over_90 FROM t2")[1] == [(428.58,)]


def test_a_section_ends_at_its_own_printed_total():
    # The reading carries "Operating expenses" down to Net income; adding up the section added them all in.
    statement = """[page 1]
[table]
FY2026 | FY2025
Revenue | FY2026: 18,642,310 | FY2025: 16,905,227
Gross profit | FY2026: 8,193,408 | FY2025: 7,173,779
Group: Operating expenses
Operating expenses | Selling and marketing | FY2026: 2,114,870 | FY2025: 1,982,101
Operating expenses | General and administrative | FY2026: 2,605,449 | FY2025: 2,411,960
Operating expenses | Total operating expenses | FY2026: 4,720,319 | FY2025: 4,394,061
Operating expenses | Operating income | FY2026: 3,473,089 | FY2025: 2,779,718
Operating expenses | Net income | FY2026: 2,604,817 | FY2025: 2,084,789
"""
    tables = Tables([("income.pdf", statement)])
    assert tables.run("SELECT SUM(fy2025) FROM t1 WHERE section = 'Operating expenses'")[1] == [(4394061.0,)]
    assert tables.run("SELECT section FROM t1 WHERE line = 'Net income'")[1] == [(None,)]


def test_a_table_of_one_row_is_loaded_for_a_question_that_names_one_of_its_columns():
    # The aging box is for "how much is over 60 days?"; beside the statement's lines for "what did we pay in
    # September?", a small model added up every payment instead of September's.
    for question in ("How much of the balance is more than 60 days past due?", "What is in the Over 90 bucket?", "What is the amount due?"):
        assert "1 rows" in Tables([("statement.pdf", STATEMENT)], question).schema(), question
    schema = Tables([("statement.pdf", STATEMENT)], "How much did we pay them in September?").schema()
    assert "1 rows" not in schema and "t2" not in schema and "CREATE TABLE t1" in schema


def test_a_query_adding_up_a_running_balance_is_asked_again_with_why():
    tables = Tables([("statement.pdf", STATEMENT)])
    replies = iter([
        "Table: t1.\nRows: all.\nValue: balance.\nSQL: SELECT SUM(balance) FROM t1",
        "Table: t2, the aging.\nRows: its one row.\nValue: 61-90 plus over 90.\nSQL: SELECT c_61_90 + over_90 AS over_60 FROM t2",
    ])
    asked = []

    def complete(_settings, messages, **_kw):
        asked.append(messages[-1]["content"])
        return next(replies)

    found = table_query.ask(None, tables, "How much of the balance is more than 60 days past due?", complete=complete)
    assert found is not None and found.rows == [(428.58,)]
    assert "running balance" in asked[-1], "the second try is told why the first was refused"


def test_a_loan_balance_paid_down_is_a_running_balance_too():
    schedule = """[page 1]
[table]
Payment | Date | Interest | Principal | Balance
Payment: 1 | Date: 01/31/2027 | Interest: 312.50 | Principal: 1,687.50 | Balance: 73,312.50
Payment: 2 | Date: 02/28/2027 | Interest: 305.47 | Principal: 1,694.53 | Balance: 71,617.97
Payment: 3 | Date: 03/31/2027 | Interest: 298.41 | Principal: 1,701.59 | Balance: 69,916.38
Payment: 4 | Date: 04/30/2027 | Interest: 291.32 | Principal: 1,708.68 | Balance: 68,207.70
"""
    tables = Tables([("loan.pdf", schedule)])
    assert "a running balance: each row's is the row above's - principal" in tables.schema()
    assert "interest REAL,  -- \"Interest\" figure\n" in tables.schema(), "a column that only looks steady isn't one"


def test_a_balance_column_in_another_table_is_its_own_and_may_be_added_up():
    # A statement of account (a running balance) and a list of open invoices (each one's balance) on one page.
    tables = Tables([("statement.pdf", STATEMENT + """
[table]
Invoice | Due date | Balance
Invoice: TF-1 | Due date: 08/01/2026 | Balance: 100.00
Invoice: TF-2 | Due date: 08/15/2026 | Balance: 250.00
Invoice: TF-3 | Due date: 09/01/2026 | Balance: 75.00
""")])
    assert tables.run("SELECT SUM(balance) FROM t2")[1] == [(425.0,)]
    assert tables.run("SELECT SUM(x.balance) FROM t2 AS x")[1] == [(425.0,)]
    for sql in ("SELECT SUM(balance) FROM t1", "SELECT SUM(a.balance) FROM t1 a", "SELECT SUM(t1.balance) FROM t1 JOIN t2 ON 1=0"):
        with pytest.raises(ValueError):
            tables.run(sql)


def test_tables_of_one_row_never_take_the_place_of_tables_of_rows():
    pages = []
    for page in range(1, 9):  # each page: an invoice's header box over its line items
        pages.append(f"""[page {page}]
[table]
Invoice No. | Due date
Invoice No.: {page} | Due date: 10/{page:02d}/2026

[table]
Item | Amount
Item: A{page} | Amount: {page}.00
Item: B{page} | Amount: {page}0.00
""")
    tables = Tables([("invoices.pdf", "\n".join(pages))])
    rows = [sheet for sheet in tables.sheets if sheet.rows >= 2]
    one = [sheet for sheet in tables.sheets if sheet.rows == 1]
    assert len(rows) == table_query.MAX_TABLES and len(one) == table_query.MAX_ONE_ROW_TABLES
