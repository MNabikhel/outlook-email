"""Attachments come out as text a model can navigate: pages, sheets with cell references, slides."""

from __future__ import annotations

import io

import pytest
from docx import Document
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from openpyxl import Workbook

from controller_inbox.demo import make_pdf
from controller_inbox.documents import (
    Part,
    compare_columns,
    outline,
    pdf_text,
    read_cells,
    read_part,
    search_parts,
    skim,
    split_parts,
    trace_cell,
)
from controller_inbox.extract import extract_text_from_bytes


def _budget() -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = "Budget"
    sheet.append(["Line", "Q3", "Q4", "Change"])
    sheet.append(["Ads", 1000, 1500, "=C2-B2"])
    sheet.append(["Travel", 250.5, 300, "=C3-B3"])
    sheet.append(["Total", "=SUM(B2:B3)", "=SUM(C2:C3)", "=D2+D3"])
    notes = book.create_sheet("Assumptions")
    notes["A1"] = "Growth"
    notes["B1"] = 0.1
    notes.sheet_state = "hidden"
    sheet["E4"] = "=D4*(1+Assumptions!B1)"
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def test_workbook_keeps_cell_references_formulas_and_hidden_sheets():
    text = extract_text_from_bytes("budget.xlsx", "", _budget())
    assert 'Workbook with 2 sheets: "Budget", "Assumptions" (hidden)' in text
    assert '[sheet "Budget" A1:E4]' in text
    assert "A2: Ads | B2: 1,000 | C2: 1,500 | D2: =C2-B2" in text
    assert "B4: =SUM(B2:B3)" in text
    assert "(4 rows with data, 6 formulas)" in text
    assert '[sheet "Assumptions" A1:B1 hidden]' in text


def test_a_formula_can_be_traced_back_to_its_inputs_across_sheets():
    trace = trace_cell(_budget(), "budget", "E4")
    assert trace.splitlines()[0] == "Budget!E4 ← =D4*(1+Assumptions!B1)"
    assert "  Budget!D4 ← =D2+D3" in trace
    assert "  Assumptions!B1 = 0.1" in trace
    assert "    Budget!D2 ← =C2-B2" in trace


def test_reading_a_range_shows_values_and_formulas():
    cells = read_cells(_budget(), "Budget", "A3:D4")
    assert cells.splitlines() == [
        'Sheet "Budget" A3:D4',
        "A3: Travel | B3: 250.5 | C3: 300 | D3: =C3-B3",
        "A4: Total | B4: =SUM(B2:B3) | C4: =SUM(C2:C3) | D4: =D2+D3",
    ]
    assert "No sheet called 'Forecast'" in read_cells(_budget(), "Forecast", "A1:B2")
    assert "isn't a cell range" in read_cells(_budget(), "Budget", "rows please")


def test_pdf_pages_and_table_columns():
    data = make_pdf(
        [
            ["Northwind Traders - Invoice INV-2231", ["Item", "Qty", "Amount"], ["Consulting", "10", "$4,000.00"], ["Total", "", "$4,350.00"]],
            ["Terms: net 30. Remit to Northwind."],
        ]
    )
    text = extract_text_from_bytes("inv.pdf", "application/pdf", data)
    assert text.startswith("[page 1]\nNorthwind Traders - Invoice INV-2231")
    assert "Item | Qty | Amount" in text and "Consulting | 10 | $4,000.00" in text
    assert "[page 2]\nTerms: net 30" in text
    assert read_part(text, "page 2").text.startswith("Terms: net 30")


def test_a_scanned_pdf_says_so_instead_of_looking_empty():
    text = extract_text_from_bytes("scan.pdf", "application/pdf", make_pdf([[], []]))
    assert text.startswith("[This PDF looks scanned")
    assert "[page 2]\n(no text on this page)" in text


