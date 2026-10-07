"""Regression tests for the fraud-rules review: notices, disguised text, wording, trust, dates, digest, corrections."""

from __future__ import annotations

from datetime import date, datetime, timezone

from controller_inbox.classify import classify_document, normalize_text
from controller_inbox.digest import build_digest
from controller_inbox.extract import extract_fields, html_to_text, parse_due_date
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


# 3. Ordinary vendor mail that says "account" or "payment" is not a bank change.
ORDINARY_VENDOR_MAIL = [
    ("Introduction", "Please note the change of account manager: Jane Lee will be your new contact from November."),
    ("Terms", "There are changes to payment terms starting next quarter: net 45 instead of net 30."),
    ("Schedule", "Heads up on a change in payment schedule for the lease, the next one is on the 15th."),
    ("Account update", "Your account information has been updated. If you did not make this change, contact support."),
    ("Receipt", "Thanks! Your payment information has been updated and your card ending 4242 will be charged on renewal."),
    ("Portal", "Log in to the new account portal to download your statements."),
    ("Invoice 881 coding", "Please use the following account for coding this invoice: 6100-200 Office Supplies."),
    ("Policy", "Your policy account number has been updated to reflect the renewal."),
    ("Welcome", "Welcome! Your new account details are below. Username: jdoe"),
    ("Card expired", "Please update the payment information on your account; your card has expired."),
    ("Billing", "Use the new account portal for all payments."),
    ("Holidays", "Our banking hours have changed for the holidays."),
    ("W-9", "Please send the signed W-9 to the new account manager."),
    ("Close", "Changes to bank reconciliation process: please finish by WD3."),
    ("Invoices", "Please send invoices for this account going forward to ap@newco.example."),
]


def test_ordinary_vendor_mail_is_not_locked(store, settings):
    for n, (subject, body) in enumerate(ORDINARY_VENDOR_MAIL):
        email = _mail(store, settings, f"vendor{n}", subject=subject, sender="sales@vendor.example", body=body,
                      attachments=[("note.txt", body.encode())])
        assert email.category != DocumentType.PAYMENT_INSTRUCTION_CHANGE, body
        assert not attachments_locked(email), body
        assert "bank_change" not in {s["key"] for s in store.fraud_check(f"vendor{n}")["signals"]}, body


def test_account_manager_intro_with_a_card_attached_is_not_locked(store, settings):
    email = _mail(
        store, settings, "fp1", subject="Introduction", sender="sales@vendor.example",
        body="Please note the change of account manager: Jane Lee will be your new contact from November.",
        attachments=[("jane-lee-card.txt", b"Jane Lee, Account Manager")],
    )
    assert email.category != DocumentType.PAYMENT_INSTRUCTION_CHANGE
    assert not attachments_locked(email)


# 4. Plain bank-change wording, in English and four other languages.
PLAIN_BANK_CHANGES = [
    "Our bank account has changed. Please pay invoice 5521 to account ****9981.",
    "Our bank details were updated last week, please update your records.",
    "Please update the bank account on file for us to the one in the attached letter.",
    "Please find our new account details below and pay invoice 5521 there.",
    "Please use account ****9981 for all payments going forward.",
    "Please use the new account for all future payments.",
    "Our bank is changing, please use the details below.",
    "Our routing number has changed.",
    "Please send this payment to a different account than usual: acct 99887766.",
    "Nuestros datos bancarios han cambiado. Por favor pague la factura 5521 a la cuenta ****9981.",
    "Adjuntamos nuestra nueva cuenta bancaria.",
    "Nos coordonnées bancaires ont changé. Merci de mettre à jour votre fichier.",
    "Veuillez noter notre nouveau RIB ci-joint.",
    "Unsere Bankverbindung hat sich geändert. Bitte überweisen Sie auf das neue Konto.",
    "Neue Bankverbindung ab 1. Oktober.",
    "Nossos dados bancários foram alterados.",
    "Segue nossa nova conta bancária.",
]


def test_plain_bank_change_wording_is_blocked():
    for body in PLAIN_BANK_CHANGES:
        check = _check(body)
        assert "bank_change" in _keys(check) and check.level == "high", body


def test_new_wording_does_not_catch_the_ordinary_mail():
    # The patterns added for item 4 checked against item 3's list, word for word.
    for subject, body in ORDINARY_VENDOR_MAIL:
        assert "bank_change" not in _keys(_check(body, subject=subject, history=5)), body
    for body in ("Su cuenta nueva ha sido creada.", "Ihr neues Konto wurde erstellt.", "Votre nouveau compte a été créé."):
        assert "bank_change" not in _keys(_check(body, history=5)), body


