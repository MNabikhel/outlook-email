from __future__ import annotations

import io
import json
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from controller_inbox import teach, web
from controller_inbox.config import Settings
from controller_inbox.demo import _pdf
from controller_inbox.extract import extract_fields, extract_text_from_bytes, parse_due_date, redact_financial_secrets
from controller_inbox.folder_mail import ingest_folder
from controller_inbox.models import RawAttachment, RawMessage
from controller_inbox.pipeline import process_message, rescore_stored
from controller_inbox.store import Store
from helpers import PAGE
from msgfactory import PDF, XLSX


def test_invoice_and_amount_and_due():
    text = "Please see attached invoice INV-10482. Amount due $12,850.00. Payment due October 5, 2026."
    fields = extract_fields(text, as_of=date(2026, 9, 22), extra_vendor="Northwind")
    assert "INV-10482" in fields.invoice_numbers
    assert 12850.0 in fields.amounts
    assert "2026-10-05" in fields.due_dates
    assert fields.mentions_attachment
    assert "Northwind" in fields.vendor_candidates


def test_relative_due_dates():
    fields = extract_fields(
        "Please approve by EOD. Please respond by Friday.",
        as_of=date(2026, 9, 22),
    )
    assert "2026-09-22" in fields.due_dates
    assert "2026-09-25" in fields.due_dates  # Friday after Tuesday


def test_redacts_routing_and_account():
    raw = "Updated banking instructions: routing 021000021 account 9988776612"
    redacted = redact_financial_secrets(raw)
    assert "021000021" not in redacted
    assert "9988776612" not in redacted
    assert "****0021" in redacted or "****6612" in redacted


def test_pdf_text_roundtrip():
    data = _pdf("Invoice INV-777 Amount due $500.00 Due date 2026-10-01")
    text = extract_text_from_bytes("INV-777.pdf", "application/pdf", data)
    assert "INV-777" in text
    assert "500.00" in text


def test_rtf_text_keeps_coded_characters_and_leaves_out_hidden_groups():
    rtf = (
        r"{\rtf1\ansi\ansicpg1252\deff0{\fonttbl{\f0\fnil\fcharset0 Calibri;}{\f1\fswiss Arial;}}"
        r"{\colortbl ;\red0\green0\blue255;}{\*\generator Riched20 10.0.19041}\viewkind4\uc1" "\r\n"
        r"\pard\sa200\sl276\slmult1\f0\fs22 Invoice INV-7781\par" "\r\n"
        r"Amount due: \'a34,250.00 \endash\~net 30\par" "\r\n"
        r"{\pict\pngblip\picw10\pich10 89504e470d0a1a0a0000000d4948445200000010}"
        r"Caf\'e9 Rouge Ltd, \{ref\} \u8364\'80 12\par" "\r\n"
        r"\trowd\intbl Item\cell Amount\cell\row}"
    )
    text = extract_text_from_bytes("remittance.rtf", "application/rtf", rtf.encode("latin-1"))
    assert text == "Invoice INV-7781\nAmount due: £4,250.00 – net 30\nCafé Rouge Ltd, {ref} € 12\nItem | Amount"
    assert extract_fields(text, as_of=date(2026, 10, 1)).invoice_numbers == ["INV-7781"]


def test_account_iban_routing_and_card_numbers_are_masked_however_they_are_printed():
    cases = {
        "Account number: 1234 5678 9012": "account ****9012",
        "Account number: 1234-5678-9012": "account ****9012",
        "IBAN: GB29 NWBK 6016 1331 9268 19": "IBAN ****6819",
        "IBAN DE89-3704-0044-0532-0130-00": "IBAN ****3000",
        "A/C No: 12345678": "account ****5678",
        "A/C No. 12345678": "account ****5678",
        "Account Number - 12345678": "account ****5678",
        "Account number is 12345678": "account ****5678",
        "Routing: 021-000-021": "routing ****0021",
        "Sort code: 12-34-56": "routing ****3456",
        "Credit card 4111 1111 1111 1111": "card ****1111",
        "Card number: 4111111111111111": "card ****1111",
    }
    for text, masked in cases.items():
        assert redact_financial_secrets(text) == masked, text
    # Already masked text stays as it is.
    assert redact_financial_secrets("account ****9012, IBAN ****6819") == "account ****9012, IBAN ****6819"


