from __future__ import annotations

import io
import os
import time
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

import pytest
from openpyxl import Workbook

from controller_inbox.actions import received_day
from controller_inbox.classify import classify_document, classify_email
from controller_inbox.extract import extract_fields
from controller_inbox.folder_mail import ingest_folder
from controller_inbox.learn import match_correction, record_correction
from controller_inbox.models import DocumentType, ExtractedFields, Importance, RawMessage
from controller_inbox.pipeline import process_message
from controller_inbox.reading import ReadingRefused, apply_bionic_reading
from msgfactory import XLSX, write_msg

AS_OF = date(2026, 9, 22)


def test_invoice_pdf_filename():
    result = classify_document(
        subject="Invoice INV-10482",
        body="Please enter this vendor invoice. Amount due $12,850.00.",
        filename="INV-10482.pdf",
        extracted_text="Invoice INV-10482 Amount due $12,850.00 Due date October 5, 2026 Remit to Northwind",
    )
    assert result.document_type == DocumentType.AP_INVOICE


def test_payment_instruction_change_is_fraud():
    result = classify_document(
        subject="Urgent: Updated wiring instructions for Apex Vendor",
        body="Our bank details have changed. Please use the following account and do not use the previous account.",
        sender="cfo.office@horizon-goods-mail.net",
    )
    assert result.document_type == DocumentType.PAYMENT_INSTRUCTION_CHANGE
    assert "fraud_risk" in result.flags


def test_bank_statement_from_chase():
    result = classify_document(
        subject="Your September 2026 business checking statement is ready",
        body="Account statement is attached. Ending balance $201,440.90.",
        filename="Chase_Stmt_Sep2026.pdf",
        sender="statements@chase.com",
        extracted_text="Chase Business Checking account statement beginning balance ending balance",
    )
    assert result.document_type == DocumentType.BANK_STATEMENT


def test_payroll_from_adp():
    result = classify_document(
        subject="Payroll register for 9/18/2026 pay date",
        body="Gross pay and net pay are in the register.",
        filename="payroll_register_2026-09-18.xlsx",
        sender="noreply@adp.com",
        extracted_text="Payroll register Gross pay Net pay Employer taxes",
    )
    assert result.document_type == DocumentType.PAYROLL


def test_legitimate_wire_is_not_fraud():
    result = classify_document(
        subject="Please wire $8,400.00 to Lakeside Tooling — invoice INV-3310",
        body=(
            "Please wire $8,400.00 to Lakeside Tooling for invoice INV-3310 today. "
            "Beneficiary bank is on file. This is an outgoing wire request for a scheduled payment, "
            "not a change of account."
        ),
        sender="ap@horizongoods.example",
    )
    assert result.document_type == DocumentType.WIRE_ACH_REQUEST
    assert "fraud_risk" not in result.flags


def test_newsletter_is_low():
    fields = ExtractedFields()
    result = classify_email(
        subject="This week in accounting: close shortcuts and FASB roundup",
        body="You are receiving this weekly roundup because you subscribed. Unsubscribe. View in browser.",
        sender="briefing@accountingtoday.example",
        outlook_importance="normal",
        attachments=[],
        fields=fields,
        as_of=AS_OF,
    )
    assert result.document_type == DocumentType.NEWSLETTER
    assert result.importance == Importance.LOW


def test_missing_attachment_flag():
    fields = extract_fields("Please see attached invoice INV-12 amount due $100.00", as_of=AS_OF)
    result = classify_email(
        subject="Invoice attached",
        body="Please see attached invoice INV-12 amount due $100.00",
        sender="ap@vendor.com",
        outlook_importance="normal",
        attachments=[],
        fields=fields,
        as_of=AS_OF,
        has_attachments=False,
    )
    assert "missing_attachment" in result.flags


