"""Mail data stays whole: a message is filed once however it arrives, a failure after an email is stored doesn't
report it as unreadable, a refile keeps what is new and what the user did, and removing mail removes all of it."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from controller_inbox import cost_codes, folder_mail, pipeline
from controller_inbox.fraud import record_fraud_verdict
from controller_inbox.models import RawAttachment, RawMessage
from controller_inbox.pipeline import ingest_demo, process_message, rescore_stored
from msgfactory import write_msg
from test_outlook_sync import NOW, FakeOutlook, sync


class NoHeaderOutlook(FakeOutlook):
    """Outlook whose saved copies carry no Internet headers: no Message-ID (an Exchange colleague's mail)."""

    def save_copy(self, entry_id, store_id, dest):
        m = self.messages[entry_id]
        self.copies += 1
        write_msg(dest, m["subject"], "Hello, see below.", sender_email=m["sender"], sender_name="Northwind AP",
                  sent=m["received"], exchange=True)


def _raw(email_id="m1", subject="September bank statement", **kwargs) -> RawMessage:
    return RawMessage(
        id=email_id,
        subject=subject,
        sender_name="First Bank",
        sender_email="statements@firstbank.com",
        received_at=kwargs.pop("received_at", datetime(2026, 9, 22, 12, tzinfo=timezone.utc)),
        body_text="Your September bank statement for account ending 1234 is attached. Please reconcile.",
        body_preview="",
        has_attachments=False,
        source=kwargs.pop("source", "folder"),
        **kwargs,
    )


# Filed once, however it arrives ----------------------------------------------------------------------------


def test_outlook_message_without_a_subject_is_stored_without_one(store, settings):
    """A message from Outlook that has no subject is stored with an empty subject, not its copy's file name."""
    outlook = FakeOutlook()
    outlook.add("e1", "")
    report = sync(store, settings, outlook)
    assert report["new"] == 1, report
    [email] = store.list_emails(limit=10)
    assert not re.fullmatch(r"[0-9a-f]{24}", email.subject), email.subject
    assert email.subject == ""


def test_outlook_message_without_subject_or_message_id_in_two_folders_is_one_email(store, settings):
    """A message with no subject and no Message-ID, seen in two synced folders, is filed once."""
    outlook = NoHeaderOutlook()
    outlook.add("e1", "", message_id="")
    outlook.add("e2", "", message_id="", folder="Inbox/Vendors")
    report = sync(store, settings, outlook)
    assert (report["new"], report["already"]) == (1, 1), report
    assert len(store.list_emails(limit=10)) == 1


def test_outlook_message_reusing_a_message_id_without_a_subject_is_one_email(store, settings):
    """A subjectless Outlook message that reuses another email's Message-ID gets one id, in every synced folder."""
    settings.ensure_data_dir()
    write_msg(settings.inbox_incoming / "scan.msg", "Scan 1", "Scanned.", sender_email="scanner@taz.com",
              message_id="<scan@taz.com>", sent=NOW - timedelta(days=2))
    folder_mail.ingest_folder(store, settings, now=NOW)
    outlook = FakeOutlook()
    outlook.add("e2", "", message_id="<scan@taz.com>")
    outlook.add("e3", "", message_id="<scan@taz.com>", folder="Inbox/Vendors")
    report = sync(store, settings, outlook)
    assert (report["new"], report["already"]) == (1, 1), report
    assert len(store.list_emails(limit=10)) == 2


def test_outlook_message_already_read_from_microsoft_365_is_not_filed_again(store, settings):
    """A message already stored from Microsoft 365 is recognised by its Message-ID when Outlook sync meets it."""
    graph = RawMessage(
        id="AAMkAD-graph-1",
        subject="Invoice INV-7741 from Northwind",
        sender_name="Northwind AP",
        sender_email="ap@northwind.com",
        received_at=NOW,
        body_text="Hello,\n\nInvoice INV-7741 from Northwind. Thanks.",
        body_preview="",
        has_attachments=False,
        internet_message_id="<e1@northwind.com>",
        source="graph",
    )
    process_message(graph, store, settings, now=NOW)
    outlook = FakeOutlook()
    outlook.add("e1", "Invoice INV-7741 from Northwind")
    report = sync(store, settings, outlook)
    assert (report["new"], report["already"]) == (0, 1), report
    assert [e.id for e in store.list_emails(limit=10)] == ["AAMkAD-graph-1"]
    assert not store.outlook_items()["e1"]["owned"], "a deletion in Outlook doesn't remove the Microsoft 365 email"


# A failure after the email is stored -------------------------------------------------------------------------


def _drop(settings, name="invoice.msg"):
    settings.ensure_data_dir()
    path = write_msg(settings.inbox_incoming / name, "Invoice INV-9 from Acme", "Please pay invoice INV-9.",
                     sender_email="ap@acme.com", sent=NOW)
    old = (NOW - timedelta(minutes=5)).timestamp()
    os.utime(path, (old, old))
    return path


def test_drop_folder_reports_a_stored_email_as_read_when_cost_codes_fail(store, settings, monkeypatch, caplog):
    """An email stored before a later step (cost codes) fails is reported read and archived, and the failure logged."""
    _drop(settings)

    def broken(*args, **kwargs):
        raise RuntimeError("workbook locked")

    monkeypatch.setattr(cost_codes, "refresh", broken)
    report: dict = {}
    with caplog.at_level(logging.WARNING):
        folder_mail.ingest_folder(store, settings, now=NOW, report=report)
    assert (report["read"], report["failed"]) == (1, []), report
    assert len(store.list_emails(limit=10)) == 1
    assert not list(settings.inbox_failed.glob("*.msg")) and list(settings.inbox_processed.rglob("*.msg"))
    assert any(rec.exc_info for rec in caplog.records if rec.name.startswith("controller_inbox"))


def test_drop_folder_reports_a_stored_email_as_read_when_archiving_fails(store, settings, monkeypatch, caplog):
    """An email stored before its file can be archived is reported read, not moved to failed, and the failure logged."""
    _drop(settings)

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(folder_mail, "_archive", broken)
    report: dict = {}
    with caplog.at_level(logging.WARNING):
        folder_mail.ingest_folder(store, settings, now=NOW, report=report)
    assert (report["read"], report["failed"]) == (1, []), report
    assert len(store.list_emails(limit=10)) == 1
    assert not list(settings.inbox_failed.glob("*.msg"))
    assert any(rec.exc_info for rec in caplog.records if rec.name.startswith("controller_inbox"))


def test_drop_folder_logs_why_a_file_could_not_be_read(store, settings, caplog):
    """A file that can't be read is reported failed and its error logged with the traceback."""
    settings.ensure_data_dir()
    bad = settings.inbox_incoming / "broken.msg"
    bad.write_bytes(b"not an Outlook message")
    old = (NOW - timedelta(minutes=5)).timestamp()
    os.utime(bad, (old, old))
    report: dict = {}
    with caplog.at_level(logging.WARNING):
        folder_mail.ingest_folder(store, settings, now=NOW, report=report)
    assert [row["file"] for row in report["failed"]] == ["broken.msg"]
    assert any(rec.exc_info for rec in caplog.records if rec.name.startswith("controller_inbox"))


# A refile keeps what is new and what the user did --------------------------------------------------------


def test_refile_after_a_fraud_change_takes_the_new_due_date_and_keeps_the_status(store, settings):
    """A refiled email's tasks take their new due dates and creation time while keeping their id and status."""
    first = process_message(_raw(), store, settings, now=datetime(2026, 9, 22, 12, tzinfo=timezone.utc))
    task = next(item for item in first.actions if item.title.startswith("Request the missing attachment"))
    store.set_action_status(task.id, "snoozed")
    later = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
    email = rescore_stored(store, settings, store.get_email("m1"), SimpleNamespace(level="high", flags=[]), now=later)
    kept = next(item for item in email.actions if item.id == task.id)
    assert kept.status.value == "snoozed"
    assert kept.due_date == "2026-10-21", "the due date is read again against today"
    verify = next(item for item in email.actions if item.source == "fraud_rule")
    assert verify.created_at.startswith("2026-10-21"), "a new task is dated now"


# Writes that land together -----------------------------------------------------------------------------------


def _attachment() -> RawAttachment:
    data = b"Statement for account ending 1234. Closing balance $12,000.00"
    return RawAttachment(id="statement.txt", filename="statement.txt", content_type="text/plain", size_bytes=len(data), content=data)


def test_fraud_verdict_given_while_a_copy_is_read_is_kept(store, settings, monkeypatch):
    """A fraud verdict given while another copy of the email is being read stays on the email."""
    process_message(_raw(), store, settings, now=NOW)
    read_file = pipeline._file_record

    def slow_read(*args, **kwargs):
        record_fraud_verdict(store, settings, "m1", verdict="safe", now=NOW)
        return read_file(*args, **kwargs)

    monkeypatch.setattr(pipeline, "_file_record", slow_read)
    email = process_message(_raw(attachments=[_attachment()]), store, settings, now=NOW)
    assert "fraud_cleared" in email.flags
    assert "fraud_cleared" in store.get_email("m1").flags
    assert [a.filename for a in email.attachments] == ["statement.txt"]


def test_task_snoozed_while_a_copy_is_read_stays_snoozed(store, settings, monkeypatch):
    """A task the user snoozes while another copy of the email is being read stays snoozed."""
    first = process_message(_raw(), store, settings, now=NOW)
    task = next(item for item in first.actions if item.source == "body")
    read_file = pipeline._file_record

    def slow_read(*args, **kwargs):
        store.set_action_status(task.id, "snoozed")
        return read_file(*args, **kwargs)

    monkeypatch.setattr(pipeline, "_file_record", slow_read)
    email = process_message(_raw(attachments=[_attachment()]), store, settings, now=NOW)
    assert next(item for item in email.actions if item.id == task.id).status.value == "snoozed"


def test_outlook_removal_that_fails_keeps_following_the_message(store, settings, monkeypatch):
    """A copy that can't be removed after a deletion in Outlook is still followed, so the next sync removes it."""
    outlook = FakeOutlook()
    outlook.add("e1", "Invoice INV-7741 from Northwind")
    sync(store, settings, outlook)
    del outlook.messages["e1"]

    def broken(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(store, "delete_emails", broken)
    try:
        sync(store, settings, outlook, now=NOW + timedelta(minutes=15))
    except RuntimeError:
        pass
    assert "e1" in store.outlook_items()
    monkeypatch.undo()
    report = sync(store, settings, outlook, now=NOW + timedelta(minutes=30))
    assert report["removed"] == 1 and not store.list_emails(limit=10)


# Removing mail removes all of it -------------------------------------------------------------------------


def test_clearing_the_sample_removes_everything_about_its_emails(store, settings, as_of_now):
    """Clearing the sample leaves no row in any table pointing at a sample email."""
    ingest_demo(store, settings, now=as_of_now)
    sample = store.list_emails(limit=1)[0]
    store.save_outlook_item({"entry_id": "x1", "email_id": sample.id, "folder": "Inbox", "message_id": "<x@y>"})
    assert store.clear_sample()
    assert store.outlook_items() == {}
