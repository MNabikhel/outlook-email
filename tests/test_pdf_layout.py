import io

from pypdf import PdfReader

from controller_inbox.documents import _pdf_page, pdf_text
from pdffactory import Text, build_pdf, sheet_rows


def _page(*items: Text) -> str:
    text = pdf_text(build_pdf([list(items)]))
    return text.split("\n", 1)[1] if text.startswith("[page 1]") else text


ROSTER_COLUMNS = [(40, "left"), (150, "left"), (205, "left"), (290, "left"), (390, "left"), (520, "right")]
ROSTER = [
    ["Employee", "ID", "Department", "Manager", "Start date", "Salary"],
    ["Maya Chen", "E-1001", "Finance", "Priya Raman", "2021-03-01", "$98,500"],
    ["Jonathan Alvarez", "E-1002", "", "Priya Raman", "2019-07-15", "$112,000"],
    ["Sofia Rossi", "E-1003", "Operations", "", "2022-11-30", "$87,250"],
    ["Liam O'Brien", "E-1004", "Sales", "Dana Kim", "", "$91,000"],
]


def test_kerned_letters_read_as_words_not_spaced_out_letters():
    data = build_pdf([[Text(72, 740, "Jonathan Alvarez", glyphs=True, kern=150), Text(72, 700, "Account holder details follow", size=10)]])
    assert "J o n a t h a n" in _pdf_page(PdfReader(io.BytesIO(data)).pages[0])
    text = pdf_text(data)
    assert "Jonathan Alvarez" in text and "J o n" not in text


def test_letter_spaced_and_placed_text_reads_as_words():
    text = _page(
        Text(72, 740, "PAYMENT HISTORY", size=12, bold=True, spacing=3),
        Text(72, 720, "Reviewer: Priya Raman", placed=True, spacing=1.2),
        Text(72, 700, "Account 5566778899 closed on 3 March 2026", size=9, placed=True),
    )
    assert text.splitlines() == ["PAYMENT HISTORY", "Reviewer: Priya Raman", "Account 5566778899 closed on 3 March 2026"]


def test_text_drawn_twice_for_bold_is_read_once():
    text = _page(Text(72, 740, "Remittance advice", twice=True), Text(72, 720, "Payment of $4,350.00 was sent to Northwind Traders today."))
    assert text.splitlines()[0] == "Remittance advice"


def test_a_sheet_saved_as_pdf_keeps_blank_cells_in_their_columns():
    text = _page(Text(40, 760, "Staff roster", size=14, bold=True), *sheet_rows(ROSTER_COLUMNS, ROSTER))
    lines = text.splitlines()
    assert lines[0] == "Staff roster"
    assert "Employee | ID | Department | Manager | Start date | Salary" in lines
    assert (
        "Employee: Jonathan Alvarez | ID: E-1002 | Department: not listed | Manager: Priya Raman | "
        "Start date: 2019-07-15 | Salary: $112,000"
    ) in lines
    assert "Employee: Sofia Rossi | ID: E-1003 | Department: Operations | Manager: not listed | Start date: 2022-11-30 | Salary: $87,250" in lines
    assert "Employee: Liam O'Brien | ID: E-1004 | Department: Sales | Manager: Dana Kim | Start date: not listed | Salary: $91,000" in lines


def test_a_table_without_a_header_still_marks_blank_cells():
    rows = [["North", "", "1,200"], ["South", "Closed", "900"], ["East", "Open", ""], ["West", "Open", "450"]]
    items = [Text(x, 700 - r * 15, value) for r, row in enumerate(rows) for x, value in zip((40, 200, 360), row) if value]
    assert _page(*items).splitlines() == ["North | (empty) | 1,200", "South | Closed | 900", "East | Open", "West | Open | 450"]


def test_a_wrapped_cell_stays_with_its_row():
    items = sheet_rows([(40, "left"), (200, "left"), (420, "right")], [["Vendor", "Note", "Amount"], ["Acme Ltd", "Paid late", "$1,200"]])
    items += [Text(40, 690, "Globex Corporation"), Text(200, 690, "Bank details changed"), Text(420 - 30, 690, "$880")]
    items += [Text(200, 682, "by phone on 2 Oct")]
    items += [Text(40, 667, "Initech"), Text(200, 667, "On time"), Text(420 - 30, 667, "$450")]
    lines = _page(*items).splitlines()
    assert "Vendor: Globex Corporation | Note: Bank details changed by phone on 2 Oct | Amount: $880" in lines
    assert not any(line.startswith("by phone") or "Note: by phone" in line for line in lines)


def test_a_wide_sheet_printed_across_two_pages_names_each_row():
    first = sheet_rows(ROSTER_COLUMNS, ROSTER)
    second = sheet_rows([(40, "left"), (160, "left")], [["Phone", "Notes"], ["555-0101", "Hybrid"], ["", "On leave"], ["555-0103", ""], ["555-0104", "Remote"]])
    text = pdf_text(build_pdf([first, second]))
    page_two = text.split("[page 2]\n", 1)[1]
    assert "each row starts with its Employee" in page_two
    assert "Employee: Jonathan Alvarez | Phone: not listed | Notes: On leave" in page_two
    assert "Employee: Sofia Rossi | Phone: 555-0103 | Notes: not listed" in page_two


def test_labels_and_values_on_one_line_stay_together():
    text = _page(
        Text(72, 740, "Invoice number:"), Text(200, 740, "INV-4471"),
        Text(72, 725, "Amount due:"), Text(200, 725, "$12,480.00"),
    )
    assert text.splitlines() == ["Invoice number: INV-4471", "Amount due: $12,480.00"]


def test_a_font_without_a_text_layer_falls_back_to_the_old_reader():
    from controller_inbox import pdf_layout

    glyphs = [pdf_layout.Glyph("(cid:12)", i * 6.0, i * 6.0 + 5, 700, 10, False) for i in range(20)]
    assert pdf_layout.page_text(glyphs).unreadable
