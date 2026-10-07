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
    assert "A1: Line | B1: Q3 | C1: Q4 | D1: Change" in text
    assert "A2 (Line): Ads | B2 (Q3): 1,000 | C2 (Q4): 1,500 | D2 (Change): =C2-B2" in text
    assert "B4 (Q3): =SUM(B2:B3)" in text
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
    assert text.startswith("[page 1]\n") and "Northwind Traders - Invoice INV-2231" in text
    assert "Item | Qty | Amount\nItem: Consulting | Qty: 10 | Amount: $4,000.00\nItem: Total | Qty: not listed | Amount: $4,350.00" in text
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
    assert lines.index("Agency | $6,000") < lines.index("Next steps come after the table.")
    assert "- Priya: Has finance approved this?" in lines


def test_csv_reads_like_a_sheet():
    text = extract_text_from_bytes("vendors.csv", "text/csv", "Vendor;Amount\nAcme;100\nGlobex;250\n".encode())
    assert text.splitlines() == [
        '[sheet "vendors.csv" A1:B3]', "A1: Vendor | B1: Amount", "A2 (Vendor): Acme | B2 (Amount): 100", "A3 (Vendor): Globex | B3 (Amount): 250"
    ]
    pairs = extract_text_from_bytes("terms.csv", "text/csv", b"Amount due,12480.00\nPaid,0\n").splitlines()
    assert pairs[1:] == ["A1: Amount due | B1: 12480.00", "A2: Paid | B2: 0"], "a list of values isn't a header"


def test_a_file_called_a_workbook_is_read_as_what_it_is():
    # Windows sends every .csv as application/vnd.ms-excel; only a real 97-2003 workbook goes to xlrd.
    data = b'Vendor,Invoice,Amount\nAcme,INV-1001,"1,250.00"\nGlobex,INV-1002,980.00\n'
    lines = extract_text_from_bytes("payments.csv", "application/vnd.ms-excel", data).splitlines()
    assert lines[0] == '[sheet "payments.csv" A1:C3]'
    assert lines[2] == "A2 (Vendor): Acme | B2 (Invoice): INV-1001 | C2 (Amount): 1,250.00"
    page = (
        b'<html xmlns:o="urn:schemas-microsoft-com:office:office"><head><style>td {color: red}</style></head><body>'
        b"<table><tr><th>Vendor</th><th>Invoice</th><th>Amount</th></tr>"
        b"<tr><td>Acme &amp; Sons</td><td>INV-1001</td><td>1,250.00</td></tr>"
        b"<tr><td colspan=2>Total</td><td>1,250.00</td></tr></table></body></html>"
    )
    lines = extract_text_from_bytes("export.xls", "application/vnd.ms-excel", page).splitlines()
    assert lines[2:] == [
        "A2 (Vendor): Acme & Sons | B2 (Invoice): INV-1001 | C2 (Amount): 1,250.00",
        "A3 (Vendor): Total | B3 (Invoice): not listed | C3 (Amount): 1,250.00",
    ]
    xml = (
        b'<?xml version="1.0"?><Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" '
        b'xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"><Worksheet ss:Name="AP"><Table>'
        b'<Row><Cell><Data ss:Type="String">Vendor</Data></Cell><Cell><Data ss:Type="String">Invoice</Data></Cell>'
        b'<Cell><Data ss:Type="String">Amount</Data></Cell></Row>'
        b'<Row><Cell><Data ss:Type="String">Acme</Data></Cell><Cell ss:Index="3"><Data ss:Type="Number">1250</Data></Cell></Row>'
        b"</Table></Worksheet></Workbook>"
    )
    assert "A2 (Vendor): Acme | B2 (Invoice): not listed | C2 (Amount): 1250" in extract_text_from_bytes("export.xls", "", xml)
    renamed = extract_text_from_bytes("budget.xls", "application/vnd.ms-excel", _budget())
    assert "A2 (Line): Ads | B2 (Q3): 1,000" in renamed