def test_masking_leaves_invoice_po_phone_date_amount_and_zip_numbers_alone():
    untouched = [
        "Invoice INV-2024-0457 for $12,450.00 due 2026-10-15",
        "Invoice 1234 5678 attached",
        "PO 4500012345, PO# 4500-0123",
        "Call 555-123-4567 or (212) 555-0199, +44 20 7946 0958",
        "Account manager: 555-123-4567. Accounts payable: 212 555 0199",
        "Account statement 2026-09-30 attached",
        "Account: 10-15-2026 review",
        "Account balance 12,345.67, account as of 09/30/2026",
        "Ship to 123 Main St, Springfield, IL 62704-1234",
        "Card 2 of 3 enclosed. Visa application 2026-10-01",
        "GL Account 1200",
        "Order 1234-5678-9012-3456 shipped",
    ]
    for text in untouched:
        assert redact_financial_secrets(text) == text, text


def test_an_account_number_in_the_subject_is_masked_too(tmp_path):
    from datetime import datetime, timezone

    from controller_inbox.config import Settings
    from controller_inbox.models import RawMessage
    from controller_inbox.pipeline import process_message
    from controller_inbox.store import Store

    settings = Settings(data_dir=tmp_path, inbox_dir=tmp_path / "inbox", llm_base_url="http://127.0.0.1:9/v1", _env_file=None)
    store = Store(settings.db_path)
    raw = RawMessage(
        id="m1", subject="New remittance details - Acct 44556677", sender_name="Vendor", sender_email="ar@vendor.com",
        received_at=datetime(2026, 9, 21, 10, tzinfo=timezone.utc), body_text="Please use IBAN DE89 3704 0044 0532 0130 00.",
        body_preview="", has_attachments=False, source="folder",
    )
    email = process_message(raw, store, settings)
    assert email.subject == "New remittance details - account ****6677"
    assert email.body_text == "Please use IBAN ****3000."
    assert all("44556677" not in task.title for task in email.actions)


def test_a_credit_notes_total_keeps_its_minus_sign():
    as_of = date(2026, 10, 20)
    assert teach.value_of("total", "Credit total (450.00)", as_of=as_of) == "-450.0"
    assert teach.value_of("total", "Amount due -1,250.00", as_of=as_of) == "-1250.0"
    assert teach.value_of("total", "Balance 1,250.00 CR", as_of=as_of) == "-1250.0"
    assert teach.value_of("total", "Total due $2,118.40", as_of=as_of) == "2118.4"


def _details(settings, data: bytes) -> dict:
    from controller_inbox import page_details, page_view

    settings.ensure_data_dir()
    found = page_view.regions(settings, data, "file.pdf", 1)["regions"]
    return {d["label"]: d["value"] for d in page_details.key_details(found, "text", None, {"invoices": [], "pos": [], "due_dates": [], "vendors": []})}


def test_a_schedules_total_due_is_its_grand_total(settings):
    from pathlib import Path

    data = (Path(__file__).parent / "fixtures" / "excel_schedules" / "AP Aging 9-30-26.pdf").read_bytes()
    # The total row lists Current, each aging bucket and the Total column: the grand total is under "Total".
    assert _details(settings, data)["Total due"].replace(" ", "") == "$267,159.31"


def test_an_invoices_total_due_is_unchanged(settings):
    from test_page_view import _detailed_invoice

    assert _details(settings, _detailed_invoice())["Total due"] == "$6,327.59"


# Word: tracked changes ------------------------------------------------------------------------------
def _tracked_docx() -> bytes:
    """A vendor's revised invoice sent with Track Changes on: the total was cut from $12,500.00 to $10,250.00."""
    import io

    from docx import Document
    from docx.oxml import parse_xml

    W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    doc = Document()
    doc.add_paragraph("Invoice INV-7781")
    doc.add_paragraph("Northwind Services")
    para = parse_xml(
        f'<w:p {W}><w:r><w:t xml:space="preserve">Total due: </w:t></w:r>'
        f'<w:del w:id="1" w:author="AR" w:date="2026-10-01T00:00:00Z"><w:r><w:delText>$12,500.00</w:delText></w:r></w:del>'
        f'<w:ins w:id="2" w:author="AR" w:date="2026-10-01T00:00:00Z"><w:r><w:t>$10,250.00</w:t></w:r></w:ins></w:p>'
    )
    doc.element.body.insert(len(doc.element.body) - 1, para)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


