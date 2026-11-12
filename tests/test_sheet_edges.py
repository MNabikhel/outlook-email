"""Workbooks and CSV files laid out the way real exports are, read into rows, tables and queries that say what
the file says: long sheets cut short, title rows over the headings, code columns, comma decimals and the like."""

from __future__ import annotations

import io
from decimal import Decimal

from openpyxl import Workbook
from openpyxl.styles import Font

from controller_inbox import documents, table_lookup, table_query

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _book(rows, *, title: str = "Sheet", bold: tuple[int, ...] = ()) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = title
    for row in rows:
        ws.append(list(row))
    for number in bold:
        for cell in ws[number]:
            cell.font = Font(bold=True)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _xlsx_text(rows, **kwargs) -> str:
    return documents.extract_document("book.xlsx", XLSX, _book(rows, **kwargs))


def _csv_text(text: str, name: str = "file.csv") -> str:
    return documents.extract_document(name, "text/csv", text.encode())


def _tables(text: str, question: str = "", name: str = "book.xlsx") -> table_query.Tables:
    return table_query.Tables([(name, text)], question)


# Long sheets -----------------------------------------------------------------------------------


def test_a_sheet_cut_short_says_so_in_the_query_and_the_lookup():
    rows = [("Vendor", "Invoice", "Amount")] + [(f"Vendor {i}", f"INV-{i}", 10.5) for i in range(1500)]
    for text in (_xlsx_text(rows), _csv_text("\n".join(",".join(map(str, row)) for row in rows))):
        found = table_lookup.tables_in(text)
        assert found and found[0].left_out == 501
        tables = _tables(text, "What is the total amount?")
        schema = tables.schema()
        assert "501 more rows" in schema and "not the whole sheet" in schema
        names, result, more = tables.run("SELECT SUM(amount) FROM t1")
        shown = tables.render(table_query.Result("", "SELECT SUM(amount) FROM t1", names, result, more))
        assert "501 more rows" in shown
        answer = table_lookup.lookup(text, "What is the total amount?")
        assert answer and not answer.startswith(table_lookup.WORKED_HEAD)
        assert "501 more rows" in answer


# Header rows -----------------------------------------------------------------------------------


def test_a_two_cell_title_row_over_the_headings_is_not_the_header():
    for bold in ((3,), (1, 3)):
        rows = [
            ("Northwind Holdings", None, None, "As of 10/31/2026"),
            (),
            ("Vendor", "Invoice", "Due", "Amount"),
            ("Acme", "INV-1", "11/15/2026", 1200.5),
            ("Beta", "INV-2", "11/20/2026", 300),
            ("Gamma", "INV-3", "12/01/2026", 4500),
        ]
        text = _xlsx_text(rows, bold=bold)
        assert "A4 (Vendor): Acme" in text and "D4 (Amount): 1,200.5" in text
        assert _tables(text).run("SELECT SUM(amount) FROM t1")[1] == [(6000.5,)]
    text = _csv_text("Northwind Holdings,,,As of 10/31/2026\nVendor,Invoice,Due,Amount\nAcme,INV-1,11/15/2026,1200.50\nBeta,INV-2,11/20/2026,300\n")
    assert "A3 (Vendor): Acme" in text


# Code columns ----------------------------------------------------------------------------------


def test_a_dept_column_picks_rows_and_is_never_added_up():
    text = _csv_text("GL,Dept,Description,Amount\n6100,210,Rent,950.00\n6200,320,Software,420.00\n6300,210,Travel,1310.00\n6100,410,Rent,800.00\n")
    assert table_lookup.tables_in(text)[0].kinds["Dept"] == "text"
    answer = table_lookup.lookup(text, "how much in total for dept 210")
    assert "2,260.00" in answer and 'Total of "Dept"' not in answer


def test_an_amount_headed_with_a_code_word_stays_a_figure():
    text = _csv_text("Department,Dept Budget\nFinance,50000\nIT,120000\nSales,50000\nHR,80000\nOps,30000\nLegal,80000\n")
    tables = _tables(text, "Which department has the largest budget?", "b.csv")
    assert tables.run("SELECT department FROM t1 ORDER BY dept_budget DESC LIMIT 1")[1] == [("IT",)]
    text = _csv_text("Region,Region Revenue\nEast,300\nWest,1200\nNorth,300\nSouth,450\n")
    tables = _tables(text, "Which regions are over 500?", "r.csv")
    assert tables.run("SELECT region FROM t1 WHERE region_revenue > 500")[1] == [("West",)]


