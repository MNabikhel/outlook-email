"""Regressions for the mail intake and triage review (one or more tests per bug)."""

from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from controller_inbox.extract import extract_fields
from controller_inbox.folder_mail import _split_address, ingest_folder
from controller_inbox.fraud import TrustContext, assess
from controller_inbox.graph import GraphMailbox
from controller_inbox.models import RawMessage
from controller_inbox.pipeline import process_message

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
BANK = "Please note our bank details have changed. Please pay invoice INV-5521 to the new account: account 99887766554."


def _signals(check) -> set[str]:
    return {signal.key for signal in check.signals}


# 1. "From: ap@taz.com <ap@evil-pay.net>" was stored as ap@taz.com: the parsed header drops the brackets. --------


@pytest.mark.parametrize(
    "header",
    ["ap@taz.com <ap@evil-pay.net>", "=?utf-8?q?ap=40taz.com?= <ap@evil-pay.net>", "ap@evil-pay.net, ap@taz.com"],
)
def test_eml_sender_is_the_real_address_not_one_in_the_name(store, settings, header):
    store.set_trust("domain", "taz.com", "safe", source="you", note="", at=NOW.isoformat())
    settings.inbox_incoming.mkdir(parents=True, exist_ok=True)
    path = settings.inbox_incoming / "m.eml"
    path.write_bytes(
        f"From: {header}\r\nTo: me@firm.com\r\nSubject: Updated remittance details\r\nMessage-ID: <m1@x>\r\n"
        f"Date: Thu, 08 Oct 2026 10:00:00 +0000\r\n\r\n{BANK}\r\n".encode()
    )
    os.utime(path, (time.time() - 100, time.time() - 100))
    record = ingest_folder(store, settings, now=NOW)[0]
    assert record.sender_email == "ap@evil-pay.net"
    assert store.fraud_check(record.id)["level"] == "high"


def test_several_addresses_without_brackets_give_the_first():
    assert _split_address("a@evil.com, b@taz.com")[1] == "a@evil.com"


# 2. A line starting with ">" or "From:" made a stranger's bank change count as quoted only. ---------------------


@pytest.mark.parametrize("marker", ["> ", "From: Acme AP\n", "Sent: from our billing system\n"])
def test_first_contact_bank_change_below_a_quote_marker_blocks(marker):
    ctx = TrustContext(domains={"firm.com"})
    check = assess(ctx, subject="Invoice INV-5521", body=f"Hi,\n{marker}{BANK}", sender_name="Acme AP",
                   sender_email="ap@acme-billing.net", history=0)
    assert check.level == "high" and "bank_change" in _signals(check)


def test_known_sender_quoting_a_bank_change_is_still_only_a_caution():
    check = assess(TrustContext(), subject="RE: invoice", body=f"Thanks, noted.\n\nFrom: Acme AP\n{BANK}",
                   sender_name="Acme AP", sender_email="ap@acme.net", history=4)
    assert check.level == "caution" and "bank_change_quoted" in _signals(check)


# 3. A trusted From with a Reply-To elsewhere: the mismatch was cancelled by trusted_domain. ----------------------


@pytest.mark.parametrize("reply_to", ["ceo@flrm.com", "ceo.firm@gmail.com"])
def test_trusted_sender_with_an_outside_reply_to_gets_a_warning(reply_to):
    ctx = TrustContext(domains={"firm.com"}, known={"firm.com": 300})
    body = "I need you to process a wire transfer of $48,500 to a new vendor today. Just reply here with confirmation."
    check = assess(ctx, subject="Quick favor", body=body, sender_name="Pat Ceo", sender_email="ceo@firm.com",
                   reply_to=reply_to, history=12, domain_history=300)
    assert check.level != "none" and "trusted_domain" not in _signals(check)


def test_reply_to_on_a_lookalike_domain_is_named():
    ctx = TrustContext(domains={"firm.com"}, known={"firm.com": 300})
    check = assess(ctx, subject="Pay", body="Pay invoice", sender_name="Pat", sender_email="pat@firm.com",
                   reply_to="pat@flrm.com", history=12)
    assert "lookalike_domain" in _signals(check)


# 4. "by Oct 15, 12pm ET" was read as 2012: a time or a word after the day is not a two-digit year. --------------


@pytest.mark.parametrize(
    "text,due",
    [
        ("Please approve by Oct 15, 12pm ET.", "2026-10-15"),
        ("Payment due Oct 15, 10:00 AM", "2026-10-15"),
        ("Need this by Oct 15 12 noon", "2026-10-15"),
        ("Please complete by Nov 3, 15:00 CET", "2026-11-03"),
        ("Due date: Oct 30, 30 days net", "2026-10-30"),
        ("Due by Oct 15, 26.", "2026-10-15"),
    ],
)
def test_time_after_a_due_day_is_not_a_year(text, due):
    assert extract_fields(text, as_of=date(2026, 10, 9)).due_dates == [due]


