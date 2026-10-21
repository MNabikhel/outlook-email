"""Which rows of a table are totals, and where one table ends and the next begins."""

from __future__ import annotations

import io

from controller_inbox.table_lookup import lookup, tables_in, verify
from controller_inbox.table_query import Tables

AGING = """[page 1]
[heading]
AP Aging Summary

[table]
Vendor | Current | Over 90 Days | Total
Vendor: Acme Industrial Supply | Current: 24,310.50 | Over 90 Days: - | Total: 24,310.50
Vendor: Total Quality Logistics | Current: 18,400.00 | Over 90 Days: 2,000.00 | Total: 20,400.00
Vendor: Harbor Steel LLC | Current: 48,500.00 | Over 90 Days: - | Total: 48,500.00
Vendor: Total | Current: 91,210.50 | Over 90 Days: 2,000.00 | Total: 93,210.50
"""


def test_a_vendor_named_total_is_a_vendor():
    # Its figures don't add up the rows above it, so "Total Quality Logistics" is a row like the others.
    rows = tables_in(AGING)[0].rows
    assert [(row.name, row.total) for row in rows] == [
        ("Acme Industrial Supply", False), ("Total Quality Logistics", False), ("Harbor Steel LLC", False), ("Total", True),
    ]
    assert Tables([("AP Aging.pdf", AGING)]).run("SELECT SUM(total), SUM(over_90_days) FROM t1")[1] == [(93210.5, 2000.0)]


def test_an_unlabeled_row_that_adds_up_the_rows_above_is_a_total():
    text = """[page 1]
[table]
Vendor | Invoice # | Amount
Vendor: Acme Industrial Supply | Invoice #: A-1001 | Amount: 24,310.50
Vendor: Harbor Steel LLC | Invoice #: H-2210 | Amount: 48,500.00
Vendor: Orion Software | Invoice #: O-77 | Amount: 18,600.00
Vendor: not listed | Invoice #: not listed | Amount: $91,410.50
"""
    assert [row.total for row in tables_in(text)[0].rows] == [False, False, False, True]
    assert Tables([("AP.pdf", text)]).run("SELECT COUNT(*), SUM(amount) FROM t1")[1] == [(3, 91410.5)]


def test_lists_under_their_own_headings_are_separate_tables():
    text = """[page 1]
[heading]
Outstanding Checks - Operating x4471

[table]
Payee | Check # | Amount
Payee: Tidewater Metals | Check #: 20418 | Amount: 52,300.00
Payee: Summit Ridge Electric | Check #: 20425 | Amount: 18,744.65
Payee: Total outstanding checks | Check #: not listed | Amount: 71,044.65

[heading]
Outstanding Checks - Payroll x0932

[table]
Payee | Check # | Amount
Payee: Maya Chen | Check #: 5512 | Amount: 3,100.00
Payee: Li Wei | Check #: 5519 | Amount: 2,850.00
Payee: Total outstanding checks | Check #: not listed | Amount: 5,950.00
"""
    assert [len(table.rows) for table in tables_in(text)] == [3, 3]
    tables = Tables([("Bank Recs.pdf", text)])
    assert "CREATE TABLE t2 (  -- Bank Recs.pdf / Outstanding Checks - Payroll x0932" in tables.schema()
    assert tables.run("SELECT SUM(amount) FROM t2")[1] == [(5950.0,)]


