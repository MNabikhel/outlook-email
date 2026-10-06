from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Protocol

from controller_inbox.actions import extract_actions
from controller_inbox.classify import Classification, classify_document, classify_email, outlook_categories
from controller_inbox.config import Settings
from controller_inbox.documents import MAX_TEXT
from controller_inbox.extract import (
    explode_archives,
    extract_fields,
    extract_text_from_bytes,
    redact_financial_secrets,
    sha256_bytes,
)
from controller_inbox.models import ActionItem, ActionStatus, AttachmentRecord, EmailRecord, RawMessage
from controller_inbox.fraud import assess, save_check, trust_context
from controller_inbox.profile import is_finance
from controller_inbox.store import Store

VERDICT_FLAGS = {"fraud_cleared", "fraud_confirmed"}

# A message the model already read, or the user corrected, is not re-scored
# when the same mail is dropped or synced again.
KEEP_READINGS = {"bionic", "corrected"}

log = logging.getLogger(__name__)


class Mailbox(Protocol):
    def list_messages(self, received_after: datetime | None = None): ...

    def get_attachments(self, message_id: str): ...

    def apply_categories(self, message_id: str, categories: list[str], flag: bool) -> str: ...


def attachment_text(filename: str, content_type: str, data: bytes) -> str:
    """An attachment's text as stored: account and card numbers masked, capped in length."""
    return redact_financial_secrets(extract_text_from_bytes(filename, content_type, data))[:MAX_TEXT]


def process_message(
    raw: RawMessage,
    store: Store,
    settings: Settings,
    mailbox: Mailbox | None = None,
    *,
    as_of=None,
    now: datetime | None = None,
    writeback: bool | None = None,
) -> EmailRecord:
    now = now or datetime.now(timezone.utc)
    existing = store.get_email(raw.id)
    if existing is not None and existing.model_status in KEEP_READINGS:
        return existing
    # Two dates. "By Friday" and "October 15" in the text are read against the day the mail was
    # sent (``anchor``); how urgent or overdue it is now is judged against today (``as_of``).
    as_of = as_of or now.astimezone(settings.tz).date()
    anchor = sent_date(raw.received_at, settings, fallback=as_of)
    # The raw text is kept for field extraction (it needs the account digits to see that a bank
    # change is asked for); everything stored or shown is built from the masked text.
    clean = replace(
        raw,
        body_text=redact_financial_secrets(raw.body_text),
        body_preview=redact_financial_secrets(raw.body_preview or raw.body_text[:240]),
    )
    attachments_raw = explode_archives(list(raw.attachments))
    if mailbox is not None and not attachments_raw and raw.has_attachments:
        attachments_raw = list(mailbox.get_attachments(raw.id))
    attachments_raw = _unique_ids(attachments_raw)

    att_records: list[AttachmentRecord] = []
    att_classifications: list[Classification] = []
    merged_fields = extract_fields(
        f"{raw.subject}\n{raw.body_text}",
        as_of=anchor,
        extra_vendor=raw.sender_name,
    )

    for raw_att in attachments_raw:
        text = attachment_text(raw_att.filename, raw_att.content_type, raw_att.content)
        fields = extract_fields(f"{raw_att.filename}\n{text}", as_of=anchor, extra_vendor=raw.sender_name)
        merged_fields = merged_fields.merged_with(fields)
        classified = classify_document(
            subject=raw.subject,
            body=raw.body_text,
            filename=raw_att.filename,
            sender=raw.sender_email,
            extracted_text=text,
            content_type=raw_att.content_type,
            has_text=bool(text.strip()),
            payment_rule=False,
        )
        att_classifications.append(classified)
        att_records.append(
            AttachmentRecord(
                id=f"{raw.id}:{raw_att.id}",
                email_id=raw.id,
                filename=raw_att.filename,
                content_type=raw_att.content_type,
                size_bytes=raw_att.size_bytes or len(raw_att.content),
                sha256=sha256_bytes(raw_att.content) if raw_att.content else "",
                extracted_text=text,
                document_type=classified.document_type,
                document_confidence=classified.confidence,
                extracted_fields=fields,
                classification_reasons=classified.reasons,
            )
        )

    body_text = clean.body_text
    verdicts = [flag for flag in (existing.flags if existing else []) if flag in VERDICT_FLAGS]
    check = assess(
        trust_context(store, settings),
        subject=raw.subject,
        body=body_text,
        sender_name=raw.sender_name,
        sender_email=raw.sender_email,
        reply_to=raw.reply_to,
        attachments=[(att.filename, att.extracted_text) for att in att_records],
        history=store.sender_history(raw.sender_email, exclude=raw.id),
        flags=verdicts,
    )
    classified_email = _classify(
        store,
        settings,
        email_id=raw.id,
        subject=raw.subject,
        body=raw.body_text,
        sender_email=raw.sender_email,
        outlook_importance=raw.outlook_importance,
        attachments=att_classifications,
        fields=merged_fields,
        as_of=as_of,
        has_attachments=bool(attachments_raw) or raw.has_attachments,
        fraud=check.level,
    )
    classified_email.flags.extend(verdicts)

    # If Graph said there were attachments but we still have none after fetch.
    has_files = bool(att_records)
    if merged_fields.mentions_attachment and not has_files and "missing_attachment" not in classified_email.flags:
        classified_email.flags.append("missing_attachment")

    actions = extract_actions(
        email_id=raw.id,
        subject=raw.subject,
        body=body_text,
        category=classified_email.document_type,
        importance=classified_email.importance,
        fields=merged_fields,
        flags=classified_email.flags,
        as_of=as_of,
        now=now,
        sender=raw.sender_name or raw.sender_email,
        has_invite=any(att.filename.lower().endswith(".ics") for att in att_records),
        received_on=anchor,
    )

    writeback_status = "skipped"
    do_write = settings.writeback if writeback is None else writeback
    if do_write and mailbox is not None and raw.source == "graph":
        try:
            cats = outlook_categories(classified_email)
            flag = classified_email.importance.value in {"critical", "high"}
            writeback_status = mailbox.apply_categories(raw.id, cats, flag)
        except Exception as exc:  # do not fail ingest because Outlook writeback failed
            writeback_status = f"error:{exc}"

    record = EmailRecord(
        id=raw.id,
        subject=raw.subject,
        sender_name=raw.sender_name,
        sender_email=raw.sender_email,
        received_at=raw.received_at.astimezone(timezone.utc).isoformat(),
        body_text=body_text[:50_000],
        body_preview=clean.body_preview[:500],
        has_attachments=has_files,
        outlook_importance=raw.outlook_importance,
        is_read=raw.is_read,
        category=classified_email.document_type,
        category_confidence=classified_email.confidence,
        importance=classified_email.importance,
        importance_score=classified_email.importance_score,
        importance_reasons=classified_email.importance_reasons,
        flags=classified_email.flags,
        extracted=merged_fields,
        source=raw.source,
        conversation_id=raw.conversation_id,
        internet_message_id=raw.internet_message_id,
        writeback_status=writeback_status,
        created_at=now.isoformat(),
        reply_to=raw.reply_to,
        attachments=att_records,
        actions=actions,
    )
    from controller_inbox.reading import assign_script_draft

    assign_script_draft(record)
    store.upsert_email(record)
    save_check(store, record, check, now=now)
    from controller_inbox import cost_codes

    cost_codes.refresh(store, settings, email_ids=[record.id])
    return store.get_email(record.id) or record