def test_task_due_date_is_not_read_from_a_time(store, settings):
    body = "Hi, please approve the PO request for the new laptops by Oct 15, 12pm ET. Thanks, Dana"
    raw = RawMessage(id="m1", subject="PO approval needed", sender_name="Dana Lee", sender_email="dana@firm.com",
                     received_at=NOW, body_text=body, body_preview=body, has_attachments=False, source="graph")
    record = process_message(raw, store, settings, now=NOW)
    assert {action.due_date for action in record.actions if "laptops" in action.title} == {"2026-10-15"}


# 5. The duplicate invoice check missed INV1001 for INV-1001 and flagged another vendor's 1001. -----------------


def _invoice(store, settings, msg_id, number, sender, days):
    body = f"Please find our invoice for September services. Invoice {number}. Amount due: $4,250.00."
    raw = RawMessage(id=msg_id, subject=f"Invoice {number}", sender_name="Acme Billing", sender_email=sender,
                     received_at=NOW + timedelta(days=days), body_text=body, body_preview=body,
                     has_attachments=False, source="graph")
    return process_message(raw, store, settings, now=NOW + timedelta(days=days))


@pytest.mark.parametrize("again", ["INV1001", "INV_1001", "INV-01001"])
def test_same_invoice_written_differently_is_a_duplicate(store, settings, again):
    _invoice(store, settings, "m1", "INV-1001", "billing@acme.com", 0)
    assert "duplicate_invoice" in _invoice(store, settings, "m2", again, "ar@acme.com", 1).flags


def test_same_number_from_another_vendor_is_not_a_duplicate(store, settings):
    _invoice(store, settings, "m1", "1001", "billing@acme.com", 0)
    assert "duplicate_invoice" not in _invoice(store, settings, "m2", "1001", "ar@othervendor.com", 1).flags


# 6. Graph listed every folder, so CloseDesk's own sent digest came back as a critical fraud email. -------------


class _Client:
    mailbox = ""

    def __init__(self, items):
        self.items, self.urls = items, []

    def _user_root(self):
        return "/me"

    def get_json(self, url, params=None):
        self.urls.append(url)
        return {"value": self.items}

    def signed_in_user(self):
        return {"mail": "Controller@firm.com", "userPrincipalName": "controller@firm.onmicrosoft.com"}


def _graph_item(msg_id, subject, sender):
    return {"id": msg_id, "subject": subject, "from": {"emailAddress": {"address": sender}},
            "receivedDateTime": "2026-10-09T12:00:00Z", "body": {"contentType": "text", "content": "x"}}


def test_graph_lists_the_inbox_only():
    client = _Client([])
    list(GraphMailbox(client).list_messages())
    assert client.urls == ["/me/mailFolders/inbox/messages"]


def test_graph_skips_closedesks_own_digest():
    client = _Client([
        _graph_item("d", "CloseDesk daily digest — 2026-10-09: 3 need you", "controller@firm.com"),
        _graph_item("f", "CloseDesk daily digest — 2026-10-09: 3 need you", "ceo@flrm.com"),
        _graph_item("v", "Invoice 1001", "billing@acme.com"),
    ])
    assert [raw.id for raw in GraphMailbox(client).list_messages()] == ["f", "v"]


# 7. mail.com and ymail.com were flagged as lookalikes of gmail.com. --------------------------------------------


@pytest.mark.parametrize("sender", ["jean@mail.com", "jean@ymail.com"])
def test_free_mail_provider_is_not_a_lookalike_of_another(sender):
    ctx = TrustContext(domains={"firm.com"}, known={"gmail.com": 60, "firm.com": 300})
    check = assess(ctx, subject="Invoice", body="Hi, attached is my invoice for September bookkeeping.",
                   sender_name="Jean Dupont", sender_email=sender)
    assert "lookalike_domain" not in _signals(check)


# 8. "Chen, Maya" and "Maya Chen (CFO)" got past the display-name check. ----------------------------------------


@pytest.mark.parametrize("name", ["Chen, Maya", "Maya Chen (CFO)", "Maya Chen via DocuSign"])
def test_display_name_written_another_way_is_still_a_spoof(name):
    ctx = TrustContext(domains={"taz.com"}, names={"Maya Chen": "maya@taz.com"})
    check = assess(ctx, subject="Wire", body="Please send the wire today for $45,000.", sender_name=name,
                   sender_email="x@evil.com")
    assert "display_name_spoof" in _signals(check)
