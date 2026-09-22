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