def test_pay_and_fee_columns_stay_figures():
    for heading in ("Employee Salary", "Project Fees", "Store Rent"):
        text = _csv_text(f"Name,{heading}\nAna,85000\nBen,72000\nCy,72000\nDee,96000\nEli,85000\n")
        tables = _tables(text, "Who is over 9000?", "p.csv")
        column = table_query._ident(heading, set())
        assert len(tables.run(f"SELECT name FROM t1 WHERE {column} > 9000")[1]) == 5, heading


def test_unique_ids_keep_their_digits_as_written():
    text = _csv_text("Invoice No,Vendor ID,Vendor,Amount\n0012345,00417,Northwind,4500.00\n0012346,00418,Granite,12500.50\n"
                     "0012347,00419,Bluebird,980.00\n0012348,00420,Kessler,7300.00\n")
    tables = _tables(text, "Which invoices are over 5,000?", "ap.csv")
    sql = "SELECT invoice_no, vendor_id FROM t1 WHERE amount > 5000"
    names, rows, more = tables.run(sql)
    shown = tables.render(table_query.Result("", sql, names, rows, more))
    assert "0012346" in shown and "00418" in shown
    text = _xlsx_text([("Invoice No", "GL Account", "Amount"), (100235, 6100, 800), (100236, 6200, 1200), (100237, 6300, 450), (100238, 6400, 90)])
    tables = _tables(text, "Which invoices are over 500?")
    sql = "SELECT invoice_no FROM t1 WHERE amount > 500"
    names, rows, more = tables.run(sql)
    assert "100,23" not in tables.render(table_query.Result("", sql, names, rows, more))
    assert table_query._codes("Check #", ["1001", "1002", "1003", "1004"])


def test_numbers_typed_into_an_id_or_year_column_are_written_as_excel_shows_them():
    rows = [("Invoice No", "Fiscal Year", "Vendor", "Amount"), (100235, 2026, "Globex", 800), (100236, 2026, "Initech", 1200)]
    text = _xlsx_text(rows)
    assert "A2 (Invoice No): 100235" in text and "B2 (Fiscal Year): 2026" in text and "D3 (Amount): 1,200" in text
    assert "800" in table_lookup.lookup(text, "What is the amount on invoice 100235?")


# Cells with no heading -------------------------------------------------------------------------


def test_row_labels_under_a_blank_a1_heading_make_a_table():
    rows = [(None, "Q3 2026", "Q3 2025"), ("Revenue", 1250000, 1100000), ("Cost of sales", -610000, -560000),
            ("Gross profit", 640000, 540000), ("Opex", -410000, -395000)]
    text = _xlsx_text(rows, bold=(1,))
    tables = _tables(text, "What was revenue in Q3 2026?")
    assert tables.schema()
    assert "1,250,000" in table_lookup.lookup(text, "What was revenue in Q3 2026?")


def test_a_note_past_the_last_heading_keeps_its_row():
    rows = [("Vendor", "Invoice", "Amount"), ("Northwind", "INV-1", 4500), ("Granite", "INV-2", 12500, "Disputed"),
            ("Bluebird", "INV-3", 980), ("Kessler", "INV-4", 7300)]
    text = _xlsx_text(rows)
    assert _tables(text, "What is the total amount?").run("SELECT SUM(amount), COUNT(*) FROM t1")[1] == [(25280.0, 4)]
    assert "12,500" in table_lookup.lookup(text, "How much do we owe Granite?")


def test_an_unheaded_row_number_column_keeps_the_table():
    rows = [(None, "Vendor", "Invoice", "Amount")] + [
        (i, *row) for i, row in enumerate([("Northwind", "INV-100", 4500), ("Granite", "INV-200", 12500.5), ("Bluebird", "INV-300", 980)], start=1)
    ]
    text = _xlsx_text(rows)
    assert _tables(text, "What is the total amount?").run("SELECT SUM(amount) FROM t1")[1] == [(17980.5,)]


# Two heading rows ------------------------------------------------------------------------------