def test_word_draft_in_order_with_tracked_changes_and_comments():
    document = Document()
    document.add_heading("Q4 Marketing Plan (DRAFT)", 1)
    paragraph = document.add_paragraph("We propose raising the ads budget to ")
    inserted = OxmlElement("w:ins")
    inserted.set(qn("w:author"), "Maya")
    run = OxmlElement("w:r")
    text = OxmlElement("w:t")
    text.text = "$18,000"
    run.append(text)
    inserted.append(run)
    paragraph._p.append(inserted)
    deleted = OxmlElement("w:del")
    deleted.set(qn("w:author"), "Maya")
    old = OxmlElement("w:r")
    old_text = OxmlElement("w:delText")
    old_text.text = "$12,000"
    old.append(old_text)
    deleted.append(old)
    paragraph._p.append(deleted)
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "Item", "Cost"
    table.cell(1, 0).text, table.cell(1, 1).text = "Agency", "$6,000"
    document.add_paragraph("Next steps come after the table.")
    document.add_comment(document.paragraphs[1].runs, text="Has finance approved this?", author="Priya")
    out = io.BytesIO()
    document.save(out)
    result = extract_text_from_bytes("plan.docx", "", out.getvalue())
    lines = result.splitlines()
    assert lines[0].startswith("[This draft has tracked changes")
    assert "# Q4 Marketing Plan (DRAFT)" in lines
    assert "We propose raising the ads budget to $18,000[deleted: $12,000]" in lines
    assert lines.index("| Agency | $6,000 |") < lines.index("Next steps come after the table.")
    assert "- Priya: Has finance approved this?" in lines


def test_csv_reads_like_a_sheet():
    text = extract_text_from_bytes("vendors.csv", "text/csv", "Vendor;Amount\nAcme;100\nGlobex;250\n".encode())
    assert text.splitlines() == ['[sheet "vendors.csv" A1:B3]', "A1: Vendor | B1: Amount", "A2: Acme | B2: 100", "A3: Globex | B3: 250"]


def test_long_documents_split_into_labelled_parts_and_search_finds_the_right_one():
    pages = [[f"Section {n} filler text about the quarterly close." for _ in range(3)] for n in range(1, 6)]
    pages[3].append("The accrual for Globex consulting is $48,200 and needs sign-off.")
    text = extract_text_from_bytes("pack.pdf", "application/pdf", make_pdf(pages))
    assert [part.label for part in split_parts(text)] == [f"page {n}" for n in range(1, 6)]
    assert [part.label for part in search_parts(text, "Globex accrual")] == ["page 4"]
    assert outline(text)[0] == "page 1: Section 1 filler text about the quarterly close."


def test_a_skim_keeps_what_differs_from_page_to_page():
    pages = [[f"Audit pack page {n}"] + [f"Section {n}.{i}: area reviewed, no exceptions noted in the sample." for i in range(8)] for n in range(1, 21)]
    pages[16][3] = "FINDING 4 (HIGH): bank-detail changes approved without a call-back. Owner: AP lead."
    pages[19][2] = "Total sampled: $4,812,300; exceptions: $61,450."
    parts = split_parts(extract_text_from_bytes("audit.pdf", "application/pdf", make_pdf(pages)))
    text = skim(parts, 600, tag="audit.pdf · ")
    assert text == (
        "[audit.pdf · page 17]\nFINDING 4 (HIGH): bank-detail changes approved without a call-back. Owner: AP lead.\n"
        "[audit.pdf · page 20]\nTotal sampled: $4,812,300; exceptions: $61,450."
    )
    varied = [Part(f"page {n}", f"Heading {chr(64 + n)} topic\nDetail line for {chr(64 + n)} alone") for n in range(1, 9)]
    covered = skim(varied, 180)
    assert "[page 7]\nHeading G topic" in covered and "[page 8]" not in covered and "Detail line" not in covered
    assert len(covered) <= 180