@pytest.mark.usefixtures("harbor_ocr")
def test_a_figure_deleted_in_tracked_changes_isnt_read_as_the_invoice_amount():
    from datetime import date

    from controller_inbox.documents import docx_text
    from controller_inbox.extract import extract_fields

    text = docx_text(_tracked_docx())
    assert "$10,250.00" in text
    fields = extract_fields(text, as_of=date(2026, 10, 20))
    assert 12500.0 not in fields.amounts, (fields.amounts, text)
    assert fields.primary_amount == 10250.0


# The Page tab's total on a schedule whose total row isn't a total due ----------------------------------------------
@pytest.mark.usefixtures("harbor_ocr")
def test_a_schedules_total_due_is_never_its_total_cost_nor_a_row_of_joined_figures(settings):
    from pathlib import Path

    fixtures = Path(__file__).parent / "fixtures"
    prepaid = _details(settings, (fixtures / "excel_schedules" / "Prepaid Amortization FY2026.pdf").read_bytes())
    assert "462,120.00" not in prepaid.get("Total due", ""), "the Total Cost column is what was paid, not what is due"
    matrix = _details(settings, (fixtures / "more_schedules" / "Intercompany Matrix 9-30-26.pdf").read_bytes())
    total = matrix.get("Total due")
    assert total is None or total.count(".") <= 1, total
    aging = _details(settings, (fixtures / "excel_schedules" / "AP Aging 9-30-26.pdf").read_bytes())
    assert aging["Total due"].replace(" ", "") == "$267,159.31"


# ISO dates after "invoice" aren't invoice numbers, but year-sequence and account-suffix invoice IDs of the same
# shape ("2025-47", "1001-15", "4512-03") are.


@pytest.mark.parametrize("text, wanted", [
    ("Please find attached Invoice No. 2025-47 for September services.", "2025-47"),
    ("Invoice #1001-15 is now past due.", "1001-15"),
    ("Attached: invoice 4512-03, amount $2,400.00", "4512-03"),
])
def test_year_sequence_invoice_id_is_kept(text, wanted):
    fields = extract_fields(text, as_of=date(2026, 10, 1))
    assert wanted in fields.invoice_numbers, fields.invoice_numbers


# Guards against: 'Payment is due on October 12, 2026' (the most common way to write it) gives the invoice no due
# date.
def test_invoice_due_on_date_is_read(store, settings):
    now = datetime(2026, 10, 9, 13, 0, tzinfo=timezone.utc)
    raw = RawMessage(
        id="m-due-on",
        subject="Invoice INV-2201 from Northwind Supply",
        sender_name="Northwind AR",
        sender_email="ar@northwind.com",
        received_at=now,
        body_text="Hello,\n\nPlease find invoice INV-2201 for $4,800.00 attached. Payment is due on October 12, 2026.\n\nThanks",
        body_preview="",
        has_attachments=False,
        source="folder",
    )
    email = process_message(raw, store, settings, now=now)
    assert email.extracted.primary_due == "2026-10-12"
    invoice_task = next(a for a in email.actions if a.source == "invoice")
    assert invoice_task.due_date == "2026-10-12"


# Guards against: A date after the word "invoice" ("Your invoice - 2026-10-01", "Invoice 2026-10") is taken as the invoice number.
# Two subscription vendors billing on the same day then match each other in the duplicate check (which compares
# numbers across all senders), and the second real invoice is flagged "possible duplicate".


def test_iso_date_is_not_an_invoice_number():
    fields = extract_fields("Your Adobe invoice - 2026-10-01", as_of=date(2026, 10, 1))
    assert "2026-10-01" not in fields.invoice_numbers


def _bill(mid, vendor, address):
    return RawMessage(
        id=mid,
        subject=f"Your {vendor} invoice - 2026-10-01",
        sender_name=vendor,
        sender_email=address,
        received_at=datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc),
        body_text=f"Thanks for your business. Your {vendor} subscription invoice total is $1,250.00. Amount due: $1,250.00",
        body_preview="",
        has_attachments=False,
        source="graph",
    )


