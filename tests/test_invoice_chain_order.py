"""An invoice's conversation gives the same board whatever order its mail is read in (the drop folder reads files
by name, Microsoft 365 by date, and mail is read again or refiled later): one "Code and enter" for the invoice, a
duplicate flag only on a second copy of it, and replies that bring no invoice file never make the invoice a
duplicate (pipeline._repeats, _entered_elsewhere, _settle_conversation, rescore_stored)."""

from __future__ import annotations

from datetime import timedelta
from email.message import EmailMessage
from email.utils import format_datetime

import pytest

from controller_inbox import pipeline
from controller_inbox.demo import make_pdf
from controller_inbox.folder_mail import ingest_folder
from controller_inbox.fraud import assess_email, record_fraud_verdict, trust_context
from controller_inbox.models import RawAttachment, RawMessage
from controller_inbox.pipeline import process_message
from test_links import START

PDF = make_pdf([["Coastal Print Co", "Invoice CP-5521", "Total due $1,840.00", "Due November 30, 2026"]])
INVOICE_BODY = "Please find invoice CP-5521 for $1,840.00 attached, due November 30."
BOTH_ORDERS = [("1-invoice.eml", "2-reply.eml"), ("2-invoice.eml", "1-reply.eml")]


def _mail(settings, name, subject, *, mid, sender="Coastal Print AR <ar@coastalprint.com>", to="AP Team <ap@taz.com>", cc="maya@taz.com",
          reply_to="", reply_to_mid="", refs="", hours=0.0, files=(), body="See below."):
    message = EmailMessage()
    message["From"], message["To"] = sender, to
    if cc:
        message["Cc"] = cc
    if reply_to:
        message["Reply-To"] = reply_to
    message["Subject"], message["Date"], message["Message-ID"] = subject, format_datetime(START + timedelta(hours=hours)), mid
    if reply_to_mid:
        message["In-Reply-To"] = reply_to_mid
    if refs:
        message["References"] = refs
    message.set_content(body)
    for filename, data in files:
        message.add_attachment(data, maintype="application", subtype="pdf", filename=filename)
    settings.ensure_data_dir()
    (settings.inbox_incoming / name).write_bytes(bytes(message))


def _invoice(settings, name, **kw):
    kw.setdefault("files", [("CP-5521.pdf", PDF)])
    kw.setdefault("body", INVOICE_BODY)
    _mail(settings, name, kw.pop("subject", "Invoice CP-5521"), mid=kw.pop("mid", "<cp1@coastalprint.com>"), **kw)


def _reply(settings, name, *, sender, body, mid="<r1@x.com>", hours=2.0, subject="RE: Invoice CP-5521", **kw):
    kw.setdefault("reply_to_mid", "<cp1@coastalprint.com>")
    kw.setdefault("refs", "<cp1@coastalprint.com>")
    _mail(settings, name, subject, sender=sender, mid=mid, hours=hours, body=body, **kw)


def _emails(store):
    return [store.get_email(email.id) for email in store.list_emails(limit=-1)]


def _by_sender(store, sender):
    return next(email for email in _emails(store) if email.sender_email == sender)


def _open(store, prefix):
    """The open tasks whose title starts with ``prefix``, as (sender, title)."""
    return sorted((email.sender_email, action.title) for email in _emails(store) for action in email.actions
                  if action.title.startswith(prefix) and action.status.value == "open")


def _entry(store):
    return _open(store, "Code and enter")


def _refile(store, settings, email, now):
    return pipeline.rescore_stored(store, settings, email, assess_email(store, trust_context(store, settings), email), now=now)


# A reply with no invoice file is no copy of the invoice, whoever wrote it and whichever is read first.
@pytest.mark.parametrize("names", BOTH_ORDERS)
@pytest.mark.parametrize("sender", ["Coastal Print AR <ar@coastalprint-billing.com>", "Bob <bob@taz.com>"])
def test_a_reply_without_the_file_never_makes_the_invoice_a_duplicate(store, settings, names, sender):
    _invoice(settings, names[0])
    _reply(settings, names[1], sender=sender, to="maya@taz.com", cc="", body="Following up on invoice CP-5521. When will it be paid?")
    ingest_folder(store, settings, now=START + timedelta(hours=5))
    invoice = _by_sender(store, "ar@coastalprint.com")
    assert "duplicate_invoice" not in invoice.flags
    assert not [action for action in invoice.actions if action.source == "duplicate_rule"]


