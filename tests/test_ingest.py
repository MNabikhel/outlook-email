"""Mail coming in: the drop folder, Microsoft 365 (Graph), the sample mailbox, one email per message
however often it arrives, and what the user did kept through a re-read."""

from __future__ import annotations

import base64
import io
import logging
import os
import time
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from controller_inbox import cost_codes, folder_mail, pipeline, web
from controller_inbox.config import Settings
from controller_inbox.folder_mail import collect_messages, forwarded_attachments, ingest_folder, reread_attachments
from controller_inbox.fraud import record_fraud_verdict
from controller_inbox.graph import GraphMailbox
from controller_inbox.models import RawAttachment, RawMessage
from controller_inbox.overnight import run_overnight
from controller_inbox.pipeline import ingest_demo, ingest_mailbox, process_message, rescore_stored
from controller_inbox.store import Store
from msgfactory import PDF, build_message, write_msg


class FakeGraph:
    def __init__(self, messages):
        self.messages = messages

    def list_messages(self, received_after=None):
        yield from self.messages

    def get_attachments(self, message_id):
        return []

    def apply_categories(self, message_id, categories, flag):
        return "skipped"


# Guards against: The sample mailbox is cleared when the first real message arrives through the drop folder, but not
# when it arrives through Outlook (Graph). The sample's made-up fraud alerts and invoices then sit beside real mail
# for good: real_mail_count() is no longer 0, so nothing ever clears them.
def test_first_outlook_sync_replaces_sample(store, settings, as_of_now):
    ingest_demo(store, settings, now=as_of_now)
    assert store.counts()["emails"] > 0 and store.real_mail_count() == 0
    now = datetime(2026, 10, 9, 13, 0, tzinfo=timezone.utc)
    real = RawMessage(
        id="AAMkAGI2-real-1",
        subject="Q3 accruals",
        sender_name="Maya Chen",
        sender_email="maya@taz.com",
        received_at=now,
        body_text="Accrual schedule is in the shared folder.",
        body_preview="",
        has_attachments=False,
        source="graph",
    )
    ingest_mailbox(FakeGraph([real]), store, settings, received_after=None, now=now)
    sources = {e.source for e in store.list_emails(limit=-1)}
    assert sources == {"graph"}, sources


def _invoice(graph_id):
    return RawMessage(
        id=graph_id,
        subject="Invoice INV-55120 from Northwind",
        sender_name="Northwind AR",
        sender_email="ar@northwind.com",
        received_at=datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc),
        body_text="Please find invoice INV-55120 for $3,200.00. Payment due: October 30, 2026.",
        body_preview="",
        has_attachments=False,
        internet_message_id="<CAF1234.northwind@mail.northwind.com>",
        conversation_id="AAQkAGI2conv",
        source="graph",
    )


# Guards against: Graph gives a message a new id when the user moves it to another folder (the default REST id is
# not immutable). CloseDesk lists /me/messages (every folder) and keys mail by that id only, never by
# internetMessageId, so a `sync` (which re-lists the last 72 hours) after the user filed an invoice reads it again
# as a new email: two copies on the board, and the invoice flagged a "possible duplicate" of itself with a review
# task.
def test_moved_message_is_not_a_second_email(store, settings):
    now = datetime(2026, 10, 9, 13, 0, tzinfo=timezone.utc)
    ingest_mailbox(FakeGraph([_invoice("AAMkAGI2-inbox-id")]), store, settings, now=now)
    # The user files it in Outlook (new id), then runs `sync`, which re-lists the last 72 hours.
    later = now + timedelta(hours=2)
    ingest_mailbox(FakeGraph([_invoice("AAMkAGI2-vendors-folder-id")]), store, settings,
                   received_after=later - timedelta(hours=72), now=later)
    emails = store.list_emails(limit=-1)
    assert len(emails) == 1, [(e.id, e.flags) for e in emails]


class WritebackGraph:
    """Keeps what CloseDesk wrote to each message, as Outlook would."""

    def __init__(self, message):
        self.message = message
        self.importance = message.outlook_importance

    def list_messages(self, received_after=None):
        yield replace(self.message, outlook_importance=self.importance)

    def get_attachments(self, message_id):
        return []

    def apply_categories(self, message_id, categories, flag):
        if flag:
            self.importance = "high"  # GraphMailbox.apply_categories sends {"importance": "high"}
        return "written"


# Guards against: With Outlook writeback on, CloseDesk PATCHes importance=high onto every message it files as
# High/Critical. When that message is read again (a `sync` re-lists the last 72 hours), Graph returns importance
# "high", which CloseDesk then scores as "Sender marked the message as high importance in Outlook" (+10): its own
# write is read back as the sender's signal and the email's score and level climb on every re-read.
def test_own_writeback_is_not_read_as_sender_importance(store, settings):
    settings.writeback = True
    now = datetime(2026, 10, 9, 13, 0, tzinfo=timezone.utc)
    raw = RawMessage(
        id="AAMk-approval",
        subject="Approval needed: PO 4500123 for Northwind",
        sender_name="Priya Raman",
        sender_email="priya@taz.com",
        received_at=now - timedelta(hours=1),
        body_text="Hi, please approve PO 4500123 for $18,400.00 by Friday. Thanks",
        body_preview="",
        has_attachments=False,
        outlook_importance="normal",
        source="graph",
    )
    mailbox = WritebackGraph(raw)
    first = ingest_mailbox(mailbox, store, settings, now=now)[0]
    assert first.importance.value in {"high", "critical"}  # so CloseDesk flagged it in Outlook
    assert mailbox.importance == "high"
    again = ingest_mailbox(mailbox, store, settings, received_after=now - timedelta(hours=72), now=now + timedelta(minutes=5))[0]
    assert again.importance == first.importance, (first.importance_score, again.importance_score, again.importance_reasons)
    assert again.importance_score == first.importance_score, again.importance_reasons
    assert not any("high importance in Outlook" in r for r in again.importance_reasons)


NOW = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)


def _raw(id_, subject, body, *, sender="ap@northwind-supply.com", when=NOW, **kw):
    return RawMessage(
        id=id_, subject=subject, sender_name="Northwind AP", sender_email=sender, received_at=when,
        body_text=body, body_preview=body[:240], has_attachments=False, source="folder", **kw,
    )


class _Graph:
    """Microsoft 365 as ingest_mailbox reads it."""

    def __init__(self, messages):
        self.messages = messages

    def list_messages(self, received_after=None):
        return list(self.messages)

    def get_attachments(self, message_id):
        return []

    def apply_categories(self, message_id, categories, flag):
        return "skipped"


def test_same_mail_from_microsoft_365_and_drop_folder_is_stored_twice(store, settings):
    """ingest_mailbox only skips a message whose Message-ID is stored under the *same* source, and the drop folder
    never looks at Graph mail: a message synced from Microsoft 365 and then saved into the drop folder (to add its
    file, or by a colleague) becomes a second email with a second set of tasks, and its own invoice is flagged as a
    duplicate of itself."""
    from controller_inbox.folder_mail import ingest_folder
    from controller_inbox.pipeline import ingest_mailbox
    from msgfactory import write_msg

    body = "Hello, invoice INV-55120 for $4,210.00 is due November 15, 2026. Please process payment."
    graph = _raw("AAMkAGI2-graph-id", "Invoice INV-55120", body, internet_message_id="<inv-55120@northwind-supply.com>")
    graph.source = "graph"
    ingest_mailbox(_Graph([graph]), store, settings, now=NOW)
    settings.ensure_data_dir()
    write_msg(settings.inbox_incoming / "Invoice INV-55120.msg", "Invoice INV-55120", body,
              sender_name="Northwind AP", sender_email="ap@northwind-supply.com", sent=NOW,
              message_id="<inv-55120@northwind-supply.com>")
    ingest_folder(store, settings, now=NOW)

    stored = store.list_emails(limit=50)
    assert len(stored) == 1, [(e.id, e.source, e.flags) for e in stored]


def test_invoice_task_marked_done_comes_back_open_when_its_pdf_arrives(store, settings):
    """pipeline._keep_task_status matches the stored tasks to the new ones by exact title, and an invoice task's title
    carries the invoice number and amount. Enter an invoice and tick its task, then drop the email again with the
    PDF that was missing: the task comes back as a new open "Code and enter invoice INV-7741 ($4,210.00)" beside the
    done one (teach.refile already pairs tasks by kind for this reason; a re-read copy doesn't)."""
    from controller_inbox.demo import make_pdf
    from controller_inbox.models import ActionStatus, DocumentType

    body = "Hello, please find our invoice for September attached. Payment terms net 30."
    first = process_message(_raw("inv1", "Northwind invoice for September", body), store, settings, now=NOW)
    assert first.category == DocumentType.AP_INVOICE, first.category
    task = next(a for a in first.actions if a.source == "invoice")
    store.set_action_status(task.id, "done")

    again = _raw("inv1", "Northwind invoice for September", body)
    pdf = make_pdf([["Northwind Supply", "Invoice INV-7741", "Total due $4,210.00"]])
    again.attachments = [RawAttachment("INV-7741.pdf", "INV-7741.pdf", "application/pdf", len(pdf), pdf)]
    again.has_attachments = True
    process_message(again, store, settings, now=NOW)

    invoice_tasks = [(a.title, a.status) for a in store.get_email("inv1").actions if a.source == "invoice"]
    assert not any(status == ActionStatus.OPEN for _, status in invoice_tasks), invoice_tasks


def test_first_short_manual_sync_moves_the_cursor_past_mail_it_never_read(store, settings):
    """``closedesk sync --hours 1`` before any scheduled sync: ingest_mailbox treats the window as "covered" because
    there is no cursor yet, and sets the cursor to now. The overnight run then resumes from that cursor instead of
    looking back ``lookback_hours`` (72), so mail from 1 to 72 hours ago is never read."""
    from controller_inbox.pipeline import SYNC_CURSOR, ingest_mailbox

    class Windowed(_Graph):
        def list_messages(self, received_after=None):
            return [m for m in self.messages if received_after is None or m.received_at >= received_after]

    older = _raw("AAMk-older", "Invoice INV-90210", "Invoice INV-90210 for $1,200.00 attached.", when=NOW - timedelta(hours=5))
    older.source = "graph"
    mailbox = Windowed([older])
    ingest_mailbox(mailbox, store, settings, received_after=NOW - timedelta(hours=1), now=NOW)  # sync --hours 1

    # The overnight run (overnight._sync_graph): from the cursor, else lookback_hours back.
    last = store.get_state(SYNC_CURSOR)
    after = datetime.fromisoformat(last) if last else NOW - timedelta(hours=settings.lookback_hours)
    ingest_mailbox(mailbox, store, settings, received_after=after, now=NOW + timedelta(hours=8))
    assert store.get_email("AAMk-older") is not None, f"cursor jumped to {last}"