def test_a_csv_of_names_only_still_names_its_columns():
    data = "Employee,Department,Manager\nJonathan Reyes,,Priya Raman\nLi Wei,Finance,Dana Cole\n"
    lines = extract_text_from_bytes("staff.csv", "text/csv", data.encode()).splitlines()
    assert lines[2] == "A2 (Employee): Jonathan Reyes | B2 (Department): not listed | C2 (Manager): Priya Raman"


def test_csv_rows_under_a_header_name_their_columns_and_blanks():
    data = "Employee,Department,Manager,Salary\nMaya Chen,Finance,Priya Raman,98500\nJonathan Alvarez,,Priya Raman,112000\n"
    lines = extract_text_from_bytes("staff.csv", "text/csv", data.encode()).splitlines()
    assert lines[1] == "A1: Employee | B1: Department | C1: Manager | D1: Salary"
    assert lines[3] == "A3 (Employee): Jonathan Alvarez | B3 (Department): not listed | C3 (Manager): Priya Raman | D3 (Salary): 112000"


def _roster_book() -> bytes:
    from openpyxl.styles import Font

    book = Workbook()
    sheet = book.active
    sheet.title = "Staff"
    sheet["A1"] = "Staff roster, September"
    for column, label in enumerate(["Employee", "ID", "Department", "Manager", "Notes"], start=1):
        sheet.cell(3, column, label).font = Font(bold=True)
    sheet.append(["Maya Chen", "E-1001", "Finance", "Priya Raman", "Hybrid"])
    sheet.append(["Jonathan Alvarez", "E-1002", None, "Priya Raman"])
    sheet.append(["Sofia Rossi", "E-1003", "Operations"])
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def test_workbook_rows_under_a_bold_header_name_their_columns():
    lines = extract_text_from_bytes("roster.xlsx", "", _roster_book()).splitlines()
    assert "A1: Staff roster, September" in lines
    assert "A3: Employee | B3: ID | C3: Department | D3: Manager | E3: Notes" in lines
    assert "A5 (Employee): Jonathan Alvarez | B5 (ID): E-1002 | C5 (Department): not listed | D5 (Manager): Priya Raman" in lines
    assert "A6 (Employee): Sofia Rossi | B6 (ID): E-1003 | C6 (Department): Operations" in lines


def test_labelled_sheet_parts_and_citations_still_find_their_cells():
    from controller_inbox.answer_check import cells_of

    text = extract_text_from_bytes("roster.xlsx", "", _roster_book())
    cells = cells_of(text)
    assert cells[("Staff", "C4")] == "Finance" and cells[("Staff", "C5")] == "not listed"
    rows = "\n".join(f"A{r} (Employee): Person {r} | B{r} (Notes): " + "x" * 40 for r in range(2, 400))
    parts = split_parts('[sheet "Staff" A1:B400]\nA1: Employee | B1: Notes\n' + rows, size=2000)
    assert parts[1].label.startswith('sheet "Staff" rows ')


def test_a_citation_finds_its_section_and_cell():
    from controller_inbox.documents import locate

    parts = split_parts(extract_text_from_bytes("roster.xlsx", "", _roster_book()) + "\n")
    assert locate(parts, "Staff!C5") == (1, "A5 (Employee): Jonathan Alvarez | B5 (ID): E-1002 | C5 (Department): not listed | D5 (Manager): Priya Raman")
    assert locate(parts, "c5")[0] == 1
    assert locate(parts, 'sheet "Staff"') == (1, "")
    pages = split_parts("[page 1]\nIntro\n[page 2]\nTerms\n[page 10]\nAppendix")
    assert locate(pages, "page 1") == (0, "") and locate(pages, "Page 10") == (2, "")
    assert locate(pages, "page 4") is None and locate(pages, "") is None


