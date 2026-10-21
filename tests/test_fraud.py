from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from controller_inbox import fraud, web
from controller_inbox.classify import classify_document, normalize_text
from controller_inbox.extract import html_to_text
from controller_inbox.folder_mail import ingest_folder
from controller_inbox.fraud import (
    TrustContext,
    assess,
    learned_weights,
    record_fraud_verdict,
    set_domain_trust,
    strip_notices,
)
from controller_inbox.models import (
    ActionStatus,
    DocumentType,
    EmailRecord,
    ExtractedFields,
    Importance,
    RawAttachment,
    RawMessage,
)
from controller_inbox.pipeline import ingest_demo, process_message
from controller_inbox.reading import FRAUD_SUMMARY, VERIFY_TITLE, _warns, overlay_reading
from helpers import PAGE

NOW = datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc)
BANK_CHANGE = (
    "Our bank details have changed. Please use the following account for the next payment: "
    "routing 021000021 account 9988776612."
)


def _mail(store, settings, msg_id, *, subject, sender, body, name="", reply_to="", attachments=(), minutes=0):
    raw = RawMessage(
        id=msg_id,
        subject=subject,
        sender_name=name or sender.split("@")[0].title(),
        sender_email=sender,
        received_at=NOW - timedelta(minutes=minutes),
        body_text=body,
        body_preview=body[:200],
        has_attachments=bool(attachments),
        source="folder",
        reply_to=reply_to,
        attachments=[
            RawAttachment(id=str(i), filename=fname, content_type="text/plain", size_bytes=len(data), content=data)
            for i, (fname, data) in enumerate(attachments)
        ],
    )
    return process_message(raw, store, settings, now=NOW)


def _level(store, email_id):
    return store.fraud_check(email_id)["level"]


def test_unknown_vendor_changing_bank_details_is_blocked(store, settings):
    email = _mail(store, settings, "m1", subject="Payment update", sender="ar@newvendor.example", body=BANK_CHANGE)
    assert email.category == DocumentType.PAYMENT_INSTRUCTION_CHANGE
    assert {"fraud_risk", "do_not_process"} <= set(email.flags)
    check = store.fraud_check("m1")
    assert check["level"] == "high"
    assert {"bank_change", "account_numbers", "first_contact"} <= {s["key"] for s in check["signals"]}
    assert store.fraud_log(events=("flagged",))[0]["email_id"] == "m1"


def test_anti_fraud_footer_and_quoted_thread_do_not_flag(store, settings):
    footer = _mail(
        store,
        settings,
        "f1",
        subject="Invoice 7781",
        sender="billing@supplier.example",
        body=(
            "Please find invoice 7781 attached.\n\n"
            "Reminder: we will never change our bank details by email. If you receive an email saying "
            "our bank details have changed, call us on the number you have on file."
        ),
    )
    assert "fraud_risk" not in footer.flags and "payment_caution" not in footer.flags
    # Sam is a colleague: only someone you trust can say the quoted request was fake.
    settings.mailbox = "me@ourco.example"
    reply = _mail(
        store,
        settings,
        "f2",
        subject="RE: Updated banking instructions",
        sender="sam@ourco.example",
        body=(
            "Called them on the number in the vendor file. It was not them, I have blocked the sender.\n\n"
            "From: ar@newvendor.example\nSent: Monday\n\n" + BANK_CHANGE
        ),
    )
    assert reply.category != DocumentType.PAYMENT_INSTRUCTION_CHANGE
    assert _level(store, "f2") == "none"
    assert {s["key"] for s in store.fraud_check("f2")["signals"]} == {"bank_change_quoted", "trusted_domain"}


def test_trusted_domain_gets_a_caution_note_not_a_block(store, settings):
    settings.trusted_domains = "@Taz.com, partner.example"
    email = _mail(store, settings, "t1", subject="New account", sender="treasury@taz.com", body=BANK_CHANGE)
    assert email.category != DocumentType.PAYMENT_INSTRUCTION_CHANGE
    assert "fraud_risk" not in email.flags
    assert "payment_caution" in email.flags
    assert store.fraud_check("t1")["level"] == "caution"
    ordinary = _mail(store, settings, "t2", subject="Lunch", sender="amy@mail.taz.com", body="Pizza on Friday? Reply by today.")
    assert _level(store, "t2") == "none" and "payment_caution" not in ordinary.flags


def test_settings_trusted_domains_accept_at_signs_and_the_mailbox_domain(settings):
    settings.trusted_domains = "@Taz.com; partner.example  bad"
    settings.mailbox = "me@OurCo.example"
    assert settings.trusted_domain_list == ["taz.com", "partner.example", "ourco.example"]