def _raw_statement(email_id="m1", subject="September bank statement", **kwargs) -> RawMessage:
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
    first = process_message(_raw_statement(), store, settings, now=datetime(2026, 9, 22, 12, tzinfo=timezone.utc))
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
    process_message(_raw_statement(), store, settings, now=NOW)
    read_file = pipeline._file_record

    def slow_read(*args, **kwargs):
        record_fraud_verdict(store, settings, "m1", verdict="safe", now=NOW)
        return read_file(*args, **kwargs)

    monkeypatch.setattr(pipeline, "_file_record", slow_read)
    email = process_message(_raw_statement(attachments=[_attachment()]), store, settings, now=NOW)
    assert "fraud_cleared" in email.flags
    assert "fraud_cleared" in store.get_email("m1").flags
    assert [a.filename for a in email.attachments] == ["statement.txt"]


def test_task_snoozed_while_a_copy_is_read_stays_snoozed(store, settings, monkeypatch):
    """A task the user snoozes while another copy of the email is being read stays snoozed."""
    first = process_message(_raw_statement(), store, settings, now=NOW)
    task = next(item for item in first.actions if item.source == "body")
    read_file = pipeline._file_record

    def slow_read(*args, **kwargs):
        store.set_action_status(task.id, "snoozed")
        return read_file(*args, **kwargs)

    monkeypatch.setattr(pipeline, "_file_record", slow_read)
    email = process_message(_raw_statement(attachments=[_attachment()]), store, settings, now=NOW)
    assert next(item for item in email.actions if item.id == task.id).status.value == "snoozed"


# Removing mail removes all of it -------------------------------------------------------------------------
def test_clearing_the_sample_removes_everything_about_its_emails(store, settings, as_of_now):
    """Clearing the sample leaves no row in any table pointing at a sample email."""
    ingest_demo(store, settings, now=as_of_now)
    sample = store.list_emails(limit=1)[0]
    store.save_outlook_item({"entry_id": "x1", "email_id": sample.id, "folder": "Inbox", "message_id": "<x@y>"})
    assert store.clear_sample()
    assert store.outlook_items() == {}


def _drop_file(settings, name: str, data: bytes) -> None:
    settings.inbox_incoming.mkdir(parents=True, exist_ok=True)
    (settings.inbox_incoming / name).write_bytes(data)


# Forwarded-mail names: Path.stem cut subjects with "/" or "." in them ("Statement 09/30" became "30.txt").
FORWARDED = (
    b"From: Bank <stmt@bank.com>\r\nSubject: Statement 09/30\r\nDate: Tue, 30 Sep 2026 10:00:00 +0000\r\n"
    b"Message-ID: <s@bank.com>\r\nContent-Type: text/plain\r\n\r\nYour statement.\r\n"
)


class _ItemClient:
    def _user_root(self):
        return "/me"

    def get_bytes(self, path):
        return FORWARDED


def test_graph_item_attachment_named_with_a_slash_keeps_its_name():
    # Graph names an item attachment after the forwarded email's subject, as Outlook shows it.
    found = GraphMailbox(_ItemClient())._item_attachment("m1", {"id": "a1", "name": "Statement 09/30"})
    assert found[0].filename == "Statement 09_30.txt", [att.filename for att in found]


def test_eml_forwarded_file_named_with_a_dot_keeps_its_name():
    parsed = BytesParser(policy=policy.default).parsebytes(FORWARDED)
    assert forwarded_attachments(parsed, "Q3 accruals v2.1 final")[0].filename == "Q3 accruals v2.1 final.txt"
    assert forwarded_attachments(parsed, "Q3 accruals v2.1 final.eml")[0].filename == "Q3 accruals v2.1 final.txt"


def test_msg_embedded_item_named_with_a_dot_keeps_its_name(settings):
    inner = build_message("Q3 accruals v2.1 final", "See workbook.", attachments=[("accruals.pdf", b"%PDF-1.4 x", PDF)])
    write_msg(settings.inbox_incoming / "fw.msg", "FW: accruals", "fyi", forwarded=[("Q3 accruals v2.1 final", inner)])
    (raw, _owned), = collect_messages(settings)
    assert "Q3 accruals v2.1 final.txt" in [att.filename for att in raw.attachments]


# An image with a Content-ID was dropped as a signature logo even when no HTML body shows it (a phone's photo).
def test_unreferenced_inline_image_in_eml_is_kept(settings):
    data = base64.encodebytes(b"\xff\xd8\xff\xe0" + b"\x00" * 50_000)
    _drop_file(settings, "receipt.eml", (
        b"From: Jordan <jordan@acme.com>\r\nSubject: Taxi receipt\r\nDate: Tue, 06 Oct 2026 10:00:00 +0000\r\n"
        b"Message-ID: <r1@acme.com>\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=B\r\n\r\n"
        b"--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nReceipt attached.\r\n"
        b"--B\r\nContent-Type: image/jpeg; name=receipt.jpg\r\nContent-Disposition: inline; filename=receipt.jpg\r\n"
        b"Content-ID: <IMG_0412@acme>\r\nContent-Transfer-Encoding: base64\r\n\r\n" + data + b"--B--\r\n"
    ))
    (raw, _owned), = collect_messages(settings)
    assert [a.filename for a in raw.attachments] == ["receipt.jpg"]


def test_logo_shown_in_the_html_body_is_still_dropped(settings):
    logo = base64.encodebytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 3000)
    _drop_file(settings, "sig.eml", (
        b"From: a@b.com\r\nSubject: Hello\r\nMessage-ID: <l1@b.com>\r\nMIME-Version: 1.0\r\n"
        b"Content-Type: multipart/related; boundary=R\r\n\r\n"
        b'--R\r\nContent-Type: text/html\r\n\r\n<p>Hi</p><img src="cid:image001.png@01DB">\r\n'
        b"--R\r\nContent-Type: image/png; name=image001.png\r\nContent-Disposition: inline; filename=image001.png\r\n"
        b"Content-ID: <image001.png@01DB>\r\nContent-Transfer-Encoding: base64\r\n\r\n" + logo + b"--R--\r\n"
    ))
    (raw, _owned), = collect_messages(settings)
    assert raw.attachments == []


# Outlook's names for some character sets are unknown to Python, so the text came out garbled.
def _charset_eml(charset: str, text: str, codec: str) -> bytes:
    return (
        b"From: a@b.com\r\nSubject: hi\r\nDate: Tue, 06 Oct 2026 10:00:00 +0000\r\nMessage-ID: <c1@b.com>\r\n"
        b"MIME-Version: 1.0\r\nContent-Type: text/plain; charset=" + charset.encode() + b"\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\n" + text.encode(codec) + b"\r\n"
    )


def test_thai_windows_874_body(settings):
    _drop_file(settings, "th.eml", _charset_eml("windows-874", "ใบแจ้งหนี้ 5,000 บาท", "cp874"))
    (raw, _), = collect_messages(settings)
    assert "ใบแจ้งหนี้" in raw.body_text, raw.body_text


def test_hebrew_iso_8859_8_i_body(settings):
    _drop_file(settings, "he.eml", _charset_eml("iso-8859-8-i", "חשבונית לתשלום", "iso-8859-8"))
    (raw, _), = collect_messages(settings)
    assert "חשבונית" in raw.body_text, raw.body_text


# Files put in inbox/attachments/<name>/ after their message was read were never read, moved or reported.
def test_attachment_folder_without_its_message_is_read_as_loose_files(store, settings, monkeypatch):
    settings.ensure_data_dir()
    (settings.inbox_incoming / "invoice.eml").write_bytes(
        b"From: a@b.com\r\nSubject: Invoice 77\r\nMessage-ID: <i77@b.com>\r\n\r\nInvoice attached.\r\n"
    )
    ingest_folder(store, settings)
    folder = settings.inbox_attachments / "invoice"
    folder.mkdir()
    (folder / "scan.pdf").write_bytes(b"%PDF-1.4 invoice 77")
    # Fresh, it waits for a message that may still be on its way...
    report: dict = {}
    assert ingest_folder(store, settings, report=report) == []
    assert report["waiting"] == ["scan.pdf"]
    # ...and is read on its own once it has sat there for over a day.
    later = time.time() + 25 * 60 * 60
    monkeypatch.setattr("controller_inbox.folder_mail.time.time", lambda: later)
    records = ingest_folder(store, settings)
    assert [r.subject for r in records] == ["scan"]
    assert not (folder / "scan.pdf").exists()


# The CLI says to put a message's files in inbox/attachments/<name>/; read at once, a folder copied in just
# before its message was split from it.
def test_attachments_folder_dropped_before_its_message_waits_for_it(store, settings):
    settings.ensure_data_dir()
    folder = settings.inbox_attachments / "Invoice 77"
    folder.mkdir()
    (folder / "backup.txt").write_text("Support for invoice INV-77: freight $1,300.00", encoding="utf-8")
    first = ingest_folder(store, settings)
    (settings.inbox_incoming / "Invoice 77.eml").write_bytes(
        b"From: ap@vendor.example\r\nSubject: Invoice 77\r\nMessage-ID: <i77@vendor.example>\r\n\r\nInvoice attached.\r\n"
    )
    second = ingest_folder(store, settings)
    assert first == []
    assert [att.filename for att in second[0].attachments] == ["backup.txt"]