def _text_box(*lines: str):
    """A run holding a text box the way Word 2010 and later save it: as a drawing, then again as VML for older Word."""
    from docx.oxml import parse_xml

    box = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in lines)
    return parse_xml(
        '<w:r xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
        'xmlns:v="urn:schemas-microsoft-com:vml"><mc:AlternateContent>'
        '<mc:Choice Requires="wps"><w:drawing><wp:anchor><a:graphic><a:graphicData><wps:wsp><wps:txbx>'
        f"<w:txbxContent>{box}</w:txbxContent></wps:txbx></wps:wsp></a:graphicData></a:graphic></wp:anchor></w:drawing></mc:Choice>"
        f"<mc:Fallback><w:pict><v:shape><v:textbox><w:txbxContent>{box}</w:txbxContent></v:textbox></v:shape></w:pict></mc:Fallback>"
        "</mc:AlternateContent></w:r>"
    )


def test_a_word_text_box_is_read_once_a_line_at_a_time():
    document = Document()
    document.add_paragraph("Invoice INV-2001")
    document.add_paragraph("See box").runs[0]._r.addnext(_text_box("Bill To:", "Acme Corp", "Amount due: $4,500.00"))
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Remit to"
    table.cell(0, 1).paragraphs[0]._p.append(_text_box("First Bank", "Account ending 4471"))
    out = io.BytesIO()
    document.save(out)
    text = extract_text_from_bytes("invoice.docx", "", out.getvalue())
    assert "See box\nBill To:\nAcme Corp\nAmount due: $4,500.00\n" in text
    assert text.count("Acme Corp") == 1 and text.count("First Bank") == 1
    assert "Remit to | First Bank Account ending 4471" in text


def test_word_table_with_a_header_row_merged_cells_and_blanks():
    document = Document()
    table = document.add_table(rows=6, cols=4)
    for column, label in enumerate(["Employee", "Department", "Manager", "Start date"]):
        table.cell(0, column).paragraphs[0].add_run(label).bold = True
    table.cell(1, 0).text, table.cell(1, 1).text, table.cell(1, 2).text, table.cell(1, 3).text = "Maya Chen", "Finance", "Priya Raman", "2021-03-01"
    table.cell(2, 0).text, table.cell(2, 3).text = "Jonathan Alvarez", "2019-07-15"
    table.cell(3, 0).text, table.cell(3, 1).text = "Sofia Rossi", "Operations"
    table.cell(2, 2).merge(table.cell(3, 2)).paragraphs[0].text = "Priya Raman"
    table.cell(4, 0).merge(table.cell(4, 3)).text = "Contractors"
    table.cell(5, 0).text, table.cell(5, 1).text, table.cell(5, 3).text = "Omar Haddad", "Finance", "2026-01-05"
    document.add_paragraph("After the table.")
    out = io.BytesIO()
    document.save(out)
    lines = extract_text_from_bytes("staff.docx", "", out.getvalue()).splitlines()
    assert lines[0] == "Employee | Department | Manager | Start date"
    assert "Employee: Jonathan Alvarez | Department: not listed | Manager: Priya Raman | Start date: 2019-07-15" in lines
    assert "Employee: Sofia Rossi | Department: Operations | Manager: Priya Raman | Start date: not listed" in lines
    assert lines.index("Contractors") == lines.index("Employee: Omar Haddad | Department: Finance | Manager: not listed | Start date: 2026-01-05") - 1
    assert lines[-1] == "After the table."


def test_powerpoint_table_rows_name_their_columns():
    from pptx import Presentation
    from pptx.util import Inches

    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[5])
    slide.shapes.title.text = "Headcount"
    shape = slide.shapes.add_table(3, 3, Inches(1), Inches(2), Inches(6), Inches(2))
    for (r, c), value in {(0, 0): "Team", (0, 1): "Lead", (0, 2): "Open roles", (1, 0): "Finance", (1, 1): "Priya Raman", (1, 2): "2", (2, 0): "Sales", (2, 2): "1"}.items():
        shape.table.cell(r, c).text = value
    out = io.BytesIO()
    deck.save(out)
    text = extract_text_from_bytes("headcount.pptx", "", out.getvalue())
    assert "Team | Lead | Open roles\nTeam: Finance | Lead: Priya Raman | Open roles: 2\nTeam: Sales | Lead: not listed | Open roles: 1" in text


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


