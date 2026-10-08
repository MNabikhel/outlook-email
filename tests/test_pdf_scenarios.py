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


def test_a_heading_merged_across_columns_names_each_of_them():
    # The group word may sit on the first sub-column or on the middle one. Both are one merge.
    leaf = [
        Text(40, 740, "Line", bold=True),
        Text(160, 740, "Actual", bold=True),
        Text(250, 740, "Budget", bold=True),
        Text(340, 740, "Variance", bold=True),
        Text(450, 740, "Actual", bold=True),
        Text(540, 740, "Budget", bold=True),
        Text(630, 740, "Variance", bold=True),
        Text(40, 724, "Sales"),
        Text(160, 724, "100"),
        Text(250, 724, "90"),
        Text(340, 724, "10"),
        Text(450, 724, "120"),
        Text(540, 724, "110"),
        Text(630, 724, "10"),
    ]
    expected = "Line: Sales | Q3 Actual: 100 | Q3 Budget: 90 | Q3 Variance: 10 | Q4 Actual: 120 | Q4 Budget: 110 | Q4 Variance: 10"
    centered = _text([Text(40, 756, "Line", bold=True), Text(250, 756, "Q3", bold=True), Text(540, 756, "Q4", bold=True), *leaf])
    left = _text([Text(40, 756, "Line", bold=True), Text(160, 756, "Q3", bold=True), Text(450, 756, "Q4", bold=True), *leaf])
    assert expected in centered
    assert expected in left


def test_a_wide_period_heading_keeps_the_year_on_every_column():
    items = [
        Text(40, 756, "Line", bold=True),
        Text(155, 756, "Three months ended", bold=True),
        Text(445, 756, "Nine months ended", bold=True),
        Text(40, 740, "Line", bold=True),
        Text(160, 740, "2024", bold=True),
        Text(250, 740, "2023", bold=True),
        Text(340, 740, "2022", bold=True),
        Text(450, 740, "2024", bold=True),
        Text(540, 740, "2023", bold=True),
        Text(630, 740, "2022", bold=True),
        Text(40, 724, "Revenue"),
        Text(160, 724, "100"),
        Text(250, 724, "90"),
        Text(340, 724, "80"),
        Text(450, 724, "300"),
        Text(540, 724, "270"),
        Text(630, 724, "240"),
    ]
    text = _text(items)
    assert "Line: Revenue | Three months ended 2024: 100 | Three months ended 2023: 90 | Three months ended 2022: 80 | Nine months ended 2024: 300 | Nine months ended 2023: 270 | Nine months ended 2022: 240" in text
    assert "\n2024 |" not in text and "2024: 2023" not in text


def test_three_header_rows_stack_onto_each_column():
    items = [
        Text(40, 772, "Line", bold=True),
        Text(200, 772, "2024", bold=True),
        Text(520, 772, "2025", bold=True),
        Text(40, 756, "Line", bold=True),
        Text(160, 756, "Q1", bold=True),
        Text(300, 756, "Q2", bold=True),
        Text(440, 756, "Q1", bold=True),
        Text(580, 756, "Q2", bold=True),
        Text(40, 740, "Line", bold=True),
        Text(160, 740, "Act", bold=True),
        Text(230, 740, "Bud", bold=True),
        Text(300, 740, "Act", bold=True),
        Text(370, 740, "Bud", bold=True),
        Text(440, 740, "Act", bold=True),
        Text(510, 740, "Bud", bold=True),
        Text(580, 740, "Act", bold=True),
        Text(650, 740, "Bud", bold=True),
        Text(40, 724, "Sales"),
        Text(160, 724, "10"),
        Text(230, 724, "9"),
        Text(300, 724, "11"),
        Text(370, 724, "8"),
        Text(440, 724, "12"),
        Text(510, 724, "7"),
        Text(580, 724, "13"),
        Text(650, 724, "6"),
    ]
    text = _text(items)
    assert "Line: Sales | 2024 Q1 Act: 10 | 2024 Q1 Bud: 9 | 2024 Q2 Act: 11 | 2024 Q2 Bud: 8 | 2025 Q1 Act: 12 | 2025 Q1 Bud: 7 | 2025 Q2 Act: 13 | 2025 Q2 Bud: 6" in text


