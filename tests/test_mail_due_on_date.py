"""'Payment is due on October 12, 2026' (the most common way to write it) gives the invoice no due date."""
from datetime import datetime, timezone

from controller_inbox.models import RawMessage
from controller_inbox.pipeline import process_message


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
