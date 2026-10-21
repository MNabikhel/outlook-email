"""Tasks made from an email: their titles, priority and due dates, and what happens to them on a correction."""

from __future__ import annotations

from datetime import date, datetime, timezone

from controller_inbox.actions import extract_actions
from controller_inbox.extract import extract_fields
from controller_inbox.learn import record_correction
from controller_inbox.models import DocumentType, ExtractedFields, Importance, RawMessage
from controller_inbox.pipeline import ingest_demo, process_message

# A category correction keeps a snoozed task only where the corrected category still asks for it: a mis-read
# "invoice" corrected to a Newsletter (no tasks) loses its snoozed pay/enter task.


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


# Guards against: Correcting an email's category rebuilds its tasks from scratch: a task the user snoozed comes back
# open (upsert_email only spares tasks marked done), unlike a re-read or a fraud refile (_keep_task_status /
# rescore_stored).
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


# "Oct." in a date does not end the sentence, so each task keeps its own due date.
def test_abbreviated_month_keeps_each_tasks_own_due_date():
    body = "Please approve the PO by Oct. 9, 2026. Please send the signed W-9 by Oct. 30, 2026."
    items = extract_actions(
        email_id="e", subject="PO 7781", body=body, category=DocumentType.OTHER, importance=Importance.MEDIUM,
        fields=extract_fields(body, as_of=date(2026, 10, 8)), flags=[], as_of=date(2026, 10, 8),
    )
    due = {item.title: item.due_date for item in items}
    assert due == {"Please approve the PO by Oct. 9, 2026.": "2026-10-09", "Please send the signed W-9 by Oct. 30, 2026.": "2026-10-30"}


# Task titles don't say a word twice ------------------------------------------------------------------
def test_task_titles_named_after_the_subject_do_not_repeat_the_kind_of_task(store, settings, as_of_now):
    ingest_demo(store, settings, now=as_of_now)
    titles = [item.title for email in store.list_emails(limit=100) for item in email.actions]
    assert titles
    for title in titles:
        words = title.lower().replace(":", " ").split()
        repeats = {word for a, word in zip(words, words[1:]) if a == word}
        assert not repeats, title
    assert any(title.startswith("Code and enter") and "Harbor Packaging" in title for title in titles)
    assert not any("invoice Invoice" in title or "remittance Payment remittance" in title for title in titles)


# --- A Critical invoice due within a week was lowered to High -----------------------------------------------------
_ORDER = [Importance.LOW, Importance.MEDIUM, Importance.HIGH, Importance.CRITICAL]


def _invoice_task(due: str, importance: Importance = Importance.CRITICAL):
    fields = ExtractedFields(invoice_numbers=["INV-1"], amounts=[250000.0], due_dates=[due])
    (task,) = extract_actions(
        email_id="e1", subject="Invoice INV-1", body="", category=DocumentType.AP_INVOICE,
        importance=importance, fields=fields, flags=[], as_of=date(2026, 10, 8),
    )
    return task


def test_a_closer_due_date_never_lowers_the_task_priority():
    for importance in (Importance.CRITICAL, Importance.HIGH):
        later = _invoice_task("2026-11-08", importance).priority
        for due in ("2026-10-09", "2026-10-13"):
            soon = _invoice_task(due, importance).priority
            assert _ORDER.index(soon) >= _ORDER.index(later), (importance, due, soon, later)
    assert _invoice_task("2026-10-09", Importance.CRITICAL).priority == Importance.CRITICAL