def test_lookalike_reply_to_and_display_name_signals():
    ctx = TrustContext(domains={"taz.com"}, known={"harborpackaging.com": 12}, names={"dana cho": "dana@taz.com"})
    look = assess(ctx, subject="Invoice", body="Please pay the invoice today.", sender_name="AP", sender_email="ap@harb0rpackaging.com")
    assert "lookalike_domain" in {s.key for s in look.signals} and look.level == "caution"
    embedded = assess(ctx, subject="Wire", body="Please wire the funds.", sender_name="Taz", sender_email="x@taz-payments.net")
    assert "lookalike_domain" in {s.key for s in embedded.signals}
    spoof = assess(ctx, subject="Quick favor", body="Can you buy gift cards for the team?", sender_name="Dana Cho", sender_email="dana.cho@gmail.com")
    keys = {s.key for s in spoof.signals}
    assert {"display_name_spoof", "gift_cards", "freemail_request", "first_contact"} <= keys and spoof.level == "high"
    newsletter = assess(ctx, subject="This week", body="Industry roundup.", sender_name="News", sender_email="news@list.example", reply_to="editor@other.example")
    assert "reply_to_mismatch" in {s.key for s in newsletter.signals} and newsletter.level == "none"


def test_notice_sentences_are_removed_but_real_requests_stay():
    assert "changed" not in strip_notices("We will never change our bank details by email. Beware of scams.")
    kept = strip_notices("Due to fraud on our old account, our bank details have changed.")
    assert "bank details have changed" in kept


def test_not_fraud_for_this_email_refiles_it_and_survives_a_redrop(store, settings):
    _mail(store, settings, "v1", subject="Payment update", sender="ar@vendor.example", body=BANK_CHANGE)
    result = record_fraud_verdict(store, settings, "v1", verdict="safe", scope="email", note="Called Pat on file")
    email = result["email"]
    assert email.category != DocumentType.PAYMENT_INSTRUCTION_CHANGE
    assert "fraud_cleared" in email.flags and "fraud_risk" not in email.flags
    assert not any(a.source == "fraud_rule" for a in email.actions)
    log = store.fraud_log(events=("marked_safe",))
    assert log[0]["email_id"] == "v1" and "Called Pat" in log[0]["note"]
    assert any(s["key"] == "bank_change" for s in log[0]["signals"])

    again = _mail(store, settings, "v1", subject="Payment update", sender="ar@vendor.example", body=BANK_CHANGE)
    assert "fraud_cleared" in again.flags and "fraud_risk" not in again.flags


def test_trusting_a_domain_rechecks_its_mail_and_reporting_a_sender_blocks_it(store, settings):
    _mail(store, settings, "d1", subject="Bank change", sender="pat@acme.example", body=BANK_CHANGE)
    _mail(store, settings, "d2", subject="Invoice 44", sender="lee@acme.example", body="Invoice 44 attached. Please pay by Friday.")
    assert "fraud_risk" in store.get_email("d1").flags

    set_domain_trust(store, settings, "@Acme.example", note="Our parent company")
    assert "fraud_risk" not in store.get_email("d1").flags
    assert _level(store, "d1") == "caution"
    assert store.fraud_log(events=("trusted",))[0]["sender_email"] == "@acme.example"

    record_fraud_verdict(store, settings, "d2", verdict="fraud", scope="sender")
    assert "fraud_risk" in store.get_email("d2").flags
    later = _mail(store, settings, "d3", subject="Hello", sender="lee@acme.example", body="Just checking in.")
    assert "fraud_risk" in later.flags
    assert "reported" in {s["key"] for s in store.fraud_check("d3")["signals"]}


def test_free_mail_domains_cannot_be_trusted_wholesale(store, settings):
    _mail(store, settings, "g1", subject="Hi", sender="someone@gmail.com", body="Hello")
    with pytest.raises(ValueError, match="free email"):
        record_fraud_verdict(store, settings, "g1", verdict="safe", scope="domain")
    with pytest.raises(ValueError, match="free email"):
        set_domain_trust(store, settings, "gmail.com")


def test_verdicts_teach_how_much_a_signal_counts(store, settings):
    for n in range(3):
        _mail(store, settings, f"w{n}", subject="Wire", sender=f"cfo{n}@unknown{n}.example",
              body="Please wire the funds today, our bank details have changed.")
        record_fraud_verdict(store, settings, f"w{n}", verdict="safe")
    weights = learned_weights(store)
    assert weights["pressure"] < 1 and weights["payment_request"] < 1
    # "Not fraud" answers never weaken a bank-change request: it must still block.
    assert weights["bank_change"] == 1.0


def test_model_alone_cannot_block_an_email(store, settings):
    email = _mail(store, settings, "x1", subject="Invoice 9", sender="ap@ok.example", body="Invoice 9 for $500 attached.")
    overlay_reading(email, {"category": "payment_instruction_change", "folder": "important", "importance": "critical", "summary": "Vendor changed bank."})
    assert email.category != DocumentType.PAYMENT_INSTRUCTION_CHANGE
    assert "fraud_risk" not in email.flags
    assert {"model_said_bank_change", "payment_caution"} <= set(email.flags)
    assert any("did not flag" in reason for reason in email.importance_reasons)


def test_bank_change_inside_an_attachment_counts_less_than_in_the_email(store, settings):
    _mail(store, settings, "a0", subject="Hello", sender="ap@steady.example", body="Hi", minutes=60)
    email = _mail(
        store,
        settings,
        "a1",
        subject="Invoice 12",
        sender="ap@steady.example",
        body="Invoice 12 attached.",
        attachments=[("invoice-12.txt", b"Invoice 12. Please note our bank details have changed.")],
    )
    assert "fraud_risk" not in email.flags
    assert "payment_caution" in email.flags
    check = store.fraud_check("a1")
    assert [s["key"] for s in check["signals"]] == ["bank_change_attachment"]


