"""Mail intake and fraud decisions: repeat fraud attempts, HTML-only bank changes, the model's caution kept on recheck,
tasks kept through the model's reading, one copy per message across sources, winmail.dat files, the first sync's cursor."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from controller_inbox.fraud import reassess_email
from controller_inbox.models import RawAttachment, RawMessage
from controller_inbox.pipeline import process_message
from controller_inbox.reading import overlay_reading

NOW = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)


def _raw(id_, subject, body, *, sender="ap@northwind-supply.com", when=NOW, **kw):
    return RawMessage(
        id=id_, subject=subject, sender_name="Northwind AP", sender_email=sender, received_at=when,
        body_text=body, body_preview=body[:240], has_attachments=False, source="folder", **kw,
    )


def test_recheck_clears_caution_the_model_raised(store, settings):
    """overlay_reading puts "payment_caution" on an email the model read as a bank change; any later fraud recheck
    (another copy dropped, a trust change for the domain) scores model_said_bank_change at 12 < CAUTION_AT and
    reassess_email strips the caution although nothing about the email changed."""
    email = process_message(_raw("m1", "Account update", "Hi, please note our remittance info is in the attached letter."),
                            store, settings, now=NOW)
    overlay_reading(email, {"category": "payment_instruction_change", "importance": "high", "folder": "important",
                            "summary": "Vendor says its remittance details changed."}, now=NOW, tz=settings.tz)
    store.upsert_email(email)
    assert "payment_caution" in store.get_email("m1").flags

    # The same email dropped again, now with the letter it mentions: the reading stays, the file is added and the
    # fraud check runs again (pipeline._add_files -> reassess_email).
    again = _raw("m1", "Account update", "Hi, please note our remittance info is in the attached letter.")
    again.attachments = [RawAttachment("letter.txt", "letter.txt", "text/plain", 20, b"Remittance letter, see above.")]
    again.has_attachments = True
    process_message(again, store, settings, now=NOW)
    flags = store.get_email("m1").flags
    assert "payment_caution" in flags, flags


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


def test_model_reading_drops_the_call_before_paying_task_on_a_bank_change_caution(store, settings):
    """A bank change from a trusted vendor is a caution, not a block, and its only safeguard is the task "Confirm the
    bank-detail change by phone before paying". overlay_reading replaces every task with the model's for any email that
    isn't blocked, so after the overnight read the board says "update the vendor record" and nothing says to call."""
    settings.trusted_domains = "northwind-supply.com"
    email = process_message(
        _raw("m2", "New bank details", "Hello, our bank details have changed. Please update your records and pay "
             "invoice INV-8812 to the new account from now on."),
        store, settings, now=NOW,
    )
    assert "bank_change_caution" in email.flags, email.flags
    assert any("by phone" in a.title for a in email.actions)

    overlay_reading(email, {"category": "ap_invoice", "importance": "high", "folder": "important",
                            "summary": "Northwind changed bank details; update the vendor and pay INV-8812.",
                            "actions": [{"title": "Update Northwind's bank details in the vendor master", "priority": "high"},
                                        {"title": "Pay invoice INV-8812", "priority": "high"}]},
                    now=NOW, tz=settings.tz)
    store.upsert_email(email)
    titles = [a.title for a in store.get_email("m2").actions]
    assert any("by phone" in t for t in titles), titles


def test_outlook_message_without_message_id_synced_and_dropped_is_stored_twice(store, settings):
    """Without a Message-ID, outlook_sync._copy_in names the email by its subject, sender, time and text, but the drop
    folder names the same message by the bytes of its file (folder_mail._message_id). So a message synced from classic
    Outlook and also saved into the drop folder (to bring its attachment along) is filed twice, with two sets of tasks
    and a "possible duplicate invoice" on the copy."""
    from controller_inbox import outlook_sync
    from controller_inbox.folder_mail import ingest_folder
    from msgfactory import write_msg
    from test_outlook_sync import FakeOutlook

    class ExchangeOutlook(FakeOutlook):
        # Internal Exchange mail saved as .msg: no transport headers, so no Message-ID.
        def save_copy(self, entry_id, store_id, dest):
            m = self.messages[entry_id]
            write_msg(dest, m["subject"], f"Hello,\n\n{m['subject']}. Thanks.", sender_email=m["sender"],
                      sender_name="Northwind AP", sent=m["received"], exchange=True)

    outlook = ExchangeOutlook()
    outlook.add("e1", "Invoice INV-7741 from Northwind", message_id="", sender="ap@taz.com")
    outlook_sync.sync(store, settings, outlook, now=NOW)
    assert len(store.list_emails(limit=50)) == 1
    m = outlook.messages["e1"]
    settings.ensure_data_dir()
    write_msg(settings.inbox_incoming / "Invoice INV-7741.msg", m["subject"], f"Hello,\n\n{m['subject']}. Thanks.",
              sender_email=m["sender"], sender_name="Northwind AP", sent=m["received"], exchange=True)
    ingest_folder(store, settings, now=NOW)

    stored = store.list_emails(limit=50)
    assert len(stored) == 1, [(e.id, e.source, e.flags) for e in stored]


