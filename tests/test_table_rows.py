"""Which rows of a table are totals, and where one table ends and the next begins."""

from __future__ import annotations

from controller_inbox.table_lookup import tables_in
from controller_inbox.table_query import Tables

AGING = """[page 1]
[heading]
AP Aging Summary

[table]
Vendor | Current | Over 90 Days | Total
Vendor: Acme Industrial Supply | Current: 24,310.50 | Over 90 Days: - | Total: 24,310.50
Vendor: Total Quality Logistics | Current: 18,400.00 | Over 90 Days: 2,000.00 | Total: 20,400.00
Vendor: Harbor Steel LLC | Current: 48,500.00 | Over 90 Days: - | Total: 48,500.00
Vendor: Total | Current: 91,210.50 | Over 90 Days: 2,000.00 | Total: 93,210.50
"""


def test_a_vendor_named_total_is_a_vendor():
    # Its figures don't add up the rows above it, so "Total Quality Logistics" is a row like the others.
    rows = tables_in(AGING)[0].rows
    assert [(row.name, row.total) for row in rows] == [
        ("Acme Industrial Supply", False), ("Total Quality Logistics", False), ("Harbor Steel LLC", False), ("Total", True),
    ]
    assert Tables([("AP Aging.pdf", AGING)]).run("SELECT SUM(total), SUM(over_90_days) FROM t1")[1] == [(93210.5, 2000.0)]


def test_an_unlabeled_row_that_adds_up_the_rows_above_is_a_total():
    text = """[page 1]
[table]
Vendor | Invoice # | Amount
Vendor: Acme Industrial Supply | Invoice #: A-1001 | Amount: 24,310.50
Vendor: Harbor Steel LLC | Invoice #: H-2210 | Amount: 48,500.00
Vendor: Orion Software | Invoice #: O-77 | Amount: 18,600.00
Vendor: not listed | Invoice #: not listed | Amount: $91,410.50
"""
    assert [row.total for row in tables_in(text)[0].rows] == [False, False, False, True]
    assert Tables([("AP.pdf", text)]).run("SELECT COUNT(*), SUM(amount) FROM t1")[1] == [(3, 91410.5)]


def test_lists_under_their_own_headings_are_separate_tables():
    text = """[page 1]
[heading]
Outstanding Checks - Operating x4471

[table]
Payee | Check # | Amount
Payee: Tidewater Metals | Check #: 20418 | Amount: 52,300.00
Payee: Summit Ridge Electric | Check #: 20425 | Amount: 18,744.65
Payee: Total outstanding checks | Check #: not listed | Amount: 71,044.65

[heading]
Outstanding Checks - Payroll x0932

[table]
Payee | Check # | Amount
Payee: Maya Chen | Check #: 5512 | Amount: 3,100.00
Payee: Li Wei | Check #: 5519 | Amount: 2,850.00
Payee: Total outstanding checks | Check #: not listed | Amount: 5,950.00
"""
    assert [len(table.rows) for table in tables_in(text)] == [3, 3]
    tables = Tables([("Bank Recs.pdf", text)])
    assert "CREATE TABLE t2 (  -- Bank Recs.pdf / Outstanding Checks - Payroll x0932" in tables.schema()
    assert tables.run("SELECT SUM(amount) FROM t2")[1] == [(5950.0,)]


def _balance_sheet() -> bytes:
    """A 10-K balance sheet: year columns, dot leaders to the figures, "Current assets:" over its items and each
    total indented past them."""
    from pdffactory import Text, build_pdf, width

    items = [Text(40, 760, "CONSOLIDATED BALANCE SHEETS", size=12, bold=True), Text(40, 744, "(in thousands)")]
    items += [Text(430, 726, "2025", right=True, bold=True), Text(520, 726, "2024", right=True, bold=True)]
    lines = [
        (40, "Current assets:", None),
        (50, "Cash and cash equivalents", ("$290,291", "$508,053")),
        (50, "Short-term investments", ("457,787", "289,758")),
        (50, "Other current assets", ("64,622", "57,330")),
        (60, "Total current assets", ("812,700", "855,141")),
        (50, "Property and equipment, net", ("131,681", "136,353")),
        (60, "Total assets", ("$944,381", "$991,494")),
        (40, "Current liabilities:", None),
        (50, "Accounts payable", ("86,468", "86,992")),
        (50, "Accrued expenses", ("53,139", "54,231")),
        (60, "Total current liabilities", ("139,607", "141,223")),
        (50, "Long-term debt", ("200,000", "200,000")),
        (60, "Total liabilities", ("339,607", "341,223")),
        (50, "Total stockholders' equity", ("604,774", "650,271")),
        (60, "Total liabilities and stockholders' equity", ("$944,381", "$991,494")),
    ]
    y = 708
    for x, label, figures in lines:
        if figures:
            # Dot leaders run from the label to the first figure column.
            dots = int((370 - x - width(label + " ", 10)) / width(". ", 10))
            items.append(Text(x, y, label + " " + ". " * dots))
            items += [Text(430, y, figures[0], right=True), Text(520, y, figures[1], right=True)]
        else:
            items.append(Text(x, y, label))
        y -= 15
    return build_pdf([items])


def test_a_balance_sheet_reads_as_one_table_whose_totals_add_up():
    from controller_inbox.documents import pdf_text
    from controller_inbox.table_lookup import verify

    text = pdf_text(_balance_sheet())
    assert ". . ." not in text, "dot leaders are the gap between a label and its figures"
    [table] = tables_in(text)
    assert table.labels == ("Line", "2025", "2024"), "years name a statement's columns"
    named = {row.name: row for row in table.rows}
    cash = named["Cash and cash equivalents"]
    assert (cash.group, cash.value("2025"), cash.value("2024"), cash.total) == ("Current assets", "$290,291", "$508,053", False)
    assert named["Total current assets"].group == "Current assets", "an indented total is not the line item above it"
    assert [row.name for row in table.rows if row.total] == [
        "Total current assets", "Total assets", "Total current liabilities", "Total liabilities",
        "Total liabilities and stockholders' equity",
    ]
    verdict = verify(table)
    assert verdict.mismatched == [] and verdict.matched == 10, "liabilities and equity add up as total liabilities plus equity"