def test_a_first_row_of_text_under_a_blank_heading_is_a_row_not_a_second_heading():
    rows = [("Vendor", "Contact", None, "Status"), ("Acme", "Jane Roe", "Primary", "Active"), ("Beta", "John Doe", "Backup", "On hold"),
            ("Gamma", "Ann Lee", "Primary", "Active")]
    text = _xlsx_text(rows, bold=(1,))
    assert "A2 (Vendor): Acme" in text and "D2 (Status): Active" in text
    assert "Active" in table_lookup.lookup(text, "What is Acme's status?")
    text = _csv_text("Employee,Department,,Location\nJonathan Alvarez,Finance,Contractor,Chicago\nMaya Chen,Sales,Employee,Austin\n"
                     "Priya Raman,IT,Employee,Remote\n")
    assert "A2 (Employee): Jonathan Alvarez" in text and "D2 (Location): Chicago" in text


def test_a_group_heading_row_over_some_columns_heads_the_row_under_it():
    for top in ((None, None, "Q3 2026", None, None, "YTD"), (None, None, "Q3 2026", None, None, "Q4 2026", None, None, "YTD")):
        months = ("Jul", "Aug", "Sep") if len(top) == 6 else ("Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
        rows = [top, ("Account", "Description", *months, "Total"),
                (6100, "Rent", *([12000] * len(months)), 12000 * len(months)), (6300, "Travel", *([2100] * len(months)), 2100 * len(months)),
                (6400, "Meals", *([800] * len(months)), 800 * len(months))]
        text = _xlsx_text(rows)
        assert "A4 (Account): 6300 | B4 (Description): Travel" in text and "Aug): 2,100" in text
        assert "2,100" in table_lookup.lookup(text, "What was travel in August?")


# Figures written other ways --------------------------------------------------------------------


def test_comma_decimals_without_thousands_separators_are_figures():
    assert table_lookup._number("1234,56") == Decimal("1234.56")
    assert table_lookup._number("-1234,56") == Decimal("-1234.56")
    assert table_lookup._number("12500,00") == Decimal("12500.00")
    assert table_lookup._number("1 234,56") == Decimal("1234.56")
    assert table_lookup._number("1 234,56") == Decimal("1234.56")
    text = _csv_text("Datum;Text;Betrag\n01.10.2026;Miete;1234,56\n02.10.2026;Strom;987,00\n03.10.2026;Server;12500,00\n"
                     "04.10.2026;Porto;45,10\n05.10.2026;Beratung;2000,00\n", "konto.csv")
    assert _tables(text, "Summe Betrag?", "konto.csv").run("SELECT ROUND(SUM(betrag), 2) FROM t1")[1] == [(16766.66,)]
    text = _csv_text("Date;Libelle;Montant\n1;Loyer;1 234,56\n2;Eau;45,75\n3;Serveur;1 455,00\n", "fr.csv")
    assert _tables(text, "Total montant?", "fr.csv").run("SELECT ROUND(SUM(montant), 2) FROM t1")[1] == [(2735.31,)]


def test_a_plus_sign_marks_money_in():
    assert table_lookup._number("+4500.00") == Decimal("4500.00")
    assert table_lookup._number("+$1,200.00") == Decimal("1200.00")
    text = _csv_text("Date,Description,Amount\n10/01/2026,Payroll,+4500.00\n10/02/2026,Rent,-950.00\n10/03/2026,Refund,+20.00\n"
                     "10/04/2026,Fees,-25.20\n10/05/2026,Supplies,-25.00\n", "bank.csv")
    tables = _tables(text, "What is the total amount?", "bank.csv")
    assert tables.run("SELECT ROUND(SUM(amount), 2), COUNT(amount) FROM t1")[1] == [(3519.8, 5)]


def test_a_currency_after_the_figure_or_a_code_before_it_is_still_a_figure():
    for written, value in (("1.250,00 €", "1250.00"), ("89,90 €", "89.90"), ("1,234.56 USD", "1234.56"), ("EUR 1.234,56", "1234.56"),
                           ("USD 1,200.00", "1200.00"), ("1,234.56 USD-", "-1234.56")):
        assert table_lookup._number(written) == Decimal(value), written
    text = _csv_text("Datum;Beschreibung;Betrag\n01.10.2026;Miete;1.250,00 €\n02.10.2026;Strom;89,90 €\n03.10.2026;Server;4.500,00 €\n", "k.csv")
    assert _tables(text, "Summe?", "k.csv").run("SELECT ROUND(SUM(betrag), 2) FROM t1")[1] == [(5839.9,)]