def test_refiling_an_email_you_corrected_takes_another_emails_correction(store, settings):
    """pipeline._classify applies the *latest* correction for the sender (learn.match_correction), even to an email
    that carries its own correction. Correct email A to "AP invoice", later correct B from the same sender to
    "Newsletter": the next time A is filed again (teaching a field on it -> teach.refile -> rescore_stored, or a fraud
    recheck) A silently becomes a newsletter and its invoice task goes."""
    from controller_inbox import teach
    from controller_inbox.learn import record_correction
    from controller_inbox.models import DocumentType

    a = process_message(_raw("a1", "October statement of account", "Attached is your statement. Invoice INV-4410 $2,300.00 open."),
                        store, settings, now=NOW)
    b = process_message(_raw("b1", "Northwind news: fall catalog", "Our fall catalog is out.", when=NOW + timedelta(minutes=5)),
                        store, settings, now=NOW)
    record_correction(store, settings, email_id=a.id, corrected_category="ap_invoice", reason="this is an invoice to enter")
    time.sleep(1.1)  # corrections are ordered by created_at, kept to the second
    record_correction(store, settings, email_id=b.id, corrected_category="newsletter", reason="marketing mail")
    assert store.get_email("a1").category == DocumentType.AP_INVOICE

    teach.refile(store, settings, store.get_email("a1"))
    assert store.get_email("a1").category == DocumentType.AP_INVOICE, store.get_email("a1").category


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


def test_bank_change_in_the_html_part_is_not_read_when_the_plain_part_is_a_stub(store, settings):
    """folder_mail._eml_content keeps the HTML part only when there is no text/plain part at all. Mail programs show
    the HTML; a sender (or phishing kit) whose plain alternative is a stub ("View this message in HTML") gets a
    bank-change request past the fraud check: the email is filed with no warning."""
    from email.message import EmailMessage

    from controller_inbox.folder_mail import ingest_folder

    msg = EmailMessage()
    msg["From"] = "Northwind Accounts <accounts@northwind-supply.com>"
    msg["To"] = "ap@taz.com"
    msg["Subject"] = "Payment information"
    msg["Date"] = "Tue, 20 Oct 2026 10:00:00 -0400"
    msg["Message-ID"] = "<pay-info-1@northwind-supply.com>"
    msg.set_content("This message is best viewed in an HTML-capable mail program.")
    msg.add_alternative(
        "<html><body><p>Dear customer,</p><p>Our bank details have changed. Please update your records and send "
        "all future payments to our new account at First Harbor Bank, account 4417 8820 1934.</p></body></html>",
        subtype="html",
    )
    settings.ensure_data_dir()
    (settings.inbox_incoming / "payment information.eml").write_bytes(bytes(msg))
    [email] = ingest_folder(store, settings, now=NOW)
    assert "fraud_risk" in email.flags or "payment_caution" in email.flags, (email.flags, email.body_text)


def _tnef(filename: str, data: bytes) -> bytes:
    """A minimal winmail.dat (TNEF) holding one file, as Outlook sends to Rich Text recipients."""
    import struct

    def attr(level, attr_id, payload):
        return struct.pack("<BII", level, attr_id, len(payload)) + payload + struct.pack("<H", sum(payload) & 0xFFFF)

    out = struct.pack("<IH", 0x223E9F78, 0x0001)
    out += attr(1, 0x00089006, struct.pack("<I", 0x00010000))  # attTnefVersion
    out += attr(2, 0x00069002, b"\x01\x00\x00\x00" + b"\x00" * 10)  # attAttachRendData: an attachment starts
    out += attr(2, 0x00018010, filename.encode("ascii") + b"\x00")  # attAttachTitle
    out += attr(2, 0x0006800F, data)  # attAttachData
    return out