def test_nested_categories_and_a_merged_quarter_stay_on_the_same_row():
    items = [
        Text(40, 756, "Line", bold=True),
        Text(220, 756, "Q3", bold=True),
        Text(400, 756, "Q4", bold=True),
        Text(40, 740, "Line", bold=True),
        Text(220, 740, "Actual", bold=True),
        Text(310, 740, "Budget", bold=True),
        Text(400, 740, "Actual", bold=True),
        Text(490, 740, "Budget", bold=True),
        Text(40, 724, "Operating", bold=True),
        Text(56, 708, "Revenue", bold=True),
        Text(72, 692, "Product"),
        Text(220, 692, "100"),
        Text(310, 692, "90"),
        Text(400, 692, "80"),
        Text(490, 692, "70"),
        Text(40, 676, "Financing", bold=True),
        Text(56, 660, "Interest"),
        Text(220, 660, "5"),
        Text(310, 660, "4"),
        Text(400, 660, "3"),
        Text(490, 660, "2"),
    ]
    text = _text(items)
    assert "Group: Operating" in text and "Group: Revenue" in text
    assert "Operating > Revenue | Line: Product | Q3 Actual: 100 | Q3 Budget: 90 | Q4 Actual: 80 | Q4 Budget: 70" in text
    assert "Group: Financing" in text
    assert "Financing | Line: Interest | Q3 Actual: 5 | Q3 Budget: 4 | Q4 Actual: 3 | Q4 Budget: 2" in text
    assert text.index("Group: Operating") < text.index("Product") < text.index("Group: Financing")


def test_a_category_merged_down_a_column_is_repeated_on_each_row():
    # Drawn once, centered between the two rows it covers.
    centered = [
        Text(40, 740, "Region", bold=True),
        Text(160, 740, "Line", bold=True),
        Text(300, 740, "Amount", bold=True),
        Text(40, 716, "North", bold=True),
        Text(160, 724, "Sales"),
        Text(300, 724, "100"),
        Text(160, 708, "Costs"),
        Text(300, 708, "40"),
        Text(40, 684, "South", bold=True),
        Text(160, 692, "Sales"),
        Text(300, 692, "80"),
        Text(160, 676, "Costs"),
        Text(300, 676, "30"),
    ]
    text = _text(centered)
    assert "Region: North | Line: Sales | Amount: 100" in text
    assert "Region: North | Line: Costs | Amount: 40" in text
    assert "Region: South | Line: Sales | Amount: 80" in text
    assert "Region: South | Line: Costs | Amount: 30" in text
    assert "Group: North" not in text and "Group: South" not in text

    # The same merge with the name on the first row and the cells below left blank.
    top = [
        Text(40, 740, "Region", bold=True),
        Text(160, 740, "Line", bold=True),
        Text(300, 740, "Amount", bold=True),
        Text(40, 724, "North"),
        Text(160, 724, "Sales"),
        Text(300, 724, "100"),
        Text(160, 708, "Costs"),
        Text(300, 708, "40"),
        Text(40, 692, "South"),
        Text(160, 692, "Sales"),
        Text(300, 692, "80"),
        Text(160, 676, "Costs"),
        Text(300, 676, "30"),
    ]
    filled = _text(top)
    assert "Region: North | Line: Costs | Amount: 40" in filled
    assert "Region: South | Line: Costs | Amount: 30" in filled


def test_an_unequal_merge_does_not_steal_the_next_column():
    items = [
        Text(40, 756, "Line", bold=True),
        Text(160, 756, "Q3", bold=True),
        Text(450, 756, "Full year", bold=True),
        Text(40, 740, "Line", bold=True),
        Text(160, 740, "Actual", bold=True),
        Text(250, 740, "Budget", bold=True),
        Text(340, 740, "Variance", bold=True),
        Text(450, 740, "Amount", bold=True),
        Text(40, 724, "Sales"),
        Text(160, 724, "100"),
        Text(250, 724, "90"),
        Text(340, 724, "10"),
        Text(450, 724, "400"),
    ]
    text = _text(items)
    assert "Line: Sales | Q3 Actual: 100 | Q3 Budget: 90 | Q3 Variance: 10 | Full year Amount: 400" in text


