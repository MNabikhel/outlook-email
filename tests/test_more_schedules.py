"""Schedule layouts beyond the first seven: how their PDFs are read into table rows.

The PDFs in fixtures/more_schedules were made by fixtures/more_schedules/make_books.py (openpyxl and
LibreOffice), formatted the way a controller formats them in Excel.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from controller_inbox.documents import extract_document
from controller_inbox.table_lookup import ROW_LABEL, tables_in

FIXTURES = Path(__file__).parent / "fixtures" / "more_schedules"


@pytest.fixture(scope="module")
def texts() -> dict[str, str]:
    return {path.name: extract_document(path.name, "application/pdf", path.read_bytes()) for path in FIXTURES.glob("*.pdf")}


def _table(texts, name: str, first_label: str):
    return next(table for table in tables_in(texts[name]) if table.labels[0] == first_label)


def test_side_by_side_accounts_in_the_accounting_format_are_one_column_each(texts):
    # "$  1,284,615.42 $  48,210.07": each cell's "$" sits just after the previous cell's figure, and a
    # positive figure ends a parenthesis short of a negative one. The headings wrap over three lines,
    # with "Total" on the middle one.
    table = _table(texts, "Bank Reconciliations 9-30-26.pdf", ROW_LABEL)
    assert table.labels == (
        ROW_LABEL,
        "Operating First Lakes Bank Acct x4471",
        "Payroll Harborview Bank Acct x0932",
        "Lockbox First Lakes Bank Acct x2205",
        "Total",
    )
    rows = {row.name: row for row in table.rows}
    assert rows["Adjusted bank balance"].value("Payroll Harborview Bank Acct x0932") == "$16,807.41"
    assert rows["Less: Outstanding checks"].value("Operating First Lakes Bank Acct x4471") == "(143,887.19)"
    # A row label with a colon in it is still the row's label, not a cell called "Add".
    assert rows["Add: Deposits in transit"].value("Total") == "101,187.80"
    assert len(table.rows) == 11


def test_facts_above_a_table_are_not_its_headings(texts):
    table = _table(texts, "Payroll Register 10-15-26.pdf", "Employee")
    assert table.labels == ("Employee", "Dept", "Hours", "Rate", "Gross", "Federal W/H", "State W/H", "FICA", "Net Pay")
    # "Finance Subtotal" and "Company Total" are totals, not people.
    assert len(table.body) == 14
    assert {row.name for row in table.rows if row.total} >= {"Finance Subtotal", "Company Total"}


def test_columns_printed_on_the_next_page_join_their_rows(texts):
    table = _table(texts, "Revenue by Customer FY2026.pdf", "Customer")
    assert table.labels[2] == "Jan-26" and table.labels[-2:] == ("Dec-26", "FY 2026 Total")
    cedar = next(row for row in table.rows if row.name == "Cedar Valley Foods")
    assert cedar.value("Apr-26") == "22,527.27" and cedar.value("FY 2026 Total") == "288,695.82"
    assert len(table.body) == 20


def test_headings_that_look_like_cell_references_are_headings(texts):
    # Entity codes ("US01", "CA02") look like workbook cell references.
    table = _table(texts, "Intercompany Matrix 9-30-26.pdf", "Due To (receivable entity)")
    assert table.labels[1:3] == ("US01", "CA02")
    us = next(row for row in table.rows if row.name.startswith("US01"))
    assert us.value("MX04") == "2,318,740.12"


def test_a_lone_dash_is_a_zero_figure(texts):
    table = _table(texts, "AR Aging 9-30-26.pdf", "Customer")
    assert table.kinds["Over 90"] == "figure" and table.kinds["61 - 90"] == "figure"


def test_a_heading_merged_down_two_heading_rows_stays_in_the_header(texts):
    # "Total" is merged down beside "Chicago | Austin | Remote", so it prints on a line of its own above them;
    # "Department" sits in the first column with nothing under it: that column's name, not a group.
    table = _table(texts, "Headcount Salary Budget FY2027.pdf", "Department")
    assert table.labels == (
        "Department", "Chicago HC", "Chicago Salary", "Austin HC", "Austin Salary", "Remote HC", "Remote Salary", "Total HC", "Total Salary",
    )
    finance = next(row for row in table.rows if row.name == "Finance")
    assert finance.value("Chicago HC") == "9" and finance.value("Total Salary") == "$1,327,000"


def test_month_headings_with_words_before_them_are_months():
    from controller_inbox.table_query import Tables

    path = FIXTURES / "Sales Tax Collected Q3 2026.pdf"
    tables = Tables([(path.name, extract_document(path.name, "application/pdf", path.read_bytes()))])
    schema = tables.schema()
    assert '"Month" as text YYYY-MM: 2026-07 (Tax Collected Jul-26) to 2026-09 (Tax Collected Sep-26)' in schema
    # Two details on one line, above the table.
    assert "Prepared by: R. Delgado; Date: 10/12/2026" in schema
    assert tables.run("SELECT SUM(amount) FROM t1_cells WHERE state LIKE '%jersey%' AND month = '2026-09'")[1] == [(-412.3,)]