def test_a_cleared_bank_change_request_is_a_wire_request_not_other(store, settings, as_of_now):
    ingest_demo(store, settings, now=as_of_now)
    fraud.record_fraud_verdict(store, settings, "demo-bec-wire", verdict="safe", scope="email", now=as_of_now)
    email = store.get_email("demo-bec-wire")
    assert email.category == DocumentType.WIRE_ACH_REQUEST, "a request to send money, cleared but still checked by phone"
    assert "fraud_risk" not in email.flags and "payment_caution" in email.flags


# "Our usual account is on hold / closed" with no payment or bank word is no bank change: a supplier's credit-hold
# notice or a customer-account consolidation note doesn't make a first-time sender's mail high.


@pytest.mark.parametrize("body", [
    "Hi, a reminder that the current account is on hold until the past-due balance on INV-2201 is paid. "
    "Orders will ship once payment is received.",
    "Your order is on credit hold: the existing account is on hold pending the August payment of $4,210.00.",
    "We merged your two ship-to locations. The old account is closed and future invoices will bill under "
    "customer account 55012.",
])
def test_customer_account_status_is_not_a_bank_change(body):
    ctx = TrustContext(domains={"taz.com"})
    check = assess(ctx, subject="Account status", body=body, sender_name="Acme AR", sender_email="ar@acme-supply.com")
    assert "bank_change" not in [s.key for s in check.signals]
    assert check.level != "high"


def test_known_vendor_credit_hold_is_not_a_bank_change():
    ctx = TrustContext(domains={"taz.com"}, known={"ar@acme-supply.com": 12}, senders={"ar@acme-supply.com": "Acme AR"})
    check = assess(ctx, subject="Credit hold", sender_name="Acme AR", sender_email="ar@acme-supply.com",
                   body="The current account is on hold until the past-due balance on INV-2201 is paid.")
    assert "bank_change" not in [s.key for s in check.signals]


# Guards against: Classic BEC wording that redirects payment to another account is not read as a bank change, so a
# first-time sender gets no fraud flag at all (level "none").
@pytest.mark.parametrize("body", [
    "Due to an ongoing audit our usual account is on hold. Please remit payment for INV-1001 to the alternate account below.",
    "Please make payment for the open invoices to our new beneficiary account.",
    "Our previous account can no longer receive payments, please use the alternative account details below.",
])
def test_redirected_payment_is_flagged(body):
    ctx = TrustContext(domains={"taz.com"})
    check = assess(ctx, subject="Outstanding invoices", body=body, sender_name="Acme Accounts",
                   sender_email="accounts@acme-supply.com")
    assert check.level == "high", [s.key for s in check.signals]


BODY = (
    "Hi Maya,\n\nPlease see the attached letter on our letterhead and update your vendor records accordingly "
    "before the next payment run.\n\nRegards,\nTom\n\n"
    "-----Original Message-----\nFrom: Maya Chen <maya@taz.com>\nSent: Monday, October 5, 2026 9:12 AM\n"
    "Subject: Invoice INV-1001\n\nHi Tom, payment is scheduled for Friday."
)


LETTER = "Northwind Supplies. Effective immediately our bank details have changed. New account 12345678 sort code 20-00-00."


# Guards against: A first-time sender from a domain never seen, untrusted, writes "RE: Updated bank details" over a
# forged "-----Original Message-----" block (whose own Subject is something else), asks to "update your vendor
# records", and attaches the bank-change letter. Because the subject starts with RE: and the body has a quote
# marker, the subject is dropped from the sender's own words, even on the stranger path. Result: only "caution", so
# the email is not blocked and its letter is opened and coded like any other.
def test_stranger_reply_with_bank_change_subject_is_blocked():
    ctx = TrustContext(domains={"taz.com"})
    check = assess(
        ctx,
        subject="RE: Updated bank details for Northwind",
        body=BODY,
        sender_name="Tom Reed",
        sender_email="tom@northwind-supplies.com",
        attachments=[("letter.pdf", LETTER)],
    )
    assert check.level == "high", (check.score, [(s.key, s.points) for s in check.signals])


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


# The usual anti-fraud footers are warnings, not requests.
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


# A change the sender says is not happening, a question, an audit request or a ledger account is not a request.
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


# Gift-card program, policy and balance mail counts as an ask only when it asks to buy those cards or send codes.
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


# Look-alikes: a domain you already have mail from is not a look-alike for being one letter off;
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
    from controller_inbox.fraud import record_fraud_verdict
    from test_fraud_review import _mail

    def lookalike(email):
        return "lookalike_domain" in {s["key"] for s in store.fraud_check(email.id)["signals"]}

    settings.trusted_domains = "pnc.com"
    body = "Attached is the PBC list for the September audit."
    first = _mail(store, settings, "pwc1", subject="PBC list", sender="audit@pwc.com", body=body)
    second = _mail(store, settings, "pwc2", subject="PBC list", sender="lead@pwc.com", body=body)
    assert lookalike(first) and lookalike(second), "a look-alike's flagged first email doesn't vouch for its second"
    record_fraud_verdict(store, settings, first.id, verdict="safe")
    third = _mail(store, settings, "pwc3", subject="PBC list", sender="audit@pwc.com", body=body)
    assert not lookalike(third), "once the user says the domain's mail isn't fraud, it is one they hear from"
    # Mail that came in before the trusted domain was added was never flagged: it counts too.
    settings.trusted_domains = ""
    _mail(store, settings, "adt1", subject="Statement", sender="billing@adt.com", body="Your September statement.")
    settings.trusted_domains = "adp.com"
    later = _mail(store, settings, "adt2", subject="Statement", sender="billing@adt.com", body="Your October statement.")
    assert not lookalike(later)


