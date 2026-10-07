"""PDF files that are a little broken or drawn an unusual way still read page for page."""

from controller_inbox.documents import pdf_text
from controller_inbox.extract import extract_text_from_bytes
from pdffactory import Text, build_pdf, sheet_rows


def _pages(text: str) -> list[str]:
    return [part.split("\n", 1)[1] if "\n" in part else "" for part in text.split("[page ")[1:]]


def _three_pages() -> bytes:
    return build_pdf([[Text(40, 700, f"Statement page {n} lists invoice INV-{n}00 for Northwind")] for n in (1, 2, 3)])


def test_a_page_missing_its_type_keeps_every_page_on_its_own_text():
    data = _three_pages().replace(b"<< /Type /Page /Parent", b"<< /Parent", 1)
    pages = _pages(pdf_text(data))
    assert [page.strip() for page in pages] == [f"Statement page {n} lists invoice INV-{n}00 for Northwind" for n in (1, 2, 3)]


def test_a_page_listed_twice_reads_the_same_page_twice():
    data = _three_pages().replace(b"/Kids [3 0 R 5 0 R 7 0 R] /Count 3", b"/Kids [3 0 R 3 0 R 5 0 R 7 0 R] /Count 4")
    pages = [page.strip() for page in _pages(pdf_text(data))]
    assert pages[0] == pages[1] and "INV-100" in pages[1]
    assert "INV-200" in pages[2] and "INV-300" in pages[3]


def test_a_file_cut_short_before_its_end_marker_is_still_read():
    data = _three_pages()
    data = data[: data.rindex(b"%%EOF")]
    text = pdf_text(data)
    assert "INV-100" in text and "INV-300" in text


def test_a_pdf_with_no_pages_says_so_instead_of_showing_its_source():
    data = _three_pages().replace(b"/Kids [3 0 R 5 0 R 7 0 R] /Count 3", b"/Kids [] /Count 0")
    text = extract_text_from_bytes("statement.pdf", "application/pdf", data)
    assert text == "[This PDF has no pages.]"


def test_pdfminer_layout_analysis_is_not_run(monkeypatch):
    # Grouping characters into text boxes is most of pdfminer's time on a big schedule, and the reader
    # works from the characters' positions itself.
    from pdfminer import layout

    def refuse(*_args, **_kwargs):
        raise AssertionError("layout analysis ran")

    monkeypatch.setattr(layout.LTLayoutContainer, "analyze", refuse)
    rows = [["Account", "Owner", "Amount"], ["GL 6000", "Finance", "1,200"], ["GL 6010", "Payroll", "880"], ["GL 6020", "Sales", "450"]]
    text = pdf_text(build_pdf([sheet_rows([(40, "left"), (200, "left"), (400, "right")], rows)]))
    assert "Account: GL 6010 | Owner: Payroll | Amount: 880" in text


def test_letters_doubled_in_a_condensed_font_are_kept():
    text = pdf_text(build_pdf([[Text(40, 700, "Allowance for doubtful accounts billed to Williams", scale=85)]]))
    assert "Allowance for doubtful accounts billed to Williams" in text


def test_text_drawn_twice_for_bold_in_a_condensed_font_is_read_once():
    text = pdf_text(build_pdf([[Text(40, 700, "Remittance advice", scale=85, twice=True), Text(40, 680, "Paid in full on 3 October.")]]))
    assert "Remittance advice" in text.splitlines()[1]


def test_a_sheet_printed_sideways_without_turning_the_page_reads_row_by_row():
    rows = [["Account", "2025", "2024"], ["Revenue", "12,500", "11,900"], ["Cost of sales", "7,100", "6,800"],
            ["Operating expenses", "3,250", ""], ["Net income", "2,150", "2,000"]]
    items = []
    for r, row in enumerate(rows):
        for offset, cell in zip((0, 250, 400), row):
            if cell:
                # Each row is a line running up the page; the next row is to its right.
                items.append(Text(100 + r * 16, 100 + offset, cell, bold=r == 0, turn=90))
    lines = _pages(pdf_text(build_pdf([items])))[0].splitlines()
    assert "Account | 2025 | 2024" in lines
    assert "Account: Operating expenses | 2025: 3,250 | 2024: not listed" in lines
    assert "Account: Net income | 2025: 2,150 | 2024: 2,000" in lines


def test_a_different_table_in_the_same_place_on_the_next_page_is_not_joined_to_the_first():
    columns = [(40, "left"), (250, "left"), (450, "right")]
    vendors = [["Vendor", "Terms", "Amount"]] + [[f"Vendor {name}", "Net 30", f"{1000 + i * 111:,}.00"] for i, name in enumerate("ABCDEF")]
    claims = [["Employee", "Department", "Expense claim"]] + [[f"Employee {i + 1}", "Sales", f"{200 + i * 7:,}.00"] for i in range(6)]
    second = _pages(pdf_text(build_pdf([sheet_rows(columns, vendors), sheet_rows(columns, claims)])))[1]
    assert "Vendor" not in second and "continue the table" not in second
    assert "Employee: Employee 2 | Department: Sales | Expense claim: 207.00" in second
