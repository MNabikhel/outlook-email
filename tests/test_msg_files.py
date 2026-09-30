"""Real Outlook .msg files dropped in the folder: senders, forwarded emails, and every attachment."""

from __future__ import annotations

import io

from openpyxl import Workbook

from controller_inbox.demo import make_pdf
from controller_inbox.folder_mail import ingest_folder
from msgfactory import PDF, XLSX, build_message, write_msg


def _xlsx(rows) -> bytes:
    book = Workbook()
    for row in rows:
        book.active.append(row)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def test_a_colleague_on_exchange_is_read_with_their_real_address(store, settings):
    write_msg(
        settings.inbox_incoming / "budget.msg",
        "Q4 budget draft",
        "Can you look over the draft before Friday?",
        sender_name="Maya Chen",
        sender_email="maya@taz.com",
        exchange=True,
        attachments=[("Q4 budget.xlsx", _xlsx([["Line", "Q4"], ["Ads", 1500]]), XLSX)],
    )
    (record,) = ingest_folder(store, settings)
    assert (record.sender_name, record.sender_email) == ("Maya Chen", "maya@taz.com")
    (attachment,) = record.attachments
    assert attachment.filename == "Q4 budget.xlsx"
    assert "A2: Ads | B2: 1,500" in attachment.extracted_text


def test_a_forwarded_email_brings_its_own_attachments(store, settings):
    quote = build_message(
        "Acme quote",
        "Our quote is attached. Valid for 30 days.",
        sender_name="Acme Sales",
        sender_email="sales@acme.com",
        attachments=[
            ("quote.pdf", make_pdf([["Acme quote Q-881", ["Item", "Amount"], ["Support plan", "$9,600.00"]]]), PDF),
        ],
    )
    write_msg(
        settings.inbox_incoming / "fw quote.msg",
        "FW: Acme quote",
        "Is this in line with last year?",
        sender_name="Priya Raman",
        sender_email="priya@taz.com",
        forwarded=[("Acme quote.msg", quote)],
        logo=b"\x89PNG\r\n\x1a\n" + b"0" * 800,
        html='<p>Is this in line with last year?</p><img src="cid:image001.png@01DB">',
    )
    (record,) = ingest_folder(store, settings)
    files = {att.filename: att.extracted_text for att in record.attachments}
    assert set(files) == {"Acme quote.txt", "Acme quote › quote.pdf"}, "the signature logo is not an attachment"
    assert "From: Acme Sales <sales@acme.com>" in files["Acme quote.txt"]
    assert "Attachments: quote.pdf" in files["Acme quote.txt"]
    assert "Date: Mon, 28 Sep 2026 14:30:00" in files["Acme quote.txt"]
    assert "Support plan | $9,600.00" in files["Acme quote › quote.pdf"]
    saved = {path.name for path in (settings.inbox_extracted / record.id).iterdir()}
    assert saved == {"Acme quote.txt", "Acme quote › quote.pdf"}


def test_reply_to_is_kept_for_the_fraud_check(store, settings):
    write_msg(
        settings.inbox_incoming / "billing.msg",
        "Invoice 7781",
        "Invoice attached.",
        sender_name="Northwind Billing",
        sender_email="billing@northwind.com",
        reply_to="Accounts <accounts@northwind-payments.co>",
    )
    (record,) = ingest_folder(store, settings)
    assert record.reply_to == "accounts@northwind-payments.co"
    assert store.get_email(record.id).reply_to == "accounts@northwind-payments.co"


def test_long_attachments_are_kept_whole(store, settings):
    rows = [[f"Line {n}", n] for n in range(1, 900)]
    write_msg(
        settings.inbox_incoming / "ledger.msg",
        "Ledger export",
        "Full ledger attached.",
        attachments=[("ledger.xlsx", _xlsx(rows), XLSX)],
    )
    (record,) = ingest_folder(store, settings)
    text = store.get_email(record.id).attachments[0].extracted_text
    assert len(text) > 20_000 and "A899: Line 899" in text
