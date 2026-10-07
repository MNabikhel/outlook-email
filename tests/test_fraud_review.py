"""Regression tests for the fraud-rules review: notices, disguised text, wording, trust, dates, digest, corrections."""

from __future__ import annotations

from datetime import datetime, timezone

from controller_inbox.classify import classify_document, normalize_text
from controller_inbox.extract import html_to_text
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


# 2. Invisible characters, HTML leftovers and look-alike letters do not hide a word.
DISGUISED_BANK = [
    "Our ba\u200bnk details have changed.",  # zero-width space
    "Our ba&#8203;nk details have changed.",  # the same, as an HTML entity
    "Our b<span></span>ank details have changed.",  # an empty inline tag
    "Our ba\u00adnk det\u2060ails have changed.",  # soft hyphen and word joiner
    "Our b\u0430nk details have changed.",  # Cyrillic a (U+0430)
    "Our \uff42\uff41\uff4e\uff4b details have changed.",  # full-width letters
]


def test_disguised_bank_change_wording_is_still_caught():
    for body in DISGUISED_BANK:
        check = _check(body, history=3)
        assert "bank_change" in _keys(check) and check.level == "high", body
        assert classify_document(subject="Hello", body=body).document_type == DocumentType.PAYMENT_INSTRUCTION_CHANGE, body


def test_disguised_subject_attachment_and_gift_card_ask_are_caught():
    assert "bank_change" in _keys(_check("See attached.", subject="Our b\u0430nk details h\u200bave changed"))
    files = [("letter.txt", "Please note our bank det&#8203;ails have changed.")]
    assert "bank_change_attachment" in _keys(_check("Invoice attached.", attachments=files))
    gift = _check("I need you to buy 5 Apple gift c\u200bards and send me the c\u043edes.", subject="Quick favor")
    assert "gift_cards" in _keys(gift)


def test_display_name_with_a_cyrillic_letter_is_still_a_borrowed_name():
    ctx = TrustContext(domains={"taz.com"}, names={"dana cho": "dana@taz.com"})
    check = assess(ctx, subject="Wire", body="Please wire the funds today.", sender_name="D\u0430na Cho", sender_email="dana.cho@gmail.com")
    assert "display_name_spoof" in _keys(check)


def test_html_to_text_keeps_words_whole_at_inline_tags():
    assert html_to_text("<p>Our b<span></span>ank <b>details</b> have changed.</p>") == "Our bank details have changed."
    assert html_to_text("<p>Our ba<!-- x -->nk de<a href='x'>tails</a></p>") == "Our bank details"
    assert html_to_text("<p>Our ba&#8203;nk &amp; co</p>") == "Our ba\u200bnk & co"
    # Block tags still break the text.
    assert html_to_text("<p>One</p><p>Two</p>Three<br>Four<td>a</td><td>b</td>") == "One\n Two\nThree\nFour a b"
    check = _check(html_to_text("<p>Our ba&#8203;nk details have changed. Remit to account 4410029981.</p>"), history=3)
    assert check.level == "high"


def test_plain_mail_reads_the_same_after_normalizing():
    assert normalize_text("Invoice 7781 attached, AT&T R&D \u2014 see <a.smith@x.com>.") == "Invoice 7781 attached, AT&T R&D - see <a.smith@x.com>."
    assert normalize_text("Caf\u00e9 don\u2019t") == "Cafe don't"