def test_a_trusted_domain_in_unicode_or_punycode_is_the_same_domain():
    for trusted, sender in (("müller.de", "ap@xn--mller-kva.de"), ("xn--mller-kva.de", "ap@müller.de")):
        check = assess(TrustContext(domains={trusted}), subject="Rechnung", body="Rechnung 5521 anbei.",
                       sender_name="AP", sender_email=sender)
        assert "lookalike_domain" not in _keys(check) and check.trust == "domain", sender
    check = assess(TrustContext(domains={"müller.de"}), subject="Rechnung", body="Rechnung 5521 anbei.",
                   sender_name="AP", sender_email="ap@muller.de")
    assert "lookalike_domain" in _keys(check)


# A colleague's display name with accents or a curly apostrophe is still recognised when a stranger borrows it.
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


# Plain text: only real entities are decoded, not a bare "&not" or "&copy" that is part of what was written.
def test_bare_named_entities_in_plain_text_are_left_as_written():
    assert normalize_text("Smith&notary, Print&copy 2026, R&D") == "Smith&notary, Print&copy 2026, R&D"
    assert normalize_text("ba&#8203;nk ba&#8203nk ba&#x200b;nk AT&amp;T") == "bank bank bank AT&T"


# Evidence in the fraud panel is quoted as the email wrote it, not as the rules read it.
def test_evidence_is_quoted_as_written():
    body = ("Bitte überweisen Sie die Rechnung von Müller GmbH auf unser neues Konto IBAN DE89 3704 0044 0532 0130 00¹ "
            "sofort. Don’t call, I am in a meeting.")
    details = {s.key: s.detail for s in _check(body, subject="Zahlung").signals}
    assert "Müller GmbH" in details["bank_change"] and "0130 00¹" in details["bank_change"], details
    assert details["pressure"] == "Don’t call"
    disguised = {s.key: s.detail for s in _check("Our bаnk details have changed.").signals}
    assert "bаnk details have changed" in disguised["bank_change"]
    wire = {s.key: s.detail for s in _check("Please wire $48,500 today to acct ９９８８７７６６.").signals}
    assert "９９８８７７６６" in wire["account_numbers"]


def test_borrowed_name_is_shown_as_written():
    ctx = TrustContext(domains={"ourco.example"}, names={"José García": "jose@ourco.example"})
    check = assess(ctx, subject="Wire", body="Please wire the funds today.", sender_name="José García",
                   sender_email="jose.garcia.ceo@gmail.com")
    assert {s.key: s.detail for s in check.signals}["display_name_spoof"] == "“José García” usually writes from jose@ourco.example"


# A payment-change warning marked done, but not yet verified by phone, still counts as needing you.
def test_need_you_count_keeps_an_unverified_payment_warning(loaded, settings, as_of_now):
    from datetime import date

    from controller_inbox.digest import build_digest

    def digest():
        return build_digest(loaded, as_of=date(2026, 9, 22), generated_at=as_of_now, tz=settings.tz, save=False)

    before = digest()
    loaded.set_done("demo-bec-wire", True)
    marked = digest()
    assert marked["focus"][0]["email_id"] == "demo-bec-wire"
    assert marked["kpis"]["need_you"] == before["kpis"]["need_you"]
    for action in loaded.get_email("demo-bec-wire").actions:
        loaded.set_action_status(action.id, "done")
    verified = digest()
    assert "demo-bec-wire" not in [row["email_id"] for row in verified["focus"]]
    assert verified["kpis"]["need_you"] == before["kpis"]["need_you"] - 1


def _check_new_sender(body, *, subject="Invoice 5521", sender="ar@acme-billing.example", history=0, ctx=None, name="Acme AR", **extra):
    return assess(ctx or TrustContext(), subject=subject, body=body, sender_name=name, sender_email=sender, history=history, **extra)


# "If you get a chance please…" is a polite request, not an "if you receive an email saying…" warning.
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
    check = _check_new_sender(body, subject="Quick request")
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
    check = _check_new_sender(f"Please find invoice 5521 attached.\n\n{notice}")
    assert not _keys(check) & {"bank_change", "gift_cards"} and check.level == "none", notice
    assert "invoice 5521" in strip_notices(f"Please find invoice 5521 attached. {notice}")