def test_two_vendors_same_billing_day_are_not_duplicates(store, settings):
    now = datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc)
    process_message(_bill("m-adobe", "Adobe", "billing@adobe.com"), store, settings, now=now)
    zoom = process_message(_bill("m-zoom", "Zoom", "billing@zoom.us"), store, settings, now=now)
    assert "duplicate_invoice" not in zoom.flags, (zoom.category, zoom.extracted.invoice_numbers)


AS_OF = date(2026, 10, 6)


NOW = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)


def _raw(body: str, *, received: datetime, attachments=None, preview: str = "") -> RawMessage:
    return RawMessage(
        id="raw-1",
        subject="Docs",
        sender_name="A Vendor",
        sender_email="a@vendor.com",
        received_at=received,
        body_text=body,
        body_preview=preview,
        has_attachments=bool(attachments),
        source="folder",
        attachments=attachments or [],
    )


# Account masking needs digits ---------------------------------------------------------------
def test_masking_leaves_ordinary_words_alone_and_masks_real_numbers():
    text = "Review the account statement, ask the account manager. Accountability. account number 123456789012"
    masked = redact_financial_secrets(text)
    assert "account statement" in masked and "account manager" in masked and "Accountability" in masked
    assert "123456789012" not in masked and "account ****9012" in masked
    assert "acct no. 55512345" not in redact_financial_secrets("acct no. 55512345")


def test_account_last4_comes_only_from_digits():
    words = extract_fields("Your account balance is overdue", as_of=AS_OF)
    assert words.account_last4 == [] and not words.mentions_routing_or_account
    real = extract_fields("Account: AB12345678", as_of=AS_OF)
    assert real.account_last4 == ["5678"] and real.mentions_routing_or_account


# Whole-dollar amounts ------------------------------------------------------------------------
def test_whole_dollar_amounts_are_read():
    fields = extract_fields("Please wire $48,000 today. Fee USD 500. Total: 5,000", as_of=AS_OF)
    assert {48000.0, 500.0, 5000.0} <= set(fields.amounts)
    assert extract_fields("Pay $1,2345 now", as_of=AS_OF).amounts == []
    assert extract_fields("total 3 items", as_of=AS_OF).amounts == []


# Relative dates read against the day the mail was sent --------------------------------------
def test_relative_dates_anchor_to_the_received_date(store: Store, settings: Settings):
    received = datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)
    record = process_message(_raw("Please send the W-9 by tomorrow.", received=received), store, settings, now=NOW)
    assert record.extracted.due_dates == ["2026-09-29"]
    task = next(a for a in record.actions if "W-9" in a.title)
    assert task.due_date == "2026-09-29"

    class Check:
        level = "none"

    again = rescore_stored(store, settings, record, Check(), now=NOW)
    assert again.extracted.due_dates == ["2026-09-29"]
    assert next(a for a in again.actions if "W-9" in a.title).due_date == "2026-09-29"


# Invoice numbers and year-less due dates ----------------------------------------------------
def test_invoice_numbers_keep_every_segment():
    fields = extract_fields("Invoice No: INV-2024-0457. Again INV-2024-0457. Invoice 12345-due", as_of=AS_OF)
    assert fields.invoice_numbers == ["INV-2024-0457", "12345"]


def test_due_dates_without_a_year():
    assert extract_fields("Payment due October 15.", as_of=AS_OF).due_dates == ["2026-10-15"]
    assert parse_due_date("October 15th", as_of=AS_OF) == "2026-10-15"
    assert parse_due_date("January 5", as_of=date(2026, 12, 20)) == "2027-01-05"
    assert parse_due_date("Oct 15, 24", as_of=AS_OF) == "2024-10-15"


AS_OF_OCT1 = date(2026, 10, 1)


@pytest.mark.parametrize("text", ["Invoice Number: 12345", "INVOICE NUMBER 12345", "Invoice Number: AB-12345"])
def test_invoice_number_label(text):
    # The separator class ate the "No" of "Number", leaving "mber" where the number should be.
    assert extract_fields(text, as_of=AS_OF_OCT1).invoice_numbers == [text.split()[-1].upper()]


@pytest.mark.parametrize("text", ["PO Number: 4500123", "Purchase Order Number: 4500123"])
def test_po_number_label(text):
    assert extract_fields(text, as_of=AS_OF_OCT1).po_numbers == ["4500123"]


