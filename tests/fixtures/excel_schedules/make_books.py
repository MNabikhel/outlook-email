"""Excel workbooks the way a controller builds schedules, then saved as PDF with LibreOffice.

Each book is formatted the way Excel users do it: merged titles, accounting number formats
($ at the left edge of the cell, figures at the right), merged category cells, subtotal rows,
landscape fit-to-width, print titles repeated on every page, page footers.

The PDFs beside this file are its output, used by tests/test_excel_schedules.py. To make them again
(needs openpyxl and LibreOffice's soffice on the PATH):

    python tests/fixtures/excel_schedules/make_books.py tests/fixtures/excel_schedules
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "books")
OUT.mkdir(parents=True, exist_ok=True)

ACCT = '_($* #,##0.00_);_($* (#,##0.00);_($* "-"??_);_(@_)'
ACCT0 = '_($* #,##0_);_($* (#,##0);_($* "-"??_);_(@_)'
NUM = "#,##0.00_);(#,##0.00)"
NUM0 = "#,##0_);(#,##0)"
PCT = "0.0%"
BOLD = Font(bold=True)
TITLE = Font(bold=True, size=14)
HEAD_FILL = PatternFill("solid", fgColor="1F4E78")
HEAD_FONT = Font(bold=True, color="FFFFFF")
SUB_FILL = PatternFill("solid", fgColor="DDEBF7")
THIN = Side(style="thin")
DOUBLE = Side(style="double")
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)


def title(ws, rows: list[str], width: int) -> int:
    for r, text in enumerate(rows, start=1):
        ws.cell(r, 1, text).font = TITLE if r == 1 else BOLD
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=width)
        ws.cell(r, 1).alignment = Alignment(horizontal="center")
    return len(rows) + 2


def header(ws, row: int, labels: list[str], *, height: float = 30) -> None:
    for c, label in enumerate(labels, start=1):
        cell = ws.cell(row, c, label)
        cell.font = HEAD_FONT
        cell.fill = HEAD_FILL
        cell.alignment = CENTER
        cell.border = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)
    ws.row_dimensions[row].height = height


def grid(ws, top: int, bottom: int, width: int) -> None:
    for r in range(top, bottom + 1):
        for c in range(1, width + 1):
            ws.cell(r, c).border = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)


def page(ws, *, landscape: bool, fit_wide: bool = True, titles: str | None = None, footer: str = "") -> None:
    ws.page_setup.orientation = "landscape" if landscape else "portrait"
    ws.page_setup.paperSize = ws.PAPERSIZE_LETTER
    if fit_wide:
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
    if titles:
        ws.print_title_rows = titles
    ws.oddFooter.center.text = "Page &P of &N"
    ws.oddFooter.left.text = footer or "&F"
    ws.page_margins.left = ws.page_margins.right = 0.4


# ---------------------------------------------------------------- A. prepaid amortization (wide)
def prepaid() -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Prepaids"
    months = [date(2026, m, 1) for m in range(1, 13)]
    labels = ["Vendor", "Description", "GL Account", "Start Date", "End Date", "Total Cost", "Monthly Amort."]
    labels += [m.strftime("%b-%y") for m in months] + ["Balance 12/31/26"]
    width = len(labels)
    row = title(ws, ["Northwind Holdings, Inc.", "Prepaid Expense Amortization Schedule", "Fiscal Year 2026"], width)
    header(ws, row, labels)
    items = [
        ("Salesforce", "CRM subscription", "1410", date(2026, 1, 1), date(2026, 12, 31), 86_400.00),
        ("Hartford Insurance", "General liability policy", "1420", date(2025, 7, 1), date(2026, 6, 30), 54_000.00),
        ("Travelers", "Workers comp policy", "1420", date(2026, 4, 1), date(2027, 3, 31), 31_200.00),
        ("Microsoft", "M365 E5 licenses", "1410", date(2026, 3, 1), date(2027, 2, 28), 47_880.00),
        ("WeWork", "Chicago office rent", "1430", date(2026, 1, 1), date(2026, 3, 31), 18_900.00),
        ("Adobe", "Creative Cloud", "1410", date(2026, 2, 1), date(2027, 1, 31), 9_960.00),
        ("Deloitte", "Tax software retainer", "1440", date(2026, 5, 1), date(2026, 10, 31), 12_000.00),
        ("ZoomInfo", "Data subscription", "1410", date(2026, 6, 1), date(2027, 5, 31), 27_600.00),
        ("Chubb", "D&O insurance", "1420", date(2026, 9, 1), date(2027, 8, 31), 64_800.00),
        ("Gartner", "Research seat", "1440", date(2026, 1, 1), date(2026, 12, 31), 33_000.00),
        ("Workday", "HRIS annual fee", "1410", date(2026, 8, 1), date(2027, 7, 31), 72_000.00),
        ("Iron Mountain", "Records storage", "1430", date(2026, 1, 1), date(2026, 12, 31), 4_380.00),
    ]
    r = row + 1
    for vendor, desc, gl, start, end, total in items:
        n = (end.year - start.year) * 12 + end.month - start.month + 1
        monthly = round(total / n, 2)
        ws.cell(r, 1, vendor)
        ws.cell(r, 2, desc)
        ws.cell(r, 3, gl)
        for c, value in ((4, start), (5, end)):
            ws.cell(r, c, value).number_format = "mm/dd/yyyy"
        ws.cell(r, 6, total).number_format = ACCT
        ws.cell(r, 7, monthly).number_format = ACCT
        amortized_before = 0.0
        for k, m in enumerate(months):
            active = start <= m <= end
            if m < date(2026, 1, 1):
                continue
            value = monthly if active else 0
            ws.cell(r, 8 + k, value).number_format = NUM
        before = max(0, (2026 - start.year) * 12 + 1 - start.month) if start < date(2026, 1, 1) else 0
        this_year = sum(1 for m in months if start <= m <= end)
        balance = round(total - monthly * (before + this_year), 2)
        ws.cell(r, width, max(balance, 0)).number_format = ACCT
        r += 1
    ws.cell(r, 1, "Total").font = BOLD
    for c in range(6, width + 1):
        if c == 7:
            continue
        col = get_column_letter(c)
        cell = ws.cell(r, c, f"=SUM({col}{row + 1}:{col}{r - 1})")
        cell.number_format = ACCT if c in (6, width) else NUM
        cell.font = BOLD
        cell.border = Border(top=THIN, bottom=DOUBLE)
    grid(ws, row + 1, r - 1, width)
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 22
    for c in range(3, width + 1):
        ws.column_dimensions[get_column_letter(c)].width = 11
    ws.column_dimensions["F"].width = 13
    ws.column_dimensions["G"].width = 12
    ws.column_dimensions[get_column_letter(width)].width = 14
    page(ws, landscape=True)
    wb.save(OUT / "Prepaid Amortization FY2026.xlsx")


# ---------------------------------------------------------------- B. fixed assets with merged categories
def fixed_assets() -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "FA Rollforward"
    labels = ["Category", "Asset ID", "Description", "In-Service Date", "Useful Life (Yrs)", "Cost",
              "Accum. Depr. 12/31/25", "2026 YTD Depr.", "Accum. Depr. 9/30/26", "NBV 9/30/26"]
    width = len(labels)
    row = title(ws, ["Northwind Holdings, Inc.", "Fixed Asset Schedule", "As of September 30, 2026"], width)
    header(ws, row, labels, height=42)
    groups = {
        "Buildings": [
            ("B-100", "Elk Grove warehouse", date(2018, 3, 1), 39, 2_450_000),
            ("B-101", "Warehouse roof replacement", date(2023, 9, 1), 20, 186_000),
        ],
        "Machinery & Equipment": [
            ("M-210", "CNC mill Haas VF-4", date(2021, 6, 15), 10, 142_500),
            ("M-211", "Forklift Toyota 8FGU25", date(2022, 2, 1), 7, 38_900),
            ("M-214", "Packaging line conveyor", date(2024, 11, 1), 10, 96_750),
        ],
        "Vehicles": [
            ("V-301", "Ford Transit 250 van", date(2023, 4, 10), 5, 52_400),
            ("V-302", "Ford F-150 pickup", date(2024, 8, 20), 5, 48_950),
            ("V-305", "Isuzu NPR box truck", date(2025, 10, 1), 7, 71_300),
        ],
        "Computer Equipment": [
            ("C-410", "Dell PowerEdge servers (4)", date(2023, 1, 15), 5, 64_800),
            ("C-412", "Laptops refresh 2025 (35)", date(2025, 3, 1), 3, 52_150),
        ],
        "Furniture & Fixtures": [
            ("F-501", "Office furniture HQ", date(2019, 7, 1), 7, 88_200),
        ],
    }
    r = row + 1
    first_rows = []
    total_rows = []
    for category, assets in groups.items():
        start = r
        for asset_id, desc, when, life, cost in assets:
            months_to_2025 = max(0, (2025 - when.year) * 12 + 12 - when.month + 1)
            monthly = cost / (life * 12)
            accum_25 = round(min(cost, monthly * months_to_2025), 2)
            ytd = round(min(cost - accum_25, monthly * 9), 2)
            ws.cell(r, 2, asset_id)
            ws.cell(r, 3, desc)
            ws.cell(r, 4, when).number_format = "m/d/yyyy"
            ws.cell(r, 5, life)
            ws.cell(r, 6, cost).number_format = ACCT
            ws.cell(r, 7, accum_25).number_format = ACCT
            ws.cell(r, 8, ytd).number_format = ACCT
            ws.cell(r, 9, f"=G{r}+H{r}").number_format = ACCT
            ws.cell(r, 10, f"=F{r}-I{r}").number_format = ACCT
            r += 1
        ws.cell(start, 1, category).alignment = Alignment(vertical="center", wrap_text=True)
        ws.cell(start, 1).font = BOLD
        if r - 1 > start:
            ws.merge_cells(start_row=start, start_column=1, end_row=r - 1, end_column=1)
        first_rows.append(start)
        ws.cell(r, 2, f"Total {category}").font = BOLD
        for c in range(6, 11):
            col = get_column_letter(c)
            cell = ws.cell(r, c, f"=SUM({col}{start}:{col}{r - 1})")
            cell.number_format = ACCT
            cell.font = BOLD
            cell.fill = SUB_FILL
        total_rows.append(r)
        r += 1
    ws.cell(r, 1, "Grand Total").font = BOLD
    for c in range(6, 11):
        col = get_column_letter(c)
        cell = ws.cell(r, c, "=" + "+".join(f"{col}{t}" for t in total_rows))
        cell.number_format = ACCT
        cell.font = BOLD
        cell.border = Border(top=THIN, bottom=DOUBLE)
    grid(ws, row + 1, r - 1, width)
    ws.cell(r + 2, 1, "Note: Depreciation is straight-line, monthly convention. Land is excluded from this schedule.")
    for c, w in enumerate([16, 9, 28, 11, 9, 14, 14, 13, 14, 14], start=1):
        ws.column_dimensions[get_column_letter(c)].width = w
    page(ws, landscape=True)
    wb.save(OUT / "Fixed Asset Schedule 9-30-26.xlsx")


# ---------------------------------------------------------------- C. loan amortization, two pages, print titles
def loan() -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Amortization"
    width = 7
    ws.cell(1, 1, "First National Bank - Term Loan #4471-22").font = TITLE
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=width)
    facts = [("Borrower:", "Northwind Holdings, Inc."), ("Principal:", 2_500_000), ("Annual Rate:", 0.0625),
             ("Term (months):", 60), ("First Payment:", date(2026, 1, 15))]
    for i, (k, v) in enumerate(facts, start=3):
        ws.cell(i, 1, k).font = BOLD
        cell = ws.cell(i, 2, v)
        if isinstance(v, float):
            cell.number_format = "0.00%"
        elif isinstance(v, int) and v > 1000:
            cell.number_format = ACCT0
        elif isinstance(v, date):
            cell.number_format = "mmmm d, yyyy"
    rate = 0.0625 / 12
    principal = 2_500_000
    payment = round(principal * rate / (1 - (1 + rate) ** -60), 2)
    ws.cell(8, 1, "Monthly Payment:").font = BOLD
    ws.cell(8, 2, payment).number_format = ACCT
    row = 10
    header(ws, row, ["Pmt #", "Payment Date", "Beginning Balance", "Payment", "Interest", "Principal", "Ending Balance"])
    balance = principal
    r = row + 1
    when = date(2026, 1, 15)
    for n in range(1, 61):
        interest = round(balance * rate, 2)
        princ = round(payment - interest, 2) if n < 60 else balance
        pay = round(interest + princ, 2)
        end = round(balance - princ, 2)
        values = [n, when, balance, pay, interest, princ, end]
        for c, v in enumerate(values, start=1):
            cell = ws.cell(r, c, v)
            if c == 2:
                cell.number_format = "mm/dd/yyyy"
            elif c > 2:
                cell.number_format = NUM
        balance = end
        r += 1
        month = when.month + 1
        when = date(when.year + (month > 12), (month - 1) % 12 + 1, 15)
    ws.cell(r, 1, "Totals").font = BOLD
    for c in (4, 5, 6):
        col = get_column_letter(c)
        cell = ws.cell(r, c, f"=SUM({col}{row + 1}:{col}{r - 1})")
        cell.number_format = ACCT
        cell.font = BOLD
    for c, w in enumerate([8, 13, 17, 13, 13, 13, 17], start=1):
        ws.column_dimensions[get_column_letter(c)].width = w
    page(ws, landscape=False, titles=f"{row}:{row}")
    wb.save(OUT / "Term Loan Amortization.xlsx")


# ---------------------------------------------------------------- D. AP aging with accounting format
def ap_aging() -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "AP Aging"
    labels = ["Vendor", "Vendor #", "Current", "1 - 30 Days", "31 - 60 Days", "61 - 90 Days", "Over 90 Days", "Total"]
    width = len(labels)
    row = title(ws, ["Northwind Holdings, Inc.", "Accounts Payable Aging Summary", "As of September 30, 2026"], width)
    header(ws, row, labels)
    vendors = [
        ("Acme Industrial Supply", "V1001", 24_310.50, 8_120.00, 0, 0, 0),
        ("Bluewater Logistics", "V1004", 0, 12_480.75, 6_200.00, 0, 0),
        ("Cintas Corporation", "V1009", 1_845.20, 0, 0, 0, 0),
        ("Delta Packaging Co.", "V1012", 9_600.00, 9_600.00, 9_600.00, 4_800.00, 0),
        ("Evergreen Electric", "V1015", 0, 0, 0, 0, 13_275.00),
        ("FedEx Freight", "V1020", 3_412.88, 1_120.40, 0, 0, 0),
        ("Grainger", "V1022", 6_731.15, 2_210.00, 845.50, 0, 0),
        ("Harbor Steel LLC", "V1030", 48_500.00, 0, 22_150.00, 0, 0),
        ("Iron Mountain", "V1033", 365.00, 365.00, 0, 0, 0),
        ("Johnson Controls", "V1037", 0, 0, 0, 7_940.00, 2_115.00),
        ("Kelly Services", "V1041", 11_250.00, 11_250.00, 0, 0, 0),
        ("Lakeside Janitorial", "V1044", 2_100.00, 0, 0, 0, 0),
        ("McMaster-Carr", "V1048", 4_118.62, 980.00, 0, 0, 0),
        ("Northshore Utilities", "V1052", 7_884.31, 0, 0, 0, 0),
        ("Orion Software", "V1055", 0, 0, 0, 0, 18_600.00),
        ("Pacific Crate & Pallet", "V1060", 5_520.00, 5_520.00, 3_100.00, 1_250.00, 0),
    ]
    r = row + 1
    for v in vendors:
        ws.cell(r, 1, v[0])
        ws.cell(r, 2, v[1])
        for c, amount in enumerate(v[2:], start=3):
            ws.cell(r, c, amount).number_format = ACCT
        ws.cell(r, 8, f"=SUM(C{r}:G{r})").number_format = ACCT
        r += 1
    ws.cell(r, 1, "Total").font = BOLD
    for c in range(3, 9):
        col = get_column_letter(c)
        cell = ws.cell(r, c, f"=SUM({col}{row + 1}:{col}{r - 1})")
        cell.number_format = ACCT
        cell.font = BOLD
        cell.border = Border(top=THIN, bottom=DOUBLE)
    ws.cell(r + 1, 1, "% of Total")
    for c in range(3, 9):
        col = get_column_letter(c)
        ws.cell(r + 1, c, f"={col}{r}/$H${r}").number_format = PCT
    for c, w in enumerate([26, 9, 14, 14, 14, 14, 14, 15], start=1):
        ws.column_dimensions[get_column_letter(c)].width = w
    page(ws, landscape=False)
    wb.save(OUT / "AP Aging 9-30-26.xlsx")


# ---------------------------------------------------------------- E. staff schedule, names turned sideways
def staffing() -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "October"
    people = ["Maya Chen", "Sam Ortiz", "Priya Raman", "Jonathan Alvarez", "Li Wei", "Grace Kim", "Tom Becker"]
    width = 2 + len(people)
    row = title(ws, ["Accounting Team - Close Coverage Schedule", "October 2026"], width)
    ws.cell(row, 1, "Date")
    ws.cell(row, 2, "Day")
    for c, name in enumerate(people, start=3):
        ws.cell(row, c, name)
    for c in range(1, width + 1):
        cell = ws.cell(row, c)
        cell.font = BOLD
        cell.alignment = Alignment(horizontal="center", vertical="bottom", text_rotation=90 if c > 2 else 0)
        cell.border = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)
    ws.row_dimensions[row].height = 95
    shifts = ["8-5", "8-5", "7-4", "9-6", "OFF", "8-5", "7-4"]
    pto = {("Priya Raman", 9), ("Priya Raman", 12), ("Sam Ortiz", 23), ("Grace Kim", 30), ("Tom Becker", 1), ("Tom Becker", 2)}
    remote = {("Maya Chen", 7), ("Li Wei", 14), ("Jonathan Alvarez", 21)}
    r = row + 1
    for d in range(1, 32):
        day = date(2026, 10, d)
        ws.cell(r, 1, day).number_format = "m/d"
        ws.cell(r, 2, day.strftime("%a"))
        for c, name in enumerate(people, start=3):
            if day.weekday() >= 5:
                value = "Close" if name in ("Maya Chen", "Jonathan Alvarez") and d in (3, 4) else ""
            elif (name, d) in pto:
                value = "PTO"
            elif (name, d) in remote:
                value = "WFH"
            else:
                value = shifts[(c + d) % len(shifts)]
            ws.cell(r, c, value).alignment = Alignment(horizontal="center")
        if day.weekday() >= 5:
            for c in range(1, width + 1):
                ws.cell(r, c).fill = PatternFill("solid", fgColor="EDEDED")
        r += 1
    grid(ws, row + 1, r - 1, width)
    ws.cell(r + 1, 1, "Legend: shift hours shown as start-end; OFF = scheduled day off; PTO = paid time off; WFH = working from home.")
    ws.column_dimensions["A"].width = 7
    ws.column_dimensions["B"].width = 6
    for c in range(3, width + 1):
        ws.column_dimensions[get_column_letter(c)].width = 7
    page(ws, landscape=False, fit_wide=True)
    ws.page_setup.fitToHeight = 1
    wb.save(OUT / "October Close Coverage.xlsx")


# ---------------------------------------------------------------- F. budget vs actual, merged quarter headings
def budget() -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "BvA"
    width = 9
    row = title(ws, ["Northwind Holdings, Inc.", "Operating Budget vs. Actual by Department", "(in USD)"], width)
    ws.cell(row, 1, "Department")
    ws.merge_cells(start_row=row, start_column=1, end_row=row + 1, end_column=1)
    ws.cell(row, 2, "Q3 2026")
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
    ws.cell(row, 6, "Q4 2026")
    ws.merge_cells(start_row=row, start_column=6, end_row=row, end_column=7)
    ws.cell(row, 8, "FY 2026")
    ws.merge_cells(start_row=row, start_column=8, end_row=row, end_column=9)
    sub = ["Actual", "Budget", "Variance $", "Variance %", "Forecast", "Budget", "Forecast", "Budget"]
    for c, label in enumerate(sub, start=2):
        ws.cell(row + 1, c, label)
    for rr in (row, row + 1):
        for c in range(1, width + 1):
            cell = ws.cell(rr, c)
            cell.font = HEAD_FONT
            cell.fill = HEAD_FILL
            cell.alignment = CENTER
    sections = {
        "Revenue": [("Product sales", 4_215_300, 4_050_000, 4_400_000, 4_300_000, 16_120_000, 15_900_000),
                    ("Services", 812_450, 900_000, 880_000, 950_000, 3_310_000, 3_600_000)],
        "Operating Expenses": [("Sales & Marketing", 1_104_220, 1_050_000, 1_150_000, 1_100_000, 4_380_000, 4_250_000),
                               ("Research & Development", 932_870, 975_000, 990_000, 1_000_000, 3_850_000, 3_900_000),
                               ("General & Administrative", 648_315, 610_000, 640_000, 625_000, 2_520_000, 2_450_000),
                               ("Facilities", 211_090, 205_000, 210_000, 205_000, 830_000, 820_000)],
    }
    r = row + 2
    for section, lines in sections.items():
        ws.cell(r, 1, section).font = BOLD
        r += 1
        start = r
        for name, a, b, q4f, q4b, fyf, fyb in lines:
            ws.cell(r, 1, "   " + name)
            ws.cell(r, 2, a).number_format = NUM0
            ws.cell(r, 3, b).number_format = NUM0
            ws.cell(r, 4, f"=B{r}-C{r}").number_format = NUM0
            ws.cell(r, 5, f"=D{r}/C{r}").number_format = PCT
            ws.cell(r, 6, q4f).number_format = NUM0
            ws.cell(r, 7, q4b).number_format = NUM0
            ws.cell(r, 8, fyf).number_format = NUM0
            ws.cell(r, 9, fyb).number_format = NUM0
            r += 1
        ws.cell(r, 1, f"Total {section}").font = BOLD
        for c in (2, 3, 4, 6, 7, 8, 9):
            col = get_column_letter(c)
            cell = ws.cell(r, c, f"=SUM({col}{start}:{col}{r - 1})")
            cell.number_format = NUM0
            cell.font = BOLD
            cell.border = Border(top=THIN)
        ws.cell(r, 5, f"=D{r}/C{r}").number_format = PCT
        r += 2
    ws.column_dimensions["A"].width = 28
    for c in range(2, width + 1):
        ws.column_dimensions[get_column_letter(c)].width = 13
    page(ws, landscape=True)
    wb.save(OUT / "Budget vs Actual Q3 2026.xlsx")


# ---------------------------------------------------------------- G. close calendar
def close_calendar() -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Sept Close"
    labels = ["Workday", "Date", "Task", "Owner", "Reviewer", "Status"]
    width = len(labels)
    row = title(ws, ["September 2026 Month-End Close Calendar"], width)
    header(ws, row, labels)
    tasks = [
        ("WD-2", date(2026, 9, 28), "Cut off AP invoice entry", "Sam Ortiz", "Maya Chen", "Done"),
        ("WD-1", date(2026, 9, 29), "Run preliminary payroll accrual", "Grace Kim", "Maya Chen", "Done"),
        ("WD1", date(2026, 10, 1), "Post bank activity & cash receipts", "Li Wei", "Maya Chen", "Done"),
        ("WD1", date(2026, 10, 1), "Accrue utilities and freight", "Sam Ortiz", "Jonathan Alvarez", "In progress"),
        ("WD2", date(2026, 10, 2), "Bank reconciliations (all accounts)", "Li Wei", "Jonathan Alvarez", "In progress"),
        ("WD2", date(2026, 10, 2), "Prepaid amortization entries", "Priya Raman", "Maya Chen", "Not started"),
        ("WD3", date(2026, 10, 5), "Fixed asset depreciation run", "Priya Raman", "Jonathan Alvarez", "Not started"),
        ("WD3", date(2026, 10, 5), "Intercompany reconciliation", "Tom Becker", "Maya Chen", "Not started"),
        ("WD4", date(2026, 10, 6), "Revenue cut-off review", "Jonathan Alvarez", "Maya Chen", "Not started"),
        ("WD4", date(2026, 10, 6), "Inventory reserve analysis", "Tom Becker", "Jonathan Alvarez", "Not started"),
        ("WD5", date(2026, 10, 7), "Flux analysis vs. budget", "Grace Kim", "Maya Chen", "Not started"),
        ("WD6", date(2026, 10, 8), "Close package to CFO", "Maya Chen", "CFO", "Not started"),
    ]
    r = row + 1
    for t in tasks:
        for c, v in enumerate(t, start=1):
            cell = ws.cell(r, c, v)
            if c == 2:
                cell.number_format = "ddd mm/dd"
        r += 1
    grid(ws, row + 1, r - 1, width)
    for c, w in enumerate([9, 11, 36, 17, 17, 12], start=1):
        ws.column_dimensions[get_column_letter(c)].width = w
    page(ws, landscape=True)
    wb.save(OUT / "Sept Close Calendar.xlsx")


for build in (prepaid, fixed_assets, loan, ap_aging, staffing, budget, close_calendar):
    build()

for book in sorted(OUT.glob("*.xlsx")):
    subprocess.run(
        ["soffice", "--headless", "--calc", "--convert-to", "pdf", "--outdir", str(OUT), str(book)],
        check=True,
        capture_output=True,
        timeout=180,
    )
print("\n".join(sorted(p.name for p in OUT.glob("*.pdf"))))
