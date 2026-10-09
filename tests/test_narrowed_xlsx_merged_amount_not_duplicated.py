"""Fix e8348c1 (_fill_merged_down) copies a vertically merged cell's value into every row it covers, figures
included. One amount merged over two lines (a lump sum for a shipment's two lines) is then counted twice: the
sheet text shows it on both rows and the worked-out total no longer matches the sheet's own Total row."""
import io

from openpyxl import Workbook

from controller_inbox import documents, table_lookup


def _freight() -> str:
    wb = Workbook()
    ws = wb.active
    ws.title = "Freight"
    ws.append(["Shipment", "Amount", "Line"])
    ws.append(["SH-1", 1200.00, "Pallets"])
    ws.append(["SH-2", 800.00, "Crates"])
    ws.append(["SH-2", None, "Fuel surcharge"])
    ws.append(["SH-3", 300.00, "Boxes"])
    ws.append(["Total", 2300.00, None])
    ws.merge_cells("B3:B4")
    out = io.BytesIO()
    wb.save(out)
    return documents.extract_document("freight.xlsx", "", out.getvalue())


def test_merged_amount_is_shown_once():
    text = _freight()
    assert "B4 (Amount): 800" not in text, text


def test_total_of_amount_matches_the_sheet():
    found = table_lookup.answer(_freight(), "What is the total amount?")
    assert found is not None
    assert ("Total of Amount", "2,300") in found.points, found.points