# Explorer and robocopy keep a copied file's modified time: a folder copied in just now with week-old files
# still waits for its message.
def test_a_folder_copied_in_with_old_file_dates_still_waits(store, settings):
    settings.ensure_data_dir()
    folder = settings.inbox_attachments / "Invoice 4471"
    folder.mkdir()
    (folder / "scan.pdf").write_bytes(b"%PDF-1.4 invoice 4471")
    week_ago = time.time() - 7 * 24 * 60 * 60
    os.utime(folder / "scan.pdf", (week_ago, week_ago))
    report: dict = {}
    assert ingest_folder(store, settings, report=report) == []
    assert report["waiting"] == ["scan.pdf"]


# Windows and Office litter folders with desktop.ini, Thumbs.db and ~$ lock files; they are not mail.
def test_windows_system_files_are_not_read_as_mail(store, settings):
    _drop_file(settings, "desktop.ini", b"[.ShellClassInfo]\r\nIconResource=C:\\x.dll,0\r\n")
    _drop_file(settings, "Thumbs.db", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 500)
    _drop_file(settings, "~$budget.xlsx", b"\x05Maya Chen" + b"\x00" * 150)
    assert ingest_folder(store, settings) == []


# A signed .eml kept its smime.p7s signature as an attachment.
def test_signed_eml_has_no_signature_attachment(settings):
    _drop_file(settings, "signed.eml", (
        b"From: Bank <ops@bank.com>\r\nSubject: Wire confirmation\r\nDate: Tue, 06 Oct 2026 10:00:00 +0000\r\n"
        b"Message-ID: <sig1@bank.com>\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/signed; protocol="application/pkcs7-signature"; micalg=sha-256; boundary=S\r\n\r\n'
        b"--S\r\nContent-Type: multipart/mixed; boundary=M\r\n\r\n"
        b"--M\r\nContent-Type: text/plain\r\n\r\nConfirmation attached.\r\n"
        b"--M\r\nContent-Type: application/pdf; name=conf.pdf\r\nContent-Disposition: attachment; filename=conf.pdf\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\nJVBERi0xLjQK\r\n--M--\r\n"
        b"--S\r\nContent-Type: application/pkcs7-signature; name=smime.p7s\r\n"
        b"Content-Disposition: attachment; filename=smime.p7s\r\nContent-Transfer-Encoding: base64\r\n\r\n"
        b"MIAGCSqGSIb3DQEHAqCAMIACAQEx\r\n--S--\r\n"
    ))
    (raw, _), = collect_messages(settings)
    assert [a.filename for a in raw.attachments] == ["conf.pdf"]


# Graph lists a signature logo as an inline fileAttachment; the Graph reader kept it.
class _AttachmentClient:
    def _user_root(self):
        return "/me"

    def get_json(self, path, params=None):
        logo = b"\x89PNG\r\n\x1a\n" + b"\x00" * 3000
        return {"value": [
            {
                "@odata.type": "#microsoft.graph.fileAttachment", "id": "A1", "name": "invoice.pdf",
                "contentType": "application/pdf", "size": 10, "isInline": False,
                "contentBytes": base64.b64encode(b"%PDF-1.4 x").decode(),
            },
            {
                "@odata.type": "#microsoft.graph.fileAttachment", "id": "A2", "name": "image001.png",
                "contentType": "image/png", "size": len(logo), "isInline": True, "contentId": "image001.png@01DB",
                "contentBytes": base64.b64encode(logo).decode(),
            },
        ]}


def test_graph_drops_inline_signature_logo():
    found = GraphMailbox(_AttachmentClient()).get_attachments("m1")
    assert [a.filename for a in found] == ["invoice.pdf"]


# Writeback PATCHed `categories`, which replaces the whole list: the user's own ("Paid") were erased.
class _Outlook:
    """A fake Graph holding one message's categories, with Graph's PATCH semantics (the list is replaced)."""

    def __init__(self, categories):
        self.categories = list(categories)

    def _user_root(self):
        return "/me"

    def get_json(self, path, params=None):
        return {"id": "m1", "categories": list(self.categories)}

    def request(self, method, path, **kwargs):
        if method == "PATCH" and "categories" in (kwargs.get("json") or {}):
            self.categories = list(kwargs["json"]["categories"])


def test_writeback_keeps_the_users_own_categories():
    outlook = _Outlook(["Paid", "Follow up"])
    GraphMailbox(outlook).apply_categories("m1", ["CloseDesk", "AP-Invoice"], False)
    assert sorted(outlook.categories) == ["AP-Invoice", "CloseDesk", "Follow up", "Paid"]


def test_writeback_replaces_closedesks_own_old_categories():
    # Filed again as a bank statement: the old AP-Invoice and CloseDesk-Important labels go.
    outlook = _Outlook(["Paid", "CloseDesk", "AP-Invoice", "CloseDesk-Important"])
    GraphMailbox(outlook).apply_categories("m1", ["CloseDesk", "Bank-Statement"], False)
    assert sorted(outlook.categories) == ["Bank-Statement", "CloseDesk", "Paid"]


# A second unreadable file with the same name landed as "<stem>-<hash>.msg" but its note replaced the first one's.
def test_each_failed_file_keeps_its_own_note(store, settings):
    settings.ensure_data_dir()
    (settings.inbox_incoming / "statement.msg").write_bytes(b"not an outlook file, first")
    ingest_folder(store, settings)
    (settings.inbox_incoming / "statement.msg").write_bytes(b"not an outlook file, second")
    ingest_folder(store, settings)
    failed = sorted(p.name for p in settings.inbox_failed.iterdir())
    files = [n for n in failed if not n.endswith(".why.txt")]
    notes = [n for n in failed if n.endswith(".why.txt")]
    assert len(files) == 2
    assert sorted(f"{n}.why.txt" for n in files) == notes, failed


# Found by fuzzing.
def _zip_with_name(name: str) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr(name, b"%PDF-1.4 not really")
    return out.getvalue()


def _utf8_flag_with_cp1252_name() -> bytes:
    # A zipper that sets the UTF-8 flag but writes the name in Windows-1252 ("Rechnung März.pdf").
    data = bytearray(_zip_with_name("Rechnung M?rz.pdf"))
    central = data.index(b"PK\x01\x02")
    data[central + 9] |= 0x08  # flag 0x800: names are UTF-8
    name_at = data.index(b"M?rz", central)
    data[name_at + 1] = 0xE4
    return bytes(data)


def _version_too_new() -> bytes:
    data = bytearray(_zip_with_name("statement.pdf"))
    central = data.index(b"PK\x01\x02")
    data[central + 6] = 64  # "version needed to extract" 6.4, above what zipfile supports
    return bytes(data)


@pytest.mark.parametrize("make_zip", [_utf8_flag_with_cp1252_name, _version_too_new])
def test_email_with_unreadable_zip_is_still_read(store, settings, make_zip):
    settings.ensure_data_dir()
    message = EmailMessage()
    message["Subject"] = "Invoice INV-555 and backup"
    message["From"] = "Harbor AP <ap@harbor.example>"
    message["Date"] = "Tue, 22 Sep 2026 09:00:00 -0400"
    message.set_content("Invoice INV-555, amount due $2,200.00. Backup zipped.")
    message.add_attachment(make_zip(), maintype="application", subtype="zip", filename="backup.zip")
    path = settings.inbox_incoming / "harbor.eml"
    path.write_bytes(message.as_bytes())
    old = time.time() - 3600
    os.utime(path, (old, old))

    report: dict = {}
    records = ingest_folder(store, settings, report=report)
    # A zip that can't be opened is kept as it came (explode_archives keeps a BadZipFile so); the email is read.
    assert report["failed"] == [], report["failed"]
    assert [r.subject for r in records] == ["Invoice INV-555 and backup"]


def test_eml_with_empty_message_id_is_read_not_failed(store, settings):
    # Some scanners and bulk mailers write an empty "Message-ID: <>". The stdlib's header parser raises
    # IndexError reading it, which sends the whole message to inbox/failed.
    settings.ensure_data_dir()
    path = settings.inbox_incoming / "statement.eml"
    path.write_bytes(
        b"From: Harbor AP <ap@harbor.example>\r\n"
        b"Subject: Invoice INV-555\r\n"
        b"Date: Tue, 22 Sep 2026 09:00:00 -0400\r\n"
        b"Message-ID: <>\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Amount due $2,200.00.\r\n"
    )
    old = time.time() - 3600
    os.utime(path, (old, old))
    report: dict = {}
    records = ingest_folder(store, settings, report=report)
    assert report["failed"] == [], report["failed"]
    assert [r.subject for r in records] == ["Invoice INV-555"]


FIXTURE = Path(__file__).parent / "fixtures" / "excel_schedules" / "AP Aging 9-30-26.pdf"


# Guards against: A first run reads each file once: the re-read for a new reader version (``reread_attachments``)
# comes before the drop folder is read, so files just read with the current reader aren't read again.
def test_a_first_run_reads_each_file_once(store, settings, monkeypatch):
    from controller_inbox import folder_mail

    reread = []
    real = folder_mail.attachment_text
    monkeypatch.setattr(folder_mail, "attachment_text", lambda name, ctype, data: reread.append(name) or real(name, ctype, data))
    settings.ensure_data_dir()
    write_msg(settings.inbox_incoming / "a.msg", "AP aging", "Attached.", sender_email="ap@vendor.com", attachments=[("AP Aging.pdf", FIXTURE.read_bytes(), PDF)])
    result = run_overnight(store, settings, sync_graph=False)
    assert result["ingested"] == 1
    assert reread == [], "read once, with the reader of this version, not again straight after"
    assert run_overnight(store, settings, sync_graph=False)["files_reread"] == 0


NOW_OCT6 = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)


def _eml(settings: Settings, name: str, body: str = "See attached.", *, message_id: str = "<m1@x>", attach=(), date_header="Mon, 05 Oct 2026 10:00:00 -0400") -> Path:
    msg = EmailMessage()
    msg["From"] = "Vendor AP <ap@vendor.com>"
    msg["To"] = "me@co.com"
    msg["Subject"] = name
    msg["Message-ID"] = message_id
    msg["Date"] = date_header
    msg.set_content(body)
    for filename, data in attach:
        msg.add_attachment(data, maintype="text", subtype="plain", filename=filename)
    path = settings.inbox_incoming / f"{name}.eml"
    path.write_bytes(bytes(msg))
    _age(path)
    return path


