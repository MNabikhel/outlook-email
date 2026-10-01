"""PDF pages in the shapes that show up at work: font sizes, several sections, more than one table.

A page with more than one kind of text is labeled [heading], [facts], [table] or [notes], and a
figure stays in the column it was printed in.
"""

from pdffactory import Text, build_pdf, sheet_rows

from controller_inbox.documents import pdf_text


def _text(items: list[Text]) -> str:
    body = pdf_text(build_pdf([items]))
    return body.split("\n", 1)[1]


VENDORS = [
    ["Vendor", "Note", "Amount"],
    ["Northwind", "Accrual", "$12,480.00"],
    ["Globex", "", "$880.00"],
    ["Initech", "On time", "$450.00"],
]


def test_a_small_font_table_and_a_large_font_table_keep_their_columns():
    small_cols = [(36, "left"), (110, "left"), (160, "left"), (240, "left"), (330, "right")]
    small = [
        ["Employee", "ID", "Department", "Manager", "Salary"],
        ["Maya Chen", "E-1001", "Finance", "Priya Raman", "$98,500"],
        ["Jonathan Alvarez", "E-1002", "", "Priya Raman", "$112,000"],
        ["Sofia Rossi", "E-1003", "Operations", "", "$87,250"],
    ]
    small_text = _text([Text(36, 760, "Staff roster", size=12, bold=True), *sheet_rows(small_cols, small, top=730, pitch=9, size=7)])
    assert "[heading]\nStaff roster" in small_text
    assert "Employee: Jonathan Alvarez | ID: E-1002 | Department: not listed | Manager: Priya Raman | Salary: $112,000" in small_text
    assert "Employee: Sofia Rossi | ID: E-1003 | Department: Operations | Manager: not listed | Salary: $87,250" in small_text

    large = _text(sheet_rows([(40, "left"), (240, "left"), (480, "right")], VENDORS, top=700, pitch=28, size=16))
    assert large.splitlines()[0] == "Vendor | Note | Amount"
    assert "Vendor: Globex | Note: not listed | Amount: $880.00" in large
    assert "[table]" not in large


def test_a_large_header_over_a_small_body_is_still_one_table():
    body = VENDORS[1:]
    items = [
        Text(40, 740, "Vendor", size=14, bold=True),
        Text(200, 740, "Note", size=14, bold=True),
        Text(380, 740, "Amount", size=14, bold=True),
        *sheet_rows([(40, "left"), (200, "left"), (380, "right")], body, top=710, pitch=12, size=8),
    ]
    text = _text(items)
    assert "Vendor: Northwind | Note: Accrual | Amount: $12,480.00" in text
    assert "Vendor: Globex | Note: not listed | Amount: $880.00" in text
    assert "[heading]" not in text


def test_a_page_is_split_into_heading_facts_table_and_notes():
    items = [
        Text(40, 760, "QUARTERLY CLOSE", size=20, bold=True),
        Text(40, 732, "Period:", size=11),
        Text(120, 732, "Q3 2026", size=11),
        Text(40, 716, "Amount due:", size=11),
        Text(140, 716, "$12,480.00", size=11),
        Text(40, 690, "Vendors", size=13, bold=True),
        *sheet_rows([(40, "left"), (180, "left"), (340, "right")], VENDORS, top=668, pitch=11, size=8),
        Text(40, 590, "Totals exclude contractors hired after 1 June.", size=7),
    ]
    text = _text(items)
    assert text.index("[heading]") < text.index("[facts]") < text.index("[table]") < text.index("[notes]")
    assert "Period: Q3 2026" in text and "Amount due: $12,480.00" in text
    assert "Vendor: Northwind | Note: Accrual | Amount: $12,480.00" in text
    assert "Vendor: Globex | Note: not listed | Amount: $880.00" in text
    assert "Code: Vendors" not in text
    facts = text.split("[facts]\n", 1)[1].split("\n\n", 1)[0]
    assert "Period: Q3 2026" in facts and "Northwind" not in facts
    table = text.split("[table]\n", 1)[1].split("\n\n", 1)[0]
    assert "Globex" in table and "Q3 2026" not in table
    assert text.split("[notes]\n", 1)[1].startswith("Totals exclude contractors")


def test_a_heading_between_two_tables_does_not_become_a_row():
    first = sheet_rows([(40, "left"), (200, "right")], [["Code", "Hours"], ["A-1", "12"], ["B-2", "7"], ["C-3", "3"]], top=740, pitch=14, size=10)
    second = sheet_rows([(40, "left"), (200, "right")], [["Item", "Cost"], ["Travel", "$640"], ["Meals", "$88"], ["Lodging", "$1,200"]], top=636, pitch=14, size=10)
    text = _text([*first, Text(40, 660, "Expenses", size=14, bold=True), *second])
    assert "Code: Expenses" not in text and "Hours: Cost" not in text
    assert "Code: C-3 | Hours: 3" in text
    assert "[heading]\nExpenses" in text
    assert "Item: Lodging | Cost: $1,200" in text
    hours, costs = text.split("[heading]", 1)
    assert "Travel" not in hours and "A-1" not in costs
