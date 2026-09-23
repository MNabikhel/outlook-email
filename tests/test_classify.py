from datetime import date

from controller_inbox.classify import classify_document, classify_email
from controller_inbox.extract import extract_fields
from controller_inbox.models import DocumentType, ExtractedFields, Importance


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