def _rewritten(data: bytes, member: str, change) -> bytes:
    """An Office file (a zip) with one part's XML changed."""
    import zipfile

    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(out, "w") as target:
        for item in source.infolist():
            body = source.read(item)
            target.writestr(item, change(body) if item.filename == member else body)
    return out.getvalue()


def test_a_sheet_that_understates_its_size_is_read_to_its_last_row():
    import re

    book = Workbook()
    sheet = book.active
    sheet.title = "AP"
    sheet.append(["Vendor", "Invoice", "Amount"])
    for n in range(5):
        sheet.append([f"Vendor {n}", f"INV-{100 + n}", 1000.5 + n])
    out = io.BytesIO()
    book.save(out)
    # Writers other than Excel often store <dimension ref="A1"/> whatever the sheet holds.
    data = _rewritten(out.getvalue(), "xl/worksheets/sheet1.xml", lambda xml: re.sub(rb'<dimension ref="[^"]*"/>', b'<dimension ref="A1"/>', xml))
    text = extract_text_from_bytes("ap.xlsx", "", data)
    assert '[sheet "AP" A1:C6]\n(6 rows with data)' in text
    assert "A6 (Vendor): Vendor 4 | B6 (Invoice): INV-104 | C6 (Amount): 1,004.5" in text
    unsized = _rewritten(out.getvalue(), "xl/worksheets/sheet1.xml", lambda xml: re.sub(rb'<dimension ref="[^"]*"/>', b"", xml))
    assert '[sheet "AP" A1:C6]' in extract_text_from_bytes("ap.xlsx", "", unsized)


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


def test_cells_on_one_baseline_stay_on_one_line():
    from controller_inbox.ocr import _rows

    # Tops differ by a few pixels, the way a scan reads a table cell and its label.
    hits = [
        (97, 697, "2023"),
        (103, 61, "（in millions except per share data）"),
        (113, 56, "GAAP basis(1):"),
        (631, 56, "Book value per share (3)"),
        (636, 722, "259.34"),
        (636, 1029, "259.34"),
        (638, 659, "$"),
        (660, 657, "$"),
        (661, 742, "5.00"),
        (661, 900, "4.88"),
        (661, 1041, "15.00"),
        (661, 1200, "14.64"),
        (662, 60, "Cash dividends declared and paid per share"),
    ]
    lines = _rows(hits)
    book = next(line for line in lines if "Book value" in line)
    dividends = next(line for line in lines if "Cash dividends" in line)
    assert book.index("Book value") < book.index("259.34")
    assert "5.00" not in book and "4.88" not in book
    assert dividends.index("Cash dividends") < dividends.index("5.00")
    assert "5.00" in dividends and "4.88" in dividends and "15.00" in dividends and "14.64" in dividends
    assert all("GAAP" not in line or "in millions" not in line for line in lines)


