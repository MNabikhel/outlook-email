from datetime import datetime, timedelta, timezone

import pytest

from controller_inbox.fraud import (
    TrustContext,
    assess,
    learned_weights,
    record_fraud_verdict,
    set_domain_trust,
    strip_notices,
)
from controller_inbox.models import DocumentType, RawAttachment, RawMessage
from controller_inbox.pipeline import process_message
from controller_inbox.reading import overlay_reading

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
    assert {s["key"] for s in store.fraud_check("f2")["signals"]} == {"bank_change_quoted"}


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
    assert weights["bank_change"] >= 0.8
    assert weights["bank_change"] < 1


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
