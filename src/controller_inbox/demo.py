from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from typing import Iterable

from controller_inbox.models import RawAttachment, RawMessage


def _pdf(text: str) -> bytes:
    safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 11 Tf 48 720 Td ({safe[:1200]}) Tj ET".encode("latin-1", errors="replace")
    objects = [
        b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj",
        b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj",
        (
            b"3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources<< /Font<< /F1 5 0 R >> >> >>endobj"
        ),
        b"4 0 obj<< /Length %d >>stream\n" % len(stream) + stream + b"\nendstream endobj",
        b"5 0 obj<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>endobj",
    ]
    header = b"%PDF-1.1\n"
    xref_positions = []
    body = b""
    offset = len(header)
    for obj in objects:
        xref_positions.append(offset)
        body += obj + b"\n"
        offset += len(obj) + 1
    xref = [b"xref", b"0 6", b"0000000000 65535 f "]
    for pos in xref_positions:
        xref.append(f"{pos:010d} 00000 n ".encode())
    trailer = (
        b"\n".join(xref)
        + b"\ntrailer<< /Size 6 /Root 1 0 R >>\nstartxref\n"
        + str(offset).encode()
        + b"\n%%EOF"
    )
    return header + body + trailer


def _xlsx(rows: list[list[object]], sheet: str = "Sheet1") -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _att(name: str, data: bytes, content_type: str) -> RawAttachment:
    return RawAttachment(
        id=f"att-{name}",
        filename=name,
        content_type=content_type,
        size_bytes=len(data),
        content=data,
    )


