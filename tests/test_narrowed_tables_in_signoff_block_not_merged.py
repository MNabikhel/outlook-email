"""Fix e8348c1 (table_lookup._same_sheet_columns) folds any later row on the same sheet whose labels are an
in-order subset of the table's into the table, across a blank row. A sign-off block under an AP list
("Prepared by | J. Smith", "Approved by | K. Lee", "Batch | B-77") becomes three more invoices: the count goes
from 3 to 6 and the "N of the table's rows" wording is wrong."""
import io

from openpyxl import Workbook

from controller_inbox import documents, table_lookup


def _ap() -> str:
    wb = Workbook()
    ws = wb.active
    ws.title = "AP"
    ws.append(["Vendor", "Invoice", "Amount", "Due"])
    ws.append(["Northwind", "INV-1", 1200, "2026-10-30"])
    ws.append(["Contoso", "INV-2", 800, "2026-11-02"])
    ws.append(["Fabrikam", "INV-3", 300, "2026-11-05"])
    ws.append([])
    ws.append(["Prepared by", "J. Smith"])
    ws.append(["Approved by", "K. Lee"])
    ws.append(["Batch", "B-77"])
    out = io.BytesIO()
    wb.save(out)
    return documents.extract_document("ap.xlsx", "", out.getvalue())


def test_signoff_rows_are_not_table_rows():
    tables = table_lookup.tables_in(_ap())
    main = next(t for t in tables if "Amount" in t.labels)
    assert len(main.rows) == 3, [r.cells for r in main.rows]


def test_count_of_invoices():
    found = table_lookup.answer(_ap(), "How many invoices are there?")
    assert found is not None and any(": 3 rows." in line for line in found.lines), found and found.lines
