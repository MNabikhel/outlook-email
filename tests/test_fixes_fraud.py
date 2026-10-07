"""Regression tests for the fraud, guard-rail, learning and model-client fixes."""

from __future__ import annotations

import gc
import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from controller_inbox import local_llm
from controller_inbox.actions import received_day
from controller_inbox.fraud import TrustContext, assess, learned_weights, record_fraud_verdict, strip_notices
from controller_inbox.learn import match_correction, record_correction
from controller_inbox.local_llm import LocalReader, _post_chat
from controller_inbox.models import DocumentType, EmailRecord, ExtractedFields, Importance, RawMessage
from controller_inbox.pipeline import process_message
from controller_inbox.reading import (
    FRAUD_SUMMARY,
    VERIFY_TITLE,
    ReadingRefused,
    _number_in,
    _ungrounded_amounts,
    _warns,
    apply_bionic_reading,
    overlay_reading,
)

NOW = datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc)
BASE = "Hi,\n\nOur banking details have changed. Please update your records and send the next payment to the new account."


def _check(body, *, subject="Payment update", sender="ap@vendor.example", history=3, ctx=None):
    return assess(ctx or TrustContext(), subject=subject, body=body, sender_name="Vendor AP", sender_email=sender, history=history)


def _mail(store, settings, msg_id, *, subject, sender, body, minutes=0):
    raw = RawMessage(
        id=msg_id, subject=subject, sender_name=sender.split("@")[0].title(), sender_email=sender,
        received_at=NOW - timedelta(minutes=minutes), body_text=body, body_preview=body[:200],
        has_attachments=False, source="folder",
    )
    return process_message(raw, store, settings, now=NOW)


# 1. A notice word in front of a real request no longer hides it.
@pytest.mark.parametrize(
    "body",
    [
        "Hi,\n\nPlease be aware our banking details have changed, update your records and send payment to the new account below.",
        "Hi,\n\nAlways confirm: our banking details have changed, so send payment to the new account below.",
        "We will never ask for gift cards, but our banking details have changed.",
    ],
)
def test_warning_words_do_not_hide_a_bank_change(body):
    check = _check(body)
    assert "bank_change" in {s.key for s in check.signals} and check.level == "high"


def test_real_notices_are_still_dropped():
    assert strip_notices("If you receive an email saying our bank details have changed, call us on the number on file.") == ""
    assert strip_notices("We will never change our bank details by email. Beware of scams.") == ""


# 2. Disclaimer paragraphs and quote markers.
def test_bank_change_inside_a_disclaimer_paragraph_blocks():
    check = _check("Hi,\n\nThis email is confidential. Our banking details have changed, please update your records.")
    assert check.level == "high" and "bank_change" in {s.key for s in check.signals}


@pytest.mark.parametrize(
    "body",
    [
        "Hi,\n> Our banking details have changed. Please send funds to the new account.",
        "Hi, please process per below.\n\nFrom: Jane Vendor\nOur banking details have changed. Please update your records.",
    ],
)
def test_bank_change_below_a_quote_marker_gets_at_least_a_caution(body):
    assert _check(body, history=0).level in {"caution", "high"}


def test_a_colleague_disowning_the_quoted_scam_stays_quiet():
    body = "It was not them, I blocked the sender.\n\nFrom: ar@x.example\nSent: Monday\n\nOur banking details have changed."
    colleagues = TrustContext(domains={"ourco.example"})
    assert _check(body, subject="RE: bank", sender="sam@ourco.example", history=0, ctx=colleagues).level == "none"


# 3. "Not fraud" answers cannot weaken the block.
def test_not_fraud_answers_never_weaken_a_bank_change(store, settings):
    for n in range(4):
        _mail(store, settings, f"w{n}", subject="Bank", sender=f"ap@v{n}.example", body=BASE)
        record_fraud_verdict(store, settings, f"w{n}", verdict="safe")
    weights = learned_weights(store)
    assert weights["bank_change"] >= 1.0
    assert _check(BASE, ctx=TrustContext(weights=weights)).level == "high"
    assert _check(BASE, ctx=TrustContext(weights={"bank_change": 0.5, "first_contact": 0.5})).level == "high"


# 4. Gift cards block, but a thank-you or a Visa card payment does not.
def test_gift_card_request_blocks_even_from_a_known_colleague():
    check = _check("Hi, I need you to buy 5 Apple gift cards for a client and send me the codes.", sender="ceo@corp.example")
    assert check.level == "high"


@pytest.mark.parametrize("body", ["Thanks for the Amazon gift card, very kind!", "We accept payment by Visa card or ACH."])
def test_gift_card_mentions_without_an_ask_do_not_count(body):
    assert "gift_cards" not in {s.key for s in _check(body).signals}


def test_trusted_sender_bank_change_is_a_caution():
    ctx = TrustContext(domains={"vendor.example"})
    assert _check(BASE, ctx=ctx).level == "caution"


# 5. Wider bank-change wording.
@pytest.mark.parametrize(
    "body",
    [
        "Our bank account details have changed. Kindly remit to the account below.",
        "We have a new bank account. Please remit future payments there.",
        "Please remit to the account below from now on.",
        "Attached are our updated ACH details for your records.",
        "Our remittance information has been updated.",
    ],
)
def test_more_bank_change_phrasings_are_caught(body):
    assert "bank_change" in {s.key for s in _check(body).signals}