def demo_messages(now: datetime | None = None) -> list[RawMessage]:
    """Realistic assistant-controller mailbox snapshot (no live Outlook required)."""
    now = now or datetime(2026, 9, 22, 8, 15, tzinfo=timezone.utc)

    def ts(hours_ago: float) -> datetime:
        return now - timedelta(hours=hours_ago)

    invoice_pdf = _pdf(
        "Northwind Supplies  Invoice INV-10482  Bill To: Horizon Goods LLC  "
        "Amount due $12,850.00  Due date October 5, 2026  Terms Net 15  "
        "Vendor: Northwind Supplies  Remit to Northwind Supplies"
    )
    stmt_pdf = _pdf(
        "Chase Business Checking  Account statement  Statement period September 1 2026 "
        "through September 21 2026  Beginning balance $184,220.11  Ending balance $201,440.90"
    )
    po_pdf = _pdf(
        "Purchase Order PO-77821  Vendor Northwind Supplies  Ship to Horizon warehouse  "
        "Please fulfill  Total $12,850.00"
    )
    tax_pdf = _pdf(
        "Internal Revenue Service Notice CP2000  Information mismatch  "
        "Please respond by October 12, 2026  Proposed amount $3,410.00"
    )
    payroll_xlsx = _xlsx(
        [
            ["Pay date", "2026-09-18"],
            ["Employee", "Gross pay", "Net pay", "Employer taxes"],
            ["A. Chen", 4200, 3188.40, 321.15],
            ["R. Patel", 5100, 3794.20, 402.10],
            ["Totals", 9300, 6982.60, 723.25],
        ],
        sheet="Payroll register",
    )
    expense_xlsx = _xlsx(
        [
            ["Expense report", "ER-2291"],
            ["Employee", "Jordan Blake"],
            ["Date", "Merchant", "Amount"],
            ["2026-09-16", "United Airlines", 412.80],
            ["2026-09-17", "Marriott", 214.19],
            ["Total due", 627.00],
        ],
        sheet="ER-2291",
    )
    remittance_csv = (
        "invoice,amount,payment_date\n9001,4400.00,2026-09-21\n9002,1875.50,2026-09-21\n"
    ).encode()
    close_xlsx = _xlsx(
        [
            ["Close item", "Owner", "Due"],
            ["Prepaid rollforward", "Close team", "2026-09-29"],
            ["AP accruals", "Close team", "2026-09-30"],
            ["Bank rec - operating", "Close team", "2026-09-28"],
        ],
        sheet="Close calendar",
    )

    messages = [
        RawMessage(
            id="demo-inv-10482",
            subject="Invoice INV-10482 from Northwind Supplies — $12,850.00 due Oct 5",
            sender_name="Northwind Billing",
            sender_email="billing@northwindsupplies.com",
            received_at=ts(6),
            body_text=(
                "Hello AP team,\n\nPlease see attached invoice INV-10482 for September freight "
                "and warehouse supplies. Amount due $12,850.00. Payment due October 5, 2026. "
                "Please code and enter the invoice today if possible.\n\nThank you,\nNorthwind Supplies"
            ),
            body_preview="Please see attached invoice INV-10482...",
            has_attachments=True,
            outlook_importance="normal",
            source="demo",
            attachments=[_att("INV-10482.pdf", invoice_pdf, "application/pdf")],
        ),
        RawMessage(
            id="demo-inv-10482-dup",
            subject="FW: Invoice INV-10482 from Northwind Supplies — $12,850.00 due Oct 5",
            sender_name="Northwind Billing",
            sender_email="billing@northwindsupplies.com",
            received_at=ts(2),
            body_text=(
                "Resending invoice INV-10482 in case the first note was missed. "
                "Invoice INV-10482 amount due $12,850.00 due October 5, 2026. Please see attached."
            ),
            body_preview="Resending invoice INV-10482...",
            has_attachments=True,
            source="demo",
            attachments=[_att("INV-10482.pdf", invoice_pdf, "application/pdf")],
        ),
        RawMessage(
            id="demo-chase-stmt",
            subject="Your September 2026 business checking statement is ready",
            sender_name="Chase Business",
            sender_email="statements@chase.com",
            received_at=ts(10),
            body_text=(
                "Your account statement for Chase Business Checking is attached. "
                "Statement period September 1, 2026 through September 21, 2026. "
                "Ending balance $201,440.90. Please reconcile before month end."
            ),
            body_preview="Your account statement for Chase Business Checking is attached.",
            has_attachments=True,
            source="demo",
            attachments=[_att("Chase_Stmt_Sep2026.pdf", stmt_pdf, "application/pdf")],
        ),
        RawMessage(
            id="demo-bec-wire",
            subject="Urgent: Updated wiring instructions for Apex Vendor",
            sender_name="CFO Office",
            sender_email="cfo.office@horizon-goods-mail.net",
            received_at=ts(1.5),
            body_text=(
                "I am in meetings the rest of the day. Our bank details have changed for Apex Vendor. "
                "Please use the following account going forward and do not use the previous account. "
                "Updated banking instructions: routing 021000021 account 9988776612. "
                "Please process the outstanding $48,500.00 wire today. Do not process to the old account."
            ),
            body_preview="Our bank details have changed for Apex Vendor...",
            has_attachments=False,
            outlook_importance="high",
            source="demo",
        ),
        RawMessage(
            id="demo-payroll",
            subject="Payroll register for 9/18/2026 pay date",
            sender_name="ADP Reports",
            sender_email="noreply@adp.com",
            received_at=ts(30),
            body_text=(
                "The payroll register for pay date 9/18/2026 is attached. "
                "Gross pay $9,300.00, employer taxes $723.25. Please review and post the payroll JE."
            ),
            body_preview="The payroll register for pay date 9/18/2026 is attached.",
            has_attachments=True,
            source="demo",
            attachments=[
                _att(
                    "payroll_register_2026-09-18.xlsx",
                    payroll_xlsx,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            ],
        ),
        RawMessage(
            id="demo-pbc",
            subject="PBC list — Q3 inventory and cash testing",
            sender_name="Tina Park, Audit Senior",
            sender_email="tina.park@big4example.com",
            received_at=ts(8),
            body_text=(
                "Hi — kicking off Q3 interim testing. Please provide by September 25, 2026: "
                "(1) August and September bank recs, (2) inventory rollforward, and "
                "(3) AP aging. This is a prepared by client (PBC) request from the external audit team. "
                "Please send the workpapers in the usual PBC folder."
            ),
            body_preview="Kicking off Q3 interim testing. Please provide by September 25...",
            has_attachments=False,
            outlook_importance="high",
            source="demo",
        ),
        RawMessage(
            id="demo-expense",
            subject="Expense report ER-2291 for approval",
            sender_name="Jordan Blake",
            sender_email="jordan.blake@horizongoods.example",
            received_at=ts(20),
            body_text=(
                "Hi, please approve my expense report ER-2291 for the Dallas vendor visit. "
                "Total $627.00. Receipts are in the attached spreadsheet."
            ),
            body_preview="Please approve my expense report ER-2291...",
            has_attachments=True,
            source="demo",
            attachments=[
                _att(
                    "ER-2291.xlsx",
                    expense_xlsx,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            ],
        ),
        RawMessage(
            id="demo-tax",
            subject="Notice CP2000 — Information mismatch",
            sender_name="IRS Correspondence",
            sender_email="noreply@irs.gov",
            received_at=ts(15),
            body_text=(
                "A CP2000 notice is attached. Please review the information mismatch and "
                "respond by October 12, 2026. Proposed amount $3,410.00."
            ),
            body_preview="A CP2000 notice is attached.",
            has_attachments=True,
            outlook_importance="high",
            source="demo",
            attachments=[_att("IRS_CP2000.pdf", tax_pdf, "application/pdf")],
        ),
        RawMessage(
            id="demo-newsletter",
            subject="This week in accounting: close shortcuts and FASB roundup",
            sender_name="Accounting Today Briefing",
            sender_email="briefing@accountingtoday.example",
            received_at=ts(12),
            body_text=(
                "You are receiving this weekly roundup because you subscribed. "
                "View in browser. Unsubscribe at any time. Five close shortcuts CFOs are trying..."
            ),
            body_preview="You are receiving this weekly roundup because you subscribed.",
            has_attachments=False,
            source="demo",
        ),
        RawMessage(
            id="demo-remittance",
            subject="Payment remittance — invoices 9001 and 9002",
            sender_name="Apex Retail AR",
            sender_email="remit@apexretail.example",
            received_at=ts(4),
            body_text=(
                "We have paid invoices 9001 and 9002. Please see the attached remittance advice "
                "and apply cash. ACH credit $6,275.50 dated September 21, 2026."
            ),
            body_preview="We have paid invoices 9001 and 9002...",
            has_attachments=True,
            source="demo",
            attachments=[_att("remittance_2026-09-21.csv", remittance_csv, "text/csv")],
        ),
        RawMessage(
            id="demo-close",
            subject="Close calendar — September 30 items",
            sender_name="Priya Shah, Controller",
            sender_email="priya.shah@horizongoods.example",
            received_at=ts(18),
            body_text=(
                "Team — month end is September 30. Please complete the prepaid rollforward and AP accruals. "
                "Close checklist is attached. Need you to finish the operating bank rec by September 28."
            ),
            body_preview="Month end is September 30. Please complete the prepaid rollforward...",
            has_attachments=True,
            outlook_importance="high",
            source="demo",
            attachments=[
                _att(
                    "Sep2026_close_calendar.xlsx",
                    close_xlsx,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            ],
        ),
        RawMessage(
            id="demo-po",
            subject="PO-77821 issued to Northwind Supplies",
            sender_name="Purchasing",
            sender_email="purchasing@horizongoods.example",
            received_at=ts(40),
            body_text="Purchase order PO-77821 has been issued to Northwind Supplies for $12,850.00. Copy attached.",
            body_preview="Purchase order PO-77821 has been issued...",
            has_attachments=True,
            source="demo",
            attachments=[_att("PO-77821.pdf", po_pdf, "application/pdf")],
        ),
        RawMessage(
            id="demo-missing-att",
            subject="Invoice attached — Harbor Packaging",
            sender_name="Harbor Packaging",
            sender_email="ap@harborpackaging.example",
            received_at=ts(3),
            body_text=(
                "Please see attached invoice for pallets shipped last week. "
                "Amount due $1,980.00, due October 1, 2026. Please process."
            ),
            body_preview="Please see attached invoice for pallets shipped last week.",
            has_attachments=False,
            source="demo",
        ),
        RawMessage(
            id="demo-wire-legit",
            subject="Please wire $8,400.00 to Lakeside Tooling — invoice INV-3310",
            sender_name="AP Shared Services",
            sender_email="ap@horizongoods.example",
            received_at=ts(5),
            body_text=(
                "Please wire $8,400.00 to Lakeside Tooling for invoice INV-3310 today. "
                "Beneficiary bank is on file. This is an outgoing wire request for a scheduled payment, "
                "not a change of account."
            ),
            body_preview="Please wire $8,400.00 to Lakeside Tooling...",
            has_attachments=False,
            outlook_importance="high",
            source="demo",
        ),
    ]
    return messages


class DemoMailbox:
    def __init__(self, now: datetime | None = None):
        self._messages = {item.id: item for item in demo_messages(now)}

    def list_messages(self, received_after: datetime | None = None) -> Iterable[RawMessage]:
        for message in self._messages.values():
            if received_after and message.received_at <= received_after:
                continue
            yield message

    def get_attachments(self, message_id: str) -> list[RawAttachment]:
        message = self._messages.get(message_id)
        return list(message.attachments) if message else []

    def apply_categories(self, message_id: str, categories: list[str], flag: bool) -> str:
        if message_id not in self._messages:
            return "missing"
        return "demo-not-written"