# A plain-text warning wrapped onto a second line is still one warning.
@pytest.mark.parametrize(
    "notice",
    [
        "To protect you from fraud: if you receive an email from anyone\nsaying our bank details have changed, call us first.",
        "Note that we will never contact you by\nemail to say our bank details have changed.",
        "Note that we will never contact you by email to say\nour bank details have changed.",
    ],
)
def test_a_wrapped_anti_fraud_notice_does_not_block(notice):
    check = _check_new_sender(f"Hi,\n\nInvoice 5521 for $8,400 is attached.\n\n{notice}\n\nThanks,\nJane")
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
    check = _check_new_sender(body)
    assert "bank_change" in _keys(check) and check.level == "high", body


# Warnings that report someone else's claim, and requests for the reader's own details, do not block.
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
    check = _check_new_sender(f"Invoice 5521 is attached. {notice}")
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
        check = _check_new_sender(body, subject=subject, history=history)
        assert "bank_change" not in _keys(check) and check.level == "none", body


def test_vendor_onboarding_reply_asking_for_bank_details_does_not_block():
    body = (
        "Thanks Jane, happy to set you up as a supplier. Please send us your updated bank details on letterhead "
        "so we can add you to the vendor master.\n\nFrom: Jane Lee\nSent: Monday\n\nPlease set us up as a vendor."
    )
    check = _check_new_sender(body, subject="RE: New vendor setup", sender="ap@buyer.example", history=6)
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
    check = _check_new_sender(body)
    assert "bank_change" in _keys(check) and check.level == "high", body


# "eGift cards" are gift cards; a rule against buying them is not a request.
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
    check = _check_new_sender(body, subject="Quick favor", sender="ceo.office@gmail.com")
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
    check = _check_new_sender(body, subject="Gift cards", sender="policy@ourco.example")
    assert "gift_cards" not in _keys(check) and check.level == "none", body


# More ways of asking for a bank change.
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
    check = _check_new_sender(body, subject="Hello")
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
    check = _check_new_sender(body, subject="Hello")
    assert "bank_change" not in _keys(check) and check.level == "none", body


def test_the_senders_own_earlier_message_is_not_a_reported_claim():
    for body in (
        "Please beware, as our letter said our bank details have changed.",
        "Beware of delays: our emails told you our bank details have changed.",
    ):
        check = _check_new_sender(body)
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
        assert _check_new_sender(body, ctx=ctx, history=5).level == "high", body


BASE = "Hi,\n\nOur banking details have changed. Please update your records and send the next payment to the new account."


def _check_known_sender(body, *, subject="Payment update", sender="ap@vendor.example", history=3, ctx=None):
    return assess(ctx or TrustContext(), subject=subject, body=body, sender_name="Vendor AP", sender_email=sender, history=history)


def _mail_plain(store, settings, msg_id, *, subject, sender, body, minutes=0):
    raw = RawMessage(
        id=msg_id, subject=subject, sender_name=sender.split("@")[0].title(), sender_email=sender,
        received_at=NOW - timedelta(minutes=minutes), body_text=body, body_preview=body[:200],
        has_attachments=False, source="folder",
    )
    return process_message(raw, store, settings, now=NOW)


# A notice word in front of a real request no longer hides it.
@pytest.mark.parametrize(
    "body",
    [
        "Hi,\n\nPlease be aware our banking details have changed, update your records and send payment to the new account below.",
        "Hi,\n\nAlways confirm: our banking details have changed, so send payment to the new account below.",
        "We will never ask for gift cards, but our banking details have changed.",
    ],
)
def test_warning_words_do_not_hide_a_bank_change(body):
    check = _check_known_sender(body)
    assert "bank_change" in {s.key for s in check.signals} and check.level == "high"


def test_real_notices_are_still_dropped():
    assert strip_notices("If you receive an email saying our bank details have changed, call us on the number on file.") == ""
    assert strip_notices("We will never change our bank details by email. Beware of scams.") == ""


# Disclaimer paragraphs and quote markers.
def test_bank_change_inside_a_disclaimer_paragraph_blocks():
    check = _check_known_sender("Hi,\n\nThis email is confidential. Our banking details have changed, please update your records.")
    assert check.level == "high" and "bank_change" in {s.key for s in check.signals}


@pytest.mark.parametrize(
    "body",
    [
        "Hi,\n> Our banking details have changed. Please send funds to the new account.",
        "Hi, please process per below.\n\nFrom: Jane Vendor\nOur banking details have changed. Please update your records.",
    ],
)
def test_bank_change_below_a_quote_marker_gets_at_least_a_caution(body):
    assert _check_known_sender(body, history=0).level in {"caution", "high"}


def test_a_colleague_disowning_the_quoted_scam_stays_quiet():
    body = "It was not them, I blocked the sender.\n\nFrom: ar@x.example\nSent: Monday\n\nOur banking details have changed."
    colleagues = TrustContext(domains={"ourco.example"})
    assert _check_known_sender(body, subject="RE: bank", sender="sam@ourco.example", history=0, ctx=colleagues).level == "none"


# "Not fraud" answers cannot weaken the block.
def test_not_fraud_answers_never_weaken_a_bank_change(store, settings):
    for n in range(4):
        _mail_plain(store, settings, f"w{n}", subject="Bank", sender=f"ap@v{n}.example", body=BASE)
        record_fraud_verdict(store, settings, f"w{n}", verdict="safe")
    weights = learned_weights(store)
    assert weights["bank_change"] >= 1.0
    assert _check_known_sender(BASE, ctx=TrustContext(weights=weights)).level == "high"
    assert _check_known_sender(BASE, ctx=TrustContext(weights={"bank_change": 0.5, "first_contact": 0.5})).level == "high"