def _age(path: Path) -> None:
    """Files written just now look like they are still being copied; date them a minute back."""
    old = time.time() - 60
    os.utime(path, (old, old))


def _raw_received(body: str, *, received: datetime, attachments=None, preview: str = "") -> RawMessage:
    return RawMessage(
        id="raw-1",
        subject="Docs",
        sender_name="A Vendor",
        sender_email="a@vendor.com",
        received_at=received,
        body_text=body,
        body_preview=preview,
        has_attachments=bool(attachments),
        source="folder",
        attachments=attachments or [],
    )


# Repeated attachment names -------------------------------------------------------------------
def test_two_attachments_with_one_name_both_ingest_with_their_own_text(settings: Settings, store: Store):
    settings.ensure_data_dir()
    _eml(settings, "Two invoices", attach=[("invoice.csv", b"Item,Amount\nFirst,100\n"), ("invoice.csv", b"Item,Amount\nSecond,200\n")])
    report: dict = {}
    [record] = ingest_folder(store, settings, report=report)
    assert report["failed"] == []
    by_name = {att.filename: att for att in record.attachments}
    assert sorted(by_name) == ["invoice (2).csv", "invoice.csv"]
    assert by_name["invoice.csv"].id == f"{record.id}:invoice.csv"  # an unrepeated name keeps its old id
    folder = settings.inbox_extracted / record.id
    assert b"First" in (folder / "invoice.csv").read_bytes() and b"Second" in (folder / "invoice (2).csv").read_bytes()

    store.set_state(folder_mail.READER_KEY, "old")
    reread_attachments(store, settings)
    texts = {att.filename: att.extracted_text for att in store.get_email(record.id).attachments}
    assert "First" in texts["invoice.csv"] and "Second" not in texts["invoice.csv"]
    assert "Second" in texts["invoice (2).csv"]


def test_repeated_names_are_numbered_the_same_way_every_time(settings: Settings):
    settings.ensure_data_dir()
    _eml(settings, "Repeat", attach=[("a.txt", b"1"), ("A.TXT", b"2"), ("a.txt", b"3")])
    first = [att.filename for att in collect_messages(settings)[0][0].attachments]
    second = [att.filename for att in collect_messages(settings)[0][0].attachments]
    assert first == second == ["a.txt", "A (2).TXT", "a (3).txt"]


def test_zip_entries_with_one_name_get_their_own_ids(store: Store, settings: Settings):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("jan/report.txt", "January report")
        archive.writestr("feb/report.txt", "February report")
    zipped = RawAttachment(id="pack.zip", filename="pack.zip", content_type="application/zip", size_bytes=1, content=buffer.getvalue())
    record = process_message(_raw_received("files", received=NOW_OCT6, attachments=[zipped]), store, settings, now=NOW_OCT6)
    ids = [att.id for att in record.attachments]
    assert len(ids) == 2 and len(set(ids)) == 2


# Preview is masked ---------------------------------------------------------------------------
def test_preview_does_not_show_account_numbers(store: Store, settings: Settings):
    raw = _raw_received("Please update our account number 123456789012 routing 021000021", received=NOW_OCT6)
    record = process_message(raw, store, settings, now=NOW_OCT6)
    stored = store.get_email(record.id)
    assert "123456789012" not in stored.body_preview and "021000021" not in stored.body_preview
    assert "123456789012" not in " ".join(a.title + a.detail for a in stored.actions)
    assert stored.extracted.mentions_routing_or_account


# Folder imports don't move the Graph cursor -------------------------------------------------
def test_folder_import_and_sample_leave_the_graph_cursor_alone(settings: Settings, store: Store):
    settings.ensure_data_dir()
    ingest_demo(store, settings, now=NOW_OCT6)
    assert store.get_state("last_sync_at") is None
    _eml(settings, "Cursor")
    assert ingest_folder(store, settings, now=NOW_OCT6)
    assert store.get_state("last_sync_at") is None
    assert store.get_state("last_folder_ingest")


# Graph item attachments and a bad message ----------------------------------------------------
class _FakeClient:
    def __init__(self, mime: bytes = b""):
        self.mime = mime
        self.params: list[dict | None] = []

    def _user_root(self) -> str:
        return "/me"

    def get_json(self, path, params=None):
        self.params.append(params)
        if path.endswith("/attachments"):
            return {"value": [{"@odata.type": "#microsoft.graph.itemAttachment", "id": "ITEM1", "name": "Old thread"}]}
        return {"value": []}

    def get_bytes(self, path):
        assert path.endswith("/attachments/ITEM1/$value")
        return self.mime


def test_graph_item_attachment_is_read_as_mime():
    inner = EmailMessage()
    inner["From"] = "Supplier <s@supplier.com>"
    inner["Subject"] = "Old thread"
    inner.set_content("Our bank details changed.")
    inner.add_attachment(b"Invoice text", maintype="text", subtype="plain", filename="inv.txt")
    found = GraphMailbox(_FakeClient(bytes(inner))).get_attachments("MSG1")
    assert [att.filename for att in found] == ["Old thread.txt", "Old thread › inv.txt"]
    assert b"Our bank details changed." in found[0].content
    assert len({att.id for att in found}) == 2


class _Mailbox:
    def __init__(self, raws):
        self.raws = raws

    def list_messages(self, received_after=None):
        return iter(self.raws)

    def get_attachments(self, message_id):
        if message_id == "bad":
            raise ValueError("Graph sent something odd")
        return []

    def apply_categories(self, message_id, categories, flag):
        return "skipped"


def test_one_bad_graph_message_does_not_stop_the_sync(store: Store, settings: Settings):
    good = _raw_received("hello", received=NOW_OCT6)
    good.id, good.source = "good", "graph"
    bad = _raw_received("broken", received=NOW_OCT6, attachments=[])
    bad.id, bad.source, bad.has_attachments = "bad", "graph", True
    report: dict = {}
    records = ingest_mailbox(_Mailbox([bad, good]), store, settings, now=NOW_OCT6, report=report)
    assert [r.id for r in records] == ["good"]
    assert report["failed"][0]["id"] == "bad"
    assert store.get_state("last_sync_at") == NOW_OCT6.isoformat()


# A message named like the drop folder --------------------------------------------------------
def test_a_message_named_incoming_does_not_take_the_loose_files(settings: Settings):
    settings.ensure_data_dir()
    (settings.inbox_incoming / "incoming.eml").write_bytes(b"From: a@b.com\r\nSubject: x\r\n\r\nhi\r\n")
    (settings.inbox_incoming / "unrelated.pdf").write_bytes(b"%PDF-1.4")
    batches = dict((p.name, [s.name for s in sidecars]) for p, sidecars in folder_mail.collect_batches(settings))
    assert batches["incoming.eml"] == [] and "unrelated.pdf" in batches


def test_a_folder_named_after_the_message_still_holds_its_files(settings: Settings):
    settings.ensure_data_dir()
    (settings.inbox_incoming / "invoice.eml").write_bytes(b"From: a@b.com\r\nSubject: x\r\n\r\nhi\r\n")
    (settings.inbox_attachments / "invoice").mkdir()
    (settings.inbox_attachments / "invoice" / "scan.pdf").write_bytes(b"%PDF-1.4")
    batches = dict((p.name, [s.name for s in sidecars]) for p, sidecars in folder_mail.collect_batches(settings))
    assert batches["invoice.eml"] == ["scan.pdf"]


# A forwarded email inside an .eml ------------------------------------------------------------
def test_forwarded_eml_is_an_attachment_not_part_of_the_body(tmp_path: Path):
    inner = EmailMessage()
    inner["From"] = "fraud@evil.com"
    inner["Subject"] = "old thread"
    inner.set_content("INNER BODY: new bank details")
    inner.add_attachment(b"letter", maintype="text", subtype="plain", filename="letter.txt")
    outer = EmailMessage()
    outer["From"] = "boss@co.com"
    outer["Subject"] = "fwd"
    outer.set_content("OUTER BODY: see forwarded")
    outer.add_attachment(inner)
    path = tmp_path / "o.eml"
    path.write_bytes(bytes(outer))
    raw = folder_mail._parse_eml(path)
    assert "INNER BODY" not in raw.body_text and "OUTER BODY" in raw.body_text
    assert [att.filename for att in raw.attachments] == ["old thread.txt", "old thread › letter.txt"]
    assert b"INNER BODY" in raw.attachments[0].content and b"fraud@evil.com" in raw.attachments[0].content


# Graph filter order -------------------------------------------------------------------------
@pytest.mark.parametrize("after", [None, datetime(2026, 10, 1, tzinfo=timezone.utc)])
def test_graph_filter_names_the_sort_property_first(after):
    client = _FakeClient()
    list(GraphMailbox(client).list_messages(received_after=after))
    params = client.params[0]
    assert params["$orderby"].startswith("receivedDateTime")
    assert params["$filter"].startswith("receivedDateTime ge ")


# Drop folder: files still copying, locked files, the sample ---------------------------------
def test_a_file_still_being_copied_waits_for_the_next_run(settings: Settings, store: Store, monkeypatch):
    settings.ensure_data_dir()
    path = settings.inbox_incoming / "growing.txt"
    path.write_bytes(b"first half")

    def still_writing(_seconds):
        with path.open("ab") as handle:
            handle.write(b" second half")

    monkeypatch.setattr(folder_mail.time, "sleep", still_writing)
    report: dict = {}
    assert ingest_folder(store, settings, report=report) == []
    assert report["waiting"] == ["growing.txt"] and path.exists()

    _age(path)
    assert len(ingest_folder(store, settings)) == 1 and not path.exists()


def test_a_freshly_written_finished_file_is_read_now(settings: Settings, store: Store):
    settings.ensure_data_dir()
    (settings.inbox_incoming / "done.txt").write_bytes(b"complete")
    assert len(ingest_folder(store, settings)) == 1


