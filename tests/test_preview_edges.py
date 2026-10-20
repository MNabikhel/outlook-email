"""The attachment preview (sheet_preview.py, preview.js) in the cases found in review: Excel's number formats,
merges over hidden rows, hidden column ranges, .xls formats, Word merges, nested tables, content controls, text boxes
and moves, CSV quoting, and the thumbnail. Each test states the Excel or Word behaviour it relies on."""

from __future__ import annotations

import io

import pytest
from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
from openpyxl import Workbook
from openpyxl.styles import Font

from controller_inbox import sheet_preview
from controller_inbox.sheet_preview import display


def _xlsx(book: Workbook) -> bytes:
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def _cells(sheet: dict) -> dict:
    return {(row["n"], cell["c"]): cell for row in sheet["rows"] for cell in row["cells"]}


# display(): Excel number formats --------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, fmt, excel",
    [
        # Built-in 46 / 20-style elapsed time: Excel shows 1.25 days as 30:00, the brackets are not literal.
        (1.25, "[h]:mm", "30:00"),
        (1.25, "[h]:mm:ss", "30:00:00"),
        # Built-in 12 / 13 fractions.
        (0.75, "# ?/?", "3/4"),
        (1.5, "# ?/?", "1 1/2"),
        # Built-in 49 "@" (Text) on a number: Excel shows the number as typed.
        (123, "@", "123"),
        # Conditional sections: 500 fails [>=1000] so Excel uses the second section.
        (500, '[>=1000]#,##0,"K";0', "500"),
        # A quoted "%" is a literal: no x100 scaling.
        (1.234, '0.0"%"', "1.2%"),
        # Empty sections hide the value (";;;" is Excel's standard "hide cell values" format).
        (5, ";;;", ""),
        (-3, "0;", ""),
        (0, "0;-0;;@", ""),
        # Special formats: digit placeholders separated by literals.
        (123456789, "000-00-0000", "123-45-6789"),
        (12, "00-00", "00-12"),
        # General on a small number: Excel writes 1.234E-05, never Python's lowercase "e".
        (1.234e-05, "General", "1.234E-05"),
    ],
)
def test_display_matches_excel(value, fmt, excel):
    assert display(value, fmt)[0] == excel


def test_elapsed_time_cell_in_a_real_workbook_reads_as_hours():
    # A timesheet total: 30 hours in a [h]:mm cell. openpyxl hands it over as a timedelta.
    book = Workbook()
    ws = book.active
    ws["A1"] = 1.25
    ws["A1"].number_format = "[h]:mm"
    cells = _cells(sheet_preview.preview(_xlsx(book), "hours.xlsx")["sheets"][0])
    assert cells[(1, 1)]["v"] == "30:00"


# _sheet(): hidden columns and merges ----------------------------------------------------------------


def test_a_grouped_hidden_column_range_is_hidden_in_full():
    # Excel's "Hide" on B:D (or a collapsed outline group) is stored as one <col min=2 max=4 hidden=1>; Excel hides
    # all three columns. openpyxl keys the dimension under "B" only, so C and D come back shown.
    book = Workbook()
    ws = book.active
    ws.append(["Name", "Cost", "Margin", "Markup", "Price"])
    ws.column_dimensions.group("B", "D", hidden=True)
    cols = sheet_preview.preview(_xlsx(book), "quote.xlsx")["sheets"][0]["cols"]
    assert [c["letter"] for c in cols if c["hidden"]] == ["B", "C", "D"]


def test_a_merge_over_a_hidden_row_spans_only_the_rows_shown():
    # A5:A7 merged, row 6 hidden: the grid has rows 5 and 7 only, so the cell must span 2 table rows. With rs=3 the
    # <td rowspan=3> swallows row 8's first slot and every cell of row 8 moves one column right.
    book = Workbook()
    ws = book.active
    ws["A5"] = "Region"
    ws.merge_cells("A5:A7")
    ws.row_dimensions[6].hidden = True
    ws["B5"], ws["B7"], ws["A8"], ws["B8"] = "x", "y", "after", "z"
    sheet = sheet_preview.preview(_xlsx(book), "regions.xlsx")["sheets"][0]
    shown = [row["n"] for row in sheet["rows"]]
    assert shown[shown.index(5):] == [5, 7, 8]
    assert _cells(sheet)[(5, 1)]["rs"] == 2


