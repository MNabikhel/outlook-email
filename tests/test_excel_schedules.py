"""Schedules built in a spreadsheet and saved as PDF (fixtures/excel_schedules, made by make_books.py).

Unlike the drawn PDFs in test_pdf_scenarios, these come from a real spreadsheet's PDF export: cells
closer together than a word space, the accounting format's "$" at the left of the cell and "-" for
zero, borders drawn as lines, headings wrapped onto two lines or merged across columns, names turned
on their side, and labels that run into the empty cell beside them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from controller_inbox.documents import extract_document
from controller_inbox.table_lookup import lookup

FIXTURES = Path(__file__).parent / "fixtures" / "excel_schedules"


def _text(name: str) -> str:
    return extract_document(name, "application/pdf", (FIXTURES / name).read_bytes())


@pytest.fixture(scope="module")
def texts() -> dict[str, str]:
    return {path.name: _text(path.name) for path in sorted(FIXTURES.glob("*.pdf"))}


def test_accounting_format_figures_stay_in_their_own_bucket(texts):
    text = texts["AP Aging 9-30-26.pdf"]
    assert (
        "Vendor: Harbor Steel LLC | Vendor #: V1030 | Current: $48,500.00 | 1 - 30 Days: $0 | 31 - 60 Days: $22,150.00 | "
        "61 - 90 Days: $0 | Over 90 Days: $0 | Total: $70,650.00"
    ) in text
    assert "Vendor: Total | Vendor #: not listed | Current: $125,637.66" in text
    assert "Over 90 Days: 12.7% | Total: 100.0%" in text


def test_a_right_aligned_date_does_not_run_into_the_next_cell(texts):
    text = texts["Sept Close Calendar.pdf"]
    assert "Workday | Date | Task | Owner | Reviewer | Status" in text
    assert (
        "Workday: WD2 | Date: Fri 10/02 | Task: Bank reconciliations (all accounts) | Owner: Li Wei | "
        "Reviewer: Jonathan Alvarez | Status: In progress"
    ) in text


def test_quarter_headings_merged_across_columns_name_each_figure(texts):
    text = texts["Budget vs Actual Q3 2026.pdf"]
    assert (
        "Department | Q3 2026 Actual | Q3 2026 Budget | Q3 2026 Variance $ | Q3 2026 Variance % | Q4 2026 Forecast | "
        "Q4 2026 Budget | FY 2026 Forecast | FY 2026 Budget"
    ) in text
    assert (
        "Revenue | Department: Services | Q3 2026 Actual: 812,450 | Q3 2026 Budget: 900,000 | Q3 2026 Variance $: (87,550) | "
        "Q3 2026 Variance %: -9.7% | Q4 2026 Forecast: 880,000 | Q4 2026 Budget: 950,000"
    ) in text
    # A section after a blank row is still the same table, under its own group.
    assert "Operating Expenses | Department: Research & Development | Q3 2026 Actual: 932,870" in text


def test_wrapped_headings_and_categories_merged_down_the_rows(texts):
    text = texts["Fixed Asset Schedule 9-30-26.pdf"]
    assert (
        "Category | Asset ID | Description | In-Service Date | Useful Life (Yrs) | Cost | Accum. Depr. 12/31/25 | "
        "2026 YTD Depr. | Accum. Depr. 9/30/26 | NBV 9/30/26"
    ) in text
    assert (
        "Category: Vehicles | Asset ID: V-302 | Description: Ford F-150 pickup | In-Service Date: 8/20/2024 | "
        "Useful Life (Yrs): 5 | Cost: $48,950.00"
    ) in text
    assert "NBV 9/30/26: $27,738.33" in text
    # A subtotal that runs into the empty cells beside it closes its category; it is not the next one's.
    assert "Category: not listed | Asset ID: Total Machinery & Equipment" in text
    assert "Category: Vehicles | Asset ID: Total" not in text


def test_names_on_their_side_and_short_dates_head_their_columns(texts):
    text = texts["October Close Coverage.pdf"]
    assert "Date | Day | Maya Chen | Sam Ortiz | Priya Raman | Jonathan Alvarez | Li Wei | Grace Kim | Tom Becker" in text
    assert (
        "Date: 10/14 | Day: Wed | Maya Chen: 9-6 | Sam Ortiz: OFF | Priya Raman: 8-5 | Jonathan Alvarez: 7-4 | "
        "Li Wei: WFH | Grace Kim: 8-5 | Tom Becker: 7-4"
    ) in text
    # A weekend row with every cell blank, even the last one, keeps its date and day.
    assert "Date: 10/31 | Day: Sat" in text


def test_a_title_over_a_wide_sheet_is_not_a_heading(texts):
    text = texts["Prepaid Amortization FY2026.pdf"]
    assert "| Monthly Amort. | Jan-26 | Feb-26 |" in text
    assert "Fiscal Jan-26" not in text and "Year 2026 Mar-26" not in text
    assert "Vendor: Chubb | Description: D&O insurance | GL Account: 1420" in text
    assert "Sep-26: 5,400.00" in text


def test_a_long_schedule_keeps_its_columns_on_the_next_page(texts):
    text = texts["Term Loan Amortization.pdf"]
    page2 = text.split("[page 2]", 1)[1]
    assert (
        "Pmt #: 37 | Payment Date: 01/15/2029 | Beginning Balance: 1,094,294.42 | Payment: 48,623.15 | "
        "Interest: 5,699.45 | Principal: 42,923.70 | Ending Balance: 1,051,370.72"
    ) in page2


@pytest.mark.parametrize(
    ("name", "question", "expected"),
    [
        ("AP Aging 9-30-26.pdf", "How much do we owe Harbor Steel in the 31-60 day bucket?", "Harbor Steel LLC → 31 - 60 Days: $22,150.00"),
        ("Budget vs Actual Q3 2026.pdf", "What is the Q4 budget for Research & Development?", "Research & Development → Q4 2026 Budget: 1,000,000"),
        ("Fixed Asset Schedule 9-30-26.pdf", "What is the net book value of the Ford F-150?", "V-302 · Ford F-150 pickup → NBV 9/30/26: $27,738.33"),
        ("Term Loan Amortization.pdf", "How much interest is in payment 37?", "Pmt # 37 → Payment: 48,623.15 | Interest: 5,699.45"),
        ("Prepaid Amortization FY2026.pdf", "How much is the Hartford Insurance amortization in Mar-26?", "Hartford Insurance → Mar-26: 4,500.00"),
        ("October Close Coverage.pdf", "What shift is Jonathan Alvarez working on October 14?", "Date 10/14 → Jonathan Alvarez: 7-4"),
        ("Sept Close Calendar.pdf", "Who owns the bank reconciliations?", "Bank reconciliations (all accounts) → Owner: Li Wei"),
    ],
)
def test_the_question_s_row_and_column_are_picked_out(texts, name, question, expected):
    assert expected in lookup(texts[name], question)


def test_a_question_over_a_column_gets_that_column_for_every_row(texts):
    found = lookup(texts["AP Aging 9-30-26.pdf"], "Which vendors have balances over 90 days?")
    assert "Evergreen Electric: $13,275.00; Johnson Controls: $2,115.00; Orion Software: $18,600.00" in found
    assert "Zero or blank: Acme Industrial Supply" in found
    largest = lookup(texts["Fixed Asset Schedule 9-30-26.pdf"], "Which asset has the largest 2026 YTD depreciation?")
    assert '"2026 YTD Depr." for every row (page 1), largest first: B-100 · Elk Grove warehouse: $47,115.38;' in largest


def test_nothing_is_picked_when_the_question_names_no_row_or_column(texts):
    assert lookup(texts["AP Aging 9-30-26.pdf"], "Summarize this for me") == ""
    assert lookup(texts["AP Aging 9-30-26.pdf"], "Is this the latest aging?") == ""


def _schedule_email(store, settings, name: str):
    from email.message import EmailMessage

    from controller_inbox.folder_mail import ingest_folder

    settings.ensure_data_dir()
    msg = EmailMessage()
    msg["From"] = "Maya Chen <maya@taz.com>"
    msg["To"] = "controller@taz.com"
    msg["Subject"] = name.removesuffix(".pdf")
    msg["Date"] = "Mon, 05 Oct 2026 09:00:00 -0500"
    msg["Message-ID"] = "<schedule-1@taz.com>"
    msg.set_content("Hi,\n\nThe schedule is attached for your review.\n\nMaya")
    msg.add_attachment((FIXTURES / name).read_bytes(), maintype="application", subtype="pdf", filename=name)
    (settings.inbox_incoming / "schedule.eml").write_bytes(bytes(msg))
    return ingest_folder(store, settings)[0]


def test_the_model_starts_from_the_rows_the_question_names(store, settings):
    from controller_inbox import agent

    email = _schedule_email(store, settings, "AP Aging 9-30-26.pdf")
    ws = agent.Workspace(store, settings, [email], question="How much do we owe Harbor Steel in the 31-60 day bucket?", current_id=email.id)
    block = agent.file_context(ws, ws.question, 12_000)[email.id]
    rows_at = block.index("Rows that match the question")
    assert "Harbor Steel LLC → 31 - 60 Days: $22,150.00" in block
    assert rows_at < block.index("[AP Aging 9-30-26.pdf · page 1"), "the matching rows come before the file text"
    assert any("picked out the table rows" in read for read in ws.reads)


def test_without_a_model_the_lookup_answer_shows_the_row(store, settings):
    from controller_inbox.assistant import answer_stream

    email = _schedule_email(store, settings, "AP Aging 9-30-26.pdf")
    events = list(answer_stream(store, settings, "How much do we owe Harbor Steel in the 31-60 day bucket?", email_id=email.id))
    text = "".join(event.get("text", "") for event in events if event["type"] == "delta")
    assert "From the table in AP Aging 9-30-26.pdf:" in text
    assert "Harbor Steel LLC → 31 - 60 Days: $22,150.00" in text


def test_a_workbook_row_is_picked_out_with_its_cell():
    import io

    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "Aging"
    sheet.append(["AP aging as of September 30"])
    sheet.append([])
    sheet.append(["Vendor", "Current", "31 - 60 Days", "Over 90 Days"])
    sheet.append(["Acme Industrial Supply", 24310.5, 0, 0])
    sheet.append(["Harbor Steel LLC", 48500, 22150, 0])
    sheet.append(["Orion Software", 0, 0, 18600])
    out = io.BytesIO()
    book.save(out)
    text = extract_document("aging.xlsx", "", out.getvalue())
    found = lookup(text, "How much do we owe Harbor Steel in the 31-60 day bucket?")
    assert "Harbor Steel LLC → C5 (31 - 60 Days): 22,150" in found
    over = lookup(text, "Which vendors are over 90 days?")
    assert "Orion Software: 18,600 (D6)" in over and "Zero or blank: Acme Industrial Supply, Harbor Steel LLC" in over