def test_a_locked_file_is_left_and_the_batch_goes_on(settings: Settings, store: Store, monkeypatch):
    settings.ensure_data_dir()
    _eml(settings, "A locked", message_id="<a@x>")
    _eml(settings, "B fine", message_id="<b@x>")
    real = folder_mail._parse_message

    def parse(path):
        if path.name.startswith("A"):
            raise PermissionError("in use by another process")
        return real(path)

    monkeypatch.setattr(folder_mail, "_parse_message", parse)
    report: dict = {}
    records = ingest_folder(store, settings, report=report)
    assert [r.subject for r in records] == ["B fine"]
    assert report["waiting"] == ["A locked.eml"] and report["failed"] == []
    assert (settings.inbox_incoming / "A locked.eml").exists()


def test_a_failed_move_to_quarantine_does_not_stop_the_batch(settings: Settings, store: Store, monkeypatch):
    settings.ensure_data_dir()
    (settings.inbox_incoming / "a-broken.msg").write_bytes(b"not an outlook file")
    _age(settings.inbox_incoming / "a-broken.msg")
    _eml(settings, "b-fine", message_id="<b@x>")
    real_move = folder_mail.shutil.move

    def move(src, dst):
        if "broken" in src:
            raise PermissionError("locked")
        return real_move(src, dst)

    monkeypatch.setattr(folder_mail.shutil, "move", move)
    report: dict = {}
    records = ingest_folder(store, settings, report=report)
    assert [r.subject for r in records] == ["b-fine"]
    assert len(report["failed"]) == 1


def test_a_message_that_fails_after_parsing_keeps_the_sample(settings: Settings, loaded: Store, monkeypatch):
    settings.ensure_data_dir()
    before = loaded.counts()["emails"]
    _eml(settings, "Breaks later")

    def boom(*_args, **_kwargs):
        raise RuntimeError("classifier fell over")

    monkeypatch.setattr(folder_mail, "process_message", boom)
    report: dict = {}
    ingest_folder(loaded, settings, report=report)
    assert len(report["failed"]) == 1 and "sample_cleared" not in report
    assert loaded.counts()["emails"] == before and not loaded.real_mail_count()


def test_an_address_in_the_display_name_is_not_the_sender(store: Store, settings: Settings):
    # "Acme Billing <billing@acme.com>" is only the name: the mail comes from acme-payments.net.
    settings.ensure_data_dir()
    settings.trusted_domains = "acme.com"
    path = settings.inbox_incoming / "spoof.eml"
    path.write_bytes(
        b'From: "Acme Billing <billing@acme.com>" <billing@acme-payments.net>\r\nTo: ap@co.com\r\n'
        b"Subject: Updated remittance details\r\nMessage-ID: <sp1@x>\r\nDate: Mon, 05 Oct 2026 10:00:00 -0400\r\n\r\n"
        b"Our bank details have changed. Please use the new account for all payments from today.\r\n"
    )
    _age(path)
    record = ingest_folder(store, settings)[0]
    assert record.sender_email == "billing@acme-payments.net"
    assert store.fraud_check(record.id)["level"] == "high" and "do_not_process" in record.flags
    # Headers the strict parser gives up on still give the address in the brackets.
    assert folder_mail._split_address("Chen, Maya <maya@x.com>") == ("Chen, Maya", "maya@x.com")
    assert folder_mail._split_address("Maya Chen") == ("Maya Chen", "")


# The overnight reading never overwrites what the user did while the model was reading -------
def test_a_reading_is_not_saved_over_a_correction_or_verdict_made_meanwhile(store: Store, settings: Settings):
    from controller_inbox.fraud import record_fraud_verdict
    from controller_inbox.learn import record_correction
    from controller_inbox.overnight import read_queue
    from test_bionic import AgreeingReader

    ingest_demo(store, settings, now=NOW_OCT6)
    first, corrected, flagged = (e.id for e in store.list_emails(model_status="script_draft", order="queue", limit=3))

    class SlowReader(AgreeingReader):
        def read(self, packet):
            if packet["email_id"] == first:
                # While the model reads the first email, the user corrects the second and calls the third fraud.
                record_correction(store, settings, email_id=corrected, corrected_category="newsletter", reason="it is a newsletter")
                record_fraud_verdict(store, settings, flagged, verdict="fraud", note="phoned the vendor; it is fake")
            return super().read(packet)

    result = read_queue(store, settings, limit=3, now=NOW_OCT6, reader=SlowReader())
    assert result["read_ids"] == [first]
    kept = store.get_email(corrected)
    assert kept.category.value == "newsletter" and kept.model_status == "corrected"
    verdict = store.get_email(flagged)
    assert {"fraud_risk", "fraud_confirmed"} <= set(verdict.flags)
    assert verdict.model_status == "script_draft", "read again on the next run, as it is now"


def test_a_limit_of_zero_reads_nothing(loaded: Store, settings: Settings):
    from controller_inbox.overnight import read_queue
    from test_bionic import AgreeingReader

    assert read_queue(loaded, settings, limit=0, reader=AgreeingReader())["read_ids"] == []
    assert loaded.counts()["waiting_on_bionic"] == 19


# Another copy of a stored message keeps the files that came with the first ---------------
def _sidecar(settings: Settings, name: str, data: bytes) -> Path:
    path = settings.inbox_incoming / name
    path.write_bytes(data)
    _age(path)
    return path


def test_a_copy_without_the_files_keeps_the_ones_dropped_with_the_first(settings: Settings, store: Store):
    settings.ensure_data_dir()
    _eml(settings, "Invoice 4410", "Invoice attached, due Oct 30.", message_id="<inv4410@vendor.com>")
    _sidecar(settings, "Invoice 4410.txt", b"Invoice INV-4410 Freight $1,250.00")
    [record] = ingest_folder(store, settings)
    assert [att.filename for att in record.attachments] == ["Invoice 4410.txt"]

    # Exported again later (a bulk "save as" of the folder), this time with no file beside it.
    _eml(settings, "Invoice 4410", "Invoice attached, due Oct 30.", message_id="<inv4410@vendor.com>")
    report: dict = {}
    ingest_folder(store, settings, report=report)
    assert report["already_read"] == 1 and report["failed"] == []
    again = store.get_email(record.id)
    assert [att.filename for att in again.attachments] == ["Invoice 4410.txt"]
    assert 1250.0 in again.extracted.amounts and again.has_attachments
    assert "missing_attachment" not in again.flags


def test_a_second_copy_in_one_drop_still_brings_its_files(settings: Settings, store: Store):
    # "Invoice 4410 (2).eml" sorts first and has no file beside it; the copy that does must not be skipped.
    settings.ensure_data_dir()
    _eml(settings, "Invoice 4410", message_id="<inv4410@vendor.com>")
    _eml(settings, "Invoice 4410", message_id="<inv4410@vendor.com>").rename(settings.inbox_incoming / "Invoice 4410 (2).eml")
    _eml(settings, "Invoice 4410", message_id="<inv4410@vendor.com>")
    _sidecar(settings, "Invoice 4410.txt", b"Invoice INV-4410 Freight $1,250.00")
    report: dict = {}
    records = ingest_folder(store, settings, report=report)
    assert (report["read"], report["already_read"]) == (1, 1)
    assert len(records) == 1 and [att.filename for att in records[0].attachments] == ["Invoice 4410.txt"]
    assert "INV-4410" in records[0].attachments[0].extracted_text
    assert not list(settings.inbox_incoming.iterdir())


def test_a_copy_of_a_read_message_adds_its_new_files_and_keeps_the_reading(settings: Settings, store: Store):
    from controller_inbox.reading import apply_bionic_reading

    settings.ensure_data_dir()
    _eml(settings, "Invoice 4410", message_id="<inv4410@vendor.com>")
    _sidecar(settings, "Invoice 4410.txt", b"Invoice INV-4410 Freight $1,250.00")
    [record] = ingest_folder(store, settings)
    reading = {"category": "ap_invoice", "folder": "important", "importance": "high", "summary": "Freight invoice.", "actions": [], "why": "x"}
    apply_bionic_reading(store, record.id, reading)

    # The same message again, with a different file under the same name: both files are kept.
    _eml(settings, "Invoice 4410", message_id="<inv4410@vendor.com>")
    _sidecar(settings, "Invoice 4410.txt", b"Revised invoice INV-4410 Freight $1,300.00")
    ingest_folder(store, settings)
    kept = store.get_email(record.id)
    assert kept.model_status == "bionic" and kept.summary == "Freight invoice."
    assert sorted(att.filename for att in kept.attachments) == ["Invoice 4410 (2).txt", "Invoice 4410.txt"]
    folder = settings.inbox_extracted / record.id
    assert b"$1,250.00" in (folder / "Invoice 4410.txt").read_bytes()
    assert b"$1,300.00" in (folder / "Invoice 4410 (2).txt").read_bytes()

    # A third copy with a file already stored adds nothing.
    _eml(settings, "Invoice 4410", message_id="<inv4410@vendor.com>")
    _sidecar(settings, "Invoice 4410.txt", b"Revised invoice INV-4410 Freight $1,300.00")
    report: dict = {}
    assert ingest_folder(store, settings, report=report) == [] and report["already_read"] == 1
    assert len(store.get_email(record.id).attachments) == 2


# Different emails that share a Message-ID stay apart ------------------------------------
def test_different_emails_with_one_message_id_are_kept_apart(settings: Settings, store: Store):
    # A scan-to-email copier gives every scan the same Message-ID.
    settings.ensure_data_dir()
    scan = "<scan@copier.local>"
    _eml(settings, "Scan 0001", "Scanned: vendor invoice", message_id=scan, attach=[("Scan 0001.txt", b"Invoice INV-7001 total $4,200.00")])
    second = _eml(settings, "Scan 0002", "Scanned: signed contract", message_id=scan, attach=[("Scan 0002.txt", b"Contract signed")],
                  date_header="Mon, 05 Oct 2026 11:00:00 -0400").read_bytes()
    report: dict = {}
    ingest_folder(store, settings, report=report)
    assert (report["read"], report["already_read"]) == (2, 0)

    # The next day another scan, while the first two are still script drafts.
    _eml(settings, "Scan 0003", "Scanned: bank statement", message_id=scan, attach=[("Scan 0003.txt", b"Statement Sept 2026")],
         date_header="Tue, 06 Oct 2026 09:00:00 -0400")
    ingest_folder(store, settings)
    stored = {email.subject: [att.filename for att in email.attachments] for email in store.list_emails()}
    assert stored == {"Scan 0001": ["Scan 0001.txt"], "Scan 0002": ["Scan 0002.txt"], "Scan 0003": ["Scan 0003.txt"]}

    # The second scan dropped again is still one email.
    path = settings.inbox_incoming / "Scan 0002 again.eml"
    path.write_bytes(second)
    _age(path)
    report = {}
    ingest_folder(store, settings, report=report)
    assert report["already_read"] == 1 and store.counts()["emails"] == 3