# Gift cards block, but a thank-you or a Visa card payment does not.
def test_gift_card_request_blocks_even_from_a_known_colleague():
    check = _check_known_sender("Hi, I need you to buy 5 Apple gift cards for a client and send me the codes.", sender="ceo@corp.example")
    assert check.level == "high"


@pytest.mark.parametrize("body", ["Thanks for the Amazon gift card, very kind!", "We accept payment by Visa card or ACH."])
def test_gift_card_mentions_without_an_ask_do_not_count(body):
    assert "gift_cards" not in {s.key for s in _check_known_sender(body).signals}


def test_trusted_sender_bank_change_is_a_caution():
    ctx = TrustContext(domains={"vendor.example"})
    assert _check_known_sender(BASE, ctx=ctx).level == "caution"


# Wider bank-change wording.
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
    assert "bank_change" in {s.key for s in _check_known_sender(body).signals}


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


# A free-mail mailbox does not make every sender at that provider trusted.
def test_freemail_mailbox_does_not_trust_every_sender_there(tmp_path, store):
    from controller_inbox.config import Settings
    from controller_inbox.fraud import trust_context

    settings = Settings(data_dir=tmp_path, inbox_dir=tmp_path / "inbox", mailbox="controller@gmail.com", _env_file=None)
    ctx = trust_context(store, settings)
    check = assess(ctx, subject="Bank change", body="Our bank details have changed. Please pay invoice 5521 to the new account.",
                   sender_name="Acme AP", sender_email="acme.ap.billing@gmail.com")
    assert check.level == "high", (check.level, check.score, [s.key for s in check.signals])


# Country and ISP free-mail domains cannot be trusted as a company domain.
@pytest.mark.parametrize("domain", ["hotmail.co.uk", "yahoo.co.uk", "outlook.fr", "comcast.net"])
def test_country_freemail_cannot_be_trusted_as_a_domain(settings, store, domain):
    from controller_inbox.fraud import set_domain_trust

    with pytest.raises(ValueError):
        set_domain_trust(store, settings, domain)


# "We are now banking with…" and "we moved our account to…" are bank changes too.
@pytest.mark.parametrize("body", [
    "We are now banking with Wells Fargo. Please direct all future payments there.",
    "We moved our account to Chase. Please use the details below for future payments.",
])
def test_bank_switch_wording_is_high(body):
    check = assess(TrustContext(domains={"taz.com"}), subject="Payment update", body=body,
                   sender_name="Vendor AP", sender_email="ap@vendorco.com")
    assert check.level == "high", (check.level, [s.key for s in check.signals])


# ...but an invoice's standing payment footer and a move to new accounting software are not.
@pytest.mark.parametrize("body", [
    "Please find attached invoice INV-2231, total $4,180.00, due October 30, 2026.\n"
    "Please use the bank details below for all payments.\nBank: Chase\nAccount name: Harbor Packaging LLC",
    "We have moved our accounts to Xero, so from next month our invoices will come from Xero.\n"
    "Invoice INV-2232 for $1,200.00 is attached.",
])
def test_invoice_footer_and_software_move_are_not_bank_changes(body):
    from controller_inbox.classify import PAYMENT_CHANGE_RE
    from controller_inbox.fraud import CAUTION_AT

    assert PAYMENT_CHANGE_RE.search(body) is None
    check = assess(TrustContext(), subject="Invoice INV-2231", body=body, sender_name="Harbor AP",
                   sender_email="ap@harbor.example", history=12, domain_history=12)
    assert check.score < CAUTION_AT, (check.level, [s.key for s in check.signals])


@pytest.mark.parametrize("body", [
    "We moved our account to Chase Bank, effective today.",
    "We have switched our accounts to a new bank. Please update your records.",
    "Please note we have moved our accounts to Bank of America. Please update your records.",
    "We moved our banking to Chase, please update your records.",
])
def test_moving_accounts_to_a_bank_is_still_a_bank_change(body):
    from controller_inbox.classify import PAYMENT_CHANGE_RE

    assert PAYMENT_CHANGE_RE.search(body)


def test_moving_accounts_to_the_bank_for_collection_is_not_a_bank_change():
    from controller_inbox.classify import PAYMENT_CHANGE_RE

    assert PAYMENT_CHANGE_RE.search("We transferred our accounts to the bank for collection last month.") is None


NOW_OCT20 = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)


def _raw(id_, subject, body, *, sender="ap@northwind-supply.com", when=NOW_OCT20, **kw):
    return RawMessage(
        id=id_, subject=subject, sender_name="Northwind AP", sender_email=sender, received_at=when,
        body_text=body, body_preview=body[:240], has_attachments=False, source="folder", **kw,
    )


