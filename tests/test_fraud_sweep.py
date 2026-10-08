"""Regression tests for the fraud-check sweep: each missed scam is caught and the legitimate mail next to it is not."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from controller_inbox.fraud import TrustContext, assess, learned_weights, record_fraud_verdict, strip_notices
from controller_inbox.models import RawMessage
from controller_inbox.pipeline import process_message

NOW = datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc)


def _check(body, *, subject="Invoice 5521", sender="ar@acme-billing.example", history=0, ctx=None, name="Acme AR", **extra):
    return assess(ctx or TrustContext(), subject=subject, body=body, sender_name=name, sender_email=sender, history=history, **extra)


def _keys(check):
    return {signal.key for signal in check.signals}


# 1. "If you get a chance please…" is a polite request, not an "if you receive an email saying…" warning.
@pytest.mark.parametrize(
    "body",
    [
        "If you get a chance please buy 5 Apple gift cards for the client and send me the codes.",
        "If you get a moment buy 5 Apple gift cards for the client and send me the codes.",
        "If you get a moment please update your records with our new bank details.",
        "If you receive our next invoice please pay it to our new bank account.",
        "If you get this today please update your records with our new bank details.",
    ],
)
def test_a_polite_if_you_get_request_still_blocks(body):
    check = _check(body, subject="Quick request")
    assert _keys(check) & {"gift_cards", "bank_change"} and check.level == "high", body


@pytest.mark.parametrize(
    "notice",
    [
        "If you receive an email saying our bank details have changed please call us on the number you have on file.",
        "If you receive a request to update our bank details kindly call us before paying.",
        "If you receive an email asking you to please update our bank details, call us first.",
        "If you get a chance, please review the attached remittance advice.",
    ],
)
def test_if_you_receive_warnings_without_a_comma_stay_quiet(notice):
    check = _check(f"Please find invoice 5521 attached.\n\n{notice}")
    assert not _keys(check) & {"bank_change", "gift_cards"} and check.level == "none", notice
    assert "invoice 5521" in strip_notices(f"Please find invoice 5521 attached. {notice}")


# 4. A plain-text warning wrapped onto a second line is still one warning.
@pytest.mark.parametrize(
    "notice",
    [
        "To protect you from fraud: if you receive an email from anyone\nsaying our bank details have changed, call us first.",
        "Note that we will never contact you by\nemail to say our bank details have changed.",
        "Note that we will never contact you by email to say\nour bank details have changed.",
    ],
)
def test_a_wrapped_anti_fraud_notice_does_not_block(notice):
    check = _check(f"Hi,\n\nInvoice 5521 for $8,400 is attached.\n\n{notice}\n\nThanks,\nJane")
    assert "bank_change" not in _keys(check) and check.level == "none", notice


@pytest.mark.parametrize(
    "body",
    [
        "Hi,\nplease note that from 1 November all payments for our invoices\nshould be sent to the new account below.",
        "If you receive an invoice from us\nOur bank details have changed\nPlease use the account below",
        "Please note our bank details have changed; if you receive\nan invoice from us please pay it to the new account.",
    ],
)
def test_a_wrapped_bank_change_request_still_blocks(body):
    check = _check(body)
    assert "bank_change" in _keys(check) and check.level == "high", body


# 5. Warnings that report someone else's claim, and requests for the reader's own details, do not block.
@pytest.mark.parametrize(
    "notice",
    [
        "Beware of emails claiming our bank details have changed.",
        "Please be aware that fraudsters may send emails claiming our bank details have changed. Always call us to verify.",
        "Fraudsters may impersonate us and claim our bank details have changed.",
        "Criminals are sending emails purporting to come from us stating our bank details have changed. Please verify by phone.",
        "Be vigilant: criminals may pretend our bank details have changed.",
        "If anyone tells you our bank details have changed, call us.",
        "Any notification of a change of bank details should be verified by calling us.",
        "Any change of bank details must be verified by phone.",
    ],
)
def test_reported_claims_in_a_warning_do_not_block(notice):
    check = _check(f"Invoice 5521 is attached. {notice}")
    assert "bank_change" not in _keys(check) and check.level == "none", notice


@pytest.mark.parametrize(
    "body, subject",
    [
        ("Can you please send me your updated bank details so we can set you up in our system?", "Vendor setup"),
        ("Please send us your new bank details on letterhead so we can update the vendor master.", "Vendor setup"),
        ("ACH return R03 - no account / unable to locate account. Please provide updated bank information.", "ACH return"),
        ("Please find attached remittance advice for invoice 5521. Payment was made by ACH to your bank account ending 9981.",
         "Remittance advice"),
        ("Your account statement for September is ready. Beginning balance $10,000. Ending balance $12,000.", "Your statement"),
    ],
)
def test_asking_for_the_readers_details_and_routine_bank_mail_do_not_block(body, subject):
    for history in (0, 4):
        check = _check(body, subject=subject, history=history)
        assert "bank_change" not in _keys(check) and check.level == "none", body


def test_vendor_onboarding_reply_asking_for_bank_details_does_not_block():
    body = (
        "Thanks Jane, happy to set you up as a supplier. Please send us your updated bank details on letterhead "
        "so we can add you to the vendor master.\n\nFrom: Jane Lee\nSent: Monday\n\nPlease set us up as a vendor."
    )
    check = _check(body, subject="RE: New vendor setup", sender="ap@buyer.example", history=6)
    assert check.level == "none"


@pytest.mark.parametrize(
    "body",
    [
        "Please be aware our bank details have changed.",
        "Beware, our bank details have changed and the new account is attached.",
        "We are writing to inform you that our bank details have changed. Please use the account below.",
        "Fraud is on the rise, so we have moved banks: our new bank details are attached.",
        "Please note our updated bank details for all future payments.",
        "Please update your records with our new bank details.",
    ],
)
def test_a_change_asserted_next_to_warning_words_still_blocks(body):
    check = _check(body)
    assert "bank_change" in _keys(check) and check.level == "high", body


# 8. "eGift cards" are gift cards; a rule against buying them is not a request.
@pytest.mark.parametrize(
    "body",
    [
        "Please buy 5 Amazon eGift cards and send me the codes.",
        "Please buy 5 Amazon egiftcards and send me the codes.",
        "Need eGift cards urgently for staff, buy them today.",
        "Do not call me, I am in a meeting. Purchase 5 e-gift cards and send me the codes.",
    ],
)
def test_egift_card_requests_block(body):
    check = _check(body, subject="Quick favor", sender="ceo.office@gmail.com")
    assert "gift_cards" in _keys(check) and check.level == "high", body


@pytest.mark.parametrize(
    "body",
    [
        "Reminder: per policy, employees may not purchase gift cards with the corporate card.",
        "We never buy gift cards for anyone who emails or texts us asking for them.",
        "Thank you for your eGift card order. Your order has shipped.",
    ],
)
def test_gift_card_rules_and_receipts_do_not_block(body):
    check = _check(body, subject="Gift cards", sender="policy@ourco.example")
    assert "gift_cards" not in _keys(check) and check.level == "none", body


# 6. More ways of asking for a bank change.
@pytest.mark.parametrize(
    "body",
    [
        "Please change our bank details to the following.",
        "Please amend our bank details on your system.",
        "Can you change the banking info we have on file?",
        "We have updated our banking information. Please see attached.",
        "Our new IBAN is GB82WEST12345698765432.",
        "Please update the IBAN for our company to GB82WEST12345698765432.",
        "Kindly update the beneficiary account details as below.",
        "Payments should now be made to the account below.",
        "All future payments must be sent to our account at Barclays.",
        "We are moving to a new bank. Please update your vendor file.",
        "Hemos cambiado de banco.",
        "Nous avons changé de banque.",
        "Wir haben die Bank gewechselt.",
    ],
)
def test_more_bank_change_requests_block(body):
    check = _check(body, subject="Hello")
    assert "bank_change" in _keys(check) and check.level == "high", body


@pytest.mark.parametrize(
    "body",
    [
        "Invoice 5521 attached. Payment should be made to the account below: Chase, account ****9981.",
        "Reminder: please change your bank details in the Workday portal before payroll closes.",
        "We have not updated our banking information, so please keep paying the usual account.",
        "Your beneficiary information has been updated in the retirement plan portal.",
        "Your new customer account number is 5512340 for our online store.",
        "No hemos cambiado de banco.",
        "Nous n'avons pas changé de banque.",
        "Wir haben die Bank nicht gewechselt.",
    ],
)
def test_lookalike_wording_that_asks_for_no_change_does_not_block(body):
    check = _check(body, subject="Hello")
    assert "bank_change" not in _keys(check) and check.level == "none", body


def test_the_senders_own_earlier_message_is_not_a_reported_claim():
    for body in (
        "Please beware, as our letter said our bank details have changed.",
        "Beware of delays: our emails told you our bank details have changed.",
    ):
        check = _check(body)
        assert "bank_change" in _keys(check) and check.level == "high", body


# The hard rule: "not fraud" answers on other mail never weaken the block on the wording caught above.
def test_not_fraud_answers_do_not_weaken_the_newly_caught_wording(store, settings):
    for n in range(4):
        body = "If you get a moment please update your records with our new bank details."
        raw = RawMessage(
            id=f"n{n}", subject="Bank", sender_name="AP", sender_email=f"ap@v{n}.example", received_at=NOW,
            body_text=body, body_preview=body, has_attachments=False, source="folder",
        )
        assert process_message(raw, store, settings, now=NOW).category.value == "payment_instruction_change"
        record_fraud_verdict(store, settings, f"n{n}", verdict="safe")
    ctx = TrustContext(weights=learned_weights(store))
    for body in ("If you get a moment please update your records with our new bank details.",
                 "If you get a chance please buy 5 Apple gift cards for the client and send me the codes.",
                 "Please change our bank details to the following."):
        assert _check(body, ctx=ctx, history=5).level == "high", body