def test_bank_letter_attachment_keeps_account_last4(store, settings):
    # Fields were read from the masked text, where "account ****6789" is no account number at all.
    letter = b"Please update our remittance details.\nBank: First National\nAccount number: 123456789\nRouting number: 021000021\n"
    raw = RawMessage(
        id="m1", subject="Updated remittance", sender_name="Acme", sender_email="ap@acme.com",
        received_at=datetime(2026, 9, 20, 14, tzinfo=timezone.utc), body_text="See attached letter.",
        body_preview="", has_attachments=True,
        attachments=[RawAttachment(id="a1", filename="letter.txt", content_type="text/plain", size_bytes=len(letter), content=letter)],
    )
    email = process_message(raw, store, settings, now=datetime(2026, 9, 21, tzinfo=timezone.utc))
    record = email.attachments[0]
    assert record.extracted_fields.mentions_routing_or_account
    assert "6789" in record.extracted_fields.account_last4
    assert email.extracted.mentions_routing_or_account
    assert "123456789" not in record.extracted_text


@pytest.mark.parametrize(
    "text,last4",
    [
        ("Please use our new IBAN: DE89 3704 0044 0532 0130 00", "3000"),
        ("Account Number - 12345678", "5678"),
        ("Account number: 1234 5678 9012", "9012"),
    ],
)
def test_bank_numbers_the_masker_sees_are_extracted(text, last4):
    # The masker recognised these shapes but the field extractor did not, so bank details read as absent.
    assert "****" in redact_financial_secrets(text)
    fields = extract_fields(text, as_of=AS_OF_OCT1)
    assert fields.mentions_routing_or_account
    assert last4 in fields.account_last4


def test_weekday_followed_by_explicit_date_uses_the_date():
    # Oct 1 2026 is a Thursday; the sender means Friday October 16, not tomorrow.
    assert extract_fields("Please pay by Friday, October 16.", as_of=AS_OF_OCT1).due_dates == ["2026-10-16"]


def test_day_month_year_due_date():
    # "15 October 2026" is how UK/EU suppliers write a due date.
    assert extract_fields("Payment due: 15 October 2026", as_of=AS_OF_OCT1).due_dates == ["2026-10-15"]
    assert extract_fields("Payment due: 15-Oct-2026", as_of=AS_OF_OCT1).due_dates == ["2026-10-15"]
    assert extract_fields("Payment due: 15-Oct-26", as_of=AS_OF_OCT1).due_dates == ["2026-10-15"]


def test_a_figure_after_a_day_first_date_is_not_its_year():
    # "10" and "12" were read as two-digit years: 2010-10-15 and 2012-06-02.
    fields = extract_fields("Payment due 15 October, 10% late fee applies after that.", as_of=date(2026, 10, 1))
    assert fields.due_dates == ["2026-10-15"]
    fields = extract_fields("Please remit by 2 June, 12 cases were short-shipped.", as_of=date(2026, 5, 20))
    assert fields.due_dates == ["2026-06-02"]
    fields = extract_fields("Please respond by 2 June 12 cases short.", as_of=date(2026, 5, 20))
    assert fields.due_dates == ["2026-06-02"]


def test_feb_29_without_year_in_december_before_leap_year():
    assert parse_due_date("February 29", as_of=date(2027, 12, 20)) == "2028-02-29"


# Found by fuzzing.
def test_iban_in_groups_after_account_label_is_masked():
    # Invoices commonly print "Account Number (IBAN): ..." or "Account: GB29 NWBK ...".
    for text in ("Account: GB29 NWBK 6016 1331 9268 19", "Account Number (IBAN): DE89 3704 0044 0532 0130 00"):
        red = redact_financial_secrets(text)
        assert "1331" not in red and "0044 0532" not in red, red


def test_iban_in_groups_after_account_label_counts_as_bank_detail():
    fields = extract_fields("Account: GB29 NWBK 6016 1331 9268 19", as_of=date(2026, 10, 8))
    assert fields.mentions_routing_or_account
    assert "6819" in fields.account_last4


