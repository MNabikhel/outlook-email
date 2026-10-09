"""Camelot as a second reader of a PDF's tables (camelot_tables.py): which tables a page keeps from each reader, how
Camelot's tables are written, and that a PDF reads as before without Camelot."""

from __future__ import annotations

import pytest

from controller_inbox import camelot_tables, documents
from controller_inbox.table_lookup import tables_in
from pdffactory import Text, build_pdf, sheet_rows

# An annual report's table as Camelot gives it: the headings over two lines, "$" set apart from its figures.
REPORT = [
    ["", "", "December 31", "", ""],
    ["Millions of dollars", "", "2009", "", "2008"],
    ["Completion and Production", "$", "5,920", "$", "5,936"],
    ["Drilling and Evaluation", "", "6,204", "", "6,205"],
    ["Shared assets", "", "914", "", "648"],
    ["Total", "$", "16,538", "$", "14,385"],
]


def test_camelots_table_is_cleaned_and_written_as_closedesk_writes_tables():
    grid = camelot_tables.clean(REPORT)
    assert grid[2] == ["Completion and Production", "$5,920", "$5,936"], "the $ joins its figure"
    assert camelot_tables.is_data(grid)
    text = camelot_tables.as_text(grid)
    # The heading lines as printed go above the table; the column names join them.
    assert text.startswith("[heading]\nDecember 31\nMillions of dollars | 2009 | 2008\n\n[table]\n")
    [table] = tables_in(text)
    rows = {row.cells[0][1]: dict(row.cells) for row in table.rows}
    assert rows["Drilling and Evaluation"]["December 31 2009"] == "6,204"
    assert rows["Total"]["December 31 2008"] == "$14,385", "a heading over several columns heads each of them"


def test_prose_read_as_a_table_is_not_kept():
    prose = [
        ["Our trade receivables are generally not collateralized. At December 31, 2009, 26% of our gross"],
        ["trade receivables were from customers in the United States."],
    ]
    assert not camelot_tables.is_data(camelot_tables.clean(prose))
    words = [["Dear Accounts Payable,", ""], ["Please find our statement attached.", "Kind regards"], ["Maya Chen", "Controller"]]
    assert not camelot_tables.is_data(camelot_tables.clean(words))
    labels = [["Invoice no.", "NW-20931"], ["Due date", "10/30/2026"]]
    assert not camelot_tables.is_data(camelot_tables.clean(labels)), "two lines of details are not a table of figures"


OURS = "[heading]\nSegment assets\n\n[table]\nLine | 2009 | 2008\nLine: Completion and Production | 2009: $5,920 | 2008: $5,936"


def test_closedesks_table_stays_and_camelots_fuller_one_is_added_beside_it():
    grid = camelot_tables.clean(REPORT)
    text = camelot_tables.combine(OURS, [grid])
    assert text.startswith(OURS), "CloseDesk's own table is never taken away"
    assert "Shared assets" in text and "$16,538" in text, "Camelot's table reads rows CloseDesk's didn't, so it is added"


def test_a_table_camelot_reads_no_better_is_left_out():
    ours = OURS + "\nLine: Drilling and Evaluation | 2009: 6,204 | 2008: 6,205\nLine: Shared assets | 2009: 914 | 2008: 648\nLine: Total | 2009: $16,538 | 2008: $14,385"
    assert camelot_tables.combine(ours, [camelot_tables.clean(REPORT)]) == ours


def test_a_table_only_one_reader_found_is_added_once():
    ours = OURS + "\nLine: Drilling and Evaluation | 2009: 6,204 | 2008: 6,205\nLine: Shared assets | 2009: 914 | 2008: 648\nLine: Total | 2009: $16,538 | 2008: $14,385\nLine: Corporate | 2009: 3,500 | 2008: 1,596"
    other = camelot_tables.clean([["Region", "2009", "2008"], ["United States", "5,201", "6,112"], ["Latin America", "1,830", "2,077"], ["Europe/Africa/CIS", "4,011", "4,384"]])
    same = camelot_tables.clean(REPORT)
    text = camelot_tables.combine(ours, [same, other])
    assert text.count("5,920") == 1, "the table both read in full is written once"
    assert "Latin America" in text, "the table only Camelot found is added"


