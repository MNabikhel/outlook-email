"""Reply chains shaped by someone trying their luck: the order mail is read in, Thread-Index-only threads,
Microsoft 365 mail with only its conversation, a resend with changed bank details, a held email's long text. The
vendor's own invoice keeps its "Code and enter" task and a lookalike's copy is flagged, whatever the order
(pipeline._repeats, _entered_elsewhere, _settle_conversation; links.new_domains)."""

from __future__ import annotations

import base64
import time
from datetime import timedelta
from email.message import EmailMessage
from email.utils import format_datetime

import pytest

from controller_inbox import links
from controller_inbox.demo import make_pdf
from controller_inbox.folder_mail import ingest_folder
from controller_inbox.models import RawAttachment, RawMessage
from controller_inbox.pipeline import ingest_mailbox, process_message
from test_links import START, _invoice_tasks

PDF = make_pdf([["Coastal Print Co", "Invoice CP-5521", "Total due $1,840.00"]])
VENDOR_TASK = ("Invoice CP-5521", "Code and enter invoice CP-5521 ($1,840.00)")
THREAD_INDEX = base64.b64encode(bytes([1]) + bytes(5) + bytes(range(40, 56)) + bytes(5)).decode()
TEXT_INVOICE = "Invoice CP-5521 for $1,840.00 is due November 30. Thank you."


def _eml(settings, name, subject, *, sender, mid, to="AP Team <ap@taz.com>", reply_to_mid="", refs="", hours=0.0, files=(), body="x", thread_index=""):
    message = EmailMessage()
    message["From"], message["To"], message["Cc"] = sender, to, "maya@taz.com"
    message["Subject"], message["Date"], message["Message-ID"] = subject, format_datetime(START + timedelta(hours=hours)), mid
    if reply_to_mid:
        message["In-Reply-To"] = reply_to_mid
    if refs:
        message["References"] = refs
    if thread_index:
        message["Thread-Index"] = thread_index
    message.set_content(body)
    for filename, data in files:
        message.add_attachment(data, maintype="application", subtype="octet-stream", filename=filename)
    settings.ensure_data_dir()
    (settings.inbox_incoming / name).write_bytes(bytes(message))


def _flagged(store) -> dict[str, bool]:
    return {email.sender_email: "duplicate_invoice" in store.get_email(email.id).flags for email in store.list_emails(limit=-1)}


@pytest.mark.parametrize("lookalike", ["coastalprlnt.com", "cpco-billing.com"])
@pytest.mark.parametrize("names", [("1-vendor.eml", "2-other.eml"), ("2-vendor.eml", "1-other.eml")])
def test_a_resent_pdf_from_another_domain_is_flagged_and_takes_no_task_in_either_order(store, settings, lookalike, names):
    _eml(settings, names[0], "Invoice CP-5521", sender="Coastal Print AR <ar@coastalprint.com>", mid="<cp1@coastalprint.com>", body=TEXT_INVOICE)
    _eml(settings, names[1], "RE: Invoice CP-5521", sender=f"Coastal Print AR <ar@{lookalike}>", mid=f"<cp9@{lookalike}>",
         to="ap@taz.com, ar@coastalprint.com", reply_to_mid="<cp1@coastalprint.com>", refs="<cp1@coastalprint.com>", hours=3,
         files=[("CP-5521.pdf", PDF)], body="Resending invoice CP-5521 as a PDF.")
    ingest_folder(store, settings, now=START + timedelta(hours=4))
    tasks = _invoice_tasks(store)
    assert VENDOR_TASK in tasks, tasks
    assert _flagged(store)[f"ar@{lookalike}"], "the other domain's copy is a possible duplicate"