# A reply from outside the invoice's side (an uncaught lookalike) keeps its own "Code and enter", so it is compared
# with the invoice as any copy is: flagged, in either order.
@pytest.mark.parametrize("names", BOTH_ORDERS)
def test_a_reply_from_outside_the_invoices_side_that_keeps_its_task_is_a_duplicate(store, settings, names):
    _invoice(settings, names[0])
    _reply(settings, names[1], sender="Coastal Print AR <ar@coastalprlnt.com>", hours=3, body="Please pay invoice CP-5521 for $1,840.00 today.")
    ingest_folder(store, settings, now=START + timedelta(hours=5))
    assert _entry(store) == [("ar@coastalprint.com", "Code and enter invoice CP-5521 ($1,840.00)"),
                             ("ar@coastalprlnt.com", "Code and enter invoice CP-5521 ($1,840.00)")]
    assert "duplicate_invoice" in _by_sender(store, "ar@coastalprlnt.com").flags
    assert "duplicate_invoice" not in _by_sender(store, "ar@coastalprint.com").flags
    assert _open(store, "Review possible duplicate") == [("ar@coastalprlnt.com", "Review possible duplicate invoice CP-5521")]


# Read again or refiled, a reply keeps the board the invoice read after it settled.
@pytest.mark.parametrize("again", ["read", "refile"])
def test_a_reply_read_again_after_its_invoice_gets_no_second_entry_task(store, settings, again):
    _reply(settings, "a.eml", sender="Maya Chen <maya@taz.com>", to="ar@coastalprint.com", cc="ap@taz.com", body="Which PO is invoice CP-5521 for?")
    ingest_folder(store, settings, now=START + timedelta(hours=5))
    _invoice(settings, "b.eml")
    ingest_folder(store, settings, now=START + timedelta(hours=6))
    assert _entry(store) == [("ar@coastalprint.com", "Code and enter invoice CP-5521 ($1,840.00)")]
    reply = _by_sender(store, "maya@taz.com")
    if again == "read":
        raw = RawMessage(id=reply.id, subject=reply.subject, sender_name="Maya Chen", sender_email=reply.sender_email,
                         received_at=START + timedelta(hours=2), body_text=reply.body_text, body_preview="", has_attachments=False,
                         internet_message_id=reply.internet_message_id, threading=reply.threading, source="graph")
        process_message(raw, store, settings, now=START + timedelta(hours=7))
    else:
        _refile(store, settings, reply, START + timedelta(hours=7))
    assert _entry(store) == [("ar@coastalprint.com", "Code and enter invoice CP-5521 ($1,840.00)")]


# A held invoice the user clears is what it would have been had it been read as it is now.
def _maya_asks(settings, name):
    _reply(settings, name, sender="Maya Chen <maya@taz.com>", to="ar@coastalprint.com", cc="ap@taz.com",
           body="Which PO is invoice CP-5521 for? I can't find it.\n\n> Please find invoice CP-5521 for $1,840.00 attached.")


def test_a_held_invoice_cleared_but_still_cautioned_is_no_duplicate_of_the_question_about_it(store, settings):
    _invoice(settings, "1-invoice.eml", body=INVOICE_BODY + " Please note our new bank details: routing 021000021 account 123456789,"
             " update your records before paying.")
    _maya_asks(settings, "2-reply.eml")
    ingest_folder(store, settings, now=START + timedelta(hours=5))
    invoice = _by_sender(store, "ar@coastalprint.com")
    assert "fraud_risk" in invoice.flags
    assert [sender for sender, _ in _entry(store)] == ["maya@taz.com"], "the reply about a held invoice keeps its own task"
    record_fraud_verdict(store, settings, invoice.id, verdict="safe", now=START + timedelta(hours=6))
    invoice = store.get_email(invoice.id)
    assert "payment_caution" in invoice.flags and "duplicate_invoice" not in invoice.flags, invoice.flags
    assert not _open(store, "Review possible duplicate")
    # Still cautioned (its bank details changed), it lets nobody off, as when read so: each keeps its own task.
    assert [sender for sender, _ in _entry(store)] == ["ar@coastalprint.com", "maya@taz.com"]


