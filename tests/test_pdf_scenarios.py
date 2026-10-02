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


def test_facts_beside_a_table_stay_out_of_its_rows():
    facts = []
    y = 740
    for label, value in (("Invoice:", "INV-4471"), ("Bill to:", "Northwind"), ("Due:", "15 March 2026"), ("Terms:", "Net 30")):
        facts += [Text(40, y, label, size=10), Text(110, y, value, size=10)]
        y -= 16
    table = sheet_rows(
        [(280, "left"), (380, "left"), (480, "right")],
        [["Item", "Qty", "Amount"], ["Consulting", "10", "$4,000"], ["Travel", "1", "$350"], ["Total", "", "$4,350"]],
        top=740,
        pitch=16,
        size=10,
    )
    text = _text(facts + table)
    assert "Invoice: INV-4471" in text and "Bill to: Northwind" in text
    assert "Item: Consulting | Qty: 10 | Amount: $4,000" in text
    assert "Item: Total | Qty: not listed | Amount: $4,350" in text
    assert "Invoice: INV-4471 | Item" not in text
    facts_block = text.split("[facts]\n", 1)[1].split("\n\n", 1)[0]
    assert "Consulting" not in facts_block


def test_a_repeated_table_on_the_next_page_is_not_named_twice():
    rows = [["Vendor", "Amount"], ["Northwind", "$12,480"], ["Globex", "$880"], ["Initech", "$450"]]
    pages = []
    for number in (1, 2):
        pages.append(
            [
                Text(40, 770, "Acme Corp", size=8),
                Text(480, 770, f"Page {number}", size=8),
                *sheet_rows([(40, "left"), (200, "right")], rows, top=730, pitch=16, size=10),
            ]
        )
    text = pdf_text(build_pdf(pages))
    assert "Vendor: Northwind | Vendor: Northwind" not in text
    assert text.count("Vendor: Northwind | Amount: $12,480") == 2
    assert "[page 2]" in text and "Page 2" not in text


def test_names_turned_sideways_in_the_top_row_are_read():
    names = [
        Text(160, 690, "Maya Chen", size=9, bold=True, turn=90),
        Text(250, 690, "Sam", size=9, bold=True, turn=90),
        Text(340, 690, "Li", size=9, bold=True, turn=90),
    ]
    body = sheet_rows(
        [(40, "left"), (160, "left"), (250, "left"), (340, "left")],
        [["North", "1", "2", "3"], ["South", "4", "5", "6"], ["East", "7", "8", "9"]],
        top=670,
        pitch=16,
        size=10,
    )
    text = _text([*names, *body, Text(40, 590, "Prepared by Maya Chen", size=9)])
    assert "Maya Chen | Sam | Li" in text
    assert "North" in text and "South" in text and "East" in text
    assert "Prepared by Maya Chen" in text
    assert "Maya Chen: 1" in text and "Sam: 2" in text and "Li: 3" in text
    assert "Maya Chen: 4" in text and "Li: 9" in text


def test_sideways_names_read_in_either_direction_with_or_without_a_stub_heading():
    columns = [(40, "left"), (150, "left"), (200, "left"), (250, "left"), (300, "left")]
    people = ["Ana", "Ben Ortiz", "Chloe", "Eli Park"]
    body = [[district, *[str(row * 4 + col) for col in range(4)]] for row, district in enumerate(["North", "South", "East", "West"])]

    def names(turn: int, y: float, spacing: float = 0) -> list[Text]:
        return [Text(x, y, name, size=8, bold=True, turn=turn, spacing=spacing) for (x, _align), name in zip(columns[1:], people)]

    expected = "North | Ana: 0 | Ben Ortiz: 1 | Chloe: 2 | Eli Park: 3"
    rows = sheet_rows(columns, body, top=670, pitch=14, size=9)
    # Reading downward, a long name hangs lower than a short one; they are still one row.
    downward = _text([*names(-90, 722), *rows])
    assert "Ana | Ben Ortiz | Chloe | Eli Park" in downward
    assert expected in downward
    stub = _text([Text(40, 690, "District", size=8, bold=True), *names(90, 690), *rows])
    assert "District: North | Ana: 0 | Ben Ortiz: 1" in stub
    tracked = _text([*names(90, 690, spacing=1.2), *rows])
    assert expected in tracked


def test_a_group_label_stays_inside_the_table():
    items = [
        Text(40, 740, "Name", size=10, bold=True),
        Text(180, 740, "Role", size=10, bold=True),
        Text(320, 740, "Pay", size=10, bold=True),
        Text(40, 724, "Employees", size=10, bold=True),
        Text(40, 708, "Maya Chen", size=10),
        Text(180, 708, "Lead", size=10),
        Text(320, 708, "$10", size=10),
        Text(40, 692, "Li Wei", size=10),
        Text(180, 692, "Analyst", size=10),
        Text(320, 692, "$8", size=10),
        Text(40, 676, "Contractors", size=10, bold=True),
        Text(40, 660, "Sam Ortiz", size=10),
        Text(180, 660, "Temp", size=10),
        Text(320, 660, "$6", size=10),
    ]
    text = _text(items)
    assert "Name Employees" not in text
    assert "Group: Employees" in text and "Name: Maya Chen | Role: Lead | Pay: $10" in text
    assert "Group: Contractors" in text and "Name: Sam Ortiz | Role: Temp | Pay: $6" in text
    assert text.index("Group: Employees") < text.index("Maya Chen") < text.index("Group: Contractors") < text.index("Sam Ortiz")


def test_two_tables_with_different_headers_stay_separate():
    people = [
        Text(40, 740, "Name", size=10, bold=True),
        Text(200, 740, "Pay", size=10, bold=True),
        Text(40, 724, "Maya Chen", size=10),
        Text(200, 724, "$10", size=10),
        Text(40, 708, "Sam Ortiz", size=10),
        Text(200, 708, "$6", size=10),
    ]
    hours = sheet_rows([(40, "left"), (200, "right")], [["Code", "Hours"], ["C-3", "3"], ["A-1", "12"]], top=660, pitch=16, size=10)
    text = _text(people + hours)
    first, second = text.split("[table]\n")[1:]
    assert "Name: Sam Ortiz" in first and "Code" not in first.split("\n\n", 1)[0]
    assert second.startswith("Code | Hours") and "Name: Sam Ortiz" not in second


def test_a_bold_total_stays_on_its_table():
    items = [
        *sheet_rows([(40, "left"), (200, "right")], [["Vendor", "Amount"], ["Northwind", "$100"], ["Globex", "$50"]], top=740, pitch=16, size=10),
        Text(40, 740 - 48, "Total", size=10, bold=True),
        Text(200, 740 - 48, "$150", size=10, bold=True, right=True),
    ]
    text = _text(items)
    assert "Vendor: Total | Amount: $150" in text