def test_without_camelot_a_pdf_reads_as_before(monkeypatch):
    pdf = build_pdf([[Text(72, 760, "Balance sheet", size=14, bold=True), *sheet_rows([(72, "left"), (400, "right")], [["Line", "Amount"], ["Cash", "12,500.00"], ["Receivables", "18,400.00"], ["Total", "30,900.00"]])]])
    monkeypatch.setattr(camelot_tables, "available", lambda: False)
    before = documents.extract_document("bs.pdf", "application/pdf", pdf)
    monkeypatch.setattr(camelot_tables, "available", lambda: True)
    monkeypatch.setattr(camelot_tables, "read", lambda data, pages: {})
    assert documents.extract_document("bs.pdf", "application/pdf", pdf) == before


def test_camelot_failing_on_a_file_leaves_it_as_read(monkeypatch):
    pytest.importorskip("camelot")
    import camelot

    def broken(*_args, **_kwargs):
        raise RuntimeError("cannot read this PDF")

    monkeypatch.setattr(camelot, "read_pdf", broken)
    assert camelot_tables.read(b"%PDF-1.4 not really", [1]) == {}


def test_camelot_reads_a_real_pdfs_table_when_installed():
    pytest.importorskip("camelot")
    pdf = build_pdf([[*sheet_rows([(72, "left"), (300, "right"), (420, "right")], [["Segment", "2009", "2008"], ["Completion", "5,920", "5,936"], ["Drilling", "6,204", "6,205"], ["Shared", "914", "648"]])]])
    found = camelot_tables.read(pdf, [1])
    assert 1 in found and any("6,204" in cell for grid in found[1] for row in grid for cell in row)


def test_a_letter_gets_no_table_from_camelot():
    pytest.importorskip("camelot")
    lines = [
        "Dear Accounts Payable,",
        "Please find our statement for September attached. The balance of $12,480.00 is due on October 30, 2026.",
        "Our remittance address is unchanged: 4120 Commerce Pkwy, Spokane WA 99202. Call (509) 555-0177 with",
        "any questions about invoices 20931, 20944 or 21077, and we will reply within two business days.",
        "Thank you for your business.",
        "Kind regards,",
        "Maya Chen, Controller",
    ]
    pdf = build_pdf([[Text(72, 740 - 18 * i, line, size=10) for i, line in enumerate(lines)]])
    assert "[table]" not in documents.extract_document("letter.pdf", "application/pdf", pdf)


def test_a_page_that_is_one_table_is_not_read_twice():
    # A page that is only a table is written without a [table] line; Camelot's reading of it adds nothing.
    rows = [(f"Vendor {i:02d}", f"INV-{1000 + i}", f"{i * 137 + 100:,}.00") for i in range(1, 8)]
    ours = "Vendor | Invoice | Amount\n" + "\n".join(f"Vendor: {v} | Invoice: {n} | Amount: {a}" for v, n, a in rows)
    grid = camelot_tables.clean([["Vendor", "Invoice", "Amount"], *[list(row) for row in rows]])
    assert camelot_tables.combine(ours, [grid]) == ours


def test_a_list_by_date_read_with_two_cells_run_together_is_not_added():
    days = [("10/1", "Thu", "OFF", "8-5", "7-4"), ("10/2", "Fri", "8-5", "7-4", "9-6"), ("10/5", "Mon", "7-4", "OFF", "8-5"), ("10/6", "Tue", "9-6", "8-5", "OFF")]
    ours = "[table]\nDate | Day | Maya Chen | Sam Ortiz | Li Wei\n" + "\n".join(
        f"Date: {d} | Day: {w} | Maya Chen: {a} | Sam Ortiz: {b} | Li Wei: {c}" for d, w, a, b, c in days
    )
    grid = [["Date Day", "Maya Chen", "Sam Ortiz", "Li Wei"], *[[f"{d} {w}", a, b, c] for d, w, a, b, c in days]]
    assert camelot_tables.combine(ours, [grid]) == ours


def test_dot_leaders_are_dropped_and_a_long_cell_of_them_is_quick():
    import time

    assert camelot_tables.clean([["Revenue . . . . . .", ". . . .", "1,200"]])[0] == ["Revenue", "1,200"]
    assert camelot_tables.clean([["Acme Inc.", "", "1,200"]])[0] == ["Acme Inc.", "1,200"], "one dot ends a name"
    start = time.monotonic()
    camelot_tables.clean([[". " * 16000 + "x", "1,200"]])
    assert time.monotonic() - start < 1