def test_a_printed_grand_total_is_the_amount_of_a_long_schedule():
    from datetime import date

    from controller_inbox.extract import extract_fields

    rows = "\n".join(f"Vendor {n} | Current: ${1000 + n * 10:,.2f} | Over 90: $0 | Total: ${1000 + n * 10:,.2f}" for n in range(12))
    text = f"Accounts Payable Aging\n{rows}\nVendor: Total | Current: $12,660.00 | Over 90: $500.00 | Total: $13,160.00\n"
    fields = extract_fields(text, as_of=date(2026, 10, 20))
    assert fields.primary_amount == 13160.0
    # A short invoice reads as before.
    invoice = extract_fields("Subtotal $4,075.00\nSales tax $244.50\nTotal Due $4,319.50", as_of=date(2026, 10, 20))
    assert invoice.amounts == [4075.0, 244.5, 4319.5] and invoice.primary_amount == 4319.5


SENT = datetime(2026, 10, 19, 13, 0, tzinfo=timezone.utc)


class _Canvas:
    """Text placed on a letter-size page, written as a PDF (the few reportlab canvas calls these tests use)."""

    def __init__(self, out: io.BytesIO):
        self.out, self.font, self.ops = out, ("F1", 12), []

    def setFont(self, name: str, size: float) -> None:
        self.font = ("F2" if name.endswith("Bold") else "F1", size)

    def drawString(self, x: float, y: float, text: str) -> None:
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        self.ops.append(f"BT /{self.font[0]} {self.font[1]} Tf {x} {y} Td ({escaped}) Tj ET")

    def showPage(self) -> None:
        pass

    def save(self) -> None:
        stream = "\n".join(self.ops).encode("latin-1")
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
        ]
        pdf = bytearray(b"%PDF-1.4\n")
        offsets = []
        for number, body in enumerate(objects, 1):
            offsets.append(len(pdf))
            pdf += b"%d 0 obj\n" % number + body + b"\nendobj\n"
        xref = len(pdf)
        pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
        pdf += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
        pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
        self.out.write(bytes(pdf))


def _bolt_invoice(number: str, amount: str) -> bytes:
    """A letterhead, a block of details on the right, a parts table and "Amount Due" with no currency mark."""
    out = io.BytesIO()
    c = _Canvas(out)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(72, 720, "Bolt Fasteners Inc")
    c.setFont("Helvetica", 10)
    c.drawString(72, 704, "77 Rivet Lane, Akron OH 44301")
    c.setFont("Helvetica-Bold", 16)
    c.drawString(420, 720, "INVOICE")
    c.setFont("Helvetica", 11)
    for i, (key, value) in enumerate([("Invoice #", number), ("Date", "10/14/2026"), ("Due", "11/13/2026"), ("PO", "PO-9001"), ("Job Site", "Riverside Plant")]):
        c.drawString(380, 690 - 16 * i, key)
        c.drawString(460, 690 - 16 * i, value)
    y = 580
    c.setFont("Helvetica-Bold", 11)
    for x, text in [(72, "Part"), (200, "Description"), (400, "Qty"), (450, "Price"), (510, "Ext")]:
        c.drawString(x, y, text)
    c.setFont("Helvetica", 11)
    for row in [("B-1", "Lag bolts", "100", "2.00", "200.00"), ("B-2", "Washers", "500", "0.10", "50.00")]:
        y -= 18
        for x, text in zip([72, 200, 400, 450, 510], row):
            c.drawString(x, y, text)
    y -= 30
    c.setFont("Helvetica-Bold", 12)
    c.drawString(400, y, "Amount Due")
    c.drawString(510, y, amount)
    c.showPage()
    c.save()
    return out.getvalue()


def _subledger() -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = "AP subledger"
    sheet.append(["Vendor", "Invoice", "Amount"])
    for row in [("Bolt Fasteners", "BF-5001", 250), ("Acme", "INV-88123", 4319.5), ("Northwind", "INV-10482", 12850)]:
        sheet.append(list(row))
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def _simple_pdf(lines: list[str]) -> bytes:
    out = io.BytesIO()
    c = _Canvas(out)
    c.setFont("Helvetica", 12)
    for i, line in enumerate(lines):
        c.drawString(72, 720 - 20 * i, line)
    c.showPage()
    c.save()
    return out.getvalue()


def _eml(folder: Path, name: str, *, subject: str, body: str, sender: str, address: str, files, when=SENT) -> None:
    message = EmailMessage()
    message["From"] = f"{sender} <{address}>"
    message["To"] = "ap@horizongoods.example"
    message["Subject"] = subject
    message["Date"] = format_datetime(when)
    message["Message-ID"] = f"<{name}@{address.split('@')[1]}>"
    message.set_content(body)
    for filename, data, mime in files:
        main, sub = mime.split("/")
        message.add_attachment(data, maintype=main, subtype=sub, filename=filename)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.eml").write_bytes(bytes(message))