def test_wide_gaps_and_glued_words_become_readable():
    from controller_inbox.ocr import _open_gaps, _polish

    def boxes(chars: str, gaps: list[float]) -> list:
        x = 0.0
        made = []
        for index, _char in enumerate(chars):
            if index:
                x += 8 + gaps[index - 1]
            made.append([[x, 0], [x + 8, 0], [x + 8, 14], [x, 14]])
        return made

    assert _open_gaps(list("YourCityAZ12345"), boxes("YourCityAZ12345", [1, 1, 1, 9, 1, 1, 1, 10, 1, 10, 1, 1, 1, 1])) == "Your City AZ 12345"
    assert _open_gaps(list("ACC #12341234"), boxes("ACC #12341234", [0, 0, 0, 0, 5, 0, 0, 1, 5, 0, 1, 0])) == "ACC # 1234 1234"
    assert _open_gaps(list("OrderNumber"), boxes("OrderNumber", [0, 1, 0, 0, 5, 5, 0, 1, 0, 1])) == "OrderNumber"
    assert _polish("OrderNumber") == "Order Number"
    assert _polish("Totalrevenue") == "Total revenue"
    assert _polish("Thisisasampledescription..") == "This is a sample description..."
    assert _polish("Cashdividendsdeclaredandpaidpershare") == "Cash dividends declared and paid per share"
    assert _polish("INV-3337") == "INV-3337"
    assert _polish("within 30days from date of invoice.Late payment of5%") == "within 30 days from date of invoice. Late payment of 5%"
    assert _polish("income(expense),less net") == "income (expense), less net"
    assert _polish("EXECUTIVESUMMARY") == "EXECUTIVE SUMMARY"
    assert _polish("September 30,2023") == "September 30, 2023"
    from controller_inbox.ocr import _already
    assert not _already(
        "Three Months Ended September 30, 2023 Compared with Three Months Ended September 30, 2022",
        ["threemonthsended"],
    )
    assert _already("Total revenue", ["totalrevenue"])
    assert _polish("$12,480.00") == "$12,480.00"
    assert _polish("（in millions except per share data）") == "(in millions except per share data)"
    assert _polish("28.412,414") == "28,412,414"
    assert _polish("(1.097.978)") == "(1,097,978)"
    assert _polish("7.792") == "7,792"
    assert _polish("0.61") == "0.61"
    assert _polish("$12,480.00") == "$12,480.00"
    assert _polish("JS'O00") == "JS'000"
    # Names are not in any word list: a capital marks where one starts, and two unknown capitalised pieces stay one name.
    assert _polish("NetincomeattributabletoNorthwind") == "Net income attributable to Northwind"
    assert _polish("TotalBlackRockstockholders'equity") == "Total BlackRock stockholders'equity"
    assert _polish("NetProfitattributableto:") == "Net Profit attributable to:"
    assert _polish("PayPal") == "PayPal" and _polish("McDonald") == "McDonald" and _polish("Chartwell") == "Chartwell"
    from controller_inbox.ocr import _rows
    assert _rows([(10, 1, "From continuing"), (12, 700, "0.61"), (11, 800, "S"), (12, 880, "0.78")]) == [
        "From continuing 0.61 $ 0.78"
    ]
    from controller_inbox.ocr import _footnote_marker

    assert _footnote_marker(["(1)", "(2) As adjusted items are described in more detail"]) == "(1)"
    assert _footnote_marker(["(2) As adjusted items are described"]) == "(2)"
    invoices = list("InvoicesI ")
    gaps = [0] * (len(invoices) - 1)
    gaps[7] = 7  # the gap before the bar that was read as I
    assert "Invoices |" in _open_gaps(invoices, boxes(invoices, gaps))
    word = list(" Invoices")
    assert "I" in _open_gaps(word, boxes(word, [0] * (len(word) - 1)))


def test_a_missing_cell_stays_in_its_own_column():
    """One amount on a row must not slide into the other year's column."""
    from controller_inbox.ocr import _rows

    def cell(top, left, right, text):
        return (top, left, right, text)

    hits = [cell(30, 150, 200, "2018"), cell(30, 270, 320, "2019")]
    rows = [
        ("Revenue", "24,544,049", "28,412,414"),
        ("Cost of sales", "(15,421,144)", "(17,878,208)"),
        ("Gross Profit", "9,122,905", "10,534,206"),
        ("Share of results of associates", "", "7,792"),
        ("Profit from discontinued operations", "41,555", ""),
    ]
    for index, (label, left, right) in enumerate(rows):
        top = 80 + index * 24
        hits.append(cell(top, 10, 140, label))
        if left:
            hits.append(cell(top, 150, 200, left))
        if right:
            hits.append(cell(top, 270, 320, right))
    # A different shape: four columns, and a sentence that is not a row of the table.
    hits.append(cell(400, 10, 500, "The audit found no exceptions in the sample."))
    lines = _rows(hits)
    text = "\n".join(lines)
    assert "2018: 24,544,049" in text and "2019: 28,412,414" in text
    assert "2018: not listed | 2019: 7,792" in text
    assert "2018: 41,555 | 2019: not listed" in text
    assert "The audit found no exceptions in the sample." in text

    wide = []
    for index, values in enumerate(
        (
            ("4,522", "4,311", "13,228", "13,536"),
            ("2,885", "2,785", "8,538", "8,578"),
            ("1,637", "1,526", "4,690", "4,958"),
            ("", "", "15.00", "14.64"),
        )
    ):
        top = 80 + index * 22
        wide.append(cell(top, 10, 80, f"Row {index}"))
        for number, value in enumerate(values):
            if value:
                left = 200 + number * 120
                wide.append(cell(top, left, left + 50, value))
    short = next(line for line in _rows(wide) if "Row 3" in line)
    assert "15.00" in short and "14.64" in short and "not listed" in short
    assert short.index("not listed") < short.index("15.00")