# A message that failed on one sync is read on the next -------------------------------------
class _FlakyMailbox(_Mailbox):
    """Lists messages by their received time, as Graph does; ``failures`` says how often each one fails first."""

    def __init__(self, raws, failures):
        super().__init__(raws)
        self.failures = dict(failures)
        self.fetched: list[str] = []

    def list_messages(self, received_after=None):
        return iter([raw for raw in self.raws if received_after is None or raw.received_at >= received_after])

    def get_attachments(self, message_id):
        self.fetched.append(message_id)
        if self.failures.get(message_id, 0):
            self.failures[message_id] -= 1
            from controller_inbox.graph import GraphError

            raise GraphError("Microsoft Graph throttled the request (HTTP 429). Wait and retry.")
        return []


def _graph_raw(message_id: str, received: datetime) -> RawMessage:
    raw = _raw_received("Invoice attached", received=received)
    raw.id, raw.source, raw.has_attachments = message_id, "graph", True
    return raw


def test_a_message_that_failed_on_a_sync_is_read_on_the_next(store: Store, settings: Settings):
    from datetime import timedelta

    t0 = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    mailbox = _FlakyMailbox([_graph_raw("throttled", t0), _graph_raw("fine", t0 + timedelta(minutes=1))], {"throttled": 1})
    report: dict = {}
    ingest_mailbox(mailbox, store, settings, received_after=t0 - timedelta(hours=72), now=t0 + timedelta(minutes=5), report=report)
    assert [row["id"] for row in report["failed"]] == ["throttled"] and store.get_email("fine")
    cursor = datetime.fromisoformat(store.get_state("last_sync_at"))
    assert cursor <= t0, "the cursor waits for the message that failed"

    mailbox.fetched.clear()
    ingest_mailbox(mailbox, store, settings, received_after=cursor, now=t0 + timedelta(hours=1))
    assert store.get_email("throttled") is not None
    assert mailbox.fetched == ["throttled"], "the message read last time is not fetched again"
    assert store.get_state("last_sync_at") == (t0 + timedelta(hours=1)).isoformat()


def test_a_message_that_always_fails_stops_holding_the_cursor(store: Store, settings: Settings, monkeypatch):
    from datetime import timedelta

    from controller_inbox import pipeline

    monkeypatch.setattr(pipeline, "MAX_SYNC_TRIES", 2)
    t0 = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    mailbox = _FlakyMailbox([_graph_raw("broken", t0)], {"broken": 99})
    ingest_mailbox(mailbox, store, settings, now=t0 + timedelta(minutes=5))
    assert store.get_state("last_sync_at") == t0.isoformat()
    later = t0 + timedelta(hours=1)
    ingest_mailbox(mailbox, store, settings, received_after=t0, now=later)
    assert store.get_state("last_sync_at") == later.isoformat(), "after the last try the sync moves on"


def test_a_cursor_held_for_a_failed_message_is_stored_in_utc(store: Store, settings: Settings):
    from datetime import timedelta

    # Outlook can give a message's time with the sender's offset: the cursor kept for it is UTC, as mail is stored.
    eastern = timezone(timedelta(hours=-4))
    t0 = datetime(2026, 10, 6, 5, 0, tzinfo=eastern)
    mailbox = _FlakyMailbox([_graph_raw("throttled", t0)], {"throttled": 1})
    ingest_mailbox(mailbox, store, settings, received_after=t0 - timedelta(hours=72), now=t0 + timedelta(minutes=5))
    assert store.get_state("last_sync_at") == "2026-10-06T09:00:00+00:00"


# watch reads the drop folder and writes the digest while Outlook is down ---------------------
def test_watch_reads_the_drop_folder_while_outlook_is_down(settings: Settings, store: Store, monkeypatch, capsys):
    from controller_inbox import cli
    from controller_inbox.graph import GraphError

    class Down:
        def list_messages(self, received_after=None):
            raise GraphError("Graph 503: Service Unavailable")

    settings.ensure_data_dir()
    settings.azure_client_id = "configured"
    monkeypatch.setattr(cli, "_graph_mailbox", lambda _settings: Down())
    _eml(settings, "Dropped while Outlook is down", message_id="<w1@x>")
    result = cli.watch_tick(settings, store, now=datetime(2026, 10, 6, 9, 0, tzinfo=settings.tz))
    assert [record.subject for record in result["records"]] == ["Dropped while Outlook is down"]
    assert "503" in result["graph_note"] and result["digest"] is not None
    assert not list(settings.inbox_incoming.iterdir())
    assert cli._watch(settings, store, once=True) == 0
    assert "Outlook could not be read (Graph 503" in capsys.readouterr().out


# Reading attachments again never gives a zipped file another file's text --------------------
def test_reading_again_reads_a_zipped_file_from_its_zip(settings: Settings, store: Store):
    from controller_inbox.demo import make_pdf

    settings.ensure_data_dir()
    outer = make_pdf([["Invoice INV-1001 from Alpha Freight", ["Item", "Amount"], ["Freight", "$1,000.00"]]])
    inner = make_pdf([["Invoice INV-2002 from Beta Paper", ["Item", "Amount"], ["Paper", "$2,000.00"]]])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("invoice.pdf", inner)
    msg = EmailMessage()
    msg["From"] = "AP <ap@vendor.com>"
    msg["Subject"] = "Two invoices"
    msg["Message-ID"] = "<two@x>"
    msg["Date"] = "Mon, 05 Oct 2026 10:00:00 -0400"
    msg.set_content("See attached.")
    # An "invoice.pdf" beside a zip that holds another "invoice.pdf".
    msg.add_attachment(outer, maintype="application", subtype="pdf", filename="invoice.pdf")
    msg.add_attachment(buffer.getvalue(), maintype="application", subtype="zip", filename="older.zip")
    path = settings.inbox_incoming / "two.eml"
    path.write_bytes(bytes(msg))
    _age(path)
    [record] = ingest_folder(store, settings)
    zipped = next(att for att in record.attachments if att.id.endswith("older.zip:invoice.pdf"))
    assert "INV-2002" in zipped.extracted_text

    store.set_attachment_text(zipped.id, "[page 1]\nas an older reader left it")
    store.set_state(folder_mail.READER_KEY, "old")
    assert reread_attachments(store, settings) == 1
    texts = {att.id: att.extracted_text for att in store.get_email(record.id).attachments}
    assert "INV-2002" in texts[zipped.id] and "INV-1001" not in texts[zipped.id]
    assert "INV-1001" in texts[f"{record.id}:invoice.pdf"]


# A long non-Latin attachment name is cut to fit, and files are saved before the email --------
def test_a_long_non_latin_file_name_is_cut_to_fit_and_keeps_its_extension(settings: Settings, store: Store):
    settings.ensure_data_dir()
    name = "請求書" * 30 + ".txt"  # 94 characters but 274 bytes in UTF-8, more than a file name can hold
    _eml(settings, "請求書", message_id="<jp1@x>", attach=[(name, b"first copy"), (name, b"second copy")])
    report: dict = {}
    [record] = ingest_folder(store, settings, report=report)
    assert report["failed"] == [] and not list(settings.inbox_failed.iterdir())
    assert record.source_path.endswith(".eml")
    saved = {path.name: path.read_bytes() for path in (settings.inbox_extracted / record.id).iterdir()}
    assert sorted(saved.values()) == [b"first copy", b"second copy"]
    assert all(len(name.encode("utf-8")) <= 255 and name.endswith(".txt") for name in saved)
    assert any(name.endswith(" (2).txt") for name in saved), "the number of the second copy is not cut off"


def test_an_email_is_not_stored_when_its_files_cannot_be_saved(settings: Settings, store: Store, monkeypatch):
    settings.ensure_data_dir()
    _eml(settings, "Disk full", message_id="<full@x>", attach=[("a.txt", b"data")])

    def full(*_args):
        raise OSError("No space left on device")

    monkeypatch.setattr(folder_mail, "write_extracted", full)
    report: dict = {}
    assert ingest_folder(store, settings, report=report) == []
    assert len(report["failed"]) == 1 and store.counts()["emails"] == 0
    assert (settings.inbox_failed / "Disk full.eml").exists()


# Outlook sync reads Reply-To, so mail whose replies go elsewhere is flagged ------------------
class _ReplyToClient(_FakeClient):
    def get_json(self, path, params=None):
        self.params.append(params)
        if path.endswith("/attachments"):
            return {"value": []}
        return {"value": [{
            "id": "AAMk1", "subject": "Updated remittance details", "receivedDateTime": "2026-10-05T14:00:00Z",
            "from": {"emailAddress": {"name": "Acme Billing", "address": "billing@acme.com"}},
            "replyTo": [{"emailAddress": {"name": "Acme Billing", "address": "Acme.Billing@protonmail.com"}}],
            "body": {"contentType": "text", "content": "Please use our new bank account for all payments."},
            "hasAttachments": False, "importance": "normal", "isRead": False,
        }]}


def test_graph_sync_reads_reply_to_for_the_fraud_check(store: Store, settings: Settings):
    client = _ReplyToClient()
    [record] = ingest_mailbox(GraphMailbox(client), store, settings, now=NOW_OCT6)
    assert "replyTo" in client.params[0]["$select"].split(",")
    assert record.reply_to == "acme.billing@protonmail.com"
    assert "reply_to_mismatch" in {signal["key"] for signal in store.fraud_check(record.id)["signals"]}


# VIP senders can be separated by semicolons, like trusted domains -----------------------------
def test_vip_senders_split_on_semicolons_too():
    settings = Settings(vip_senders="cfo@taz.com; ceo@taz.com,board@taz.com", _env_file=None)
    assert settings.vip_list == ["cfo@taz.com", "ceo@taz.com", "board@taz.com"]


