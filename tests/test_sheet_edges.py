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


def test_a_cell_holding_the_separators_stays_one_cell_of_its_row():
    text = _csv_text("Date,Memo,Amount\n10/01/2026,Rent,950.00\n10/02/2026,ACH | Ref: 4471,1200.00\n10/03/2026,Card,45.20\n10/04/2026,Fee,5.00\n")
    tables = _tables(text, "What is the total amount?", "memo.csv")
    assert len([sheet for sheet in tables.sheets if not sheet.of]) == 1
    assert tables.run("SELECT COUNT(*), ROUND(SUM(amount), 2) FROM t1")[1] == [(4, 2200.2)]
    from controller_inbox import tables as table_text

    line = table_text.labelled_row(["Memo", "Amount"], ["ACH | Ref: 4471", "1,200.00"])
    assert line.count(" | ") == 1


def test_a_heading_with_a_colon_keeps_its_rows_one_table():
    rows = [("Department", "Budget: FY26", "Actual: FY26"), ("Finance", 120000, 118000), ("Sales", 340000, 365000), ("IT", 210000, 199000),
            ("HR", 90000, 93000)]
    tables = _tables(_xlsx_text(rows), "Which departments are over budget?")
    assert len([sheet for sheet in tables.sheets if not sheet.of]) == 1 and tables.sheets[0].rows == 4
    from controller_inbox import tables as table_text

    line = table_text.labelled_row(["Item", "Amount: USD"], ["Freight", "100"])
    assert table_lookup._row(line, "page 1").cells == [("Item", "Freight"), ("Amount - USD", "100")]


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


# Dates -----------------------------------------------------------------------------------------


def test_a_column_of_day_first_dates_is_read_day_first():
    text = _csv_text("Date,Description,Amount\n03/11/2026,Card payment Tesco,45.20\n05/11/2026,Direct debit BT,60.00\n"
                     "19/11/2026,Transfer in,1500.00\n02/12/2026,Standing order rent,950.00\n", "uk.csv")
    table = table_lookup.tables_in(text)[0]
    assert table.kinds["Date"] == "date"
    tables = _tables(text, "What was paid in November?", "uk.csv")
    sql = "SELECT COUNT(*), ROUND(SUM(amount), 2) FROM t1 WHERE date BETWEEN '2026-11-01' AND '2026-11-30'"
    assert tables.run(sql)[1] == [(3, 1605.2)]
    answer = table_lookup.lookup(text, "What was the total amount in November?")
    assert "1,605.20" in answer
    # A column that can be read month first still is (10/03 is October 3).
    us = _csv_text("Date,Amount\n10/03/2026,5.00\n10/15/2026,7.00\n11/02/2026,1.00\n", "us.csv")
    assert _tables(us, "October?", "us.csv").run("SELECT COUNT(*) FROM t1 WHERE date BETWEEN '2026-10-01' AND '2026-10-31'")[1] == [(2,)]


def test_dotted_dates_are_dates():
    assert table_lookup.cell_date("03.10.2026") == (2026, 10, 3)
    text = _csv_text("Datum;Text;Betrag\n01.10.2026;Miete;1234,56\n15.10.2026;Strom;987,00\n02.11.2026;Server;100,00\n", "k.csv")
    tables = _tables(text, "Oktober?", "k.csv")
    assert tables.run("SELECT COUNT(*) FROM t1 WHERE datum BETWEEN '2026-10-01' AND '2026-10-31'")[1] == [(2,)]


def test_a_date_with_a_time_is_a_date():
    for written, when in (("10/15/2026 02:30:00 PM", (2026, 10, 15)), ("10/31/2026 04:05 PM", (2026, 10, 31)),
                          ("2026-10-01T00:00:00", (2026, 10, 1)), ("2026-10-01T00:00:00.000", (2026, 10, 1)), ("2026-10-31 16:05", (2026, 10, 31))):
        assert table_lookup.cell_date(written) == when, written
    text = _csv_text("Posted,Description,Amount\n10/15/2026 02:30:00 PM,Card,500.10\n10/31/2026 04:05 PM,Wire,500.10\n"
                     "11/02/2026 09:00 AM,Fee,12.00\n10/20/2026 01:00 PM,Refund,0.00\n", "bank.csv")
    tables = _tables(text, "What was posted in October?", "bank.csv")
    sql = "SELECT ROUND(SUM(amount), 2) FROM t1 WHERE posted BETWEEN '2026-10-01' AND '2026-10-31'"
    assert tables.run(sql)[1] == [(1000.2,)]
    assert "1,000.20" in table_lookup.lookup(text, "What was the total amount posted in October?")