def _balance_sheet() -> bytes:
    """A 10-K balance sheet: year columns, dot leaders to the figures, "Current assets:" over its items and each
    total indented past them."""
    from pdffactory import Text, build_pdf, width

    items = [Text(40, 760, "CONSOLIDATED BALANCE SHEETS", size=12, bold=True), Text(40, 744, "(in thousands)")]
    items += [Text(430, 726, "2025", right=True, bold=True), Text(520, 726, "2024", right=True, bold=True)]
    lines = [
        (40, "Current assets:", None),
        (50, "Cash and cash equivalents", ("$290,291", "$508,053")),
        (50, "Short-term investments", ("457,787", "289,758")),
        (50, "Other current assets", ("64,622", "57,330")),
        (60, "Total current assets", ("812,700", "855,141")),
        (50, "Property and equipment, net", ("131,681", "136,353")),
        (60, "Total assets", ("$944,381", "$991,494")),
        (40, "Current liabilities:", None),
        (50, "Accounts payable", ("86,468", "86,992")),
        (50, "Accrued expenses", ("53,139", "54,231")),
        (60, "Total current liabilities", ("139,607", "141,223")),
        (50, "Long-term debt", ("200,000", "200,000")),
        (60, "Total liabilities", ("339,607", "341,223")),
        (50, "Total stockholders' equity", ("604,774", "650,271")),
        (60, "Total liabilities and stockholders' equity", ("$944,381", "$991,494")),
    ]
    y = 708
    for x, label, figures in lines:
        if figures:
            # Dot leaders run from the label to the first figure column.
            dots = int((370 - x - width(label + " ", 10)) / width(". ", 10))
            items.append(Text(x, y, label + " " + ". " * dots))
            items += [Text(430, y, figures[0], right=True), Text(520, y, figures[1], right=True)]
        else:
            items.append(Text(x, y, label))
        y -= 15
    return build_pdf([items])


def test_a_balance_sheet_reads_as_one_table_whose_totals_add_up():
    from controller_inbox.documents import pdf_text
    from controller_inbox.table_lookup import verify

    text = pdf_text(_balance_sheet())
    assert ". . ." not in text, "dot leaders are the gap between a label and its figures"
    [table] = tables_in(text)
    assert table.labels == ("Line", "2025", "2024"), "years name a statement's columns"
    named = {row.name: row for row in table.rows}
    cash = named["Cash and cash equivalents"]
    assert (cash.group, cash.value("2025"), cash.value("2024"), cash.total) == ("Current assets", "$290,291", "$508,053", False)
    assert named["Total current assets"].group == "Current assets", "an indented total is not the line item above it"
    assert [row.name for row in table.rows if row.total] == [
        "Total current assets", "Total assets", "Total current liabilities", "Total liabilities",
        "Total liabilities and stockholders' equity",
    ]
    verdict = verify(table)
    assert verdict.mismatched == [] and verdict.matched == 10, "liabilities and equity add up as total liabilities plus equity"


def _table(lines: list[str]):
    [table] = tables_in("[page 1]\n[table]\n" + "\n".join(lines) + "\n")
    return table


def test_a_misread_figure_is_caught_by_its_total_instead_of_hiding_it():
    from controller_inbox.table_lookup import verify

    checks = [
        "Payee: Tidewater Metals | Check #: 20418 | Amount: 32,300.00",  # 52,300.00 misread
        "Payee: Summit Ridge Electric | Check #: 20425 | Amount: 18,744.65",
        "Payee: Total outstanding checks | Check #: not listed | Amount: 71,044.65",
    ]
    table = _table(["Payee | Check # | Amount", *checks])
    assert [row.total for row in table.rows] == [False, False, True], "the total stays a total"
    assert verify(table).mismatched == ["Total outstanding checks (Amount): printed 71,044.65, the rows above add to 51,044.65"]
    # One subtotal off doesn't turn the right ones after it into rows.
    assets = [
        "Category: Buildings | Asset: B-100 Warehouse | Cost: 2,450,000.00",
        "Category: Buildings | Asset: B-101 Roof | Cost: 186,000.00",
        "Category: Buildings | Asset: Total Buildings | Cost: 2,636,500.00",
        "Category: Vehicles | Asset: V-301 Van | Cost: 52,400.00",
        "Category: Vehicles | Asset: V-302 Pickup | Cost: 48,950.00",
        "Category: Vehicles | Asset: Total Vehicles | Cost: 101,350.00",
    ]
    table = _table(["Category | Asset | Cost", *assets])
    assert [row.name for row in table.rows if row.total] == ["Total Buildings", "Total Vehicles"]
    assert Tables([("FA.pdf", "[page 1]\n[table]\nCategory | Asset | Cost\n" + "\n".join(assets))]).run("SELECT SUM(cost) FROM t1")[1] == [(2737350.0,)]


