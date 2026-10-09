"""A date after the word "invoice" ("Your invoice - 2026-10-01", "Invoice 2026-10") is taken as the invoice
number. Two subscription vendors billing on the same day then match each other in the duplicate check (which
compares numbers across all senders), and the second real invoice is flagged "possible duplicate"."""
from datetime import date, datetime, timezone

from controller_inbox.extract import extract_fields
from controller_inbox.models import RawMessage
from controller_inbox.pipeline import process_message


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