def test_an_invoice_cleared_after_the_user_held_it_takes_the_entry_task_off_the_reply(store, settings):
    _invoice(settings, "1-invoice.eml")
    ingest_folder(store, settings, now=START + timedelta(hours=1))
    invoice = _by_sender(store, "ar@coastalprint.com")
    record_fraud_verdict(store, settings, invoice.id, verdict="fraud", now=START + timedelta(hours=1))
    _maya_asks(settings, "2-reply.eml")
    ingest_folder(store, settings, now=START + timedelta(hours=5))
    assert [sender for sender, _ in _entry(store)] == ["maya@taz.com"], "the reply about a held invoice keeps its own task"
    record_fraud_verdict(store, settings, invoice.id, verdict="safe", now=START + timedelta(hours=6))
    assert [sender for sender, _ in _entry(store)] == ["ar@coastalprint.com"]
    assert not _open(store, "Review possible duplicate")


# Microsoft 365 gives no reply headers: a tagged or translated "RE:" is a reply all the same.
@pytest.mark.parametrize("subject", ["[EXTERNAL] RE: Invoice CP-5521", "EXT: RE: Invoice CP-5521", "Automatic reply: RE: Invoice CP-5521",
                                     "RES: Invoice CP-5521", "RV: Invoice CP-5521", "Antw: Invoice CP-5521"])
def test_a_tagged_or_translated_reply_from_microsoft_365_is_a_reply(store, settings, subject):
    def raw(id_, subject, sender, hours, to, body, files=()):
        return RawMessage(id=id_, subject=subject, sender_name="", sender_email=sender, received_at=START + timedelta(hours=hours), body_text=body,
                          body_preview=body, has_attachments=bool(files), conversation_id="C1", internet_message_id=f"<{id_}@x>",
                          threading={"to": list(to)}, attachments=list(files), source="graph")

    pdf = RawAttachment(id="a", filename="CP-5521.pdf", content_type="application/pdf", size_bytes=len(PDF), content=PDF)
    now = START + timedelta(hours=10)
    process_message(raw("g1", "Invoice CP-5521", "ar@coastalprint.com", 0, ["ap@taz.com", "maya@taz.com"], INVOICE_BODY, [pdf]), store, settings, now=now)
    process_message(raw("g2", "RE: Invoice CP-5521", "maya@taz.com", 2, ["ar@coastalprint.com"], "Which PO is invoice CP-5521 for?"), store, settings, now=now)
    answer = process_message(raw("g3", subject, "ar@coastalprint.com", 4, ["maya@taz.com", "ap@taz.com"], "Invoice CP-5521 is for PO-88012."),
                             store, settings, now=now)
    assert "duplicate_invoice" not in answer.flags
    assert not [action for action in answer.actions if action.source in {"invoice", "duplicate_rule"}], answer.actions


@pytest.mark.parametrize(("subject", "reply"), [
    ("RE: Invoice", True), ("[EXTERNAL] Re: Invoice", True), ("EXT: FW: Invoice", True), ("Automatic reply: Invoice", True),
    ("SV: Faktura", True), ("ODP: Faktura", True), ("Invoice CP-5521", False), ("Re-issued invoice CP-5521", False),
    ("Remittance: invoice CP-5521", False), ("Invoice: CP-5521", False), ("[EXTERNAL] Invoice CP-5521", False),
])
def test_what_subject_says_reply(subject, reply):
    assert pipeline._replying(type("Mail", (), {"subject": subject, "threading": {}})()) is reply


# The invoice number only in the PDF: the colleague's question about it is no second invoice to enter.
@pytest.mark.parametrize("names", BOTH_ORDERS)
def test_a_reply_naming_no_invoice_number_gets_no_entry_task(store, settings, names):
    _invoice(settings, names[0], subject="Invoice from Coastal Print", body="Please find our invoice attached, due November 30.")
    _reply(settings, names[1], subject="RE: Invoice from Coastal Print", sender="Maya Chen <maya@taz.com>", to="ar@coastalprint.com",
           cc="ap@taz.com", body="Which PO is this invoice for? I can't find it.\n\n> Please find our invoice attached.")
    ingest_folder(store, settings, now=START + timedelta(hours=10))
    assert [sender for sender, _ in _entry(store)] == ["ar@coastalprint.com"], _entry(store)


