"""Fix e8348c1 makes every <table> of a web-page .xls a sheet of its own. Grid exports with a fixed header (the
header row in one <table>, the body rows in the next, as many web apps' "Export to Excel" write them) lose their
headers: the body sheet's columns have no names, the one sheet is renamed "aging.xls table 2", and a lookup by
column no longer finds the figure."""
from controller_inbox import documents, table_lookup

HTML = b"""<html><body><div class=hdr><table><tr><th>Vendor</th><th>Invoice</th><th>Amount</th></tr></table></div>
<div class=body><table>
<tr><td>Northwind</td><td>INV-1001</td><td>1,200.00</td></tr>
<tr><td>Contoso</td><td>INV-1002</td><td>800.00</td></tr>
<tr><td>Fabrikam</td><td>INV-1003</td><td>300.00</td></tr>
</table></div></body></html>"""


def test_body_rows_keep_the_header_tables_column_names():
    text = documents.extract_document("aging.xls", "application/vnd.ms-excel", HTML)
    assert "(Amount): 800.00" in text, text


def test_lookup_by_column_still_answers():
    text = documents.extract_document("aging.xls", "application/vnd.ms-excel", HTML)
    found = table_lookup.answer(text, "What is the amount for Contoso?")
    assert found is not None and any(value == "800.00" for _p, value in found.points), found
