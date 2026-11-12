"""Hard attachments, made the way real ones are, with the questions people ask of them and the answers.

These are lookalikes of the files that trip a reader up: workbooks whose summary is formulas over other tabs,
two-row headings, several tables stacked on one tab, statements whose table runs over pages with the heading
repeated, a long agreement with the figure buried in a clause, European number formats, a wide payroll sheet, a
deck with a table. Nothing here is real mail.

The files beside this script are its output, with ``cases.json`` (each question, the file, and the answers that
count as right). ``tests/test_hard_files.py`` uses them. To make them again (needs openpyxl, python-docx,
python-pptx and LibreOffice's soffice on the PATH):

    python tests/hard_files/make_hard.py tests/hard_files/files
"""

from __future__ import annotations

import csv
import json
import random
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

from docx import Document
from docx.shared import Pt
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from pptx import Presentation
from pptx.util import Inches

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "files")
NUM = "#,##0.00_);(#,##0.00)"
BOLD = Font(bold=True)
CASES: list[dict] = []


def case(file: str, question: str, answers: list[str], *, where: str = "", held_out: bool = False, sql: str = "", needs: str = "") -> None:
    """``answers``: any one of these in what the model is given (or says) counts. ``where``: the page or cell it is
    in. ``held_out``: not looked at while fixing, to check a fix isn't tuned to the questions. ``sql``: a query a
    model could write over the file's tables that gives the answer; ``{Canada}`` stands for the table whose title
    ends with that word. ``needs``: "ocr" when the file can only be read with the OCR extra installed."""
    CASES.append({"file": file, "question": question, "answers": answers, "where": where, "held_out": held_out, "sql": sql, "needs": needs})


def money(value: float) -> str:
    return f"{value:,.2f}"


def recalculated(path: Path) -> None:
    """openpyxl writes formulas without their values; Excel and LibreOffice save both. LibreOffice works them out."""
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            ["soffice", "--headless", "--calc", "--convert-to", "xlsx:Calc MS Excel 2007 XML", "--outdir", tmp, str(path)],
            check=True,
            capture_output=True,
        )
        (Path(tmp) / path.name).replace(path)


def as_pdf(path: Path) -> Path:
    subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(path.parent), str(path)], check=True, capture_output=True)
    return path.with_suffix(".pdf")


