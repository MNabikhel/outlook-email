"""Regression tests for the second fraud-rules review: footers, negations, gift cards, look-alikes, names,
evidence, the digest count and long crafted emails."""

from __future__ import annotations

import time

from controller_inbox.classify import classify_document, normalize_text
from controller_inbox.extract import html_to_text
from controller_inbox.fraud import TrustContext, assess


def _check(body, *, subject="Invoice 5521", sender="ar@vendor.example", history=5, ctx=None, **extra):
    return assess(ctx or TrustContext(), subject=subject, body=body, sender_name="Vendor AR", sender_email=sender,
                  history=history, **extra)


def _keys(check):
    return {signal.key for signal in check.signals}


def _cpu_seconds(work) -> float:
    start = time.process_time()
    work()
    return time.process_time() - start


# Long crafted emails are read in one pass: each of these took from several seconds to minutes.
CRAFTED_200KB = {
    "unclosed comments": "<!--" * 50_000,
    "repeated warnings": "if you receive " * 13_333,
    "gift card mentions": "gift card " * 20_000,
    "gift card program mentions": "gift card program " * 11_111,
}


def test_long_crafted_bodies_are_read_quickly():
    for name, body in CRAFTED_200KB.items():
        spent = _cpu_seconds(lambda: _check(body, attachments=[("a.txt", body)]))
        assert spent < 2.5, f"{name}: {spent:.1f}s"
        assert _cpu_seconds(lambda: classify_document(subject="Hello", body=body, extracted_text=body)) < 2.5, name


def test_long_crafted_html_is_read_quickly():
    for body in ("<script" * 30_000, "<style>" * 28_000, "<!--" * 50_000, "<" * 200_000):
        assert _cpu_seconds(lambda: html_to_text(body)) < 1.0, body[:8]


def test_comments_are_still_dropped():
    assert normalize_text("Our ba<!-- x -->nk details") == "Our bank details"
    assert normalize_text("a <!-- never closed") == "a <!-- never closed"
    assert html_to_text("<p>x<script>var a = '<b>';</script>y<style>p {}</style>z</p>") == "x y z"


# 1. The usual anti-fraud footers are warnings, not requests.
FOOTERS = [
    "Please note that we will never notify you of a change to our bank details by email.",
    "We will never inform you of any change in our bank account details via email, so always call us before paying.",
    "We won't advise you of changes to our banking details by email.",
    "If you receive an email, text or call purporting to be from us, advising that our bank details have changed, "
    "please contact us immediately.",
    "If you receive an email from us, or from anyone claiming to be us, saying our bank details have changed, please call us.",
    "If you receive any of the following: emails claiming our bank details have changed or requests for gift cards, call us.",
]


def test_anti_fraud_footers_do_not_block_mail():
    for footer in FOOTERS:
        check = _check(f"Please find the completion statement attached.\n\nKind regards,\nJane\n\n{footer}")
        assert check.level == "none" and not _keys(check), footer


def test_a_request_next_to_warning_words_is_still_a_request():
    for body in (
        "We will never ask you for your password, our bank details have changed, please update your records.",
        "If you receive this, it means our bank details have changed, please pay the new account.",
        "If you get stuck, call me, I am telling you our bank details have changed.",
        "We never notify customers by email, but our bank details have changed and the new account is attached.",
        "If you receive an email saying our old account is closed, ignore it, our bank details have changed.",
    ):
        check = _check(body)
        assert "bank_change" in _keys(check) and check.level == "high", body
    gift = _check("I will never ask this normally, but I am asking you to buy 5 Apple gift cards and send me the codes.")
    assert "gift_cards" in _keys(gift) and gift.level == "high"


# 2. A change the sender says is not happening, a question, an audit request or a ledger account is not a request.
NOT_A_CHANGE = [
    "Invoice 7781 for $4,200.00 attached. Please note there is no change to our bank details.",
    "We have not made any changes to our bank details; only our address has moved.",
    "There have been no recent changes to our banking information.",
    "Adjuntamos la factura 7781. Nuestros datos bancarios no han cambiado.",
    "No hay cambio de cuenta bancaria.",
    "Il n'y a pas de changement de RIB. Aucun changement de coordonnées bancaires.",
    "Es gibt keine Änderung unserer Bankverbindung.",
    "Não há alteração de dados bancários. Sem alteração de dados bancários.",
    "Have your bank details changed? Please let us know before the next run.",
    "Please send the vendor master file and the log of bank account changes for the year by Friday.",
    "Please confirm whether any vendor bank details changed during FY2026.",
    "Please use account 6150 for all software payments going forward.",
    "Use GL account 5200 for these fuel purchases going forward.",
    "Important changes to your bank account fees from November.",
]

# The same kinds of sentence when they do ask for a change, in each of the five languages.
REAL_CHANGES = [
    "Please note our bank details have changed; pay invoice 7781 to the new account below.",
    "Our payment details changed last week, please update your records.",
    "Please use acct 99887766 for all payments going forward.",
    "Not only has our address changed, our bank details have changed too.",
    "Nuestros datos bancarios han cambiado. Por favor pague a la nueva cuenta bancaria.",
    "Nos coordonnées bancaires ont été modifiées, merci de noter notre nouveau RIB.",
    "Unsere Bankverbindung hat sich geändert. Bitte überweisen Sie auf das neue Konto.",
    "Nossos dados bancários foram alterados, segue a nova conta bancária.",
]