# _xls(): Excel 97-2003 ---------------------------------------------------------------------------------


def test_an_xls_time_is_not_shown_as_a_date_in_1899(monkeypatch):
    # An .xls time-only cell (0.5 = 12:00 PM). Excel shows a time; it never shows 12/31/1899. The reader opens the
    # book with formatting_info=False and formats every cell as General, so the cell's format is never used.
    import xlrd
    from xlrd.sheet import Cell

    class Sheet:
        name, nrows, ncols, visibility = "Times", 1, 1, 0

        def cell(self, r, c):
            return Cell(xlrd.XL_CELL_DATE, 0.5)

    class Book:
        nsheets, datemode = 1, 0

        def sheet_by_index(self, i):
            return Sheet()

        def release_resources(self):
            pass

    monkeypatch.setattr(xlrd, "open_workbook", lambda **kw: Book())
    data = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 504
    value = sheet_preview.preview(data, "times.xls")["sheets"][0]["rows"][0]["cells"][0]["v"]
    assert "1899" not in value


# _csv() --------------------------------------------------------------------------------------------------


def test_a_csv_with_apostrophes_is_split_as_excel_splits_it():
    # Excel's CSV import only treats double quotes as quoting; csv.Sniffer guesses "'" and merges the fields.
    data = "Vendor,Note,Amt\nAcme,'Rent, March',5\nBeta,'Power, April',6\n".encode()
    row = sheet_preview.preview(data, "export.csv")["sheets"][0]["rows"][1]
    assert [c["v"] for c in row["cells"]] == ["Acme", "'Rent", " March'", "5"]


# docx_blocks() -----------------------------------------------------------------------------------------


def _docx(document) -> bytes:
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def test_a_horizontally_merged_word_cell_keeps_its_span():
    # "Q3" spans two columns in Word. The block drops the covered cell and says nothing of the span, so the grid
    # draws "Total" over column 2 and the row has 2 cells where the others have 3.
    document = Document()
    table = document.add_table(rows=2, cols=3)
    table.cell(0, 0).merge(table.cell(0, 1))
    table.cell(0, 0).text, table.cell(0, 2).text = "Q3", "Total"
    for i, v in enumerate(["Rent", "Power", "1,200"]):
        table.cell(1, i).text = v
    [block] = sheet_preview.docx_blocks(_docx(document))
    widths = {len(row) for row in block["rows"]}
    assert len(widths) == 1 or any(isinstance(cell, dict) for row in block["rows"] for cell in row), block


def test_a_nested_word_table_keeps_its_text():
    document = Document()
    outer = document.add_table(rows=1, cols=1)
    inner = outer.cell(0, 0).add_table(rows=1, cols=2)
    inner.cell(0, 0).text, inner.cell(0, 1).text = "Retainer", "$5,000"
    text = repr(sheet_preview.docx_blocks(_docx(document)))
    assert "Retainer" in text and "$5,000" in text


def test_text_in_a_content_control_is_shown():
    # Body-level <w:sdt> (cover pages, contract templates, form fields) holds ordinary paragraphs.
    document = Document()
    sdt = parse_xml(f'<w:sdt {nsdecls("w")}><w:sdtContent><w:p><w:r><w:t>Payment due in 30 days</w:t></w:r></w:p></w:sdtContent></w:sdt>')
    document.element.body.insert(0, sdt)
    assert "Payment due in 30 days" in repr(sheet_preview.docx_blocks(_docx(document)))


def test_a_text_box_is_not_shown_twice():
    # Word saves every text box twice: <mc:Choice> (DrawingML) and <mc:Fallback> (VML). Only one is read.
    document = Document()
    p = document.add_paragraph("See ")
    p._p.append(parse_xml(
        f'<w:r {nsdecls("w")} xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
        'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" xmlns:v="urn:schemas-microsoft-com:vml">'
        "<mc:AlternateContent><mc:Choice Requires=\"wps\"><w:drawing><wps:txbx><w:txbxContent><w:p><w:r><w:t>BOX</w:t></w:r></w:p>"
        "</w:txbxContent></wps:txbx></w:drawing></mc:Choice><mc:Fallback><w:pict><v:shape><v:textbox><w:txbxContent><w:p><w:r>"
        "<w:t>BOX</w:t></w:r></w:p></w:txbxContent></v:textbox></v:shape></w:pict></mc:Fallback></mc:AlternateContent></w:r>"
    ))
    [block] = sheet_preview.docx_blocks(_docx(document))
    assert block["text"].count("BOX") == 1, block


