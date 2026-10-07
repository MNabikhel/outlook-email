"""Regression tests for the fraud-rules review: notices, disguised text, wording, trust, dates, digest, corrections."""

from __future__ import annotations

from datetime import datetime, timezone

from controller_inbox.fraud import TrustContext, assess, attachments_locked, strip_notices
from controller_inbox.models import DocumentType, RawAttachment, RawMessage
from controller_inbox.pipeline import process_message

NOW = datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc)


def _mail(store, settings, msg_id, *, subject, sender, body, attachments=()):
    raw = RawMessage(
        id=msg_id, subject=subject, sender_name="Acme AR", sender_email=sender,
        received_at=NOW, body_text=body, body_preview=body[:200], has_attachments=bool(attachments),
        source="folder",
        attachments=[
            RawAttachment(id=str(i), filename=name, content_type="text/plain", size_bytes=len(data), content=data)
            for i, (name, data) in enumerate(attachments)
        ],
    )
    return process_message(raw, store, settings, now=NOW)


def _check(body, *, subject="Invoice 5521", sender="ar@acme-billing.example", history=0, ctx=None, **extra):
    return assess(ctx or TrustContext(), subject=subject, body=body, sender_name="Acme AR", sender_email=sender, history=history, **extra)


def _keys(check):
    return {signal.key for signal in check.signals}


# 1. Warning words at the end of a request do not turn the request into a notice.
def test_bank_change_with_a_call_us_clause_is_blocked(store, settings):
    email = _mail(
        store, settings, "notice1", subject="Invoice 5521", sender="ar@acme-billing.example",
        body="Hi,\n\nOur bank details have changed, please pay invoice 5521 to the new account in the attached letter, "
             "and if you receive any other instructions call us.",
        attachments=[("letter.txt", b"New account 4410029981 routing 021000021")],
    )
    assert email.category == DocumentType.PAYMENT_INSTRUCTION_CHANGE
    assert attachments_locked(email)
    assert store.fraud_check("notice1")["level"] == "high"


def test_gift_card_ask_with_dont_call_is_blocked():
    # "Don't call" is the scammer's pressure, not advice to verify.
    check = _check(
        "I will never ask this normally, but I need you to buy 5 Apple gift cards and send me the codes, don't call, I'm in a meeting.",
        subject="Quick favor", sender="ceo.office@gmail.com",
    )
    assert "gift_cards" in _keys(check) and check.level == "high"
    stuck = _check("I need you to buy 5 Apple gift cards for a client and send me the codes, if you get stuck call me.", subject="Quick favor")
    assert stuck.level == "high"


def test_a_request_followed_by_a_warning_is_kept():
    sentence = "Our bank details have changed, so pay the new account below, and if you get other instructions call us."
    assert strip_notices(sentence) == sentence


def test_warnings_about_a_change_are_still_dropped():
    # The change wording comes after "if you receive … saying", so it is what the warning is about.
    assert strip_notices("If you receive an email saying our bank details have changed, call us on the number on file.") == ""
    assert strip_notices("Beware of phishing emails claiming our bank details have changed, and call us.") == ""
    assert strip_notices("We will never ask you to buy gift cards or send codes by email.") == ""
