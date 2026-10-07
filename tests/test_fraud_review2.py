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
