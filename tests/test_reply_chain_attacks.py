"""Attacks from the fifth review on invoice conversations, kept as they were written: a backdated email joining
by Thread-Index in one batch, an attacker's own earlier email referenced to start the thread, a free-mail lookalike,
a stored email read again, and a fraud verdict given later. The vendor's invoice keeps its task in each."""

from __future__ import annotations

from datetime import timedelta

import pytest

from controller_inbox.folder_mail import ingest_folder
from controller_inbox.fraud import record_fraud_verdict
from controller_inbox.models import RawAttachment
from controller_inbox.pipeline import ingest_mailbox
from test_links import START, _invoice_tasks
from test_reply_chains import PDF, TEXT_INVOICE, THREAD_INDEX, VENDOR_TASK, _eml, _flagged, _NewestFirst, _raw





def test_attacker_root_by_merging_a_merge(store, settings):
    _eml(settings, "x1.eml", "Hello", sender="Coastal Print Accounts <accounts@cpco-billing.com>", mid="<x1@cpco-billing.com>",
         to="ap@taz.com, ar@coastalprint.com", hours=-48, body="Hi, we handle billing for Coastal Print. Talk soon.")
    ingest_folder(store, settings, now=START - timedelta(hours=47))
    _eml(settings, "r.eml", "Invoice CP-5521", sender="Coastal Print AR <ar@coastalprint.com>", mid="<cp1@coastalprint.com>", body=TEXT_INVOICE)
    ingest_folder(store, settings, now=START + timedelta(hours=1))
    _eml(settings, "x2.eml", "RE: Invoice CP-5521", sender="Coastal Print Accounts <accounts@cpco-billing.com>", mid="<x2@cpco-billing.com>",
         to="ap@taz.com, ar@coastalprint.com", reply_to_mid="<cp1@coastalprint.com>", refs="<x1@cpco-billing.com> <cp1@coastalprint.com>", hours=3,
         files=[("CP-5521.pdf", PDF)], body="Resending invoice CP-5521 as a PDF.")
    ingest_folder(store, settings, now=START + timedelta(hours=4))
    tasks = _invoice_tasks(store)
    assert VENDOR_TASK in tasks


@pytest.mark.parametrize("cc", [True, False])
@pytest.mark.parametrize("vendor_pdf", [True, False])
@pytest.mark.parametrize("via_index", [True, False])
def test_variants_a2(store, settings, cc, vendor_pdf, via_index):
    to = "ap@taz.com, ar@coastalprint.com" if cc else "ap@taz.com"
    _eml(settings, "x1.eml", "Hello", sender="Coastal Print Accounts <accounts@cpco-billing.com>", mid="<x1@cpco-billing.com>",
         to=to, hours=-48, body="Hi, we handle billing for Coastal Print. Talk soon.", thread_index=THREAD_INDEX if via_index else "")
    ingest_folder(store, settings, now=START - timedelta(hours=47))
    _eml(settings, "r.eml", "Invoice CP-5521", sender="Coastal Print AR <ar@coastalprint.com>", mid="<cp1@coastalprint.com>", body=TEXT_INVOICE,
         files=[("CP-5521.pdf", PDF)] if vendor_pdf else ())
    ingest_folder(store, settings, now=START + timedelta(hours=1))
    _eml(settings, "x2.eml", "RE: Invoice CP-5521", sender="Coastal Print Accounts <accounts@cpco-billing.com>", mid="<x2@cpco-billing.com>",
         to=to, reply_to_mid="<cp1@coastalprint.com>", refs="<cp1@coastalprint.com>" if via_index else "<x1@cpco-billing.com> <cp1@coastalprint.com>", hours=3,
         files=[("CP-5521v.pdf", PDF + b"1")], body="Resending invoice CP-5521 as a PDF.", thread_index=THREAD_INDEX if via_index else "")
    ingest_folder(store, settings, now=START + timedelta(hours=4))
    assert VENDOR_TASK in _invoice_tasks(store)
    assert not _flagged(store)["ar@coastalprint.com"]