@pytest.mark.parametrize("thread_index", [True, False])
def test_two_emails_answering_each_other_dont_start_a_thread_they_joined(store, settings, thread_index):
    _eml(settings, "1-vendor.eml", "Invoice CP-5521", sender="Coastal Print AR <ar@coastalprint.com>", mid="<cp1@coastalprint.com>",
         body=TEXT_INVOICE, thread_index=THREAD_INDEX)
    ingest_folder(store, settings, now=START + timedelta(hours=1))
    _eml(settings, "2-a.eml", "RE: Invoice CP-5521", sender="Coastal Print Accounts <accounts@cpco-billing.com>", mid="<a1@cpco-billing.com>",
         to="ap@taz.com, ar@coastalprint.com", hours=2, thread_index=THREAD_INDEX if thread_index else "", body="Following up on invoice CP-5521.")
    ingest_folder(store, settings, now=START + timedelta(hours=2.5))
    _eml(settings, "3-b.eml", "RE: Invoice CP-5521", sender="Coastal Print Accounts <accounts@cpco-billing.com>", mid="<b1@cpco-billing.com>",
         to="ap@taz.com, ar@coastalprint.com", reply_to_mid="<a1@cpco-billing.com>", refs="<a1@cpco-billing.com>", hours=3,
         thread_index=THREAD_INDEX, files=[("CP-5521.pdf", PDF)], body="Resending invoice CP-5521 as a PDF.")
    ingest_folder(store, settings, now=START + timedelta(hours=4))
    assert VENDOR_TASK in _invoice_tasks(store)
    vendor = next(email for email in store.list_emails(limit=-1) if email.sender_email == "ar@coastalprint.com")
    marked = links.related(store, store.get_email(vendor.id))["new_domains"]
    assert vendor.id not in marked and marked, "the outsiders are new to the vendor's conversation, not the vendor"


def _raw(id_, sender, subject, body, hours, to, files=()):
    return RawMessage(id=id_, subject=subject, sender_name="", sender_email=sender, received_at=START + timedelta(hours=hours), body_text=body,
                      body_preview=body, has_attachments=bool(files), conversation_id="conv-1", internet_message_id=f"<{id_}@x>",
                      threading={"to": to}, attachments=list(files), source="graph")


class _NewestFirst:
    """A mailbox listing its mail newest first, as Microsoft 365 does."""

    def __init__(self, messages):
        self.messages = messages

    def list_messages(self, received_after=None):
        return sorted(self.messages, key=lambda m: m.received_at, reverse=True)

    def get_attachments(self, _message_id):
        return []


def test_one_microsoft_365_sync_reads_its_mail_oldest_first(store, settings):
    vendor = _raw("R", "ar@coastalprint.com", "Invoice CP-5521", TEXT_INVOICE, 0, ["ap@taz.com"])
    other = _raw("L", "accounts@cpco-billing.com", "RE: Invoice CP-5521", "Resending invoice CP-5521 as a PDF.", 2, ["ap@taz.com", "ar@coastalprint.com"],
                 [RawAttachment(id="a", filename="CP-5521.pdf", content_type="application/pdf", size_bytes=len(PDF), content=PDF)])
    ingest_mailbox(_NewestFirst([vendor, other]), store, settings, now=START + timedelta(hours=8), cursor=None)
    assert any(action.source == "invoice" for action in store.get_email("R").actions), "the vendor's invoice keeps its task"
    assert "duplicate_invoice" in store.get_email("L").flags
    assert links.related(store, store.get_email("R"))["new_domains"] == {"L"}


def test_a_resend_from_the_vendors_own_domain_with_new_bank_details_is_a_duplicate(store, settings):
    pdf = make_pdf([["Coastal Print Co", "Invoice CP-5521", "Total due $1,840.00", "Please note our bank details have changed. New account 55512345, routing 021000021"]])
    _eml(settings, "1-vendor.eml", "Invoice CP-5521", sender="Coastal Print AR <ar@coastalprint.com>", mid="<cp1@coastalprint.com>", body=TEXT_INVOICE)
    ingest_folder(store, settings, now=START + timedelta(hours=1))
    _eml(settings, "2-v.eml", "RE: Invoice CP-5521", sender="Coastal Print AR <ar@coastalprint.com>", mid="<cp2@coastalprint.com>",
         reply_to_mid="<cp1@coastalprint.com>", refs="<cp1@coastalprint.com>", hours=3, files=[("CP-5521.pdf", pdf)], body="Resending invoice CP-5521 as a PDF.")
    ingest_folder(store, settings, now=START + timedelta(hours=4))
    resend = next(email for email in store.list_emails(limit=-1) if email.subject.startswith("RE:"))
    flags = store.get_email(resend.id).flags
    assert "payment_caution" in flags and "duplicate_invoice" in flags, flags
    assert VENDOR_TASK in _invoice_tasks(store), "a cautioned resend never takes the task off the first invoice"


def test_a_long_references_header_keeps_the_conversations_first_email():
    refs = " ".join(f"<m{k}@x>" for k in range(40))
    assert links.threading(references=refs)["references"] == ["m0@x", *[f"m{k}@x" for k in range(21, 40)]]