# The vendor bills through a service (QuickBooks) and answers from its own address, the invoice's Reply-To.
@pytest.mark.parametrize("names", BOTH_ORDERS)
def test_the_vendor_answering_from_the_invoices_reply_to_gets_no_entry_task(store, settings, names):
    _invoice(settings, names[0], sender="Coastal Print Co <quickbooks@notification.intuit.com>", reply_to="ar@coastalprint.com",
             mid="<qb1@intuit.com>", subject="Invoice CP-5521 from Coastal Print Co")
    _reply(settings, names[1], subject="RE: Invoice CP-5521 from Coastal Print Co", sender="Coastal Print AR <ar@coastalprint.com>",
           to="maya@taz.com", cc="ap@taz.com", reply_to_mid="<qb1@intuit.com>", refs="<qb1@intuit.com>", body="Invoice CP-5521 is for PO-88012.")
    ingest_folder(store, settings, now=START + timedelta(hours=10))
    assert [sender for sender, _ in _entry(store)] == ["quickbooks@notification.intuit.com"], _entry(store)
    assert not _open(store, "Review possible duplicate")


# A statement listing several invoices is no copy of any of them, PDF or spreadsheet.
@pytest.mark.parametrize("names", [("1-statement.eml", "2-invoice.eml"), ("2-statement.eml", "1-invoice.eml")])
def test_a_statement_listing_the_invoice_makes_it_no_duplicate(store, settings, names):
    statement = make_pdf([["Coastal Print Co", "Statement of Account", "As of October 31, 2026", "Invoice CP-5519   Oct 02, 2026   $500.00",
                           "Invoice CP-5520   Oct 09, 2026   $700.00", "Invoice CP-5521   Oct 16, 2026   $1,840.00", "Balance due $3,040.00"]])
    _mail(settings, names[0], "Statement of account - October", mid="<st1@coastalprint.com>", files=[("Statement October.pdf", statement)],
          body="Please find your statement of account attached.")
    _invoice(settings, names[1], hours=24)
    ingest_folder(store, settings, now=START + timedelta(days=3))
    invoice = next(email for email in _emails(store) if email.subject == "Invoice CP-5521")
    assert "duplicate_invoice" not in invoice.flags
    assert not _open(store, "Review possible duplicate")


# A lookalike's copy, read first, marked as fraud later: the real invoice is no duplicate of held mail.
@pytest.mark.parametrize("names", [("1-invoice.eml", "2-fake.eml"), ("2-invoice.eml", "1-fake.eml")])
def test_marking_a_lookalikes_copy_as_fraud_clears_the_real_invoices_duplicate_flag(store, settings, names):
    _invoice(settings, names[0])
    _mail(settings, names[1], "Invoice CP-5521", sender="Coastal Print AR <ar@coastalprlnt.com>", mid="<cp9@coastalprlnt.com>", hours=3,
          files=[("CP-5521 corrected.pdf", PDF + b" ")], body="Please use this corrected copy of invoice CP-5521.")
    ingest_folder(store, settings, now=START + timedelta(hours=10))
    fake = _by_sender(store, "ar@coastalprlnt.com")
    record_fraud_verdict(store, settings, fake.id, verdict="fraud", now=START + timedelta(hours=11))
    real = _by_sender(store, "ar@coastalprint.com")
    assert "duplicate_invoice" not in real.flags
    assert not [action for action in real.actions if action.title.startswith("Review possible duplicate") and action.status.value == "open"]


# A reply bringing the file again is the copy, whichever is read first; the vendor's original is not.
@pytest.mark.parametrize("names", [("Invoice CP-5521.eml", "RE x.eml"), ("Invoice CP-5521.eml", "A RE.eml")])
@pytest.mark.parametrize(("prefix", "sender"), [("FW", "Maya Chen <maya@taz.com>"), ("RE", "Coastal Print AR <ar@coastalprint.com>")])
def test_a_reply_bringing_the_file_again_is_the_duplicate_in_either_order(store, settings, names, prefix, sender):
    _invoice(settings, names[0])
    _reply(settings, names[1], subject=f"{prefix}: Invoice CP-5521", sender=sender, mid="<cp2@x.com>", hours=3, files=[("CP-5521.pdf", PDF)],
           body="Resending invoice CP-5521 as requested.")
    ingest_folder(store, settings, now=START + timedelta(hours=4))
    by_subject = {email.subject: email for email in _emails(store)}
    assert "duplicate_invoice" not in by_subject["Invoice CP-5521"].flags
    assert "duplicate_invoice" in by_subject[f"{prefix}: Invoice CP-5521"].flags