def test_recheck_clears_caution_the_model_raised(store, settings):
    """overlay_reading puts "payment_caution" on an email the model read as a bank change; any later fraud recheck
    (another copy dropped, a trust change for the domain) scores model_said_bank_change at 12 < CAUTION_AT and
    reassess_email strips the caution although nothing about the email changed."""
    email = process_message(_raw("m1", "Account update", "Hi, please note our remittance info is in the attached letter."),
                            store, settings, now=NOW_OCT20)
    overlay_reading(email, {"category": "payment_instruction_change", "importance": "high", "folder": "important",
                            "summary": "Vendor says its remittance details changed."}, now=NOW_OCT20, tz=settings.tz)
    store.upsert_email(email)
    assert "payment_caution" in store.get_email("m1").flags

    # The same email dropped again, now with the letter it mentions: the reading stays, the file is added and the
    # fraud check runs again (pipeline._add_files -> reassess_email).
    again = _raw("m1", "Account update", "Hi, please note our remittance info is in the attached letter.")
    again.attachments = [RawAttachment("letter.txt", "letter.txt", "text/plain", 20, b"Remittance letter, see above.")]
    again.has_attachments = True
    process_message(again, store, settings, now=NOW_OCT20)
    flags = store.get_email("m1").flags
    assert "payment_caution" in flags, flags


def test_model_reading_drops_the_call_before_paying_task_on_a_bank_change_caution(store, settings):
    """A bank change from a trusted vendor is a caution, not a block, and its only safeguard is the task "Confirm the
    bank-detail change by phone before paying". overlay_reading replaces every task with the model's for any email that
    isn't blocked, so after the overnight read the board says "update the vendor record" and nothing says to call."""
    settings.trusted_domains = "northwind-supply.com"
    email = process_message(
        _raw("m2", "New bank details", "Hello, our bank details have changed. Please update your records and pay "
             "invoice INV-8812 to the new account from now on."),
        store, settings, now=NOW_OCT20,
    )
    assert "bank_change_caution" in email.flags, email.flags
    assert any("by phone" in a.title for a in email.actions)

    overlay_reading(email, {"category": "ap_invoice", "importance": "high", "folder": "important",
                            "summary": "Northwind changed bank details; update the vendor and pay INV-8812.",
                            "actions": [{"title": "Update Northwind's bank details in the vendor master", "priority": "high"},
                                        {"title": "Pay invoice INV-8812", "priority": "high"}]},
                    now=NOW_OCT20, tz=settings.tz)
    store.upsert_email(email)
    titles = [a.title for a in store.get_email("m2").actions]
    assert any("by phone" in t for t in titles), titles


def test_bank_change_in_the_html_part_is_not_read_when_the_plain_part_is_a_stub(store, settings):
    """folder_mail._eml_content keeps the HTML part only when there is no text/plain part at all. Mail programs show
    the HTML; a sender (or phishing kit) whose plain alternative is a stub ("View this message in HTML") gets a
    bank-change request past the fraud check: the email is filed with no warning."""
    from email.message import EmailMessage

    from controller_inbox.folder_mail import ingest_folder

    msg = EmailMessage()
    msg["From"] = "Northwind Accounts <accounts@northwind-supply.com>"
    msg["To"] = "ap@taz.com"
    msg["Subject"] = "Payment information"
    msg["Date"] = "Tue, 20 Oct 2026 10:00:00 -0400"
    msg["Message-ID"] = "<pay-info-1@northwind-supply.com>"
    msg.set_content("This message is best viewed in an HTML-capable mail program.")
    msg.add_alternative(
        "<html><body><p>Dear customer,</p><p>Our bank details have changed. Please update your records and send "
        "all future payments to our new account at First Harbor Bank, account 4417 8820 1934.</p></body></html>",
        subtype="html",
    )
    settings.ensure_data_dir()
    (settings.inbox_incoming / "payment information.eml").write_bytes(bytes(msg))
    [email] = ingest_folder(store, settings, now=NOW_OCT20)
    assert "fraud_risk" in email.flags or "payment_caution" in email.flags, (email.flags, email.body_text)


def test_second_forged_thread_from_the_same_fraudster_is_only_a_caution(store, settings):
    """A stranger's bank change written below a forged "From:" line counts as their own words (fraud.assess
    ``stranger``) only while ``history`` is 0, and store.sender_history counts every earlier email from the address,
    the blocked ones too (domain_history leaves flagged mail out). So the fraudster's first attempt is blocked, and the
    same attempt sent again a day later is only a caution: it no longer blocks payment."""

    body = (
        "Please see below and action today.\n\n"
        "-----Original Message-----\nFrom: Northwind AP <ap@northwind-supply.com>\nSent: Monday\nSubject: Remittance\n\n"
        "Our bank details have changed. Please send all payments to our new account, routing 021000021 account 99887766."
    )
    first = process_message(_raw("f1", "RE: Remittance", body, sender="ap@northwind-supp1y.com"), store, settings, now=NOW_OCT20)
    assert "fraud_risk" in first.flags, first.flags
    second = process_message(_raw("f2", "RE: Remittance", body, sender="ap@northwind-supp1y.com", when=NOW_OCT20 + timedelta(days=1)),
                             store, settings, now=NOW_OCT20 + timedelta(days=1))
    assert "fraud_risk" in second.flags, (second.flags, store.fraud_check("f2"))


