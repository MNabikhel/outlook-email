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