def test_large_invoice_near_due_is_high():
    classified = classify_document(
        subject="Invoice",
        body="vendor invoice amount due $48,000.00 due September 23, 2026",
        filename="invoice.pdf",
        extracted_text="Invoice INV-9 amount due $48,000.00 due September 23, 2026",
    )
    fields = extract_fields(
        "Invoice INV-9 amount due $48,000.00 due September 23, 2026",
        as_of=AS_OF,
    )
    result = classify_email(
        subject="Invoice INV-9",
        body="Please enter. Amount due $48,000.00 due September 23, 2026",
        sender="billing@vendor.com",
        outlook_importance="normal",
        attachments=[classified],
        fields=fields,
        as_of=AS_OF,
        has_attachments=True,
        high_amount=10_000,
    )
    assert result.importance in {Importance.HIGH, Importance.CRITICAL}
    assert result.document_type == DocumentType.AP_INVOICE


def test_everyday_bank_change_wording_is_fraud():
    for body in (
        "We have changed our bank. Please update our remittance details before your next payment run.",
        "Kindly send the INV-8841 payment to the new account below.",
        "We switched banks last month; please update your records with our payment information.",
    ):
        result = classify_document(subject="Remittance update", body=body, sender="accounts@harb0r-logistics.co")
        assert result.document_type == DocumentType.PAYMENT_INSTRUCTION_CHANGE, body
        assert "fraud_risk" in result.flags


def test_bank_words_in_ordinary_mail_are_not_fraud():
    for body in (
        "We moved bank reconciliation to Friday this month.",
        "Please review the new account reconciliation before close.",
        "We have not changed our bank; the remit-to on the invoice is correct.",
    ):
        result = classify_document(subject="Update", body=body, sender="team@corp.example")
        assert "fraud_risk" not in result.flags, body


def _email(subject: str, body: str, *, duplicate: bool = False):
    return classify_email(
        subject=subject,
        body=body,
        sender="someone@corp.example",
        outlook_importance="normal",
        attachments=[],
        fields=extract_fields(f"{subject}\n{body}", as_of=AS_OF),
        as_of=AS_OF,
        has_attachments=False,
        duplicate_invoice=duplicate,
    )


def test_office_closed_is_not_month_end_work():
    result = _email("Office closed Monday Sep 28 for maintenance", "FYI: the office will be closed Monday.")
    assert "month_end" not in result.flags
    assert "month_end" in _email("September close checklist", "Please finish your close tasks.").flags


def test_duplicate_invoice_flag_only_on_invoices():
    mention = _email("Question", "Can you confirm when INV-8841 will be paid?", duplicate=True)
    assert "duplicate_invoice" not in mention.flags
    invoice = _email("Invoice INV-8841", "Vendor invoice INV-8841, amount due $1,200.00.", duplicate=True)
    assert "duplicate_invoice" in invoice.flags


def test_a_reply_is_filed_by_what_the_sender_wrote_not_the_thread_it_quotes():
    quoted = "\n\n-----Original Message-----\nFrom: Accounts <a@vendor.example>\n\nPlease pay invoice INV-7 from the new account. Amount due $4,000."
    reply = _email("RE: Northwind bank details update", "I called Northwind and nothing has changed. Ignore it." + quoted)
    assert reply.document_type != DocumentType.AP_INVOICE and "fraud_risk" not in reply.flags
    assert "Urgent timing language" not in _email("RE: bank", "Nothing changed." + quoted + " Pay today.").importance_reasons
    assert _email("RE: Invoice INV-7", "Approved, go ahead." + quoted).document_type == DocumentType.AP_INVOICE
    forward = _email("FW: see below", quoted.strip())
    assert forward.document_type == DocumentType.AP_INVOICE


def test_what_a_file_is_called_outweighs_one_word_in_its_pages():
    contract = classify_document(
        filename="Master Supply Agreement.pdf",
        extracted_text="Master Supply Agreement. 3. Payment: each invoice is payable within 30 days. 5. Termination.",
    )
    assert contract.document_type == DocumentType.CONTRACT
    assert classify_document(filename="Sep2026_close_calendar.xlsx", extracted_text="Close checklist").document_type != DocumentType.CONTRACT
    assert classify_document(filename="ChaseStmt_Sep.pdf", extracted_text="Summary").document_type == DocumentType.BANK_STATEMENT