# 5. A trusted colleague forwarding a vendor's bank change still gets a caution.
FORWARD = (
    "Hi Maria, please update Acme in the vendor master per below and pay invoice 5521 this week.\n\n"
    "---------- Forwarded message ----------\nFrom: Acme AR <ar@acme-billing.example>\n\n"
    "Our bank details have changed. Please use the following account for all future payments."
)
FORGED = (
    "Hi, this is not phishing, please process the payment as below.\n\n"
    "From: Acme AR\nSent: Monday\n\nOur bank details have changed. Please use the following account."
)
COLLEAGUES = TrustContext(domains={"ourco.example"})


def test_trusted_colleague_forwarding_a_bank_change_gets_a_caution():
    check = _check(FORWARD, subject="FW: Acme payment", sender="sam@ourco.example", history=40, ctx=COLLEAGUES)
    assert "bank_change_quoted" in _keys(check) and check.level == "caution"


# 6. Only someone you trust can say a quoted request was fake.
def test_anyone_cannot_silence_the_quoted_thread_warning():
    assert _check(FORGED).level == "caution"
    disowned = "It was not them, this is phishing.\n\nFrom: Acme AR\nSent: Monday\n\nOur bank details have changed."
    assert _check(disowned, subject="RE: bank").level == "caution", "a stranger saying so does not count"
    assert _check(disowned, subject="RE: bank", sender="sam@ourco.example", ctx=COLLEAGUES).level == "none"


def test_a_colleague_vouching_for_the_thread_is_not_disowning_it():
    vouched = FORGED.replace("Hi, this is not phishing", "Hi, I checked, this is not phishing")
    assert _check(vouched, sender="sam@ourco.example", ctx=COLLEAGUES).level == "caution"


# 7. "RE:" over a body with no quoted thread: the subject is the sender's own words.
def test_reply_prefix_without_a_quote_does_not_hide_the_subject():
    body = "Hi,\n\nAs discussed, please use the details in the attached letter for invoice 5521."
    check = _check(body, subject="RE: Our bank details have changed")
    assert "bank_change" in _keys(check) and check.level == "high"
    # With the thread quoted below, the subject belongs to the thread, as before.
    quoted = "Thanks, I will call them first.\n\nFrom: Acme AR\nSent: Monday\n\nPlease see below."
    assert "bank_change" not in _keys(_check(quoted, subject="RE: Our bank details have changed"))


# 8. A no-reply address does not discount a bank change or a sender you have never heard from.
def test_no_reply_sender_does_not_remove_the_warning():
    letter = [("remittance-update.pdf", "Please note our bank details have changed. New account ****9981 routing ****0021.")]
    for sender in ("no-reply@acme-billing.example", "notifications@acme-billing.example"):
        check = _check("Please see the attached invoice and remittance update.", sender=sender, history=5, attachments=letter)
        assert "automated_sender" not in _keys(check) and check.level == "caution", sender
    payment = "Please remit invoice 5521 today to account 4410029981."
    assert _check(payment, sender="no-reply@acme-billing.example").level == "caution", "a stranger's no-reply address"
    # A no-reply address you have mail from still counts as machine mail.
    familiar = _check(payment, sender="no-reply@acme-billing.example", history=5)
    assert "automated_sender" in _keys(familiar) and familiar.level == "none"


# 9. "Program" does not hide a gift-card request that says to buy cards or send codes.
def test_gift_card_request_for_a_program_is_still_an_ask():
    ctx = TrustContext(domains={"ourco.example"}, names={"dana cho": "dana@ourco.example"})
    body = "I need you to buy 10 Amazon gift cards for our staff rewards program today and send me the codes."
    check = assess(ctx, subject="Quick favor", body=body, sender_name="Dana Cho", sender_email="dana.cho.ceo@gmail.com")
    assert "gift_cards" in _keys(check) and check.level == "high"


def test_gift_card_program_news_is_not_an_ask():
    for body in (
        "Our gift card program is open to all staff this year; we need sign-ups by Friday.",
        "Your gift card balance is $25; use the codes at checkout.",
    ):
        assert "gift_cards" not in _keys(_check(body)), body


# 10. Look-alikes of a short trusted name, in Unicode or punycode.
def test_lookalikes_of_a_short_trusted_domain_are_caught():
    ctx = TrustContext(domains={"taz.com"})
    for domain in ("tax.com", "tazz.com", "t4z.com", "tаz.com", "xn--tz-7kc.com"):
        check = assess(ctx, subject="Wire today", body="Please wire $48,500 today for invoice 5521.",
                       sender_name="Treasury", sender_email=f"treasury@{domain}")
        assert "lookalike_domain" in _keys(check) and check.level == "caution", domain
    for domain in ("mail.taz.com", "acme.com", "tazmaniafoods.com"):
        check = assess(ctx, subject="Hi", body="Please pay invoice 5521.", sender_name="AP", sender_email=f"ap@{domain}")
        assert "lookalike_domain" not in _keys(check), domain