def test_a_row_with_a_date_is_never_taken_for_a_total():
    deposits = [f"Date: 10/{day}/2026 | Amount: {amount}" for day, amount in (("01", "2,500.00"), ("08", "2,500.00"), ("15", "5,000.00"), ("22", "1,200.00"))]
    table = _table(["Date | Amount", *deposits])
    assert not any(row.total for row in table.rows), "5,000 after 2,500 and 2,500 is a deposit"


def test_a_section_total_under_a_worked_out_line_and_a_running_balance_add_up():
    from controller_inbox.table_lookup import verify

    cash = _table([
        "Item | Wk 1 | Wk 2",
        "Item: Customer collections | Wk 1: 377,897 | Wk 2: 386,220",
        "Item: Total Operating Receipts | Wk 1: 377,897 | Wk 2: 386,220",
        "Item: Payroll and benefits | Wk 1: (274,275) | Wk 2: (264,495)",
        "Item: Total Operating Disbursements | Wk 1: (274,275) | Wk 2: (264,495)",
        "Item: Net Operating Cash Flow | Wk 1: 103,622 | Wk 2: 121,725",
        "Item: Revolver draw (repayment) | Wk 1: - | Wk 2: 150,000",
        "Item: Interest paid | Wk 1: (9,842) | Wk 2: -",
        "Item: Total Financing | Wk 1: (9,842) | Wk 2: 150,000",
    ])
    assert [row.name for row in cash.rows if row.total][-1] == "Total Financing"
    assert verify(cash).mismatched == []
    ledger = _table([
        "Date | Name | Debit | Credit | Balance",
        "Date: 10/07/2026 | Name: Old Dominion | Debit: 4,567.06 | Credit: not listed | Balance: 4,567.06",
        "Date: 10/14/2026 | Name: Estes Express | Debit: 1,555.89 | Credit: not listed | Balance: 6,122.95",
        "Date: 10/31/2026 | Name: FedEx Freight | Debit: not listed | Credit: 122.95 | Balance: 6,000.00",
        "Date: Total 6600 Freight Out | Name: not listed | Debit: 6,122.95 | Credit: 122.95 | Balance: 6,000.00",
    ])
    assert verify(ledger).mismatched == [], "a balance column's total is its last balance"


def test_a_small_misread_in_a_long_table_still_shows():
    from controller_inbox.table_lookup import verify

    lines = ["Department | Account | Budget"]
    for group, name in enumerate(("Finance", "Sales", "IT")):
        values = [1000 + 37 * i + 101 * group for i in range(20)]
        lines += [f"Department: {name} | Account: {name[:3]}-{6000 + i} | Budget: {value:,}" for i, value in enumerate(values)]
        lines.append(f"Department: {name} | Account: Total {name} | Budget: {sum(values):,}")
    assert verify(_table(lines)).mismatched == []
    misread = [line.replace("Budget: 1,202", "Budget: 1,242") if "IT-6000" in line else line for line in lines]
    assert verify(_table(misread)).mismatched == ["Total IT (Budget): printed 31,070, the rows above add to 31,110"]


def test_a_long_table_reads_quickly():
    import time

    rows = [f"Date: 10/{i % 28 + 1:02d}/2026 | Name: Vendor {i} | Debit: {(i * 37) % 9000 + 100:,}.00 | Balance: {i * 31 % 99999:,}.00" for i in range(3000)]
    started = time.monotonic()
    tables_in("[page 1]\n[table]\nDate | Name | Debit | Balance\n" + "\n".join(rows))
    assert time.monotonic() - started < 2.0


def test_a_minus_printed_last_is_a_negative():
    # SAP and Oracle reports print a credit as "1,234.00-"; read as no figure, the total would lose it.
    text = """[page 1]
[table]
Account | Department | Amount
Account: 4000 Sales | Department: Retail | Amount: 10,000.00
Account: 4100 Returns | Department: Retail | Amount: 1,234.00-
Account: 5000 Rent | Department: Admin | Amount: 2,000.00
Account: 5100 Fees | Department: Admin | Amount: 250.00
Account: Total | Department: not listed | Amount: 11,016.00
"""
    assert verify(tables_in(text)[0]).mismatched == []
    assert Tables([("GL.pdf", text)]).run("SELECT SUM(amount) FROM t1 WHERE account LIKE '%returns%'")[1] == [(-1234.0,)]
    assert "Total of \"Amount\" (page 1) over 4 rows: 11,016.00" in lookup(text, "what is the total amount")