def test_scanned_invoice_lines_and_statement_dashes_keep_their_columns():
    from controller_inbox.ocr import _rows

    def cell(top, left, right, text):
        return (top, left, right, text)

    invoice = [cell(20, 10, 120, "Description"), cell(20, 300, 330, "Qty"), cell(20, 400, 480, "Unit Price"), cell(20, 560, 620, "Amount")]
    for index, (item, qty, price, amount) in enumerate(
        [("Paper A4", "10", "4.50", "45.00"), ("Toner", "2", "80.00", "160.00"), ("Shipping", "1", "12.00", "12.00"), ("Discount", "", "", "(5.00)")]
    ):
        top = 60 + index * 24
        invoice.append(cell(top, 10, 150, item))
        if qty:
            invoice.append(cell(top, 315, 330, qty))
        if price:
            invoice.append(cell(top, 440, 480, price))
        invoice.append(cell(top, 570, 620, amount))
    text = "\n".join(_rows(invoice))
    assert "Toner | Qty: 2 | Unit Price: 80.00 | Amount: 160.00" in text
    assert "Discount | Qty: not listed | Unit Price: not listed | Amount: (5.00)" in text

    # A dash or N/A printed in a column is that column's entry, and a reference beside the date stays on the row.
    statement = [cell(10, 240, 300, "Debit"), cell(10, 370, 420, "Credit")]
    entries = [
        ("01/03/2024", "INV-1001", "1,200.00", "-"),
        ("01/09/2024", "PMT-77", "-", "800.00"),
        ("01/20/2024", "INV-1002", "450.00", "-"),
        ("01/28/2024", "CRN-4", "N/A", "50.00"),
        ("02/02/2024", "INV-1003", "75.00", "20.00"),
    ]
    for index, (date, reference, debit, credit) in enumerate(entries):
        top = 40 + index * 22
        statement += [cell(top, 10, 90, date), cell(top, 120, 190, reference)]
        statement += [cell(top, 300 - 6 * len(debit), 300, debit), cell(top, 420 - 6 * len(credit), 420, credit)]
    text = "\n".join(_rows(statement))
    assert "01/03/2024 INV-1001 | Debit: 1,200.00 | Credit: -" in text
    assert "01/09/2024 PMT-77 | Debit: - | Credit: 800.00" in text
    assert "CRN-4 | Debit: N/A | Credit: 50.00" in text


def test_ocr_puts_back_dropped_spaces_without_splitting_codes_or_times():
    from controller_inbox.ocr import _spaced

    assert _spaced("Duedate:15October2026") == "Duedate: 15 October 2026"
    assert _spaced("Due date: 150ctober 2026") == "Due date: 15 October 2026"
    assert _spaced("Order 200ct2026, Due10ctober") == "Order 20 Oct 2026, Due 1 October"
    for kept in ("INVOICE4471", "INV-4471 Q4 FY26", "Meeting at 10:30", "Total:$12,480.00", "Box of 500ct", "Paid $100ct 2026"):
        assert _spaced(kept) == kept