def test_a_column_of_months_is_stored_as_months():
    from datetime import datetime

    wb = Workbook()
    ws = wb.active
    ws.append(["Month", "Account", "Accrual"])
    for number, value in enumerate((100.5, 110.25, 120.75, 90.5, 80.0), start=1):
        ws.append([datetime(2026, number, 1), "Rent", value])
        ws.cell(number + 1, 1).number_format = "mmm-yy"
    out = io.BytesIO()
    wb.save(out)
    text = documents.extract_document("acc.xlsx", XLSX, out.getvalue())
    tables = _tables(text, "What was the March accrual?", "acc.xlsx")
    assert tables.run("SELECT accrual FROM t1 WHERE month = '2026-03'")[1] == [(120.75,)]
    assert tables.run("SELECT month FROM t1 ORDER BY month DESC LIMIT 1")[1] == [("2026-05",)]
    assert "YYYY-MM" in tables.schema()


def test_excel_s_sep_line_names_the_delimiter():
    text = _csv_text("sep=;\nVendor;Invoice;Amount\nAcme;INV-1;1.234,56\nBeta;INV-2;99,00\n", "ap.csv")
    assert "A2 (Vendor): Acme | B2 (Invoice): INV-1 | C2 (Amount): 1.234,56" in text and "sep=" not in text


def test_a_no_column_heads_its_table():
    for text in (_csv_text("No,Description,Qty,Amount\n1,Paper,10,45.00\n2,Toner,2,180.00\n3,Pens,50,25.00\n", "lines.csv"),
                 _xlsx_text([("No", "Description", "Qty", "Amount"), (1, "Paper", 10, 45.0), (2, "Toner", 2, 180.0), (3, "Pens", 50, 25.0)]),
                 _xlsx_text([("No", "Description", "Qty", "Amount"), (1, "Paper", 10, 45.0), (2, "Toner", 2, 180.0), (3, "Pens", 50, 25.0)], bold=(1,))):
        assert "B3 (Description): Toner" in text
        assert _tables(text, "What is the total amount?").run("SELECT SUM(amount) FROM t1")[1] == [(250.0,)]


def test_month_and_date_headings_head_their_columns_without_bold():
    for heading in (("Sep 2026", "Oct 2026", "Nov 2026"), ("2026-09-30", "2026-10-31", "2026-11-30")):
        text = _xlsx_text([("Account", *heading), ("Rent", 950, 950, 950), ("Payroll", 12000, 12500, 12100), ("Software", 400, 420, 430)])
        assert f"C3 ({heading[1]}): 12,500" in text
        assert _tables(text, "What was payroll in October?")
    text = _xlsx_text([("Account", "Sep 2026", "Oct 2026"), ("Rent", 950.25, 950.25), ("Payroll", 12000.5, 12500.75)])
    assert "C3 (Oct 2026): 12,500.75" in text
    # A first row of data with one year in it is still a row.
    text = _xlsx_text([("Rent", "2026", 950, 950), ("Payroll", "2026", 12000, 12500), ("Software", "2026", 400, 420)])
    assert "A1: Rent" in text and "(Rent)" not in text


# Totals ----------------------------------------------------------------------------------------


def test_a_vendor_named_total_stays_a_row_beside_an_empty_bucket():
    text = _csv_text("Vendor,Current,1-30,31-60,Total\nAcme Supply,1200,100,0,1300\nTotal Quality Logistics,8450,2300,0,10750\n"
                     "Beta Corp,300,150,0,450\nTotal,9950,2550,0,12500\n", "aging.csv")
    rows = [(row.name, row.total) for row in table_lookup.tables_in(text)[0].rows]
    assert ("Total Quality Logistics", False) in rows and rows[-1][1]
    tables = _tables(text, "What is the total?", "aging.csv")
    assert tables.run("SELECT COUNT(*), SUM(total) FROM t1")[1] == [(3, 12500.0)]


# Cells as Excel shows them ---------------------------------------------------------------------


def _formatted_book(rows, formats: dict[int, str]) -> str:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(list(row))
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for column, number_format in formats.items():
        for cells in ws.iter_rows(min_row=2, min_col=column, max_col=column):
            cells[0].number_format = number_format
    out = io.BytesIO()
    wb.save(out)
    return documents.extract_document("book.xlsx", XLSX, out.getvalue())


def test_durations_read_as_hours_and_minutes():
    text = _formatted_book([("Employee", "Hours"), ("Ann", 37.5 / 24), ("Bob", 41.25 / 24), ("Cy", 22 / 24)], {2: "[h]:mm"})
    assert "B2 (Hours): 37:30" in text and "B3 (Hours): 41:15" in text and "B4 (Hours): 22:00" in text
    assert "day" not in text