def test_compare_columns_ranks_each_row_and_keeps_totals_apart():
    book = Workbook()
    sheet = book.active
    sheet.title = "Detail"
    sheet.append(["Dept", "Line", "Q3 actual", "Q4 budget"])
    for row in (["Mkt", "Trade show", 18000, 36500], ["Sales", "Travel", 31000, 29500], ["Ops", "Lease", 45000, 45000], ["", "Total", 94000, 111000]):
        sheet.append(row)
    out = io.BytesIO()
    book.save(out)
    by_header = compare_columns(out.getvalue(), "detail", "Q3", "Q4 budget")
    assert by_header.splitlines() == [
        'Sheet "Detail": C “Q3 actual” → D “Q4 budget”, 3 rows, biggest increase first',
        "Mkt · Trade show (row 2): 18,000 → 36,500, +18,500 (+102.8%)",
        "Ops · Lease (row 4): 45,000 → 45,000, +0 (+0.0%)",
        "Sales · Travel (row 3): 31,000 → 29,500, −1,500 (−4.8%)",
        "Biggest increase: Mkt · Trade show. Biggest decrease: Sales · Travel.",
        "Total rows:",
        "Total (row 5): 94,000 → 111,000, +17,000 (+18.1%)",
    ]
    assert compare_columns(out.getvalue(), "", "C", "D") == by_header
    assert compare_columns(out.getvalue(), "", "Q2", "Q4").startswith("No column called 'Q2'. Give column letters")
    assert "saved without values" in compare_columns(_budget(), "Budget", "Change", "D")


def test_big_sheets_are_cut_into_row_ranges():
    book = Workbook()
    sheet = book.active
    sheet.title = "Ledger"
    for n in range(1, 301):
        sheet.append([f"Line {n}", n * 10, f"note {n}"])
    out = io.BytesIO()
    book.save(out)
    text = extract_text_from_bytes("ledger.xlsx", "", out.getvalue())
    labels = [part.label for part in split_parts(text)]
    assert labels[0] == "start"
    assert labels[1].startswith('sheet "Ledger" rows 1–') and len(labels) > 3
    last = split_parts(text)[-1]
    assert "A300: Line 300" in last.text
    assert read_part(text, "Ledger").label == labels[1]


def _scanned_pdf(lines: list[str]) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    page = Image.new("L", (1240, 1754), 255)
    draw = ImageDraw.Draw(page)
    # Pillow's own font on every OS: it is set tight, like the scans that make OCR drop spaces.
    font = ImageFont.load_default(size=34)
    for n, line in enumerate(lines):
        draw.text((100, 150 + n * 70), line, fill=0, font=font)
    out = io.BytesIO()
    page.save(out, "PDF", resolution=150)
    return out.getvalue()


def test_scanned_pdf_pages_are_read_with_ocr():
    from controller_inbox import ocr

    if not ocr.engine_name():
        pytest.skip("no OCR engine installed")
    text = pdf_text(_scanned_pdf(["INVOICE 4471", "Northwind Supply Co.", "Amount due: $12,480.00", "Due date: 15 October 2026"]))
    assert text.startswith("[Scanned page 1 read with OCR")
    assert "$12,480.00" in text and "Northwind Supply Co." in text and "15 October 2026" in text


def test_scanned_pdf_without_ocr_says_how_to_add_it(monkeypatch):
    from controller_inbox import ocr

    monkeypatch.setattr(ocr, "engine_name", lambda: "")
    text = pdf_text(_scanned_pdf(["Amount due: $12,480.00"]))
    assert "looks scanned" in text and 'pip install -e ".[ocr]"' in text and "12,480" not in text


def test_ocr_puts_back_dropped_spaces_without_splitting_codes_or_times():
    from controller_inbox.ocr import _spaced

    assert _spaced("Duedate:15October2026") == "Duedate: 15 October 2026"
    assert _spaced("Due date: 150ctober 2026") == "Due date: 15 October 2026"
    assert _spaced("Order 200ct2026, Due10ctober") == "Order 20 Oct 2026, Due 1 October"
    for kept in ("INVOICE4471", "INV-4471 Q4 FY26", "Meeting at 10:30", "Total:$12,480.00", "Box of 500ct", "Paid $100ct 2026"):
        assert _spaced(kept) == kept