def test_a_vendor_named_total_is_a_vendor_with_no_total_under_it():
    text = """[page 1]
[table]
Vendor | Invoice | Due | Amount
Vendor: Harbor Steel | Invoice: INV-1 | Due: 10/14/2026 | Amount: 4,500.00
Vendor: Total Wine & More | Invoice: INV-2 | Due: 10/15/2026 | Amount: 1,250.00
Vendor: Acme Supply | Invoice: INV-3 | Due: 10/16/2026 | Amount: 800.00
Vendor: Blue Freight | Invoice: INV-4 | Due: 10/17/2026 | Amount: 300.00
"""
    assert [row.total for row in tables_in(text)[0].rows] == [False, False, False, False]
    assert Tables([("AP.pdf", text)]).run("SELECT COUNT(*), SUM(amount) FROM t1")[1] == [(4, 6850.0)]


def test_european_figures_keep_their_decimals():
    from decimal import Decimal

    from controller_inbox.table_lookup import _number

    # "€447,15" was read as 44715, "12,5%" as 125 and "€1.234,56" as no figure at all.
    assert _number("€447,15") == Decimal("447.15")
    assert _number("12,5%") == Decimal("12.5")
    assert _number("€1.234,56") == Decimal("1234.56")
    assert _number("1,250.00") == Decimal("1250.00")
    assert _number("1,2345") is None
    text = "Item: Consulting | Qty: 1 | Amount: €447,15\nItem: Travel | Qty: 2 | Amount: €82,40\nItem: Licence | Qty: 1 | Amount: €1.234,56\n"
    assert "1,764.11" in lookup(text, "what is the total amount")
    assert Tables([("Rechnung.pdf", text)]).run("SELECT ROUND(SUM(amount), 2) FROM t1")[1] == [(1764.11,)]


def test_a_name_starting_with_total_and_no_total_below_is_a_row():
    # No total row below says otherwise, so a vendor or description that starts "Total" is an ordinary row.
    text = (
        "Vendor: Harbor Steel LLC | Terms: Net 30 | Balance: $48,500.00\n"
        "Vendor: Total Quality Logistics | Terms: Net 30 | Balance: $12,250.00\n"
        "Vendor: Brightline Packaging | Terms: Net 45 | Balance: $9,800.00\n"
        "Vendor: Cedar Valley Supply | Terms: Net 30 | Balance: $3,100.00\n"
    )
    assert [row.total for row in tables_in(text)[0].rows] == [False, False, False, False]
    assert Tables([("AP open items.xlsx", text)]).run("SELECT COUNT(*), SUM(balance) FROM t1")[1] == [(4, 73650.0)]
    fees = (
        "Vendor: Acme | Description: Monthly hosting | Amount: 1,000.00\n"
        "Vendor: Bolt | Description: Total rewards platform fee | Amount: 500.00\n"
        "Vendor: Crest | Description: Office supplies | Amount: 250.00\n"
    )
    assert [row.total for row in tables_in(fees)[0].rows] == [False, False, False]


def test_a_total_named_in_a_later_column_is_still_a_total():
    # "Total" alone, or "Total Revenue" over rows of "Revenue", says total in whichever column it is.
    lines = (
        "Entity: TAZ US | Line: Revenue | Amount: 5,000\n"
        "Entity: TAZ UK | Line: Revenue | Amount: 3,000\n"
        "Entity: Consolidated | Line: Total Revenue | Amount: 8,000\n"
    )
    assert [row.total for row in tables_in(lines)[0].rows] == [False, False, True]
    assert 'Total of "Amount" over 2 rows: 8,000' in lookup(lines, "what is the total amount")
    customers = (
        "Customer: Acme Corp | Invoice: INV-1 | Amount: 1,000.00\n"
        "Customer: Beta LLC | Invoice: INV-2 | Amount: 500.00\n"
        "Customer: All customers | Invoice: Total | Amount: 1,500.00\n"
    )
    assert [row.total for row in tables_in(customers)[0].rows] == [False, False, True]