# --- 1. An opex pack: the summary is formulas over the detail tab, under a two-row heading -------------------------
def opex_pack() -> None:
    rng = random.Random(1)
    depts = ["Marketing", "Sales", "IT", "Finance", "Operations", "HR"]
    accounts = [("6100", "Salaries"), ("6200", "Travel"), ("6300", "Software"), ("6400", "Consulting"), ("6500", "Rent")]
    months = ["Jul", "Aug", "Sep"]
    wb = Workbook()
    summary = wb.active
    summary.title = "Summary"
    detail = wb.create_sheet("Detail")
    old = wb.create_sheet("Old version")
    old.sheet_state = "hidden"
    detail.append(["Dept", "Account", "Account name", "Month", "Amount"])
    totals: dict[tuple[str, str], float] = {}
    for dept in depts:
        for code, name in accounts:
            for month in months:
                amount = round(rng.uniform(2_000, 60_000), 2)
                detail.append([dept, code, name, month, amount])
                totals[(dept, month)] = totals.get((dept, month), 0) + amount
                totals[(dept, name)] = totals.get((dept, name), 0) + amount
    last = detail.max_row
    for row in detail.iter_rows(min_row=2, min_col=5, max_col=5):
        row[0].number_format = NUM
    summary["A1"] = "Q3 2026 operating expenses by department"
    summary["A1"].font = Font(bold=True, size=14)
    summary.merge_cells("A1:F1")
    summary["A3"] = "Department"
    summary.merge_cells("A3:A4")
    summary["B3"] = "Actual"
    summary.merge_cells("B3:D3")
    summary["E3"] = "Q3"
    summary.merge_cells("E3:E4")
    summary["F3"] = "Budget"
    summary.merge_cells("F3:F4")
    for col, month in zip("BCD", months):
        summary[f"{col}4"] = month
    for cell in ("A3", "B3", "E3", "F3", "B4", "C4", "D4"):
        summary[cell].font = BOLD
        summary[cell].alignment = Alignment(horizontal="center")
    budgets = {}
    for r, dept in enumerate(depts, start=5):
        summary[f"A{r}"] = dept
        for col, month in zip("BCD", months):
            summary[f"{col}{r}"] = f'=SUMIFS(Detail!$E$2:$E${last},Detail!$A$2:$A${last},$A{r},Detail!$D$2:$D${last},"{month}")'
            summary[f"{col}{r}"].number_format = NUM
        summary[f"E{r}"] = f"=SUM(B{r}:D{r})"
        summary[f"E{r}"].number_format = NUM
        budgets[dept] = round(sum(totals[(dept, m)] for m in months) * rng.uniform(0.9, 1.1), -2)
        summary[f"F{r}"] = budgets[dept]
        summary[f"F{r}"].number_format = NUM
    end = 4 + len(depts)
    summary[f"A{end + 1}"] = "Total"
    summary[f"A{end + 1}"].font = BOLD
    for col in "BCDEF":
        summary[f"{col}{end + 1}"] = f"=SUM({col}5:{col}{end})"
        summary[f"{col}{end + 1}"].number_format = NUM
    old.append(["Department", "Q3 (draft)"])
    for dept in depts:
        old.append([dept, round(rng.uniform(100_000, 600_000), 2)])
    path = OUT / "Q3 Opex Pack.xlsx"
    wb.save(path)
    recalculated(path)
    mkt_q3 = sum(totals[("Marketing", m)] for m in months)
    it_aug = totals[("IT", "Aug")]
    grand = sum(totals[(d, m)] for d in depts for m in months)
    sales_travel = totals[("Sales", "Travel")]
    case("Q3 Opex Pack.xlsx", "What is Marketing's total actual opex for Q3 on the summary?", [money(mkt_q3)], where="Summary!E5",
         sql="SELECT q3 FROM {Summary} WHERE department = 'Marketing'")
    case("Q3 Opex Pack.xlsx", "How much did IT spend in August?", [money(it_aug)], where="Summary!C7",
         sql="SELECT actual_aug FROM {Summary} WHERE department = 'IT'")
    case("Q3 Opex Pack.xlsx", "What is the total Q3 actual across all departments?", [money(grand)], where="Summary!E11")
    case("Q3 Opex Pack.xlsx", "How much did Sales spend on travel in Q3?", [money(sales_travel)], where="Detail", held_out=True)
    case(
        "Q3 Opex Pack.xlsx",
        "Which department is furthest over its Q3 budget, and by how much?",
        _furthest_over(depts, totals, months, budgets),
        where="Summary",
        held_out=True,
    )


def _furthest_over(depts, totals, months, budgets) -> list[str]:
    over = {d: sum(totals[(d, m)] for m in months) - budgets[d] for d in depts}
    worst = max(over, key=over.get)
    return [money(over[worst])] if over[worst] > 0 else [worst]


