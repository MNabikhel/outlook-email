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