def test_a_change_that_is_not_happening_is_not_a_request(store, settings):
    from test_fraud_review import _mail

    for n, body in enumerate(NOT_A_CHANGE):
        assert "bank_change" not in _keys(_check(body)), body
        email = _mail(store, settings, f"nochange{n}", subject="Invoice 7781", sender="ar@vendor.example", body=body)
        assert email.category.value != "payment_instruction_change", body


def test_real_changes_in_five_languages_still_block():
    for body in REAL_CHANGES:
        check = _check(body)
        assert "bank_change" in _keys(check) and check.level == "high", body
    assert _cpu_seconds(lambda: _check("pay account " + "1 " * 100_000)) < 2.5
    assert _cpu_seconds(lambda: _check(("use acct " + "x" * 200 + " ") * 950)) < 2.5


# 3. Gift-card program, policy and balance mail counts as an ask only when it asks to buy those cards or send codes.
def test_gift_card_program_mail_with_a_buying_word_elsewhere_is_not_an_ask():
    for body in (
        "Thank you for your gift card program purchase, invoice INV-2201 is attached.",
        "Want to buy for your whole team? Our corporate gift card program makes year-end rewards easy.",
        "Please check the gift card balance before you buy anything else on the account.",
        "Reminder of our gift card policy: do not purchase them with the corporate card.",
        "Purchase order 4410 for the gift card program is attached.",
    ):
        check = _check(body, sender="orders@giftvendor.example")
        assert "gift_cards" not in _keys(check) and check.level == "none", body


def test_gift_card_requests_for_a_program_still_block():
    for body in (
        "I need you to buy 10 Amazon gift cards for our staff rewards program today and send me the codes.",
        "Please purchase five gift cards for the client program and scratch off the backs.",
        "Our gift card program needs 10 more cards, send me the codes by noon.",
    ):
        check = _check(body, subject="Quick favor", sender="ceo.office@gmail.com", history=0)
        assert "gift_cards" in _keys(check) and check.level == "high", body


# 4. Look-alikes: a domain you already have mail from is not a look-alike for being one letter off;
#    a trusted domain written in Unicode or punycode is the same domain.
def test_an_established_domain_one_letter_off_a_trusted_one_is_not_a_lookalike():
    for sender, trusted in (("audit@pwc.com", "pnc.com"), ("auto@usps.com", "ups.com"), ("billing@adt.com", "adp.com")):
        check = assess(TrustContext(domains={trusted}), subject="Statement", body="Attached is our September statement.",
                       sender_name="AR", sender_email=sender, history=4, domain_history=4)
        assert "lookalike_domain" not in _keys(check) and check.level == "none", sender


def test_a_new_lookalike_of_a_trusted_domain_is_still_caught():
    ctx = TrustContext(domains={"taz.com"})
    for domain, history in (("tax.com", 0), ("tazz.com", 0), ("t4z.com", 0), ("tаz.com", 3), ("xn--tz-7kc.com", 3)):
        check = assess(ctx, subject="Wire today", body="Please wire $48,500 today for invoice 5521.", sender_name="Treasury",
                       sender_email=f"treasury@{domain}", domain_history=history)
        assert "lookalike_domain" in _keys(check) and check.level == "caution", domain


def test_history_is_read_from_the_store(store, settings):
    from test_fraud_review import _mail

    settings.trusted_domains = "pnc.com"
    body = "Attached is the PBC list for the September audit."
    first = _mail(store, settings, "pwc1", subject="PBC list", sender="audit@pwc.com", body=body)
    second = _mail(store, settings, "pwc2", subject="PBC list", sender="lead@pwc.com", body=body)
    assert "lookalike_domain" in {s["key"] for s in store.fraud_check(first.id)["signals"]}
    assert "lookalike_domain" not in {s["key"] for s in store.fraud_check(second.id)["signals"]}


def test_a_trusted_domain_in_unicode_or_punycode_is_the_same_domain():
    for trusted, sender in (("müller.de", "ap@xn--mller-kva.de"), ("xn--mller-kva.de", "ap@müller.de")):
        check = assess(TrustContext(domains={trusted}), subject="Rechnung", body="Rechnung 5521 anbei.",
                       sender_name="AP", sender_email=sender)
        assert "lookalike_domain" not in _keys(check) and check.trust == "domain", sender
    check = assess(TrustContext(domains={"müller.de"}), subject="Rechnung", body="Rechnung 5521 anbei.",
                   sender_name="AP", sender_email="ap@muller.de")
    assert "lookalike_domain" in _keys(check)


# 5. A colleague's display name with accents or a curly apostrophe is still recognised when a stranger borrows it.
def test_borrowed_accented_display_names_are_caught(store, settings):
    from controller_inbox.models import RawMessage
    from controller_inbox.pipeline import process_message
    from test_fraud_review import NOW

    settings.trusted_domains = "ourco.example"

    def mail(msg_id, name, sender, subject, body):
        raw = RawMessage(id=msg_id, subject=subject, sender_name=name, sender_email=sender, received_at=NOW,
                         body_text=body, body_preview=body[:200], has_attachments=False, source="folder")
        return process_message(raw, store, settings, now=NOW)

    for n, (name, borrowed) in enumerate((("José García", "José García"), ("Dana O’Brien", "Dana O'Brien"),
                                          ("Zoë Kim", "Zoë Kim"), ("Dana Cho", "Dаna Cho"))):
        mail(f"colleague{n}", name, f"person{n}@ourco.example", "Staff meeting", "See you at 10.")
        email = mail(f"stranger{n}", borrowed, f"person{n}.ceo@gmail.com", "Wire today",
                     "Please wire $48,500 today for invoice 5521.")
        assert "display_name_spoof" in {s["key"] for s in store.fraud_check(email.id)["signals"]}, borrowed
