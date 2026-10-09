"""Fix 2c96198 (read_question) only keeps the "change" op when the question has two distinct period tokens
(years, FYxx, Qn/Hn) or "from ... to". Comparative columns named in words ("Prior Year"/"Current Year",
"Prior Month"/"Current Month") asked about with "between ... and ...", "year over year" or "month over month"
lose the worked-out change they had: only the two figures are listed."""
import io

import pytest
from openpyxl import Workbook

from controller_inbox import documents, table_lookup


def _sheet(title, head, rows) -> str:
    wb = Workbook()
    ws = wb.active
    ws.title = title
    ws.append(head)
    for row in rows:
        ws.append(row)
    out = io.BytesIO()
    wb.save(out)
    return documents.extract_document(f"{title.lower()}.xlsx", "", out.getvalue())


PL = ("PL", ["Line", "Prior Year", "Current Year"], [["Revenue", 1200000, 1350000], ["COGS", 700000, 760000], ["Opex", 300000, 310000]])
EXP = ("Expenses", ["Account", "Prior Month", "Current Month"], [["Rent", 10000, 10500], ["Utilities", 2200, 1900], ["Payroll", 80000, 82000]])


@pytest.mark.parametrize("sheet, question, point", [
    (PL, "What is the year over year change in revenue?", ("Revenue: change", "150,000")),
    (PL, "What was the change in opex between prior year and current year?", ("Opex: change", "10,000")),
    (EXP, "What is the change in utilities month over month?", ("Utilities: change", "-300")),
])
def test_change_between_word_named_periods_is_worked_out(sheet, question, point):
    found = table_lookup.answer(_sheet(*sheet), question)
    assert found is not None and point in found.points, found and found.points