def test_a_credit_marked_cr_is_a_negative_figure():
    from decimal import Decimal

    from controller_inbox.table_lookup import _number

    assert _number("1,250.00 CR") == Decimal("-1250.00")
    assert _number("$300.00 Cr") == Decimal("-300.00")
    assert _number("1,250.00 DR") == Decimal("1250.00")
    text = (
        "Customer: Acme Corp | Balance: 4,000.00\nCustomer: Beta LLC | Balance: 2,500.00\n"
        "Customer: Gamma Inc | Balance: 1,250.00 CR\nCustomer: Delta Co | Balance: 800.00\n"
    )
    assert "6,050.00" in lookup(text, "total balance")


def test_an_average_keeps_its_cents():
    # The average of 10, 11, 11 and 13 days is 11.25, not "11".
    days = "".join(f"Invoice: INV-{n} | Days Outstanding: {d}\n" for n, d in [(101, 10), (102, 11), (103, 11), (104, 13)])
    assert "11.25" in lookup(days, "what is the average days outstanding")
    assert "$1,000.50" in lookup("Vendor: Acme | Amount: $1,000\nVendor: Beta | Amount: $1,001\n", "average amount")
    grouped = (
        "Vendor: Acme | Category: Rent | Amount: $1,000\nVendor: Beta | Category: Rent | Amount: $1,001\n"
        "Vendor: Crest | Category: Travel | Amount: $300\nVendor: Dune | Category: Travel | Amount: $400\n"
    )
    assert "$1,000.50" in lookup(grouped, "average amount by category")


def test_columns_headed_alike_are_each_read():
    text = (
        "Customer: Acme | Date: 09/05/2026 | Amount: $1,000.00 | Date: 09/20/2026 | Amount: $250.00\n"
        "Customer: Beta | Date: 09/07/2026 | Amount: $2,000.00 | Date: 09/22/2026 | Amount: $500.00\n"
    )
    row = tables_in(text)[0].rows[0]
    assert (row.value("Amount"), row.value("Amount (2)"), row.value("Date (2)")) == ("$1,000.00", "$250.00", "09/20/2026")



def test_rows_leaving_an_end_column_blank_stay_in_the_table():
    """A workbook or CSV row is written from its first filled cell to its last, so a row with no note (or no
    first cell) has fewer labels; it is still a row of the same table, and a total covers it."""
    import io

    from openpyxl import Workbook

    from controller_inbox import documents

    book = Workbook()
    ws = book.active
    ws.append(["Vendor", "Amount", "Notes"])
    ws.append(["Acme", 1200, "disputed"])
    ws.append(["Globex", 800, None])
    ws.append(["Hooli", 300, None])
    ws.append(["Initech", 450, "paid late"])
    buf = io.BytesIO()
    book.save(buf)
    text = documents.extract_document("ap.xlsx", "", buf.getvalue())
    found = tables_in(text)
    assert len(found) == 1, [(t.labels, [r.name for r in t.rows]) for t in found]
    assert found[0].rows[1].value("Notes") == "" and found[0].rows[1].cell("Amount") == "B3 (Amount): 800"
    assert "2,750" in lookup(text, "What is the total amount across all vendors?")
    # The first rows without the column: the table widens when a row has it.
    csv = b"Date,Payee,Amount,Memo\n10/01/2026,Acme,100.00,\n10/02/2026,Globex,50.00,\n10/03/2026,Hooli,25.00,rent\n10/04/2026,Initech,10.00,fee\n"
    found = tables_in(documents.extract_document("bank.csv", "text/csv", csv))
    assert len(found) == 1 and found[0].labels == ("Date", "Payee", "Amount", "Memo"), [(t.labels, len(t.rows)) for t in found]
    assert [row.value("Memo") for row in found[0].rows] == ["", "", "rent", "fee"]