def test_a_keyword_that_ends_a_sentence_still_counts():
    assert _email("Hello", "Hi, attached is our invoice.").document_type == DocumentType.AP_INVOICE
    assert _email("September", "Please see the attached remittance advice.").document_type == DocumentType.REMITTANCE_ADVICE
    assert _email("Docs", "Attached is the payroll register.").document_type == DocumentType.PAYROLL
    assert _email("Docs", "We received a notice from the IRS.").document_type == DocumentType.TAX_DOCUMENT
    # Still whole words: "irs" is not in "first." and an address such as irs.gov is not the word.
    assert _email("Docs", "Who was first.").document_type != DocumentType.TAX_DOCUMENT
    assert _email("Docs", "Copy sent to records@irs.gov for them").document_type != DocumentType.TAX_DOCUMENT


_INVOICE_TEXT = "INVOICE\nInvoice Number: INV-20931\nBill To: Our Co\nAmount Due: $4,250.00\nPayment Terms: Net 30\n"


def test_an_invoice_with_many_invoice_words_outweighs_one_stray_word():
    for line in ("Payment by wire transfer to: Beneficiary bank: Chase", "We accept checks, ACH credit or wire transfer.",
                 "Audit fees for the external audit of FY2025."):
        result = classify_document(subject="Invoice INV-20931", body="Please find attached our invoice.", filename="20931.pdf",
                                   extracted_text=_INVOICE_TEXT + line)
        assert result.document_type == DocumentType.AP_INVOICE, line
    # A remittance advice listing invoice numbers and amounts, and an auditor's list of invoice fields, keep their own type.
    remittance = classify_document(subject="Remittance advice", body="Please see attached remittance advice.", filename="pay_0921.pdf",
                                   extracted_text="REMITTANCE ADVICE\nInvoice Number  Invoice Date  Amount Due  Amount Paid\nINV-1001 1,200.00")
    assert remittance.document_type == DocumentType.REMITTANCE_ADVICE
    pbc = classify_document(subject="PBC list", body="Attached is the PBC list for the external audit.", filename="list.xlsx",
                            extracted_text="Invoice number | Amount due | Payment terms | Bill to")
    assert pbc.document_type == DocumentType.AUDIT_REQUEST


def test_a_domain_needle_matches_only_the_sender_domain():
    # "ey.com" and "chase.com" used to match anywhere in the address: surveymonkey.com and purchase.com.
    survey = classify_document(subject="We want your feedback", body="Take our 2 minute survey.", sender="member@surveymonkey.com")
    assert survey.document_type != DocumentType.AUDIT_REQUEST, survey.reasons
    shop = classify_document(subject="Your order", body="Thanks for shopping.", sender="orders@purchase.com")
    assert shop.document_type != DocumentType.BANK_STATEMENT, shop.reasons
    survey_mail = classify_email(subject="Quick survey", body="Tell us what you think.", sender="member@surveymonkey.com",
                                 outlook_importance="normal", attachments=[], fields=ExtractedFields(), as_of=AS_OF)
    assert "VIP / elevated sender" not in survey_mail.importance_reasons
    # The domain itself and its subdomains still match.
    auditor = classify_email(subject="Quick question", body="Tell us what you think.", sender="partner@uk.ey.com",
                             outlook_importance="normal", attachments=[], fields=ExtractedFields(), as_of=AS_OF)
    assert "VIP / elevated sender" in auditor.importance_reasons


def test_a_vip_entry_with_a_dot_in_the_name_still_matches():
    # Domain matching for "ey.com" made the VIP entry "maya.chen" look for a domain "maya.chen".
    kw = dict(subject="Quick question", body="Can we talk today?", outlook_importance="normal", attachments=[],
              fields=ExtractedFields(), as_of=AS_OF)
    assert "VIP / elevated sender" in classify_email(sender="maya.chen@taz.example", vip_senders=["maya.chen"], **kw).importance_reasons
    # An entry written "@taz.com" names a domain.
    assert "VIP / elevated sender" in classify_email(sender="bob@uk.taz.com", vip_senders=["@taz.com"], **kw).importance_reasons
    assert "VIP / elevated sender" not in classify_email(sender="bob@notaz.com", vip_senders=["@taz.com"], **kw).importance_reasons