def test_small_rates_and_formatted_decimals_keep_their_digits():
    text = _formatted_book([("Currency", "Rate"), ("JPY", 0.006712), ("KRW", 0.000731), ("IDR", 0.0000637), ("EUR", 1.083456)], {})
    assert "B2 (Rate): 0.006712" in text and "B3 (Rate): 0.000731" in text and "B4 (Rate): 0.0000637" in text
    text = _formatted_book([("Currency", "Rate"), ("JPY", 0.006712), ("EUR", 1.083456)], {2: "0.000000"})
    assert "B2 (Rate): 0.006712" in text and "B3 (Rate): 1.083456" in text
    # Four decimals stay the most for a figure of one or more, unformatted (100/3).
    assert "33.3333" in _formatted_book([("Dept", "Rent"), ("Ops", 100 / 3), ("IT", 1.5)], {})


def test_a_zero_padded_format_keeps_its_zeros():
    text = _formatted_book([("Vendor ID", "Vendor", "Balance"), (417, "Northwind", 4500.0), (418, "Granite", 12500.5)], {1: "00000"})
    assert "A2 (Vendor ID): 00417" in text and "A3 (Vendor ID): 00418" in text


# What reaches the query ------------------------------------------------------------------------


def test_tables_past_the_ones_loaded_are_named_in_the_schema():
    wb = Workbook()
    wb.remove(wb.active)
    for month in ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"):
        ws = wb.create_sheet(f"{month} 2026")
        ws.append(["Vendor", "Category", "Amount"])
        for row in (("Delta", "Travel", 1000), ("Hilton", "Travel", 500), ("Staples", "Supplies", 75)):
            ws.append(row)
    out = io.BytesIO()
    wb.save(out)
    text = documents.extract_document("E.xlsx", XLSX, out.getvalue())
    schema = _tables(text, "What was the total travel spend for the year?", "E.xlsx").schema()
    assert "not loaded" in schema and '"Jul 2026"' in schema and '"Dec 2026"' in schema


def test_a_variance_worked_out_as_actual_less_budget_is_the_worked_out_column():
    rows = [("Department", "Budget", "Actual", "Variance"), ("Sales", 1000, 1100, 100), ("IT", 5000, 4500, -500), ("HR", 300, 320, 20),
            ("Legal", 400, 400, 0), ("Ops", 700, 650, -50)]
    schema = _tables(_xlsx_text(rows), "Which departments are over budget?").schema()
    assert "= - budget + actual on every row" in schema
    lines = {line.split()[0]: line for line in schema.splitlines() if line.startswith("  ")}
    assert " = " not in lines["budget"] and " = " not in lines["actual"]


def test_header_rows_and_the_sheet_list_are_not_facts():
    text = _csv_text("Region,Region Revenue\nEast,300\nWest,1200\nNorth,300\nSouth,450\n", "r.csv")
    assert _tables(text, "Which region sold most?", "r.csv").facts == []
    rows = [("Pay date", "10/15/2026"), (), ("Employee", "Dept", "Gross"), ("Ann", "Sales", 4100), ("Bob", "IT", 3250), ("Cy", "IT", 2000)]
    facts = [(name, value) for _file, name, value in _tables(_xlsx_text(rows), "Gross for IT?").facts]
    assert facts == [("Pay date", "10/15/2026")]


def test_a_running_balance_listed_newest_first_is_marked():
    lines, balance, rows = ["Posting Date,Description,Amount,Balance"], 10000.0, []
    for day, amount in enumerate([-120.5, 400.0, -75.25, 1200.0, -980.0, 310.0], start=1):
        balance = round(balance + amount, 2)
        rows.append(f"10/{day:02d}/2026,Item {day},{amount:.2f},{balance:.2f}")
    text = _csv_text("\n".join(lines + rows[::-1]), "chase.csv")
    tables = _tables(text, "What is the ending balance?", "chase.csv")
    assert "running balance listed newest first" in tables.schema() and "the first row's is the latest balance" in tables.schema()
    try:
        tables.run("SELECT SUM(balance) FROM t1")
    except ValueError as refused:
        assert "first row's" in str(refused)
    else:
        raise AssertionError("a sum over a running balance runs")


# Wide sheets -----------------------------------------------------------------------------------


def test_columns_past_the_sixtieth_are_said_to_be_left_out():
    heading = ["Account"] + [f"M{n}" for n in range(1, 71)] + ["FY Total"]
    rows = [heading] + [[name] + [100] * 70 + [7000] for name in ("Rent", "Payroll", "Travel")]
    data = _book(rows)
    text = documents.extract_document("wide.xlsx", XLSX, data)
    assert "[Columns BI–BT not shown" in text
    cells = documents.read_cells(data, "Sheet", "A2:BT2")
    assert "Columns BI–BT not shown" in cells