# Someone at the vendor, or at the company behind a shared mailbox, answers about the invoice: no second entry task.
@pytest.mark.parametrize("names", BOTH_ORDERS)
@pytest.mark.parametrize(("invoice_from", "invoice_to", "replier", "reply_to"), [
    ("Coastal Print Billing <billing@coastalprint.com>", "AP Team <ap@taz.com>", "Dana <dana@coastalprint.com>", "ap@taz.com"),
    ("Coastal Print AR <ar@coastalprint.com>", "Accounts Payable <invoices@taz.com>", "Lee <lee@taz.com>", "ar@coastalprint.com, invoices@taz.com"),
])
def test_a_colleague_of_the_sender_or_of_a_recipient_answering_gets_no_entry_task(store, settings, names, invoice_from, invoice_to, replier, reply_to):
    _invoice(settings, names[0], sender=invoice_from, to=invoice_to)
    _reply(settings, names[1], sender=replier, to=reply_to, cc="", body="Invoice CP-5521: which PO is it for?")
    ingest_folder(store, settings, now=START + timedelta(hours=6))
    sender = invoice_from.split("<")[1].rstrip(">")
    assert [who for who, _ in _entry(store)] == [sender], _entry(store)
    assert "duplicate_invoice" not in _by_sender(store, sender).flags


# The vendor's next invoice, written in the first one's thread, keeps its own task whichever is read first.
@pytest.mark.parametrize("names", BOTH_ORDERS)
def test_a_reply_about_another_invoice_keeps_its_own_entry_task(store, settings, names):
    _invoice(settings, names[0])
    _reply(settings, names[1], subject="RE: Invoice CP-5522", sender="Coastal Print AR <ar@coastalprint.com>", hours=30,
           body="Invoice CP-5522 for $900.00 is due December 1. Invoice CP-5521 is paid, thank you.")
    ingest_folder(store, settings, now=START + timedelta(hours=40))
    assert ("ar@coastalprint.com", "Code and enter invoice CP-5522 ($900.00)") in _entry(store), _entry(store)


# A cautioned reply about the invoice is judged the same whichever is read first: never let off, and a duplicate.
@pytest.mark.parametrize("names", BOTH_ORDERS)
def test_a_cautioned_reply_keeps_its_task_and_is_a_duplicate_in_either_order(store, settings, names):
    settings.trusted_domains = "coastalprint.com"
    _invoice(settings, names[0])
    _reply(settings, names[1], sender="Coastal Print AR <ar@coastalprint.com>", hours=3,
           body="About invoice CP-5521 for $1,840.00: please note our bank account has changed. Please update our bank details and pay the new account.")
    ingest_folder(store, settings, now=START + timedelta(hours=10))
    reply = next(email for email in _emails(store) if email.subject.startswith("RE:"))
    assert "payment_caution" in reply.flags
    assert "duplicate_invoice" in reply.flags
    assert any(action.source == "invoice" and action.status.value == "open" for action in reply.actions)


# Correcting a reply to "AP invoice" gives it the tasks reading it would: none of its own to enter.
def test_correcting_a_reply_to_ap_invoice_gives_it_no_second_entry_task(store, settings):
    from controller_inbox.learn import record_correction

    _invoice(settings, "1.eml")
    _reply(settings, "2.eml", sender="Coastal Print AR <ar@coastalprint.com>", body="Can you send us the PO number for invoice CP-5521? Please reply today.")
    ingest_folder(store, settings, now=START + timedelta(hours=4))
    reply = next(email for email in _emails(store) if email.subject.startswith("RE:"))
    record_correction(store, settings, email_id=reply.id, corrected_category="reply_needed", reason="It asks for the PO")
    record_correction(store, settings, email_id=reply.id, corrected_category="ap_invoice", reason="This is about the invoice")
    assert _entry(store) == [("ar@coastalprint.com", "Code and enter invoice CP-5521 ($1,840.00)")]