def test_formula_text_after_a_figure_is_not_counted_as_its_decimals():
    """A workbook cell is written "33.3333 (=100/3)": the places are the figure's four, not the formula's."""
    text = (
        '[sheet "Sheet" A1:C5]\n'
        "A2 (Department): Finance | B2 (Headcount): 3 | C2 (Allocated rent): 33.3333 (=100/3)\n"
        "A3 (Department): Sales | B3 (Headcount): 4 | C3 (Allocated rent): 33.3333 (=100/3)\n"
        "A4 (Department): Ops | B4 (Headcount): 2 | C4 (Allocated rent): 33.3333 (=100/3)\n"
        "A5 (Department): Total | B5 (Headcount): 9 | C5 (Allocated rent): 100 (=SUM(C2:C4))\n"
    )
    verdicts = [verify(table) for table in tables_in(text)]
    assert [m for v in verdicts for m in v.mismatched] == [] and sum(v.matched for v in verdicts) == 2, verdicts


class _Canvas:
    """Text placed on a letter-size page, written as a PDF (the few reportlab canvas calls these tests use)."""

    def __init__(self, out: io.BytesIO):
        self.out, self.font, self.ops = out, ("F1", 12), []

    def setFont(self, name: str, size: float) -> None:
        self.font = ("F2" if name.endswith("Bold") else "F1", size)

    def drawString(self, x: float, y: float, text: str) -> None:
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        self.ops.append(f"BT /{self.font[0]} {self.font[1]} Tf {x} {y} Td ({escaped}) Tj ET")

    def showPage(self) -> None:
        pass

    def save(self) -> None:
        stream = "\n".join(self.ops).encode("latin-1")
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
        ]
        pdf = bytearray(b"%PDF-1.4\n")
        offsets = []
        for number, body in enumerate(objects, 1):
            offsets.append(len(pdf))
            pdf += b"%d 0 obj\n" % number + body + b"\nendobj\n"
        xref = len(pdf)
        pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
        pdf += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
        pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
        self.out.write(bytes(pdf))


def _bolt_invoice(number: str, amount: str) -> bytes:
    """A letterhead, a block of details on the right, a parts table and "Amount Due" with no currency mark."""
    out = io.BytesIO()
    c = _Canvas(out)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(72, 720, "Bolt Fasteners Inc")
    c.setFont("Helvetica", 10)
    c.drawString(72, 704, "77 Rivet Lane, Akron OH 44301")
    c.setFont("Helvetica-Bold", 16)
    c.drawString(420, 720, "INVOICE")
    c.setFont("Helvetica", 11)
    for i, (key, value) in enumerate([("Invoice #", number), ("Date", "10/14/2026"), ("Due", "11/13/2026"), ("PO", "PO-9001"), ("Job Site", "Riverside Plant")]):
        c.drawString(380, 690 - 16 * i, key)
        c.drawString(460, 690 - 16 * i, value)
    y = 580
    c.setFont("Helvetica-Bold", 11)
    for x, text in [(72, "Part"), (200, "Description"), (400, "Qty"), (450, "Price"), (510, "Ext")]:
        c.drawString(x, y, text)
    c.setFont("Helvetica", 11)
    for row in [("B-1", "Lag bolts", "100", "2.00", "200.00"), ("B-2", "Washers", "500", "0.10", "50.00")]:
        y -= 18
        for x, text in zip([72, 200, 400, 450, 510], row):
            c.drawString(x, y, text)
    y -= 30
    c.setFont("Helvetica-Bold", 12)
    c.drawString(400, y, "Amount Due")
    c.drawString(510, y, amount)
    c.showPage()
    c.save()
    return out.getvalue()


def test_a_letterhead_beside_the_invoice_details_is_not_read_as_a_table():
    """The vendor's name and address beside "INVOICE" and its details read as lines, not "Bolt Fasteners Inc: not listed"."""
    from controller_inbox import documents

    text = documents.pdf_text(_bolt_invoice("BF-5001", "250.00"))
    assert "not listed | INVOICE" not in text and "Bolt Fasteners Inc: not listed" not in text
    assert "Invoice # BF-5001" in text and "Due 11/13/2026" in text