def test_an_indented_subtotal_names_the_rows_under_it():
    items = [
        Text(40, 756, "Line", bold=True),
        Text(220, 756, "Amount", bold=True),
        Text(40, 740, "Revenue", bold=True),
        Text(220, 740, "120"),
        Text(56, 724, "Product"),
        Text(220, 724, "100"),
        Text(56, 708, "Service"),
        Text(220, 708, "20"),
    ]
    text = _text(items)
    assert "Line: Revenue | Amount: 120" in text
    assert "Revenue | Line: Product | Amount: 100" in text
    assert "Revenue | Line: Service | Amount: 20" in text


def test_date_headings_keep_right_aligned_figures():
    items = [
        Text(40, 756, "Line", bold=True),
        Text(180, 756, "Three months ended", bold=True),
        Text(420, 756, "Nine months ended", bold=True),
        Text(180, 740, "31 March 2024", bold=True),
        Text(300, 740, "31 March 2023", bold=True),
        Text(420, 740, "31 March 2024", bold=True),
        Text(540, 740, "31 March 2023", bold=True),
        Text(40, 724, "Revenue"),
        Text(250, 724, "1,240", right=True),
        Text(370, 724, "1,100", right=True),
        Text(490, 724, "3,600", right=True),
        Text(610, 724, "3,200", right=True),
        Text(40, 708, "Expenses"),
        Text(250, 708, "(480)", right=True),
        Text(370, 708, "(450)", right=True),
        Text(490, 708, "(1,400)", right=True),
        Text(610, 708, "(1,250)", right=True),
    ]
    text = _text(items)
    assert (
        "Line: Revenue | Three months ended 31 March 2024: 1,240 | Three months ended 31 March 2023: 1,100 | "
        "Nine months ended 31 March 2024: 3,600 | Nine months ended 31 March 2023: 3,200"
    ) in text
    assert "Three months ended 31 March 2024: (480)" in text
    assert "31 March 2024: 31 March 2023" not in text


def test_right_aligned_figures_stay_under_short_headings():
    items = [
        Text(40, 756, "Line", bold=True),
        Text(175, 756, "Q3", bold=True),
        Text(430, 756, "Q4", bold=True),
        Text(40, 740, "Line", bold=True),
        Text(160, 740, "Actual", bold=True),
        Text(260, 740, "Budget", bold=True),
        Text(400, 740, "Actual", bold=True),
        Text(500, 740, "Budget", bold=True),
        Text(40, 724, "Sales"),
        Text(240, 724, "1,240", right=True),
        Text(360, 724, "1,100", right=True),
        Text(480, 724, "3,600", right=True),
        Text(600, 724, "3,200", right=True),
    ]
    text = _text(items)
    assert "Line: Sales | Q3 Actual: 1,240 | Q3 Budget: 1,100 | Q4 Actual: 3,600 | Q4 Budget: 3,200" in text
    assert "not listed" not in text


def test_a_wide_column_keeps_a_short_figure_at_its_right_edge():
    items = [
        Text(40, 740, "Line", bold=True),
        Text(180, 740, "Actual", bold=True),
        Text(360, 740, "Budget", bold=True),
        Text(40, 724, "Sales"),
        Text(330, 724, "9", right=True),
        Text(520, 724, "8", right=True),
        Text(40, 708, "Costs"),
        Text(330, 708, "4", right=True),
        Text(520, 708, "3", right=True),
    ]
    text = _text(items)
    assert "Line: Sales | Actual: 9 | Budget: 8" in text
    assert "Line: Costs | Actual: 4 | Budget: 3" in text


def test_a_year_and_a_change_column_name_the_row():
    items = [
        Text(40, 740, "Line", bold=True),
        Text(180, 740, "2024", bold=True),
        Text(280, 740, "2023", bold=True),
        Text(380, 740, "Change", bold=True),
        Text(480, 740, "%", bold=True),
        Text(40, 724, "Sales"),
        Text(180, 724, "100"),
        Text(280, 724, "90"),
        Text(380, 724, "10"),
        Text(480, 724, "11%"),
    ]
    text = _text(items)
    assert "Line: Sales | 2024: 100 | 2023: 90 | Change: 10 | %: 11%" in text