def test_a_held_emails_long_text_is_read_in_a_moment(store, settings):
    body = "URGENT: our bank details have changed, wire the payment for invoice 4471 to account 55512345 routing 021000021 today. " + "x" * 49000
    started = time.monotonic()
    process_message(_raw("H", "ceo@taz-c0rp.com", "Wire today", body, 0, ["ap@taz.com"]), store, settings, now=START + timedelta(hours=1))
    assert time.monotonic() - started < 5


def test_replies_dropped_before_the_invoice_settle_once_it_arrives(store, settings):
    """A lookalike's "updated bank details" and a colleague's "send the PO" are dropped first; the invoice and the
    vendor's reply come in the next batch. One "Code and enter", on the invoice; it is no duplicate."""
    _eml(settings, "a.eml", "RE: Invoice CP-5521", sender="Coastal Print AR <ar@coastalprint-billing.com>", mid="<x1@coastalprint-billing.com>",
         reply_to_mid="<cp1@coastalprint.com>", refs="<cp1@coastalprint.com>", hours=30,
         body="Our bank details have changed. Please pay invoice CP-5521 to account 55512345, routing 021000021, today.")
    _eml(settings, "b.eml", "RE: Invoice CP-5521", sender="Maya Chen <maya@taz.com>", mid="<m1@taz.com>",
         reply_to_mid="<cp1@coastalprint.com>", refs="<cp1@coastalprint.com>", hours=2, body="Can you send the PO for invoice CP-5521?")
    ingest_folder(store, settings, now=START + timedelta(hours=31))
    _eml(settings, "c.eml", "Invoice CP-5521", sender="Coastal Print AR <ar@coastalprint.com>", mid="<cp1@coastalprint.com>",
         files=[("CP-5521.pdf", PDF)], body="Please find invoice CP-5521 for $1,840.00 attached, due November 30.")
    _eml(settings, "d.eml", "RE: Invoice CP-5521", sender="Coastal Print AR <ar@coastalprint.com>", mid="<cp3@coastalprint.com>",
         reply_to_mid="<m1@taz.com>", refs="<cp1@coastalprint.com> <m1@taz.com>", hours=4, body="Invoice CP-5521 is for PO-88012.")
    ingest_folder(store, settings, now=START + timedelta(hours=32))
    by_sender = {email.sender_email: store.get_email(email.id) for email in store.list_emails(limit=-1) if email.subject == "Invoice CP-5521"}
    invoice = by_sender["ar@coastalprint.com"]
    assert any(action.source == "invoice" for action in invoice.actions) and "duplicate_invoice" not in invoice.flags, (invoice.flags, invoice.actions)
    colleague = next(store.get_email(e.id) for e in store.list_emails(limit=-1) if e.sender_email == "maya@taz.com")
    assert not any(action.source == "invoice" and action.status.value == "open" for action in colleague.actions)


def test_a_reply_deep_in_a_long_invoice_thread_is_no_duplicate_and_is_read_quickly(store, settings):
    """A thread of hundreds of replies about one invoice: a new reply is no duplicate of it, and reading it doesn't
    walk the thread once per reply."""
    from test_links import _record

    first = _record("e000", sender="ar@coastalprint.com", mid="<m0@coastalprint.com>", invoices=["CP-5521"],
                    threading={"to": ["ap@taz.com"]}, received="2026-10-01T09:00:00+00:00")
    first.created_at = "2026-10-01T09:00:00+00:00"
    store.upsert_email(first)
    for n in range(1, 300):
        sender = "ap@taz.com" if n % 2 else "ar@coastalprint.com"
        record = _record(f"e{n:03d}", sender=sender, mid=f"<m{n}@x>", invoices=["CP-5521"], received=f"2026-10-{1 + n // 24:02d}T{n % 24:02d}:00:00+00:00",
                         threading={"in_reply_to": f"m{n - 1}@x" if n > 1 else "m0@coastalprint.com", "references": ["m0@coastalprint.com", *[f"m{k}@x" for k in range(max(1, n - 19), n)]],
                                    "to": ["ar@coastalprint.com" if sender == "ap@taz.com" else "ap@taz.com"]})
        record.created_at = record.received_at
        store.upsert_email(record)
    reply = RawMessage(id="new", subject="RE: Invoice CP-5521", sender_name="", sender_email="ap@taz.com", received_at=START + timedelta(days=20),
                       body_text="Any news on invoice CP-5521?", body_preview="", has_attachments=False, source="folder",
                       internet_message_id="<new@taz.com>", threading={"in_reply_to": "m299@x", "references": ["m0@coastalprint.com", *[f"m{k}@x" for k in range(281, 300)]],
                                                                         "to": ["ar@coastalprint.com"]})
    started = time.monotonic()
    record = process_message(reply, store, settings, now=START + timedelta(days=20))
    assert time.monotonic() - started < 3
    assert "duplicate_invoice" not in record.flags