def test_files_inside_winmail_dat_are_never_read(store, settings):
    """Outlook sends mail written in Rich Text with its files packed in winmail.dat (application/ms-tnef). Nothing in
    CloseDesk unpacks it: the invoice PDF inside is never read, its number and amount never extracted, and since the
    email "has an attachment" no missing-attachment warning is raised either."""
    from email.message import EmailMessage

    from controller_inbox.demo import make_pdf
    from controller_inbox.folder_mail import ingest_folder

    msg = EmailMessage()
    msg["From"] = "Northwind AP <ap@northwind-supply.com>"
    msg["Subject"] = "Invoice attached"
    msg["Date"] = "Tue, 20 Oct 2026 10:00:00 -0400"
    msg["Message-ID"] = "<tnef-1@northwind-supply.com>"
    msg.set_content("Please find our invoice attached.")
    pdf = make_pdf([["Northwind Supply", "Invoice INV-7741", "Total due $4,210.00"]])
    msg.add_attachment(_tnef("INV-7741.pdf", pdf), maintype="application", subtype="ms-tnef", filename="winmail.dat")
    settings.ensure_data_dir()
    (settings.inbox_incoming / "invoice.eml").write_bytes(bytes(msg))
    [email] = ingest_folder(store, settings, now=NOW)
    assert "INV-7741" in email.extracted.invoice_numbers, ([a.filename for a in email.attachments], email.extracted.invoice_numbers)


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


def test_second_forged_thread_from_the_same_fraudster_is_only_a_caution(store, settings):
    """A stranger's bank change written below a forged "From:" line counts as their own words (fraud.assess
    ``stranger``) only while ``history`` is 0, and store.sender_history counts every earlier email from the address,
    the blocked ones too (domain_history leaves flagged mail out). So the fraudster's first attempt is blocked, and the
    same attempt sent again a day later is only a caution: it no longer blocks payment."""
    from controller_inbox.fraud import assess, trust_context

    body = (
        "Please see below and action today.\n\n"
        "-----Original Message-----\nFrom: Northwind AP <ap@northwind-supply.com>\nSent: Monday\nSubject: Remittance\n\n"
        "Our bank details have changed. Please send all payments to our new account, routing 021000021 account 99887766."
    )
    first = process_message(_raw("f1", "RE: Remittance", body, sender="ap@northwind-supp1y.com"), store, settings, now=NOW)
    assert "fraud_risk" in first.flags, first.flags
    second = process_message(_raw("f2", "RE: Remittance", body, sender="ap@northwind-supp1y.com", when=NOW + timedelta(days=1)),
                             store, settings, now=NOW + timedelta(days=1))
    assert "fraud_risk" in second.flags, (second.flags, store.fraud_check("f2"))


def test_two_files_with_one_name_synced_from_outlook_each_keep_their_original(store, settings):
    """A vendor sends two invoices both called "Invoice.pdf". Dropped in as a .msg, the second is renamed
    "Invoice (2).pdf"; synced from classic Outlook, both are written to inbox/extracted/<id>/Invoice.pdf, the second
    over the first, so the first has no original: no Preview, no Page, no Download."""
    from controller_inbox import agent
    from controller_inbox.demo import make_pdf
    from msgfactory import PDF
    from test_outlook_sync import FakeOutlook, sync

    outlook = FakeOutlook()
    first = make_pdf([["Invoice INV-7741", "Total due $4,210.00"]])
    second = make_pdf([["Invoice INV-7742", "Total due $980.00"]])
    outlook.add("e1", "Two invoices from Northwind", attachments=[("Invoice.pdf", first, PDF), ("Invoice.pdf", second, PDF)])
    report = sync(store, settings, outlook)
    assert report["new"] == 1 and not report["failed"], report
    [email] = store.list_emails(limit=10)
    assert len(email.attachments) == 2
    originals = [agent.original_file(settings, email, att) for att in email.attachments]
    assert all(path is not None for path in originals), [(a.filename, p) for a, p in zip(email.attachments, originals)]