def test_a_code_and_a_date_stay_in_their_columns():
    items = [
        Text(40, 740, "Code", bold=True),
        Text(120, 740, "Description", bold=True),
        Text(280, 740, "Due", bold=True),
        Text(420, 740, "Amount", bold=True),
        Text(40, 724, "4100"),
        Text(120, 724, "Product"),
        Text(280, 724, "15 March 2024"),
        Text(420, 724, "1,240", right=True),
        Text(40, 708, "5100"),
        Text(120, 708, "Payroll"),
        Text(280, 708, "31 March 2024"),
        Text(420, 708, "480", right=True),
    ]
    text = _text(items)
    assert "Code: 4100 | Description: Product | Due: 15 March 2024 | Amount: 1,240" in text
    assert "Code: 5100 | Description: Payroll | Due: 31 March 2024 | Amount: 480" in text


def test_a_larger_section_label_names_the_rows_under_it():
    items = [
        Text(40, 756, "Line", bold=True),
        Text(220, 756, "Amount", bold=True),
        Text(40, 736, "Operating", size=12),
        Text(40, 716, "Product"),
        Text(220, 716, "100"),
        Text(40, 700, "Service"),
        Text(220, 700, "40"),
        Text(40, 680, "Financing", size=12),
        Text(40, 660, "Interest"),
        Text(220, 660, "5"),
    ]
    text = _text(items)
    assert "Group: Operating" in text and "Group: Financing" in text
    assert "Operating | Line: Product | Amount: 100" in text
    assert "Operating | Line: Service | Amount: 40" in text
    assert "Financing | Line: Interest | Amount: 5" in text
    assert "Service Financing" not in text
    assert "Amount: not listed" not in text


def test_a_wrapped_note_stays_on_its_row():
    items = [
        Text(40, 740, "Item", bold=True),
        Text(180, 740, "Amount", bold=True),
        Text(300, 740, "Note", bold=True),
        Text(40, 724, "Consulting"),
        Text(180, 724, "4,000"),
        Text(300, 724, "Signed in"),
        Text(300, 712, "March"),
        Text(40, 696, "Travel"),
        Text(180, 696, "350"),
        Text(40, 680, "Lodging"),
        Text(180, 680, "1,200"),
        Text(300, 680, "Prepaid"),
    ]
    text = _text(items)
    assert "Item: Consulting | Amount: 4,000 | Note: Signed in March" in text
    assert "Item: Travel | Amount: 350 | Note: not listed" in text
    assert "Item: Lodging | Amount: 1,200 | Note: Prepaid" in text
    assert "Note: March" not in text


def _sum_and_check(text: str) -> tuple[int, float, list[str]]:
    from controller_inbox.table_lookup import tables_in, verify
    from controller_inbox.table_query import Tables

    table = tables_in(text)[0]
    count, total = Tables([("sheet.pdf", text)]).run("SELECT COUNT(*), SUM(amount) FROM t1")[1][0]
    return count, total, verify(table).mismatched


def test_a_long_table_printed_on_without_its_headings_keeps_its_columns():
    # Excel prints a long list over several pages without repeating its headings unless told to: the rows on the
    # next page are the same table's, and its total counts every one of them.
    columns = [(40, "left"), (200, "left"), (420, "right")]
    body = [[f"Vendor {i:02d}", f"INV-{1000 + i}", f"{(i * 137) % 5000 + 100:,}.00"] for i in range(1, 61)]
    total = sum(float(row[2].replace(",", "")) for row in body)
    first = sheet_rows(columns, [["Vendor", "Invoice", "Amount"], *body[:40]], top=740, pitch=16)
    rest = [*body[40:], ["Total", "", f"{total:,.2f}"]]
    second = [
        Text(x, 740 - r * 16, value, right=align == "right") for r, row in enumerate(rest) for (x, align), value in zip(columns, row) if value
    ]
    text = pdf_text(build_pdf([first, second]))
    page_two = text.split("[page 2]\n", 1)[1]
    assert "Vendor: Vendor 41 | Invoice: INV-1041 | Amount: 717.00" in page_two
    assert "Vendor | 41" not in page_two
    assert _sum_and_check(text) == (60, total, [])