def test_a_learned_category_is_scored_as_that_category():
    # A no-reply notification corrected to an AP invoice kept the notification's Low score and "Automated notification".
    from controller_inbox.classify import score_importance
    from controller_inbox.learn import apply_learned

    kw = dict(subject="New document available", body="A new document from VendorCo is available in the portal.",
              sender="noreply@billing.vendorco.com", outlook_importance="normal", fields=ExtractedFields(), as_of=AS_OF)
    classified = classify_email(attachments=[], fraud="none", **kw)
    assert classified.document_type == DocumentType.NOTIFICATION
    learned = apply_learned(classified, {"corrected_category": "ap_invoice", "reason": "These are vendor invoices"})
    assert learned.document_type == DocumentType.AP_INVOICE
    assert "Automated notification" not in learned.importance_reasons, learned.importance_reasons
    expected = score_importance(category=DocumentType.AP_INVOICE, flags=[], high_amount=10_000, vip_senders=[], **kw)
    assert (learned.importance, learned.importance_score) == expected[:2]


# A corrected message is not overwritten by a reading.
def test_corrected_message_refuses_a_model_reading(loaded, settings):
    record_correction(loaded, settings, email_id="demo-expense", corrected_category="internal_fyi", reason="Just FYI from HR")
    with pytest.raises(ReadingRefused):
        apply_bionic_reading(loaded, "demo-expense", {"category": "ap_invoice", "folder": "important"})
    assert loaded.get_email("demo-expense").category == DocumentType.INTERNAL_FYI


# Dates: blank or naive timestamps, and today vs. arrival day.
def test_received_day_handles_blank_and_naive_timestamps():
    fallback = date(2026, 1, 1)
    assert received_day("", fallback=fallback) == fallback
    assert received_day("garbage", fallback=fallback) == fallback
    from zoneinfo import ZoneInfo

    assert received_day("2026-09-30T02:00:00", ZoneInfo("America/New_York")) == date(2026, 9, 29), "naive is UTC"


def test_correction_survives_a_blank_received_at(loaded, settings):
    email = loaded.get_email("demo-expense")
    email.received_at = ""
    loaded.upsert_email(email)
    record_correction(loaded, settings, email_id="demo-expense", corrected_category="internal_fyi", reason="Just FYI")
    assert loaded.get_email("demo-expense").category == DocumentType.INTERNAL_FYI


# Subject-only corrections stay within the sender's company.
def test_subject_correction_does_not_cross_senders(loaded, settings):
    email = loaded.get_email("demo-expense")
    record_correction(loaded, settings, email_id=email.id, corrected_category="newsletter", reason="Bulk mail")
    domain = email.sender_email.split("@")[1]
    assert match_correction(loaded, sender_email="someone@else.example", subject=email.subject) is None
    assert match_correction(loaded, sender_email=f"colleague@{domain}", subject=email.subject) is not None
    assert match_correction(loaded, sender_email="", subject=email.subject) is None


NOW = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)


def _raw(id_, subject, body, *, sender="ap@northwind-supply.com", when=NOW, **kw):
    return RawMessage(
        id=id_, subject=subject, sender_name="Northwind AP", sender_email=sender, received_at=when,
        body_text=body, body_preview=body[:240], has_attachments=False, source="folder", **kw,
    )


def test_refiling_an_email_you_corrected_takes_another_emails_correction(store, settings):
    """pipeline._classify applies the *latest* correction for the sender (learn.match_correction), even to an email
    that carries its own correction. Correct email A to "AP invoice", later correct B from the same sender to
    "Newsletter": the next time A is filed again (teaching a field on it -> teach.refile -> rescore_stored, or a fraud
    recheck) A silently becomes a newsletter and its invoice task goes."""
    from controller_inbox import teach
    from controller_inbox.learn import record_correction
    from controller_inbox.models import DocumentType

    a = process_message(_raw("a1", "October statement of account", "Attached is your statement. Invoice INV-4410 $2,300.00 open."),
                        store, settings, now=NOW)
    b = process_message(_raw("b1", "Northwind news: fall catalog", "Our fall catalog is out.", when=NOW + timedelta(minutes=5)),
                        store, settings, now=NOW)
    record_correction(store, settings, email_id=a.id, corrected_category="ap_invoice", reason="this is an invoice to enter")
    record_correction(store, settings, email_id=b.id, corrected_category="newsletter", reason="marketing mail")
    assert store.get_email("a1").category == DocumentType.AP_INVOICE

    teach.refile(store, settings, store.get_email("a1"))
    assert store.get_email("a1").category == DocumentType.AP_INVOICE, store.get_email("a1").category