# An invitation's calendar sent inline, with no file name, is kept ----------------------------
INVITE = "BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\nSUMMARY:Q3 close review\r\nDTSTART:20261008T150000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"


def _invite(settings: Settings, name: str, *, attached: bool) -> None:
    msg = EmailMessage()
    msg["From"] = "Controller <cfo@taz.com>"
    msg["Subject"] = "Q3 close review"
    msg["Message-ID"] = f"<{name}@x>"
    msg["Date"] = "Mon, 05 Oct 2026 10:00:00 -0400"
    msg.set_content("You have been invited.")
    msg.add_alternative(INVITE, subtype="calendar", params={"method": "REQUEST"})
    if attached:
        msg.add_attachment(INVITE.encode(), maintype="text", subtype="calendar", filename="invite.ics")
    path = settings.inbox_incoming / f"{name}.eml"
    path.write_bytes(bytes(msg))
    _age(path)


def test_an_inline_calendar_is_kept_as_the_invite(settings: Settings, store: Store):
    settings.ensure_data_dir()
    _invite(settings, "inline", attached=False)
    _invite(settings, "both", attached=True)
    records = {record.internet_message_id: record for record in ingest_folder(store, settings)}
    inline = records["<inline@x>"]
    assert [att.filename for att in inline.attachments] == ["invite.ics"]
    assert "Q3 close review" in inline.attachments[0].extracted_text
    assert [att.filename for att in records["<both@x>"].attachments] == ["invite.ics"], "not kept twice"


# The overnight reading is saved for an email whose files didn't arrive in name order --------
def test_a_reading_is_saved_for_an_email_whose_files_are_not_in_name_order(settings: Settings, store: Store):
    from controller_inbox.overnight import read_queue
    from test_bionic import AgreeingReader

    settings.ensure_data_dir()
    # "Statement" comes before "Invoice" in the email; the reading was checked against a copy in name order.
    _eml(settings, "September statement", message_id="<st9@vendor.com>",
         attach=[("Statement.txt", b"Statement of account Sept 2026"), ("Invoice INV-9.txt", b"Invoice INV-9 $310.00")])
    [record] = ingest_folder(store, settings)
    result = read_queue(store, settings, now=NOW_OCT6, reader=AgreeingReader())
    assert result["read_ids"] == [record.id]
    assert store.get_email(record.id).model_status == "bionic"
    assert store.counts()["waiting_on_bionic"] == 0


# A copy of a read email with new files doesn't undo what the user did while its files were read --
@pytest.mark.parametrize("meanwhile", ["correction", "verdict"])
def test_new_files_on_a_read_email_keep_what_the_user_did_meanwhile(settings: Settings, store: Store, monkeypatch, meanwhile):
    from controller_inbox import pipeline
    from controller_inbox.fraud import record_fraud_verdict
    from controller_inbox.learn import record_correction
    from controller_inbox.reading import apply_bionic_reading

    settings.ensure_data_dir()
    _eml(settings, "Invoice 4410", message_id="<inv4410@vendor.com>")
    _sidecar(settings, "Invoice 4410.txt", b"Invoice INV-4410 Freight $1,250.00")
    [record] = ingest_folder(store, settings)
    reading = {"category": "ap_invoice", "folder": "important", "importance": "high", "summary": "Freight invoice.", "actions": [], "why": "x"}
    apply_bionic_reading(store, record.id, reading)

    real = pipeline.extract_text_from_bytes

    def slow_read(filename, content_type, data):
        # A scan read with OCR takes a while; the user works on the email in the dashboard meanwhile.
        if meanwhile == "correction":
            record_correction(store, settings, email_id=record.id, corrected_category="newsletter", reason="it is a newsletter")
        else:
            record_fraud_verdict(store, settings, record.id, verdict="fraud", note="phoned the vendor; it is fake")
        return real(filename, content_type, data)

    monkeypatch.setattr(pipeline, "extract_text_from_bytes", slow_read)
    _eml(settings, "Invoice 4410", message_id="<inv4410@vendor.com>")
    _sidecar(settings, "Invoice 4410.txt", b"Revised invoice INV-4410 Freight $1,300.00")
    ingest_folder(store, settings)

    kept = store.get_email(record.id)
    assert sorted(att.filename for att in kept.attachments) == ["Invoice 4410 (2).txt", "Invoice 4410.txt"]
    if meanwhile == "correction":
        assert kept.model_status == "corrected" and kept.category.value == "newsletter"
    else:
        assert {"fraud_risk", "fraud_confirmed"} <= set(kept.flags)


# A copy of an email with no Subject or no Date, saved again under another name, is still one email --
def _scan(settings: Settings, name: str, *, subject: str | None, date_header: str | None, age: float, data: bytes | None = None) -> bytes:
    """A copier's scan; ``data`` saves the very bytes of an earlier one again under ``name``."""
    if data is None:
        msg = EmailMessage()
        msg["From"] = "Scanner <scan@copier.local>"
        msg["Message-ID"] = "<scan@copier.local>"
        if subject is not None:
            msg["Subject"] = subject
        if date_header is not None:
            msg["Date"] = date_header
        msg.set_content("Scanned document attached.")
        msg.add_attachment(f"Scanned page {name}".encode(), maintype="text", subtype="plain", filename="scan.txt")
        data = bytes(msg)
    path = settings.inbox_incoming / name
    path.write_bytes(data)
    old = time.time() - age
    os.utime(path, (old, old))
    return data


@pytest.mark.parametrize("missing", ["subject", "date"])
def test_a_copy_saved_again_is_one_email_when_the_message_has_no_subject_or_date(settings: Settings, store: Store, missing):
    settings.ensure_data_dir()
    subject = None if missing == "subject" else "Scan"
    date_header = None if missing == "date" else "Mon, 05 Oct 2026 10:00:00 -0400"
    # Its subject would be the file's name, or its date the file's date: both differ for the copy saved later.
    first = _scan(settings, "Scan from copier.eml", subject=subject, date_header=date_header, age=3600)
    ingest_folder(store, settings)
    _scan(settings, "Scan from copier (copy).eml", subject=subject, date_header=date_header, age=60, data=first)
    report: dict = {}
    ingest_folder(store, settings, report=report)
    assert (report["read"], report["already_read"]) == (0, 1) and store.counts()["emails"] == 1


def test_scans_without_a_subject_that_share_a_message_id_are_still_kept_apart(settings: Settings, store: Store):
    settings.ensure_data_dir()
    _scan(settings, "a.eml", subject=None, date_header="Mon, 05 Oct 2026 10:00:00 -0400", age=60)
    _scan(settings, "b.eml", subject=None, date_header="Mon, 05 Oct 2026 11:00:00 -0400", age=60)
    second = (settings.inbox_incoming / "b.eml").read_bytes()
    report: dict = {}
    ingest_folder(store, settings, report=report)
    assert report["read"] == 2 and store.counts()["emails"] == 2
    _scan(settings, "b again.eml", subject=None, date_header=None, age=30, data=second)  # the second, saved again
    report = {}
    ingest_folder(store, settings, report=report)
    assert report["already_read"] == 1 and store.counts()["emails"] == 2


# VIP senders copied from Outlook count by their addresses, not the words of their names ------
def test_vip_senders_copied_from_outlook_count_by_their_addresses(store: Store, settings: Settings):
    pasted = Settings(vip_senders="Chen, Maya <maya@taz.com>; Bob Lee <Bob@taz.com>", _env_file=None)
    assert pasted.vip_list == ["maya@taz.com", "bob@taz.com"]
    assert Settings(vip_senders="cfo@taz.com ceo@taz.com; irs.gov, Maya Chen", _env_file=None).vip_list == [
        "cfo@taz.com", "ceo@taz.com", "irs.gov"
    ]
    assert Settings(vip_senders="treasurer", _env_file=None).vip_list == ["treasurer"]

    settings.vip_senders = pasted.vip_senders
    vip = {}
    for n, sender in enumerate(["maya@taz.com", "colleen@randomvendor.com", "noreply@bobcat-rentals.com"]):
        raw = _raw_received("Just checking in.", received=NOW_OCT6)
        raw.id, raw.sender_email = f"v{n}", sender
        vip[sender] = "VIP / elevated sender" in process_message(raw, store, settings, now=NOW_OCT6).importance_reasons
    assert vip == {"maya@taz.com": True, "colleen@randomvendor.com": False, "noreply@bobcat-rentals.com": False}


# A manual sync of a shorter window leaves the cursor and the message it waits for alone ------
def test_a_shorter_manual_sync_keeps_the_message_the_cursor_waits_for(store: Store, settings: Settings):
    from datetime import timedelta

    t0 = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    mailbox = _FlakyMailbox([_graph_raw("throttled", t0)], {"throttled": 1})
    ingest_mailbox(mailbox, store, settings, received_after=t0 - timedelta(hours=72), now=t0 + timedelta(minutes=5))
    held = store.get_state("last_sync_at")
    assert held == t0.isoformat()

    # "closedesk sync --hours 1" three hours later: it doesn't reach back to the message that failed.
    later = t0 + timedelta(hours=3)
    ingest_mailbox(mailbox, store, settings, received_after=later - timedelta(hours=1), now=later)
    assert store.get_state("last_sync_at") == held, "the cursor doesn't jump past mail this sync didn't read"

    ingest_mailbox(mailbox, store, settings, received_after=datetime.fromisoformat(held), now=later + timedelta(hours=1))
    assert store.get_email("throttled") is not None
    assert store.get_state("last_sync_at") == (later + timedelta(hours=1)).isoformat()


# A copy dropped again doesn't rename or redate the email, or bring back snoozed tasks ----------
def _bare_copy(settings: Settings, name: str, body: str, message_id: str) -> Path:
    """The same message saved again by a tool that kept only some of its headers (no Subject, no Date)."""
    msg = EmailMessage()
    msg["From"] = "Vendor AP <ap@vendor.com>"
    msg["Message-ID"] = message_id
    msg.set_content(body)
    path = settings.inbox_incoming / name
    path.write_bytes(bytes(msg))
    _age(path)
    return path