def test_moved_text_reads_once_with_changes_accepted():
    # Tracked move: <w:moveFrom> is the old place (gone once accepted), <w:moveTo> the new one.
    document = Document()
    document.element.body.insert(0, parse_xml(
        f'<w:p {nsdecls("w")}><w:moveFrom w:id="1" w:author="a" w:date="2026-01-01T00:00:00Z"><w:r><w:t>Net 30. </w:t></w:r>'
        '</w:moveFrom><w:r><w:t>Terms: </w:t></w:r><w:moveTo w:id="2" w:author="a" w:date="2026-01-01T00:00:00Z"><w:r>'
        "<w:t>Net 30.</w:t></w:r></w:moveTo></w:p>"
    ))
    assert sheet_preview.docx_blocks(_docx(document))[0]["text"] == "Terms: Net 30."


# Front end (Playwright) ----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def browser():
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    from liveserver import chromium_path

    path = chromium_path()
    if path is None:
        pytest.skip("no Chromium for Playwright on this machine")
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=path)
        yield b
        b.close()


def _review_book() -> bytes:
    book = Workbook()
    ws = book.active
    ws.title = "Quote"
    ws.append(["Name", "Cost", "Price"])
    ws.append(["Steel", 900, 1200])
    ws.column_dimensions["B"].hidden = True  # the cost column, hidden by the sender
    ws["A5"] = "Region"
    ws.merge_cells("A5:A7")
    ws.row_dimensions[6].hidden = True
    ws["B5"], ws["B7"], ws["A8"], ws["C8"] = "x", "y", "after", "z"
    ws["C2"].font = Font(color="FF0000FF")
    ws["A3"] = -5
    ws["A3"].number_format = "0;[Red]-0"
    ws["A3"].font = Font(color="FF0000FF")
    return _xlsx(book)


@pytest.fixture
def served(settings, store, browser):
    from controller_inbox import web
    from controller_inbox.folder_mail import ingest_folder
    from liveserver import serving
    from msgfactory import XLSX, write_msg

    settings.trusted_domains = "taz.com"
    settings.ensure_data_dir()
    write_msg(settings.inbox_incoming / "q.msg", "Quote", "Attached.", sender_name="Harbor Steel", sender_email="ar@taz.com",
              attachments=[("quote.xlsx", _review_book(), XLSX)])
    [email] = ingest_folder(store, settings)
    context = browser.new_context()
    context.set_default_timeout(15000)
    with serving(web.create_app(settings, store)) as base:
        yield context.new_page(), base, email
    context.close()


def _open_grid(page, base, email):
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base}/app/mail/{email.id}/file/1?tab=preview")
    page.wait_for_selector(".xl-grid")


def test_grid_cells_after_a_merge_over_a_hidden_row_stay_in_their_columns(served):
    page, base, email = served
    _open_grid(page, base, email)
    # Column A's header and the cell in row 8 that holds "after" (column A in Excel) should line up.
    got = page.evaluate("""() => {
      const head = [...document.querySelectorAll('.xl-grid thead th')].find((th) => th.textContent === 'A').getBoundingClientRect();
      const row = [...document.querySelectorAll('.xl-grid tbody tr')].find((tr) => tr.querySelector('th').textContent === '8');
      const td = [...row.querySelectorAll('td')].find((td) => td.textContent === 'after').getBoundingClientRect();
      return [head.left, td.left];
    }""")
    assert abs(got[0] - got[1]) < 1, got


def test_a_red_negative_format_wins_over_the_font_colour(served):
    # Excel: a [Red] section in the number format overrides the cell's font colour.
    page, base, email = served
    _open_grid(page, base, email)
    colour = page.evaluate("""() => getComputedStyle([...document.querySelectorAll('.xl-grid td')].find((td) => td.textContent === '-5')).color""")
    assert colour == "rgb(192, 0, 0)"


def test_the_attachment_thumbnail_leaves_out_hidden_columns(served):
    page, base, email = served
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base}/app/mail/{email.id}")
    page.wait_for_selector(".file-thumb-grid")
    text = page.text_content(".file-thumb-grid")
    assert "Cost" not in text and "900" not in text, text
