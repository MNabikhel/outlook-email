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
