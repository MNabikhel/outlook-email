"""Which row is a total: one rule for the file's reader (pdf_layout) and the table questions (table_lookup)."""

from __future__ import annotations

import pytest
from pdffactory import Text, build_pdf

from controller_inbox import pdf_layout
from controller_inbox.documents import pdf_text
from controller_inbox.table_lookup import tables_in
from controller_inbox.totals import is_total_label, is_total_word


@pytest.mark.parametrize(
    "label, total",
    [
        # Starts with the word.
        ("Total", True),
        ("Totals", True),
        ("Total Vehicles", True),
        ("Total current assets", True),
        ("Grand Total", True),
        ("Subtotal", True),
        ("Sub-total", True),
        ("Sub total", True),
        ("Total due:", True),
        # Ends with it.
        ("Finance Subtotal", True),
        ("Company Total", True),
        ("Northeast total", True),
        ("Fire Sub-total", True),
        # What is owed, whole.
        ("Amount Due", True),
        ("Amount due:", True),
        ("Balance due", True),
        ("Balance owing", True),
        ("Amount payable", True),
        # Not totals.
        ("Totally Fresh Produce Co", False),
        ("Fuel", False),
        ("Amount", False),
        ("Balance", False),
        ("Amount due by 10/5 for the Fire department", False),
        ("Prior balance due to vendor credit", False),
        ("Rewards platform total fee", False),
        ("% of budget", False),
        ("", False),
    ],
)
def test_is_total_label(label, total):
    assert is_total_label(label) is total


def test_what_is_owed_only_counts_with_due():
    assert not is_total_label("Amount due", due=False)
    assert is_total_label("Total due", due=False)


def test_a_percent_row_only_counts_with_percent():
    assert is_total_label("% of budget", percent=True)
    assert is_total_word("% of revenue", percent=True)
    assert not is_total_word("% of revenue")


@pytest.mark.parametrize(
    "label, word",
    [
        ("Total", True), ("Grand total:", True), ("Subtotal", True), ("Sub total", True), ("Totals", True),
        ("Total due", True), ("Amount Due", True), ("Balance owing:", True),
        ("Total Vehicles", False), ("Finance Subtotal", False), ("Total Quality Logistics", False), ("Due", False),
    ],
)
def test_is_total_word(label, word):
    assert is_total_word(label) is word


ACCOUNTS = ["Salaries", "Overtime", "Fuel", "Utilities"]


def _budget(last: str) -> list[Text]:
    """Two departments, each drawn once beside its four accounts, and a last line ``last`` under them."""
    items = [Text(40, 700, "Department", bold=True), Text(140, 700, "Account", bold=True), Text(360, 700, "Amount", bold=True, right=True)]
    y, amount = 686, 1000
    for name in ("Fire", "Parks"):
        items.append(Text(40, y, name))
        for account in ACCOUNTS:
            amount += 137
            items += [Text(140, y, account), Text(360, y, f"{amount:,}.00", right=True)]
            y -= 14
    return items + [Text(140, y, last), Text(360, y, "9,999.00", right=True)]


@pytest.mark.parametrize("last", ["Total", "Amount due", "Balance due"])
def test_the_amount_due_under_a_merged_category_is_not_that_categorys(last):
    # "Amount due" under the lines is the whole table's, as "Total" is: it used to be read as one of Parks' accounts.
    text = pdf_text(build_pdf([_budget(last)]))
    assert f"Department: not listed | Account: {last} | Amount: 9,999.00" in text, text
    assert pdf_layout._total_row(["", last, "9,999.00"])


def test_a_balance_owing_row_is_a_total_not_a_record():
    # Its figure isn't the sum of the lines (a payment came off), yet it is the table's total, not a third line.
    text = """[page 1]
[table]
Item | Amount
Item: Freight | Amount: 100.00
Item: Pallets | Amount: 200.00
Item: Balance owing | Amount: 250.00
"""
    assert [(row.name, row.total) for row in tables_in(text)[0].rows] == [
        ("Freight", False), ("Pallets", False), ("Balance owing", True),
    ]