@pytest.mark.parametrize("names", [("1-r.eml", "2-x.eml"), ("2-r.eml", "1-x.eml")])
def test_backdated_thread_index_join_in_one_batch_j_batch(store, settings, names):
    _eml(settings, names[0], "Invoice CP-5521", sender="Coastal Print AR <ar@coastalprint.com>", mid="<cp1@coastalprint.com>", body=TEXT_INVOICE,
         thread_index=THREAD_INDEX)
    _eml(settings, names[1], "RE: Invoice CP-5521", sender="Coastal Print Accounts <accounts@cpco-billing.com>", mid="<x2@cpco-billing.com>",
         to="ap@taz.com, ar@coastalprint.com", hours=-5, thread_index=THREAD_INDEX,
         files=[("CP-5521.pdf", PDF)], body="Invoice CP-5521 attached as a PDF.")
    ingest_folder(store, settings, now=START + timedelta(hours=8))
    assert VENDOR_TASK in _invoice_tasks(store)


def test_freemail_vendor_i_freemail(store, settings):
    _eml(settings, "r.eml", "Invoice CP-5521", sender="Coastal Print <coastalprint.ar@gmail.com>", mid="<cp1@mail.gmail.com>", body=TEXT_INVOICE)
    ingest_folder(store, settings, now=START + timedelta(hours=1))
    _eml(settings, "x.eml", "RE: Invoice CP-5521", sender="Coastal Print <coastalprint.billing@gmail.com>", mid="<cp2@mail.gmail.com>",
         reply_to_mid="<cp1@mail.gmail.com>", refs="<cp1@mail.gmail.com>", hours=3, files=[("CP-5521.pdf", PDF)], body="Resending invoice CP-5521 as a PDF.")
    ingest_folder(store, settings, now=START + timedelta(hours=4))
    assert VENDOR_TASK in _invoice_tasks(store)


def test_reread_moves_root_f_reread(store, settings):
    vendor = _raw("R", "ar@coastalprint.com", "Invoice CP-5521", TEXT_INVOICE, 0, ["ap@taz.com"])
    other = _raw("L", "accounts@cpco-billing.com", "RE: Invoice CP-5521", "Resending invoice CP-5521 as a PDF.", 2, ["ap@taz.com", "ar@coastalprint.com"],
                 [RawAttachment(id="a", filename="CP-5521.pdf", content_type="application/pdf", size_bytes=len(PDF), content=PDF)])
    ingest_mailbox(_NewestFirst([vendor]), store, settings, now=START + timedelta(hours=1), cursor=None)
    ingest_mailbox(_NewestFirst([other]), store, settings, now=START + timedelta(hours=3), cursor=None)
    assert any(a.source == "invoice" for a in store.get_email("R").actions)
    # the same two listed again (a sync of a window before the cursor, e.g. "sync --hours 24")
    ingest_mailbox(_NewestFirst([vendor, other]), store, settings, now=START + timedelta(hours=5), cursor=None)
    assert any(a.source == "invoice" for a in store.get_email("R").actions), "the vendor's invoice keeps its task"


def test_task_not_back_after_fraud_verdict_b_verdict(store, settings):
    _eml(settings, "r.eml", "Invoice CP-5521", sender="Coastal Print AR <ar@coastalprint.com>", mid="<cp1@coastalprint.com>", body=TEXT_INVOICE)
    ingest_folder(store, settings, now=START + timedelta(hours=1))
    _eml(settings, "b.eml", "RE: Invoice CP-5521", sender="Coastal Print Billing <billing@coastalprint.com>", mid="<cp2@coastalprint.com>",
         reply_to_mid="<cp1@coastalprint.com>", refs="<cp1@coastalprint.com>", hours=3,
         files=[("CP-5521.pdf", PDF)], body="Resending invoice CP-5521 as a PDF.")
    ingest_folder(store, settings, now=START + timedelta(hours=4))
    b = next(e for e in store.list_emails(limit=-1) if e.sender_email == "billing@coastalprint.com")
    record_fraud_verdict(store, settings, b.id, verdict="fraud", now=START + timedelta(hours=5))
    assert VENDOR_TASK in _invoice_tasks(store)