def sent_date(received_at, settings: Settings, *, fallback):
    """The day a message was sent, in the user's time zone: what "tomorrow" in its text means."""
    try:
        when = received_at if isinstance(received_at, datetime) else datetime.fromisoformat(str(received_at))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.astimezone(settings.tz).date()
    except (TypeError, ValueError, OverflowError):
        return fallback


def _unique_ids(attachments: list) -> list:
    """Attachment ids must be unique within a message (they key the stored record); a repeat gets a number."""
    seen: set[str] = set()
    out = []
    for att in attachments:
        att_id = str(att.id)
        if att_id in seen:
            number = 2
            while f"{att_id} ({number})" in seen:
                number += 1
            att = replace(att, id=f"{att_id} ({number})")
        seen.add(str(att.id))
        out.append(att)
    return out


SYNC_CURSOR = "last_sync_at"


def ingest_mailbox(
    mailbox: Mailbox,
    store: Store,
    settings: Settings,
    *,
    received_after: datetime | None = None,
    now: datetime | None = None,
    report: dict | None = None,
    cursor: str | None = SYNC_CURSOR,
) -> list[EmailRecord]:
    """Read every message since ``received_after``. One message that can't be read is logged in
    ``report["failed"]`` and skipped; the rest are still read and the cursor still moves.

    The cursor is the time the sync started, so mail that arrives while it runs is read next time.
    ``cursor=None`` leaves it alone (the sample mailbox is not a sync).
    """
    started = now or datetime.now(timezone.utc)
    report = report if report is not None else {}
    report.setdefault("failed", [])
    processed: list[EmailRecord] = []
    for raw in mailbox.list_messages(received_after=received_after):
        try:
            processed.append(process_message(raw, store, settings, mailbox, now=now))
        except Exception as exc:
            log.warning("Couldn't read message %s (%s)", raw.id, raw.subject, exc_info=True)
            report["failed"].append({"id": raw.id, "subject": raw.subject, "error": str(exc)[:300]})
    if cursor:
        store.set_state(cursor, started.astimezone(timezone.utc).isoformat())
    return processed