def test_a_next_page_with_a_table_of_its_own_is_not_given_the_names_before():
    columns = [(40, "left"), (200, "left"), (420, "right")]
    first = sheet_rows(columns, [["Vendor", "Invoice", "Amount"], ["Acme", "INV-1", "1,200.00"], ["Globex", "INV-2", "880.00"], ["Initech", "INV-3", "450.00"]])
    # Two columns of figures in other places: not the table above.
    second = [Text(x, 700 - r * 15, value, right=True) for r, row in enumerate([["10.00", "20.00"], ["30.00", "40.00"], ["50.00", "60.00"]]) for x, value in zip((300, 520), row)]
    page_two = pdf_text(build_pdf([first, second])).split("[page 2]\n", 1)[1]
    assert "Vendor:" not in page_two and "Amount:" not in page_two


def test_negatives_in_the_accounting_format_are_figures_not_a_second_heading_row():
    # Excel's accounting format draws "$" at the cell's left and "(1,234.00)" at its right: "$(1,234.00)".
    rows = [
        ["Account", "Q1", "Q2", "Q3"],
        ["Refunds", "(1,234.00)", "(500.00)", "(75.25)"],
        ["Sales", "10,000.00", "12,000.00", "9,500.00"],
        ["Fees", "250.00", "300.00", "125.00"],
        ["Rent", "2,000.00", "2,000.00", "2,000.00"],
        ["Total", "11,016.00", "13,800.00", "11,549.75"],
    ]
    items = []
    for r, row in enumerate(rows):
        items.append(Text(40, 700 - r * 15, row[0], bold=r == 0))
        for c, value in enumerate(row[1:]):
            right = 260 + c * 110
            if r:
                items.append(Text(right - 80, 700 - r * 15, "$"))
            items.append(Text(right, 700 - r * 15, value, bold=r == 0, right=True))
    lines = _text(items).splitlines()
    assert lines[0] == "Account | Q1 | Q2 | Q3"
    assert "Account: Refunds | Q1: $(1,234.00) | Q2: $(500.00) | Q3: $(75.25)" in lines


def test_a_note_in_a_column_without_a_heading_keeps_its_row_in_the_sums():
    columns = [(40, "left"), (180, "left"), (330, "right"), (420, "left")]
    rows = [
        ["Vendor", "Invoice", "Amount", ""],
        ["Harbor Steel", "INV-1", "4,500.00", "Disputed"],
        ["Acme Supply", "INV-3", "800.00", ""],
        ["Blue Freight", "INV-4", "300.00", ""],
        ["Lakeside Power", "INV-5", "1,100.00", ""],
        ["Total", "", "6,700.00", ""],
    ]
    text = _text(sheet_rows(columns, rows))
    assert "Vendor: Harbor Steel | Invoice: INV-1 | Amount: 4,500.00 | Disputed" in text.splitlines()
    assert _sum_and_check(text) == (4, 6700.0, [])


def test_a_mark_on_some_rows_is_not_copied_onto_the_rows_without_it():
    # A merged category is named once over its rows; "HOLD" twice in a Status column marks just those two invoices.
    columns = [(40, "left"), (180, "left"), (260, "left"), (470, "right")]
    rows = [
        ["Vendor", "Status", "Invoice", "Amount"],
        ["Harbor Steel", "HOLD", "INV-1", "4,500.00"],
        ["Acme Supply", "", "INV-3", "800.00"],
        ["Blue Freight", "", "INV-4", "300.00"],
        ["Lakeside Power", "HOLD", "INV-5", "1,100.00"],
    ]
    lines = _text(sheet_rows(columns, rows)).splitlines()
    assert "Vendor: Acme Supply | Status: not listed | Invoice: INV-3 | Amount: 800.00" in lines
    assert "Vendor: Lakeside Power | Status: HOLD | Invoice: INV-5 | Amount: 1,100.00" in lines


def test_whole_numbers_that_look_like_years_are_a_row_not_headings():
    columns = [(40, "left"), (250, "right"), (360, "right"), (470, "right")]
    rows = [
        ["Item", "On Hand", "On Order", "Committed"],
        ["Hex Bolts", "2000", "1950", "2010"],
        ["Nuts", "450", "120", "300"],
        ["Washers", "800", "75", "60"],
        ["Screws", "1200", "500", "410"],
    ]
    lines = _text(sheet_rows(columns, rows)).splitlines()
    assert lines[0] == "Item | On Hand | On Order | Committed"
    assert "Item: Hex Bolts | On Hand: 2000 | On Order: 1950 | Committed: 2010" in lines
