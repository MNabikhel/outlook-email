"""Fix 4796099 (learn.record_correction -> _keep_task_status) keeps every snoozed task through a category
correction, even one the corrected category doesn't ask for. Correcting a mis-read "invoice" to a Newsletter
(no tasks) used to clear its tasks; now the snoozed "pay/enter invoice" task stays and comes back when its
snooze ends, on an email the user said is not an invoice."""
from datetime import datetime, timezone

from controller_inbox.learn import record_correction
from controller_inbox.models import RawMessage
from controller_inbox.pipeline import process_message


def test_snoozed_invoice_task_goes_when_corrected_to_newsletter(store, settings):
    now = datetime(2026, 10, 9, 13, 0, tzinfo=timezone.utc)
    raw = RawMessage(
        id="m-news",
        subject="Invoice INV-55120 from Northwind",
        sender_name="Northwind AR",
        sender_email="ar@northwind.com",
        received_at=now,
        body_text="Please find invoice INV-55120 for $3,200.00. Payment due: October 30, 2026.",
        body_preview="",
        has_attachments=False,
        source="folder",
    )
    email = process_message(raw, store, settings, now=now)
    assert email.actions, "the invoice should get a task"
    for task in email.actions:
        assert store.set_action_status(task.id, "snoozed")

    record_correction(store, settings, email_id=email.id, corrected_category="newsletter",
                      reason="This is Northwind's marketing digest, not a bill")

    after = store.get_email(email.id)
    assert [a for a in after.actions if a.status.value == "snoozed"] == [], [(a.title, a.status.value) for a in after.actions]