def ingest_demo(store: Store, settings: Settings, *, now: datetime | None = None) -> list[EmailRecord]:
    from controller_inbox.demo import DemoMailbox

    mailbox = DemoMailbox(now=now)
    return ingest_mailbox(mailbox, store, settings, now=now, cursor=None)


def _classify(
    store: Store,
    settings: Settings,
    *,
    email_id: str,
    subject: str,
    body: str,
    sender_email: str,
    outlook_importance: str,
    attachments: list[Classification],
    fields,
    as_of,
    has_attachments: bool,
    fraud: str,
) -> Classification:
    """Script classification, then any correction the user saved for this sender or subject.

    The model is the parser when it is enabled. That happens after the
    script draft is built, so it can see amounts and dates. A correction
    the user already saved is kept and is not sent back to the model.
    """
    from controller_inbox.learn import apply_learned, match_correction

    duplicate = bool(fields.primary_invoice and store.find_duplicate_invoices(fields.primary_invoice, email_id))
    classified = classify_email(
        subject=subject,
        body=body,
        sender=sender_email,
        outlook_importance=outlook_importance,
        attachments=attachments,
        fields=fields,
        as_of=as_of,
        high_amount=settings.high_amount,
        vip_senders=settings.vip_list,
        has_attachments=has_attachments,
        duplicate_invoice=duplicate,
        finance=is_finance(settings, store),
        fraud=fraud,
    )
    learned = match_correction(store, sender_email=sender_email, subject=subject)
    return apply_learned(classified, learned) if learned else classified


def rescore_stored(
    store: Store, settings: Settings, email: EmailRecord, check, *, now: datetime | None = None
) -> EmailRecord:
    """Refile a stored email after its fraud level changed (a verdict, a trusted domain, a report).

    The script draft is rebuilt from the stored text, so an overnight reading made
    under the old level is read again. Tasks already done or dismissed are kept.
    As in ``process_message``, urgency is judged against today; the stored fields were
    already read against the day the mail was sent.
    """
    as_of = (now or datetime.now(timezone.utc)).astimezone(settings.tz).date()
    attachments = [
        classify_document(
            subject=email.subject,
            body=email.body_text,
            filename=att.filename,
            sender=email.sender_email,
            extracted_text=att.extracted_text,
            content_type=att.content_type,
            has_text=bool((att.extracted_text or "").strip()),
            payment_rule=False,
        )
        for att in email.attachments
    ]
    classified = _classify(
        store,
        settings,
        email_id=email.id,
        subject=email.subject,
        body=email.body_text,
        sender_email=email.sender_email,
        outlook_importance=email.outlook_importance,
        attachments=attachments,
        fields=email.extracted,
        as_of=as_of,
        has_attachments=email.has_attachments,
        fraud=check.level,
    )
    kept = [flag for flag in email.flags if flag in VERDICT_FLAGS | {"model_said_bank_change"}]
    email.category = classified.document_type
    email.category_confidence = classified.confidence
    email.importance = classified.importance
    email.importance_score = classified.importance_score
    email.importance_reasons = classified.importance_reasons
    email.flags = list(dict.fromkeys(classified.flags + kept))
    def key(item: ActionItem) -> str:
        return item.title.strip().lower()

    old = {key(item): item for item in email.actions}
    fresh = extract_actions(
        email_id=email.id,
        subject=email.subject,
        body=email.body_text,
        category=email.category,
        importance=email.importance,
        fields=email.extracted,
        flags=email.flags,
        as_of=as_of,
        sender=email.sender_name or email.sender_email,
        has_invite=any(att.filename.lower().endswith(".ics") for att in email.attachments),
        received_on=sent_date(email.received_at, settings, fallback=as_of),
    )
    fresh_keys = {key(item) for item in fresh}
    finished = [item for item in email.actions if item.status != ActionStatus.OPEN and key(item) not in fresh_keys]
    email.actions = [old.get(key(item), item) for item in fresh] + finished
    from controller_inbox.reading import assign_script_draft

    assign_script_draft(email)
    store.upsert_email(email)
    return store.get_email(email.id) or email
