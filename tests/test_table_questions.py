"""Questions answered from a schedule's rows: lookups, filters, totals, counts, averages, extremes, differences.

Each answer is worked out from the schedules in fixtures/excel_schedules (saved as PDF from a spreadsheet)
and checked against the figures the spreadsheet itself computed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from controller_inbox.documents import extract_document
from controller_inbox.table_lookup import lookup, read_question

FIXTURES = Path(__file__).parent / "fixtures" / "excel_schedules"
AP = "AP Aging 9-30-26.pdf"
BUDGET = "Budget vs Actual Q3 2026.pdf"
ASSETS = "Fixed Asset Schedule 9-30-26.pdf"
LOAN = "Term Loan Amortization.pdf"
PREPAID = "Prepaid Amortization FY2026.pdf"
STAFF = "October Close Coverage.pdf"
CLOSE = "Sept Close Calendar.pdf"


@pytest.fixture(scope="module")
def texts() -> dict[str, str]:
    return {path.name: extract_document(path.name, "application/pdf", path.read_bytes()) for path in FIXTURES.glob("*.pdf")}


CASES = [
    # (file, question, must contain, must not contain)
    (AP, "How many vendors have a balance over 90 days?", ["Count (page 1): 3 rows", "Evergreen Electric", "Orion Software"], ["Acme"]),
    (AP, "What is the total of the Current column?", ['Total of "Current" (page 1) over 16 rows', "$125,637.66"], []),
    (AP, "Which vendors have a total balance over $30,000?", ["3 of the table's", "Acme Industrial Supply", "Delta Packaging Co.", "Harbor Steel LLC"], ["Grainger", "Vendor: Total |"]),
    (AP, "Which vendor has the largest total balance?", ['Largest "Total"', "Harbor Steel LLC — $70,650.00"], []),
    (AP, "What is the average total balance per vendor?", ['Average of "Total" (page 1) over 16 rows', "$16,697.46"], []),
    (AP, "How much more do we owe Harbor Steel than Acme in total?", ["Harbor Steel LLC $70,650.00 − Acme Industrial Supply $32,430.50 = $38,219.50"], []),
    (BUDGET, "Which departments were over budget in Q3?", ["Product sales", "Sales & Marketing", "General & Administrative", "Facilities"], ["Research & Development →", "Services →"]),
    (BUDGET, "What is the difference between the FY 2026 forecast and budget for Services?", ["= -290,000"], []),
    (BUDGET, "Which operating expense line has the largest Q3 actual?", ["Sales & Marketing — 1,104,220"], []),
    (BUDGET, "Which revenue line missed its Q3 budget?", ["Services", "(87,550)"], ["Product sales →"]),
    (ASSETS, "How many assets are in the Machinery & Equipment category?", ["Count (page 1): 3 rows"], []),
    (ASSETS, "What is the total NBV of all vehicles?", ["$104,572.61", 'the table\'s own "Total Vehicles" row'], []),
    (ASSETS, "Which assets have a useful life of more than 7 years?", ["4 of the table's", "B-100", "B-101", "M-210", "M-214"], ["V-305", "F-501"]),
    (ASSETS, "Which assets were placed in service in 2025?", ["V-305", "C-412"], ["V-302", "B-100"]),
    (ASSETS, "Which asset has the smallest NBV?", ['Smallest "NBV 9/30/26"', "F-501 · Office furniture HQ — $0"], []),
    (ASSETS, "What is the average cost of the vehicles?", ["$57,550.00", "over 3 rows"], []),
    (ASSETS, "What is the total cost by category?", ["Buildings (2 rows): Cost $2,636,000.00", "Vehicles (3 rows): Cost $172,650.00"], []),
    (LOAN, "How much interest will be paid in 2027?", ['Total of "Interest"', "over 12 rows", "115,515.66"], []),
    (LOAN, "How much principal is paid in total during 2026?", ["439,681.10"], []),
    (LOAN, "What is the balance after the last payment in 2028?", ["Pmt # 36", "1,094,294.42"], []),
    (LOAN, "When does the principal portion first exceed $45,000?", ["Pmt # 47", "11/15/2029", "45,212.44"], []),
    (LOAN, "How many payments are left after December 2027?", ["Count (page 1): 36 rows"], []),
    (LOAN, "What is the total interest over the life of the loan?", ["417,389.30"], []),
    (PREPAID, "Which prepaids have a balance remaining at 12/31/26?", ["Travelers", "Microsoft", "Adobe", "ZoomInfo", "Chubb", "Workday"], ["Salesforce:"]),
    (PREPAID, "Which prepaids end in 2027?", ["6 of the table's", "Travelers", "Workday"], ["Salesforce", "Gartner"]),
    (PREPAID, "How many prepaids are in GL account 1410?", ["Count (page 1): 5 rows"], []),
    (PREPAID, "What is the total amortization from Jul-26 through Sep-26?", ["Jul-26", "Aug-26", "Sep-26", "83,505.00"], []),
    (STAFF, "How many days is Priya Raman on PTO in October?", ["Count (page 1): 2 rows", "Date 10/9", "Date 10/12"], []),
    (STAFF, "Which days is Tom Becker on PTO?", ["Date 10/1", "Date 10/2"], ["Date 10/9"]),
    (STAFF, "Who works the 9-6 shift on 10/21?", ["Date 10/21", "Maya Chen: 9-6"], []),
    (CLOSE, "How many tasks are still not started?", ["Count (page 1): 7 rows"], []),
    (CLOSE, "Which tasks does Jonathan Alvarez review?", ["4 of the table's", "Accrue utilities and freight", "Inventory reserve analysis"], ["Revenue cut-off review →"]),
    (CLOSE, "What tasks are due after October 5?", ["4 of the table's", "Revenue cut-off review", "Close package to CFO"], ["Intercompany"]),
    (CLOSE, "Which tasks are in progress?", ["2 of the table's", "Accrue utilities and freight", "Bank reconciliations"], []),
    # The plain lookups keep working.
    (AP, "How much do we owe Harbor Steel in the 31-60 day bucket?", ["Harbor Steel LLC → 31 - 60 Days: $22,150.00"], []),
    (LOAN, "How much interest is in payment 37?", ["Pmt # 37 → Payment: 48,623.15 | Interest: 5,699.45"], []),
]


@pytest.mark.parametrize(("name", "question", "wanted", "unwanted"), CASES, ids=[case[1] for case in CASES])
def test_a_question_is_answered_from_the_rows(texts, name, question, wanted, unwanted):
    found = lookup(texts[name], question, limit=6000)
    for piece in wanted:
        assert piece in found, f"{piece!r} not in:\n{found}"
    for piece in unwanted:
        assert piece not in found, f"{piece!r} in:\n{found}"


def test_the_reading_says_how_the_rows_were_limited(texts):
    found = lookup(texts[LOAN], "How much interest will be paid in 2027?")
    assert re.search(r"where Payment Date in 2027", found)
    assert found.startswith("Worked out from the table")


def test_a_limit_that_is_a_column_heading_names_the_column():
    asked = read_question("Which vendors have balances over 90 days?")
    assert asked.op == "list" and [c.phrase for c in asked.conditions] == ["over 90"]
    # The table decides: with an "Over 90 Days" column, "over 90" is that column (see the cases above).


def test_a_unit_after_a_figure_is_a_whole_word():
    assert [(c.op, c.value) for c in read_question("assets with a useful life of more than 60 months").conditions] == [(">", 60)]
    assert [(c.op, c.value) for c in read_question("vendors over $30k").conditions] == [(">", 30000)]
    assert [(c.op, c.value) for c in read_question("anything under 2.5 million?").conditions] == [("<", 2500000)]


def test_a_year_says_nothing_about_dates_written_without_one(texts):
    # The coverage schedule's dates are "10/14": "in 2027" can't pick its rows, so it is not applied.
    found = lookup(texts[STAFF], "How many days is Priya Raman on PTO in 2027?")
    assert "where Date in 2027" not in found


def test_the_question_s_first_word_is_not_a_name():
    from controller_inbox.assistant import _distinctive

    assert not _distinctive("payroll", "Payroll accrual variance for March?")
    assert _distinctive("march", "Payroll accrual variance for March?")
    assert _distinctive("31-60", "how much is in 31-60?")


def test_the_last_row_by_date_is_the_latest_dated_one():
    # An undated row sorted as if dated 9999-12-31, so "the last amount" was the opening balance.
    text = (
        "Date: not listed | Description: Opening balance | Amount: 1,000.00\n"
        "Date: 10/01/2026 | Description: Payment ACH | Amount: 250.00\n"
        "Date: 10/15/2026 | Description: Payment wire | Amount: 300.00\n"
        "Date: 10/20/2026 | Description: Invoice 77 | Amount: 410.00\n"
    )
    answer = lookup(text, "what's the last amount")
    assert "Invoice 77" in answer and "410.00" in answer and "Opening balance" not in answer


def test_a_half_cent_rounds_up_as_a_spreadsheet_does():
    # The average of 1.00 and 2.01 is 1.505: a spreadsheet shows 1.51, Python's half to even 1.50.
    assert "1.51" in lookup("Item: A | Amount: 1.00\nItem: B | Amount: 2.01\n", "what is the average amount")