NOW_1PM = datetime(2026, 9, 22, 13, 0, tzinfo=timezone.utc)


def _age(path: Path) -> None:
    """Files written just now look like they are still being copied; date them a minute back."""
    old = time.time() - 60
    os.utime(path, (old, old))


# A close pack with a workbook is not an AP invoice --------------------------------------------------
def _invoice_eml(settings) -> Path:
    message = EmailMessage()
    message["From"] = "Acme AR <ar@acme-industrial.example>"
    message["To"] = "ap@horizongoods.example"
    message["Subject"] = "Invoice INV-88123 from Acme Industrial Supply"
    message["Date"] = format_datetime(NOW_1PM)
    message["Message-ID"] = "<acme-inv-1@acme-industrial.example>"
    message.set_content("Hello,\n\nPlease find attached invoice INV-88123 for PO-4410. Total due $4,319.50 by 11/11/2026.\n\nThanks,\nAcme AR")
    path = settings.inbox_incoming / "acme-inv-1.eml"
    path.write_bytes(bytes(message))
    _age(path)
    return path


def _close_pack(settings) -> Path:
    book = Workbook()
    summary = book.active
    summary.title = "Summary"
    summary.append(["Account", "Sep", "Oct", "Var"])
    summary.append(["Revenue", 120000, 131000, "=C2-B2"])
    detail = book.create_sheet("Detail")
    detail.append(["Vendor", "Invoice", "Amount"])
    detail.append(["Acme", "INV-88123", 4319.5])
    detail.append(["Northwind", "INV-10482", 12850])
    data = io.BytesIO()
    book.save(data)
    csv = b"date,vendor,amount\n2026-09-01,Acme,4319.50\n2026-09-03,Northwind,12850.00\n"
    path = write_msg(
        settings.inbox_incoming / "maya-close.msg",
        "September close pack",
        "Hi,\n\nAttached are the close workbook, the AP export and a receipt photo.\n\nMaya",
        attachments=[("Close workbook.xlsx", data.getvalue(), XLSX), ("AP export.csv", csv, "text/csv")],
        sent=NOW_1PM + timedelta(minutes=10),
    )
    _age(path)
    return path


def test_a_close_pack_workbook_listing_an_invoice_is_not_filed_as_that_invoice(store, settings):
    settings.ensure_data_dir()
    _invoice_eml(settings)
    _close_pack(settings)
    records = {record.subject: record for record in ingest_folder(store, settings, now=NOW_1PM)}
    pack = store.get_email(records["September close pack"].id)
    invoice = store.get_email(records["Invoice INV-88123 from Acme Industrial Supply"].id)
    assert pack.category not in {DocumentType.AP_INVOICE, DocumentType.AR_INVOICE}
    assert "INV-88123" not in pack.extracted.invoice_numbers
    assert "duplicate_invoice" not in pack.flags and not any("duplicate" in a.title.lower() for a in pack.actions)
    # The real invoice is not taken for a repeat of the workbook's row either.
    assert store.find_duplicate_invoices("INV-88123", invoice.id) == []
    # The workbook's text is still there to search and ask about.
    workbook = next(att for att in pack.attachments if att.filename == "Close workbook.xlsx")
    assert "INV-88123" in workbook.extracted_text


def test_a_workbook_named_as_an_invoice_is_still_an_invoice(store, settings):
    from controller_inbox.classify import classify_document

    named = classify_document(subject="Files", body="See attached.", filename="Invoice 4410.xlsx",
                              extracted_text="Invoice INV-4410 amount due 1,200.00", content_type=XLSX)
    said = classify_document(subject="Invoice INV-4410", body="Our invoice is attached.", filename="book.xlsx",
                             extracted_text="Invoice INV-4410 amount due 1,200.00", content_type=XLSX)
    assert named.document_type == DocumentType.AP_INVOICE and said.document_type == DocumentType.AP_INVOICE
