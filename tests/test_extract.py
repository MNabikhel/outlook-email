from datetime import date

from controller_inbox.demo import _pdf
from controller_inbox.extract import (
    extract_fields,
    extract_text_from_bytes,
    redact_financial_secrets,
)


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
    from controller_inbox.config import Settings
    from controller_inbox.models import RawMessage
    from controller_inbox.pipeline import process_message
    from controller_inbox.store import Store
    from datetime import datetime, timezone

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