def test_a_trusted_name_inside_another_domain_is_still_caught():
    # The trusted name and the sender's domain are compared as they read, so letters such as "cl"
    # (read like "d") or "5" (read like "s") in the trusted name do not hide it.
    ctx = TrustContext(domains={"clarkco.com", "harbor5.com", "taz.com"})
    for domain in ("clarkco-payments.net", "harbor5-billing.com", "taz-payments.net"):
        check = assess(ctx, subject="Wire", body="Please wire the funds.", sender_name="AP", sender_email=f"ap@{domain}")
        assert "lookalike_domain" in _keys(check), domain


def test_a_short_name_you_only_hear_from_often_is_not_a_lookalike_target():
    # Mail from your bank (pnc.com) does not make your auditor (pwc.com) a look-alike.
    ctx = TrustContext(known={"pnc.com": 30, "pwc.com": 3})
    check = assess(ctx, subject="PBC list", body="Please pay invoice 5521.", sender_name="Audit", sender_email="audit@pwc.com")
    assert "lookalike_domain" not in _keys(check)


# 11. A month and day with no year, read just after New Year, is last December.
def test_december_date_read_in_january_is_last_year():
    assert parse_due_date("December 28", as_of=date(2027, 1, 3)) == "2026-12-28"
    fields = extract_fields("Reminder: invoice INV-3301 for $4,200.00 was due December 28. Please remit.", as_of=date(2027, 1, 3))
    assert fields.primary_due == "2026-12-28"
    # The other way round, and dates near today, as before.
    assert parse_due_date("January 5", as_of=date(2026, 12, 20)) == "2027-01-05"
    assert parse_due_date("September 30", as_of=date(2026, 10, 7)) == "2026-09-30"
    assert parse_due_date("June 30", as_of=date(2026, 1, 10)) == "2026-06-30"


# 12. Mail marked done leaves the digest's focus list and the "need you" count.
def test_done_email_leaves_focus_and_the_need_you_count(loaded, settings, as_of_now):
    def digest():
        return build_digest(loaded, as_of=date(2026, 9, 22), generated_at=as_of_now, tz=settings.tz, save=False)

    before = digest()
    target = next(row for row in before["focus"] if row["kind"] != "fraud")
    # Not the payment-change warning: marked done, it still needs its phone check (see test_fraud_review2).
    important = next(
        row["id"] for row in before["new_mail"]["important"]
        if row["id"] != target["email_id"] and "fraud_risk" not in row["flags"]
    )
    loaded.set_done(target["email_id"], True)
    loaded.set_done(important, True)
    after = digest()
    assert target["email_id"] not in [row["email_id"] for row in after["focus"]]
    assert after["kpis"]["need_you"] == before["kpis"]["need_you"] - 2 + (target["folder"] != "important")
    assert after["headline"].startswith(f"{after['kpis']['need_you']} of 18 emails")


def test_a_done_payment_change_warning_stays_until_its_phone_check(loaded, settings, as_of_now):
    loaded.set_done("demo-bec-wire", True)
    focus = build_digest(loaded, as_of=date(2026, 9, 22), generated_at=as_of_now, tz=settings.tz, save=False)["focus"]
    assert focus[0]["email_id"] == "demo-bec-wire" and focus[0]["kind"] == "fraud"


# 13. Correcting a fraud email's category scores it again instead of leaving it Critical for fraud.
def test_correcting_a_fraud_email_recomputes_importance(loaded, settings):
    from controller_inbox.learn import record_correction

    record_correction(loaded, settings, email_id="demo-bec-wire", corrected_category="wire_ach_request", reason="Called CFO, real wire")
    after = loaded.get_email("demo-bec-wire")
    assert after.category == DocumentType.WIRE_ACH_REQUEST and "fraud_risk" not in after.flags
    assert after.importance_score < 100
    assert not any("BEC fraud" in reason for reason in after.importance_reasons)
    assert "Outgoing payment request" in after.importance_reasons


def test_correcting_to_newsletter_is_still_low(loaded, settings):
    from controller_inbox.learn import record_correction

    record_correction(loaded, settings, email_id="demo-question", corrected_category="newsletter", reason="Bulk mail")
    after = loaded.get_email("demo-question")
    assert after.importance.value == "low" and after.importance_score <= 15


# 14. A code kept as a number in the workbook is found on the invoice, not also reported as unlisted.
def test_loose_code_found_on_invoice_is_not_also_unlisted(settings):
    from openpyxl import Workbook

    from controller_inbox import cost_codes
    from test_cost_codes import _invoice

    book = Workbook()
    sheet = book.active
    sheet.append(["Description", "Cost Code"])
    sheet.append(["Freight and delivery", "1100.6420"])
    sheet.append(["Rent", 1100.611])  # typed into a number cell: Excel kept 1100.611
    path = cost_codes.workbook_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)
    codebook = cost_codes.load(settings)
    email = _invoice("rent", text="October rent\nCoding 1100.6110\nOld code 1100.6990")
    assert [found["code"] for found in cost_codes.codes_on(email, codebook)] == ["1100.611"]
    assert cost_codes.unlisted_codes(email, codebook) == ["1100.6990"]