# Text the reader is never shown does not split the words the fraud check reads.
def test_hidden_html_text_does_not_hide_a_bank_change():
    for html in (
        '<p>Our bank <span style="display:none">zz</span>details have changed.</p>',
        '<p>Our bank d<span style="font-size:0px">q</span>etails have changed.</p>',
        '<p>Our ba<font style="visibility: hidden"><b>x</b>y</font>nk details have changed.</p>',
        '<p>Our bank <span style="mso-hide:all">zz</span>details have changed.</p>',
    ):
        text = html_to_text(html)
        assert text == "Our bank details have changed.", html
        check = assess(TrustContext(), subject="Invoice", body=text, sender_name="AR", sender_email="ar@vendor.example")
        assert check.level == "high", html


def test_visible_html_text_is_kept():
    shown = html_to_text('<p style="font-size:10px">Invoice 5521</p><p style="font-size:0.9em">is attached.</p>')
    assert " ".join(shown.split()) == "Invoice 5521 is attached."
    # A hidden element never closed hides only its own tag, and a long run of them is read quickly.
    assert html_to_text('<span style="display:none">Hi there') == "Hi there"
    start = time.process_time()
    html_to_text('<span style="display:none">x' * 20_000)
    assert time.process_time() - start < 2


# "Not fraud" on a bank-detail change ---------------------------------------------------------------
def test_not_fraud_on_a_bank_change_lifts_the_block_but_keeps_the_caution(store, settings, as_of_now):
    ingest_demo(store, settings, now=as_of_now)
    fraud.record_fraud_verdict(store, settings, "demo-bec-wire", verdict="safe", scope="email", now=as_of_now)
    email = store.get_email("demo-bec-wire")
    check = store.fraud_check("demo-bec-wire")
    assert "fraud_risk" not in email.flags and "fraud_cleared" in email.flags
    # The bank details still changed: a phone call before paying stays on the list.
    assert check["level"] == "caution" and "payment_caution" in email.flags
    assert "phone" in email.summary.lower() and "meetings" not in email.summary.lower()
    titles = [item.title for item in email.actions if item.status == ActionStatus.OPEN]
    assert titles and "phone" in titles[0].lower()

    page = TestClient(web.create_app(settings, store)).get("/inbox/demo-bec-wire").text
    assert "nothing suspicious" not in page.lower() and "double-check before paying" in page.lower()


def test_a_cleared_email_with_a_high_score_does_not_say_nothing_suspicious(store, settings, as_of_now):
    ingest_demo(store, settings, now=as_of_now)
    client = TestClient(web.create_app(settings, store))
    # Payment wording without a bank change: the verdict clears it entirely, and the panel says who cleared it.
    email = store.get_email("demo-bec-wire")
    email.subject = "Urgent wire today"
    email.body_text = "Please process the outstanding $48,500.00 wire today to account 9988776612. Urgent, I am in meetings."
    store.upsert_email(email)
    fraud.record_fraud_verdict(store, settings, "demo-bec-wire", verdict="safe", scope="email", now=as_of_now)
    check = store.fraud_check("demo-bec-wire")
    assert check["level"] == "none"
    page = client.get("/inbox/demo-bec-wire").text.lower()
    assert "nothing suspicious" not in page and "marked not fraud" in page


def _ingest(store, settings) -> dict:
    return {email.subject: store.get_email(email.id) for email in ingest_folder(store, settings)}


# A loose dropped file asking for a bank change is scored as the same words in an email are.
def test_a_loose_bank_change_note_is_held_as_an_email_would_be(store, settings):
    """A dropped .txt saying the bank details changed, wire today, keep it confidential is held as fraud."""
    settings.ensure_data_dir()
    (settings.inbox_incoming / "loose-bank-change.txt").write_bytes(
        b"From the CFO: our bank details have changed. Send the $48,000 wire today to routing 021000021 "
        b"account 11223344. Keep this confidential, I'm in meetings."
    )
    [note] = _ingest(store, settings).values()
    check = store.fraud_check(note.id)
    assert check["level"] == "high", check
    assert "fraud_risk" in note.flags


# "Not fraud" on the sample's bank-change wire leaves one coherent list: the phone call first, then the payment.
def test_not_fraud_on_the_sample_wire_puts_the_phone_call_first(store, settings):
    """After "Not fraud" on demo-bec-wire, the call is the most urgent task, no "process the wire today" task
    contradicts it, and the new tasks are dated on the sample board's day (Sep 22), not today."""
    from controller_inbox.cli import load_sample

    settings.ensure_data_dir()
    load_sample(store, settings)
    client = TestClient(web.create_app(settings, store))
    assert client.post("/api/mail/demo-bec-wire/fraud", json={"choice": "safe:email"}, headers=PAGE).json()["ok"]
    email = store.get_email("demo-bec-wire")
    open_tasks = [item for item in email.actions if item.status.value == "open"]
    titles = [item.title for item in open_tasks]
    assert titles[0] == "Confirm the bank-detail change by phone before paying"
    assert not [t for t in titles if "process" in t.lower()], titles
    rank = ["low", "medium", "high", "critical"]
    call = open_tasks[0]
    assert all(rank.index(call.priority.value) >= rank.index(item.priority.value) for item in open_tasks)
    assert {item.due_date for item in open_tasks if item.due_date} == {"2026-09-22"}
