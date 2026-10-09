"""Correcting an email's category rebuilds its tasks from scratch: a task the user snoozed comes back open
(upsert_email only spares tasks marked done), unlike a re-read or a fraud refile (_keep_task_status / rescore_stored)."""
from datetime import datetime, timezone

from controller_inbox.learn import record_correction
from controller_inbox.models import RawMessage
from controller_inbox.pipeline import process_message


def test_snoozed_task_stays_snoozed_after_correction(store, settings):
    now = datetime(2026, 10, 9, 13, 0, tzinfo=timezone.utc)
    raw = RawMessage(
        id="m-snooze",
        subject="Vendor setup",
        sender_name="Priya Raman",
        sender_email="priya@taz.com",
        received_at=now,
        body_text="Hi,\n\nPlease review the new vendor form for Northwind before we add them.\n\nThanks",
        body_preview="",
        has_attachments=False,
        source="folder",
    )
    email = process_message(raw, store, settings, now=now)
    task = next(a for a in email.actions if a.source == "body")
    assert store.set_action_status(task.id, "snoozed")

    record_correction(store, settings, email_id=email.id, corrected_category="approval_request",
                      reason="Priya needs a sign-off, not just a review")

    after = store.get_email(email.id)
    same = [a for a in after.actions if a.title == task.title]
    assert same and all(a.status.value == "snoozed" for a in same), [(a.title, a.status.value) for a in after.actions]