def test_a_copy_dropped_again_keeps_the_name_the_date_and_snoozed_tasks(settings: Settings, store: Store):
    from controller_inbox.reading import apply_bionic_reading

    settings.ensure_data_dir()
    body = "Please approve and pay invoice INV-4410 for $12,400.00. Due date: October 15, 2026."
    subject = "Invoice INV-4410 due October 15, 2026"
    _eml(settings, subject, body, message_id="<inv4410@vendor.com>", date_header="Mon, 21 Sep 2026 10:00:00 +0000")
    [record] = ingest_folder(store, settings)
    assert record.actions
    for task in record.actions:
        store.set_action_status(task.id, "snoozed")
    snoozed = {task.title for task in record.actions}

    def check(email) -> None:
        assert email.subject == subject and email.received_at == record.received_at
        assert {task.title: task.status.value for task in email.actions if task.title in snoozed} == dict.fromkeys(snoozed, "snoozed")
        assert store.counts()["emails"] == 1

    # A copy with nothing new is already read: nothing about the email changes.
    _bare_copy(settings, "scan-export-0001.eml", body, "<inv4410@vendor.com>")
    report: dict = {}
    assert ingest_folder(store, settings, report=report) == [] and report["already_read"] == 1
    check(store.get_email(record.id))

    # A copy that brings a file still adds it, and keeps the name, the date and the snoozed tasks.
    _bare_copy(settings, "scan-export-0002.eml", body, "<inv4410@vendor.com>")
    _sidecar(settings, "scan-export-0002.txt", b"Remittance advice for INV-4410 $12,400.00")
    report = {}
    ingest_folder(store, settings, report=report)
    assert report["already_read"] == 1 and report["failed"] == []
    again = store.get_email(record.id)
    assert [att.filename for att in again.attachments] == ["scan-export-0002.txt"]
    check(again)

    # The model can still read it afterwards.
    reading = {"category": "ap_invoice", "folder": "important", "importance": "high", "summary": "Pay INV-4410.", "actions": [], "why": "x"}
    assert apply_bionic_reading(store, record.id, reading).model_status == "bionic"


def test_a_copy_of_an_email_whose_subject_has_an_account_number_is_still_one_email(settings: Settings, store: Store):
    # The stored subject is masked ("account ****6677"); the copy's own subject isn't yet when they are compared.
    settings.ensure_data_dir()
    _eml(settings, "Remittance - Acct 44556677", "Paid.", message_id="<rem1@vendor.com>")
    [record] = ingest_folder(store, settings)
    assert record.subject == "Remittance - account ****6677"
    copy = _eml(settings, "Remittance - Acct 44556677", "Paid.", message_id="<rem1@vendor.com>")
    copy.rename(settings.inbox_incoming / "Remittance copy.eml")
    report: dict = {}
    ingest_folder(store, settings, report=report)
    assert report["already_read"] == 1 and store.counts()["emails"] == 1


# A Date header past year 9999 in UTC, 8-bit text with no character set ------------------------
def test_an_email_dated_past_what_utc_can_hold_is_read_and_dated_by_its_file(settings: Settings, store: Store):
    settings.ensure_data_dir()
    path = _eml(settings, "Odd date", "Invoice INV-1 for $10.00.", date_header="Fri, 31 Dec 9999 23:30:00 -0100")
    dropped = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    report: dict = {}
    [record] = ingest_folder(store, settings, report=report)
    assert report["failed"] == []
    assert datetime.fromisoformat(record.received_at) == dropped


def test_8bit_text_without_a_character_set_keeps_its_accents(settings: Settings, store: Store):
    settings.ensure_data_dir()
    data = (
        b"From: =?utf-8?q?Jos=C3=A9?= <jose@vendor.com>\r\nTo: ap@co.com\r\n"
        b"Subject: Factura n\xba 4471 \xe9t\xe9\r\nDate: Mon, 21 Sep 2026 10:00:00 +0000\r\nMessage-ID: <a1@vendor.com>\r\n"
        b"MIME-Version: 1.0\r\nContent-Type: text/plain\r\nContent-Transfer-Encoding: 8bit\r\n\r\n"
        b"Montant d\xfb: 1 200,00 EUR. Caf\xe9.\r\n"
    )
    path = settings.inbox_incoming / "factura.eml"
    path.write_bytes(data)
    _age(path)
    [record] = ingest_folder(store, settings)
    assert record.subject == "Factura nº 4471 été"
    assert record.body_text == "Montant dû: 1 200,00 EUR. Café."
    # Declared UTF-8 stays UTF-8.
    assert folder_mail._decode_with("Café".encode(), "utf-8") == "Café"


# An attached email its sender's program encoded in base64 is still read --------------------
def test_an_attached_email_encoded_in_base64_is_read(settings: Settings, store: Store):
    import base64

    settings.ensure_data_dir()
    inner = (
        b"From: Vendor <ar@vendor.com>\r\nSubject: Invoice INV-9001\r\nDate: Mon, 21 Sep 2026 09:00:00 +0000\r\n"
        b"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=YY\r\n\r\n--YY\r\nContent-Type: text/plain\r\n\r\n"
        b"Invoice INV-9001 for $3,400.00 attached.\r\n--YY\r\nContent-Type: text/csv\r\n"
        b"Content-Disposition: attachment; filename=\"inv.csv\"\r\n\r\nitem,amount\r\nwidgets,3400.00\r\n--YY--\r\n"
    )
    outer = (
        b"From: Ann <ann@co.com>\r\nTo: ap@co.com\r\nSubject: FW invoice\r\nDate: Mon, 21 Sep 2026 10:00:00 +0000\r\n"
        b"Message-ID: <fw-b64@co.com>\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=XX\r\n\r\n"
        b"--XX\r\nContent-Type: text/plain\r\n\r\nFYI\r\n--XX\r\nContent-Type: message/rfc822; name=\"Invoice.eml\"\r\n"
        b"Content-Disposition: attachment; filename=\"Invoice.eml\"\r\nContent-Transfer-Encoding: base64\r\n\r\n"
        + base64.encodebytes(inner) + b"\r\n--XX--\r\n"
    )
    path = settings.inbox_incoming / "fw.eml"
    path.write_bytes(outer)
    _age(path)
    [record] = ingest_folder(store, settings)
    files = {att.filename: att.extracted_text for att in record.attachments}
    assert set(files) == {"Invoice.txt", "Invoice › inv.csv"}
    assert "Subject: Invoice INV-9001" in files["Invoice.txt"] and "3400.00" in files["Invoice › inv.csv"]
    assert "INV-9001" in record.extracted.invoice_numbers


# A zip with files CloseDesk doesn't unpack keeps a note of them ------------------------------
def test_a_zip_with_more_files_than_are_unpacked_is_kept_with_a_list_of_the_rest(settings: Settings, store: Store):
    settings.ensure_data_dir()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as packed:
        for number in range(1, 46):
            packed.writestr(f"invoices/INV-{number:04d}.csv", f"invoice,amount\nINV-{number:04d},{number * 100}.00\n")
    path = settings.inbox_incoming / "September AP batch.zip"
    path.write_bytes(archive.getvalue())
    _age(path)
    [record] = ingest_folder(store, settings)
    names = {att.filename: att for att in record.attachments}
    assert len(names) == 41 and "INV-0040.csv" in names and "INV-0041.csv" not in names
    note = names["September AP batch.zip"].extracted_text
    assert "did not unpack" in note and "INV-0041.csv (past the first 40 files)" in note and "INV-0045.csv" in note

    # Dropped again, it adds nothing and stays one email.
    path.write_bytes(archive.getvalue())
    _age(path)
    report: dict = {}
    ingest_folder(store, settings, report=report)
    assert report["already_read"] == 1 and len(store.get_email(record.id).attachments) == 41


def test_a_zip_that_is_unpacked_whole_is_not_kept_beside_its_files(settings: Settings, store: Store):
    settings.ensure_data_dir()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as packed:
        packed.writestr("INV-1.csv", "invoice,amount\nINV-1,100.00\n")
    path = settings.inbox_incoming / "one.zip"
    path.write_bytes(archive.getvalue())
    _age(path)
    [record] = ingest_folder(store, settings)
    assert [att.filename for att in record.attachments] == ["INV-1.csv"]


# MSAL refuses a device sign-in that names one of its own reserved scopes.
def test_graph_device_sign_in_asks_only_for_scopes_msal_accepts(monkeypatch, tmp_path):
    import msal

    from controller_inbox import graph

    assert not set(graph.DELEGATED_SCOPES) & {"openid", "profile", "offline_access"}
    decorate = getattr(msal.ClientApplication, "_decorate_scope", None)
    seen = {}

    class App:
        _exclude_scopes = frozenset()

        def __init__(self, *args, **kwargs):
            pass

        def get_accounts(self):
            return []

        def initiate_device_flow(self, scopes):
            # MSAL's own check, which raised ValueError for "offline_access" before any network call.
            seen["scopes"] = decorate(self, scopes) if decorate else scopes
            return {"user_code": "ABC", "message": "Sign in at https://microsoft.com/devicelogin"}

        def acquire_token_by_device_flow(self, flow):
            return {"access_token": "token"}

    monkeypatch.setattr(graph.msal, "PublicClientApplication", App)
    client = graph.GraphClient(client_id="app-id", cache_path=tmp_path / "token-cache.json")
    assert client.acquire_token(device_code_printer=lambda _message: None) == "token"
    assert "Mail.Read" in seen["scopes"]


NOW_1PM = datetime(2026, 9, 22, 13, 0, tzinfo=timezone.utc)


# A file dropped on its own --------------------------------------------------------------------------
def test_a_loose_dropped_file_has_no_empty_address_or_made_up_vendor(store, settings):
    settings.ensure_data_dir()
    path = settings.inbox_incoming / "Budget notes.txt"
    path.write_text("Budget vs actual for Q3. Travel is over by $1,200.00.", encoding="utf-8")
    _age(path)
    [record] = ingest_folder(store, settings, now=NOW_1PM)
    email = store.get_email(record.id)
    assert "Dropped file" not in email.extracted.vendor_candidates
    client = TestClient(web.create_app(settings, store))
    assert "&lt;&gt;" not in client.get(f"/inbox/{record.id}").text