# 6-8. Model reading guard rails.
def _fraud_email():
    return EmailRecord(
        id="e1", subject="Bank change", sender_name="V", sender_email="ap@v.example", received_at="2026-10-01T15:00:00+00:00",
        body_text="Our banking details have changed. Invoice total $48,500.00 due.", body_preview="", has_attachments=False,
        outlook_importance="normal", is_read=False, category=DocumentType.PAYMENT_INSTRUCTION_CHANGE, category_confidence=0.9,
        importance=Importance.CRITICAL, importance_score=100, flags=["fraud_risk", "do_not_process"],
        extracted=ExtractedFields(amounts=[48500.0]), summary="script",
    )


def test_fraud_mail_keeps_only_the_verify_by_phone_task():
    email = overlay_reading(_fraud_email(), {
        "category": "ap_invoice", "folder": "important", "importance": "critical", "summary": FRAUD_SUMMARY,
        "actions": [
            {"title": "Call vendor, then update bank account in vendor master to new routing number", "due": None, "priority": "high"},
            {"title": "Confirm by email and wire the funds to new account", "due": None, "priority": "high"},
        ],
    })
    assert [a.title for a in email.actions] == [VERIFY_TITLE]
    assert any("Removed 2 task" in r for r in email.importance_reasons)


def test_verify_alone_is_not_a_fraud_warning():
    email = overlay_reading(_fraud_email(), {"category": "ap_invoice", "summary": "Vendor asks to verify the new bank account and pay today."})
    assert email.summary == FRAUD_SUMMARY
    assert not _warns("Vendor asks to verify the new bank account and pay $48,500.00.")
    assert _warns("Call the vendor on the number on file; do not pay until confirmed.")


def test_amount_check_matches_whole_numbers_only():
    email = _fraud_email()
    assert _ungrounded_amounts("Pay $4,850 deposit.", email) == ["$4,850"]
    assert _ungrounded_amounts("Pay $500.", email) == ["$500"]
    assert _ungrounded_amounts("Invoice for $48,500.00.", email) == []
    assert _number_in("48500", "total 48500.00") and not _number_in("4850", "total 48500.00")


# 9. A corrected message is not overwritten by a reading.
def test_corrected_message_refuses_a_model_reading(loaded, settings):
    record_correction(loaded, settings, email_id="demo-expense", corrected_category="internal_fyi", reason="Just FYI from HR")
    with pytest.raises(ReadingRefused):
        apply_bionic_reading(loaded, "demo-expense", {"category": "ap_invoice", "folder": "important"})
    assert loaded.get_email("demo-expense").category == DocumentType.INTERNAL_FYI


# 10. reasoning_effort is only blamed when the request without it works.
def test_effort_not_blamed_for_a_response_format_rejection(settings):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(("reasoning_effort" in body, "response_format" in body))
        if "response_format" in body:
            return httpx.Response(400, json={"error": "response_format not supported"})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"category": "ap_invoice"})}}]})

    reader = LocalReader(settings, model="tiny", client=httpx.Client(transport=httpx.MockTransport(handler)))
    reader._effort = "none"
    assert reader.read({"subject": "x"}) == {"category": "ap_invoice"}
    assert "tiny" not in local_llm._effort_rejected
    assert calls[-1] == (True, False), "the plain retry still asks for no thinking"


def test_effort_blamed_when_dropping_it_works(settings):
    def handler(request):
        if "reasoning_effort" in json.loads(request.content):
            return httpx.Response(400, json={"error": "Unrecognized key"})
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    payload = {"model": "m1", "reasoning_effort": "none"}
    assert _post_chat(client.post, "http://x/chat/completions", payload, settings).status_code == 200
    assert "m1" in local_llm._effort_rejected


# 11. The reader closes the client it made.
def test_reader_closes_its_own_client(settings):
    with LocalReader(settings, model="tiny") as reader:
        client = reader.client
    assert client.is_closed
    leaked = LocalReader(settings, model="tiny").client
    gc.collect()
    assert leaked.is_closed
    mine = httpx.Client()
    LocalReader(settings, model="tiny", client=mine).close()
    assert not mine.is_closed
    mine.close()


# 12. Dates: blank or naive timestamps, and today vs. arrival day.
def test_received_day_handles_blank_and_naive_timestamps():
    fallback = date(2026, 1, 1)
    assert received_day("", fallback=fallback) == fallback
    assert received_day("garbage", fallback=fallback) == fallback
    from zoneinfo import ZoneInfo

    assert received_day("2026-09-30T02:00:00", ZoneInfo("America/New_York")) == date(2026, 9, 29), "naive is UTC"


def test_correction_survives_a_blank_received_at(loaded, settings):
    email = loaded.get_email("demo-expense")
    email.received_at = ""
    loaded.upsert_email(email)
    record_correction(loaded, settings, email_id="demo-expense", corrected_category="internal_fyi", reason="Just FYI")
    assert loaded.get_email("demo-expense").category == DocumentType.INTERNAL_FYI


# 13. Subject-only corrections stay within the sender's company.
def test_subject_correction_does_not_cross_senders(loaded, settings):
    email = loaded.get_email("demo-expense")
    record_correction(loaded, settings, email_id=email.id, corrected_category="newsletter", reason="Bulk mail")
    domain = email.sender_email.split("@")[1]
    assert match_correction(loaded, sender_email="someone@else.example", subject=email.subject) is None
    assert match_correction(loaded, sender_email=f"colleague@{domain}", subject=email.subject) is not None
    assert match_correction(loaded, sender_email="", subject=email.subject) is None