def test_at_a_free_mail_service_a_new_address_is_new_to_the_conversation():
    vendor = _raw("v", "coastalprint.ar@gmail.com", "Invoice CP-5521", TEXT_INVOICE, 0, ["ap@taz.com"])
    other = _raw("o", "coastalprint.billing@gmail.com", "RE: Invoice CP-5521", "Resending invoice CP-5521 as a PDF.", 2, ["ap@taz.com"])
    vendor.created_at, other.created_at = "2026-10-05T14:00:00+00:00", "2026-10-05T16:00:00+00:00"
    assert links.new_domains([vendor, other]) == {"o"}


def test_a_reply_quoting_please_find_attached_is_missing_nothing(store, settings):
    body = "Can you send the PO for this one?\n\n-----Original Message-----\nFrom: Coastal Print AR\nPlease find attached invoice CP-5521."
    record = process_message(_raw("q", "maya@taz.com", "RE: Invoice CP-5521", body, 1, ["ar@coastalprint.com"]), store, settings, now=START + timedelta(hours=2))
    assert "missing_attachment" not in record.flags
    forgot = process_message(_raw("f", "ar@coastalprint.com", "Invoice CP-5522", "Please find attached invoice CP-5522.", 1, ["ap@taz.com"]),
                             store, settings, now=START + timedelta(hours=2))
    assert "missing_attachment" in forgot.flags


@pytest.mark.parametrize(("text", "number"), [("Invoice NW-88001 is due", "NW-88001"), ("Invoice ON-7731 attached", "ON-7731"),
                                              ("Invoice No. 12345", "12345"), ("Invoice #: INV-2024-0457", "INV-2024-0457")])
def test_an_invoice_number_keeps_its_own_letters(text, number):
    from controller_inbox.extract import INVOICE_RE

    assert INVOICE_RE.search(text).group(1) == number


def test_a_po_number_keeps_its_own_letters():
    from controller_inbox.extract import PO_RE

    assert PO_RE.search("PO NX-4410 approved").group(1) == "NX-4410" and PO_RE.search("PO No. 4500123").group(1) == "4500123"


# Guards against: once the separators before a number took no letters, a label word after a separator was no
# longer read: "Invoice-No. 12345" (common on German vendors' English invoices), "Invoice: No. 4711" and
# "PO-No. 77821" gave no number at all, and "Invoice No123456" gave "NO123456", which isn't the vendor's 123456.
@pytest.mark.parametrize(("text", "invoice", "po"), [
    ("Invoice-No. 12345", ["12345"], []), ("Invoice-No.: 2024117", ["2024117"], []), ("Invoice: No. 4711", ["4711"], []),
    ("Invoice - No. 4711", ["4711"], []), ("Invoice #No. 4711", ["4711"], []), ("Invoice N. 4433", ["4433"], []),
    ("Invoice No123456", ["123456"], []), ("Invoice Number: NO-7788", ["NO-7788"], []), ("Invoice NO-7788 attached", ["NO-7788"], []),
    ("Invoice NW-88001 is due", ["NW-88001"], []), ("Invoice ON-7731 attached", ["ON-7731"], []),
    ("PO-No. 77821", [], ["77821"]), ("PO: No. 77821", [], ["77821"]), ("Purchase Order: No. 4500123", [], ["4500123"]),
    ("PO No4500123", [], ["4500123"]), ("PO NO-4567 approved", [], ["NO-4567"]),
])
def test_a_label_word_after_a_separator_is_still_read(text, invoice, po):
    from datetime import date

    from controller_inbox.extract import extract_fields

    fields = extract_fields(text, as_of=date(2026, 11, 1))
    assert (fields.invoice_numbers, fields.po_numbers) == (invoice, po)