def _ingest(store, settings) -> dict:
    return {email.subject: store.get_email(email.id) for email in ingest_folder(store, settings)}


def _fraud_with_files(settings) -> None:
    _eml(
        settings.inbox_incoming,
        "fraud-att",
        subject="URGENT change of bank details - Bolt Fasteners",
        body="Our bank details have changed. Please update your records and send all future payments to the new account: "
        "routing 021000021 account 99887766. Please pay invoice BF-5001 $250.00 today to the new account. Keep this confidential.",
        sender="Bolt Fasteners Accounts",
        address="accounts@b0ltfasteners.example",
        files=[("new-bank-letter.pdf", _bolt_invoice("BF-5001", "250.00"), PDF), ("bank.xlsx", _subledger(), XLSX)],
    )


# Invoice numbers listed in a workbook on an email are not that email's own invoices.
def test_an_invoice_listed_in_a_subledger_is_no_duplicate(store, settings):
    """Email A's bank.xlsx lists INV-10482; email B's invoice INV-10482 is not flagged as a possible duplicate."""
    settings.ensure_data_dir()
    _fraud_with_files(settings)
    first = _ingest(store, settings)["URGENT change of bank details - Bolt Fasteners"]
    assert "INV-10482" not in first.extracted.invoice_numbers
    _eml(
        settings.inbox_incoming,
        "nw-10482",
        subject="Invoice INV-10482 from Northwind Supplies",
        body="Please find invoice INV-10482 attached. Total $12,850.00.",
        sender="Northwind Billing",
        address="billing@northwind.example",
        files=[("INV-10482.pdf", _simple_pdf(["Northwind Supplies", "INVOICE", "Invoice No: INV-10482", "Total Due: $12,850.00"]), PDF)],
        when=SENT + timedelta(hours=2),
    )
    second = _ingest(store, settings)["Invoice INV-10482 from Northwind Supplies"]
    assert "duplicate_invoice" not in second.flags
    assert not [a for a in second.actions if "duplicate" in a.title.lower()]


# "Amount Due 250.00" with no currency mark is the email's amount.
def test_amount_due_without_a_currency_mark_is_the_emails_amount(store, settings):
    """The invoice's printed "Amount Due 250.00" becomes the email's amount, as the Page tab reads it."""
    settings.ensure_data_dir()
    _eml(
        settings.inbox_incoming,
        "bolt-1",
        subject="Invoice BF-5001 from Bolt Fasteners",
        body="Invoice attached.\nBolt AR",
        sender="Bolt Fasteners AR",
        address="ar@boltfasteners.example",
        files=[("BF-5001.pdf", _bolt_invoice("BF-5001", "250.00"), PDF)],
    )
    email = _ingest(store, settings)["Invoice BF-5001 from Bolt Fasteners"]
    assert email.extracted.primary_amount == 250.0
    client = TestClient(web.create_app(settings, store))
    [card] = [item for item in client.get("/api/mail", headers=PAGE).json()["items"] if item["id"] == email.id]
    assert "250.00" in json.dumps(card)


def test_an_invoice_table_keeps_qty_and_price_apart_and_checks_its_amount_due(store, settings):
    """"Amount Due" written across the Qty and Price columns doesn't merge them, and is checked as the table's total."""
    settings.ensure_data_dir()
    _eml(settings.inbox_incoming, "bolt-1", subject="Invoice BF-5001 from Bolt Fasteners", body="Invoice attached.",
         sender="Bolt Fasteners AR", address="ar@boltfasteners.example", files=[("BF-5001.pdf", _bolt_invoice("BF-5001", "250.00"), PDF)])
    [email] = _ingest(store, settings).values()
    data = TestClient(web.create_app(settings, store)).get(f"/api/mail/{email.id}/files/1", headers=PAGE).json()
    [parts] = [table for table in data["tables"] if "Part" in table["labels"]]
    assert parts["labels"] == ["Part", "Description", "Qty", "Price", "Ext"]
    assert parts["check"]["matched"] == 1 and not parts["check"]["mismatched"]