# --- 2. An AP aging with three entities stacked on one tab, each with its subtotal ----------------------------------
def stacked_aging() -> None:
    rng = random.Random(2)
    vendors = ["Harbor Steel", "Northwind Supply", "Bluebird Freight", "Apex Electric", "Clearwater Labs", "Delta Office", "Granite Movers"]
    buckets = ["Current", "1-30", "31-60", "61-90", "Over 90"]
    wb = Workbook()
    ws = wb.active
    ws.title = "Aging"
    ws.append(["Accounts payable aging as of 10/31/2026"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([])
    found: dict[str, dict] = {}
    for entity in ("US entity (USD)", "Canada entity (CAD)", "UK entity (GBP)"):
        ws.append([entity])
        ws.cell(ws.max_row, 1).font = BOLD
        ws.append(["Vendor"] + buckets + ["Total"])
        first = ws.max_row + 1
        picked = rng.sample(vendors, 5)
        rows = {}
        for vendor in picked:
            values = [round(rng.choice([0, 0, rng.uniform(500, 40_000)]), 2) for _ in buckets]
            rows[vendor] = values
            r = ws.max_row + 1
            ws.append([vendor] + values + [f"=SUM(B{r}:F{r})"])
        lastrow = ws.max_row
        ws.append(["Subtotal"] + [f"=SUM({c}{first}:{c}{lastrow})" for c in "BCDEFG"])
        ws.cell(ws.max_row, 1).font = BOLD
        ws.append([])
        found[entity] = rows
    for row in ws.iter_rows(min_row=3, min_col=2, max_col=7):
        for cell in row:
            cell.number_format = NUM
    path = OUT / "AP Aging by entity 10-31-26.xlsx"
    wb.save(path)
    recalculated(path)
    canada = found["Canada entity (CAD)"]
    case("AP Aging by entity 10-31-26.xlsx", "What is the Canada entity's total 61-90 day balance?", [money(sum(v[3] for v in canada.values()))],
         sql="SELECT SUM(c_61_90) FROM {(CAD)}")
    uk = found["UK entity (GBP)"]
    worst = max(uk, key=lambda v: uk[v][4])
    case("AP Aging by entity 10-31-26.xlsx", "Which vendor has the largest over-90 balance in the UK entity?", [worst], held_out=True)
    us = found["US entity (USD)"]
    vendor = sorted(us)[0]
    case("AP Aging by entity 10-31-26.xlsx", f"What do we owe {vendor} in total in the US entity?", [money(sum(us[vendor]))],
         sql=f"SELECT total FROM {{(USD)}} WHERE vendor = '{vendor}'")


# --- 3. A vendor statement over three pages: heading repeated, running balance, credits in brackets -----------------
def vendor_statement() -> None:
    rng = random.Random(3)
    doc = Document()
    doc.add_heading("Granite Industrial Supply — Statement of account", 1)
    doc.add_paragraph("Customer: Taz Holdings LLC    Account: 40-1188    Statement date: October 31, 2026")
    table = doc.add_table(rows=1, cols=6)
    table.style = "Table Grid"
    for cell, label in zip(table.rows[0].cells, ["Date", "Reference", "Description", "Charges", "Credits", "Balance"]):
        cell.text = label
    table.rows[0].repeat_header = True
    _repeat_heading(table.rows[0])
    balance = 18_204.55
    row = table.add_row().cells
    row[0].text, row[2].text, row[5].text = "10/01/2026", "Balance forward", money(balance)
    day = date(2026, 10, 1)
    refs = {}
    for i in range(78):
        day += timedelta(days=rng.choice([0, 0, 1]))
        if i % 9 == 4:
            amount = round(rng.uniform(150, 3_000), 2)
            ref = f"CM-{2200 + i}"
            desc = rng.choice(["Return — damaged pallets", "Pricing adjustment", "Short shipment credit"])
            balance -= amount
            cells = [day.strftime("%m/%d/%Y"), ref, desc, "", f"({money(amount)})", money(balance)]
        elif i % 13 == 7:
            amount = round(rng.uniform(2_000, 9_000), 2)
            ref = f"PMT-{5100 + i}"
            desc = "Payment received — thank you"
            balance -= amount
            cells = [day.strftime("%m/%d/%Y"), ref, desc, "", f"({money(amount)})", money(balance)]
        else:
            amount = round(rng.uniform(80, 4_500), 2)
            ref = f"{88340 + i}"
            desc = rng.choice(["Steel brackets", "Anchor bolts", "Welding wire", "Safety gloves", "Pallet wrap", "Grinding discs"])
            balance += amount
            cells = [day.strftime("%m/%d/%Y"), ref, desc, money(amount), "", money(balance)]
        refs[ref] = (desc, amount)
        for cell, text in zip(table.add_row().cells, cells):
            cell.text = text
    doc.add_paragraph(f"Closing balance: {money(balance)}")
    doc.add_paragraph("Please remit within 30 days. Questions: ar@granite-industrial.example")
    path = OUT / "Granite statement Oct 2026.docx"
    doc.save(path)
    pdf = as_pdf(path)
    path.unlink()
    name = pdf.name
    case(name, "What is the closing balance on the Granite statement?", [money(balance)], where="last page")
    late = [r for r in refs if r.isdigit()][-3]
    case(name, f"What is the amount of invoice {late} on the statement?", [money(refs[late][1])], where="page 3")
    credit = [r for r in refs if r.startswith("CM-")][2]
    case(name, f"What was credit memo {credit} for, and how much?", [money(refs[credit][1])], held_out=True)


def _repeat_heading(row) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    props = row._tr.get_or_add_trPr()
    mark = OxmlElement("w:tblHeader")
    mark.set(qn("w:val"), "true")
    props.append(mark)


# --- 4. A long agreement with the figures in its clauses -------------------------------------------------------------
def long_agreement() -> None:
    rng = random.Random(4)
    doc = Document()
    doc.add_heading("Master Services Agreement", 0)
    doc.add_paragraph("between Taz Holdings LLC (“Client”) and Brightline Data Services Inc. (“Provider”), effective March 1, 2026.")
    filler = (
        "The parties shall cooperate in good faith and provide such information, access and assistance as is reasonably "
        "required for the performance of the Services, and each party shall bear its own costs except as expressly set out "
        "in this Agreement. Nothing in this section limits the remedies available under applicable law. "
    )
    clauses = {
        7: ("Fees", "Client shall pay the monthly service fee of $18,750.00 for the Core Services described in Schedule A."),
        11: ("Invoicing", "Provider shall invoice monthly in arrears. Invoices are due net 45 days from the invoice date."),
        14: (
            "Late payment",
            "Undisputed amounts not paid when due accrue interest at 1.25% per month, or the maximum rate permitted by law if lower.",
        ),
        19: ("Limitation of liability", "Each party's aggregate liability is capped at the fees paid in the twelve months before the claim."),
        22: (
            "Termination for convenience",
            "Client may terminate on 90 days' written notice and shall pay an early termination fee of $42,500.00 if notice is given before February 28, 2027.",
        ),
    }
    for number in range(1, 27):
        heading, special = clauses.get(number, (rng.choice(["Confidentiality", "Personnel", "Audit", "Insurance", "Notices", "Compliance", "Data protection", "Subcontracting"]), ""))
        doc.add_heading(f"{number}. {heading}", 2)
        for sub in range(1, 4):
            text = f"{number}.{sub} " + filler * rng.randint(2, 4)
            if special and sub == 2:
                text = f"{number}.{sub} {special} " + filler
            para = doc.add_paragraph(text)
            para.runs[0].font.size = Pt(10)
    path = OUT / "Brightline MSA signed.docx"
    doc.save(path)
    case(path.name, "What interest rate applies to late payments under the Brightline agreement?", ["1.25%"], where="clause 14.2")
    case(path.name, "What is the early termination fee in the MSA?", ["42,500.00", "42,500"], where="clause 22.2")
    case(path.name, "What are the payment terms on Brightline's invoices?", ["net 45", "45 days"], where="clause 11.2", held_out=True)


# --- 5. A German supplier's invoice: comma decimals, VAT, euros ------------------------------------------------------
def euro_invoice() -> None:
    doc = Document()
    doc.add_heading("RECHNUNG / INVOICE  Nr. 2026-4471", 1)
    doc.add_paragraph("Kessler Präzisionsteile GmbH · Industriestraße 12 · 70565 Stuttgart · USt-IdNr. DE812345678")
    doc.add_paragraph("Rechnungsdatum / Invoice date: 28.10.2026     Fällig / Due: 27.11.2026")
    lines = [("Präzisionswelle PW-40", 120, 38.75), ("Lagerbuchse LB-12", 400, 4.10), ("Montage / Assembly (Std.)", 16, 92.00)]
    table = doc.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    for cell, label in zip(table.rows[0].cells, ["Artikel / Item", "Menge / Qty", "Preis / Unit €", "Betrag / Amount €"]):
        cell.text = label
    net = 0.0
    for item, qty, price in lines:
        amount = qty * price
        net += amount
        for cell, text in zip(table.add_row().cells, [item, str(qty), _de(price), _de(amount)]):
            cell.text = text
    vat = round(net * 0.19, 2)
    gross = net + vat
    doc.add_paragraph(f"Nettobetrag / Net: {_de(net)} €")
    doc.add_paragraph(f"MwSt. 19% / VAT 19%: {_de(vat)} €")
    doc.add_paragraph(f"Gesamtbetrag / Total: {_de(gross)} €")
    doc.add_paragraph("Bankverbindung: IBAN DE89 3704 0044 0532 0130 00 · BIC COBADEFFXXX")
    path = OUT / "Kessler Rechnung 2026-4471.docx"
    doc.save(path)
    pdf = as_pdf(path)
    path.unlink()
    case(pdf.name, "What is the total on the Kessler invoice in euros?", [_de(gross), money(gross)])
    case(pdf.name, "How much VAT is on the Kessler invoice?", [_de(vat), money(vat)], held_out=True)


def scanned_statement() -> None:
    """A vendor's invoice printed and scanned: pages as pictures, slightly tilted and grey, no text to copy."""
    import io

    import pypdfium2
    from PIL import ImageFilter

    doc = Document()
    doc.add_heading("Bluebird Freight Lines — INVOICE", 1)
    doc.add_paragraph("Invoice no. BF-77310        Invoice date: 10/22/2026        Terms: Net 30")
    doc.add_paragraph("Bill to: Taz Holdings LLC, 400 Commerce Way, Columbus OH")
    rows = [("10/03/2026", "Columbus → Chicago, 18 pallets", 1_840.00), ("10/09/2026", "Chicago → Detroit, 6 pallets", 920.50),
            ("10/14/2026", "Liftgate and inside delivery", 185.00), ("10/18/2026", "Fuel surcharge 12%", 353.46)]
    table = doc.add_table(rows=1, cols=3)
    table.style = "Table Grid"
    for cell, label in zip(table.rows[0].cells, ["Date", "Description", "Amount"]):
        cell.text = label
    for row in rows:
        for cell, text in zip(table.add_row().cells, [row[0], row[1], money(row[2])]):
            cell.text = text
    total = sum(r[2] for r in rows)
    doc.add_paragraph(f"TOTAL DUE: ${money(total)}")
    doc.add_paragraph("Remit to: Bluebird Freight Lines, PO Box 2210, Toledo OH")
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "scan.docx"
        doc.save(source)
        pdf = pypdfium2.PdfDocument(str(as_pdf(source)))
        pages = []
        for page in pdf:
            image = page.render(scale=150 / 72).to_pil().convert("L").rotate(0.6, expand=True, fillcolor=255)
            pages.append(image.filter(ImageFilter.GaussianBlur(0.6)))
        pdf.close()
    out = io.BytesIO()
    pages[0].save(out, "PDF", resolution=150, save_all=True, append_images=pages[1:])
    name = "Bluebird scan BF-77310.pdf"
    (OUT / name).write_bytes(out.getvalue())
    case(name, "What is the total due on the Bluebird scanned invoice?", [money(total)], needs="ocr")
    case(name, "How much is the fuel surcharge on the Bluebird invoice?", ["353.46"], needs="ocr", held_out=True)


def _de(value: float) -> str:
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# --- 6. A bank export: semicolons, European decimals, hundreds of rows ----------------------------------------------
def bank_export() -> None:
    rng = random.Random(6)
    rows = []
    day = date(2026, 10, 1)
    by_day: dict[str, float] = {}
    credits = []
    for i in range(420):
        if i and i % 14 == 0:
            day += timedelta(days=1)
        debit = rng.random() < 0.7
        amount = round(rng.uniform(12, 9_800), 2)
        payee = rng.choice(["AMAZON MKTPL", "ADP PAYROLL", "UPS", "COMCAST", "STAPLES", "DELTA AIR", "CUSTOMER DEPOSIT", "WIRE IN"])
        rows.append([day.strftime("%Y-%m-%d"), payee, _de(-amount if debit else amount), "EUR"])
        if debit:
            by_day[day.isoformat()] = by_day.get(day.isoformat(), 0) + amount
        else:
            credits.append(amount)
    path = OUT / "bank_export_oct2026.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(["Buchungstag", "Empfänger", "Betrag", "Währung"])
        writer.writerows(rows)
    case(path.name, "What is the total of debits on 2026-10-15 in the bank export?", [_de(by_day["2026-10-15"]), money(by_day["2026-10-15"])],
         sql="SELECT -SUM(betrag) FROM {csv} WHERE buchungstag = '2026-10-15' AND betrag < 0")
    case(path.name, "What was the largest single credit in the bank export?", [_de(max(credits)), money(max(credits))], held_out=True)


# --- 7. A board deck with a table on a slide --------------------------------------------------------------------------
def board_deck() -> None:
    deck = Presentation()
    title = deck.slides.add_slide(deck.slide_layouts[0])
    title.shapes.title.text = "Q3 2026 Board update"
    title.placeholders[1].text = "Finance — confidential"
    text = deck.slides.add_slide(deck.slide_layouts[1])
    text.shapes.title.text = "Highlights"
    text.placeholders[1].text = "Revenue up 8% on Q2\nGross margin 41.2%\nTwo new distribution partners in APAC"
    slide = deck.slides.add_slide(deck.slide_layouts[5])
    slide.shapes.title.text = "Revenue by region ($000)"
    regions = [("North America", 14_220, 15_105), ("EMEA", 6_480, 7_315), ("APAC", 3_905, 4_260), ("LATAM", 1_210, 1_198)]
    shape = slide.shapes.add_table(len(regions) + 2, 3, Inches(1), Inches(1.8), Inches(8), Inches(3))
    grid = shape.table
    for c, label in enumerate(["Region", "Q2 2026", "Q3 2026"]):
        grid.cell(0, c).text = label
    for r, (name, q2, q3) in enumerate(regions, start=1):
        grid.cell(r, 0).text, grid.cell(r, 1).text, grid.cell(r, 2).text = name, f"{q2:,}", f"{q3:,}"
    grid.cell(len(regions) + 1, 0).text = "Total"
    grid.cell(len(regions) + 1, 1).text = f"{sum(r[1] for r in regions):,}"
    grid.cell(len(regions) + 1, 2).text = f"{sum(r[2] for r in regions):,}"
    path = OUT / "Board update Q3 2026.pptx"
    deck.save(path)
    case(path.name, "What was EMEA revenue in Q3 according to the board deck?", ["7,315"])
    case(path.name, "Which region's revenue fell from Q2 to Q3 in the board deck?", ["LATAM"], held_out=True)


# --- 8. A wide payroll journal: forty columns, three hundred rows ----------------------------------------------------
def payroll_journal() -> None:
    rng = random.Random(8)
    wb = Workbook()
    ws = wb.active
    ws.title = "Journal 10-15-26"
    extra = [f"Code {n}" for n in range(1, 25)]
    head = ["Emp ID", "Name", "Dept", "Gross", "Fed WH", "State WH", "FICA", "Medicare", "401k EE", "401k ER", "Health EE", "Health ER",
            "Dental", "Vision", "HSA", "Net pay"] + extra
    ws.append(head)
    match_410 = 0.0
    gross_by_dept: dict[str, float] = {}
    for i in range(300):
        dept = rng.choice(["210", "320", "410", "520"])
        gross = round(rng.uniform(1_800, 9_500), 2)
        er = round(gross * rng.choice([0.03, 0.04, 0.05]), 2)
        values = [f"E{1000 + i}", f"Employee {i:03d}", dept, gross, round(gross * 0.12, 2), round(gross * 0.04, 2), round(gross * 0.062, 2),
                  round(gross * 0.0145, 2), round(gross * 0.06, 2), er, 140.0, 410.0, 22.5, 6.1, 50.0, round(gross * 0.68, 2)]
        values += [0 if rng.random() < 0.9 else round(rng.uniform(1, 90), 2) for _ in extra]
        ws.append(values)
        if dept == "410":
            match_410 += er
        gross_by_dept[dept] = gross_by_dept.get(dept, 0) + gross
    path = OUT / "Payroll journal 10-15-26.xlsx"
    wb.save(path)
    case(path.name, "What is the total employer 401k match for department 410 in the payroll journal?", [money(match_410)],
         sql="SELECT SUM(c_401k_er) FROM {26} WHERE dept = 410")
    case(path.name, "What is total gross pay for department 520?", [money(gross_by_dept["520"])], held_out=True)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for make in (opex_pack, stacked_aging, vendor_statement, long_agreement, euro_invoice, bank_export, board_deck, payroll_journal, scanned_statement):
        make()
    (OUT / "cases.json").write_text(json.dumps(CASES, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(CASES)} questions over {len({c['file'] for c in CASES})} files in {OUT}")


if __name__ == "__main__":
    main()
