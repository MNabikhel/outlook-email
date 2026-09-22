from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from controller_inbox.actions import extract_actions
from controller_inbox.classify import Classification, classify_document, classify_email, outlook_categories
from controller_inbox.config import Settings
from controller_inbox.extract import (
    explode_archives,
    extract_fields,
    extract_text_from_bytes,
    redact_financial_secrets,
    sha256_bytes,
)
from controller_inbox.models import AttachmentRecord, EmailRecord, RawMessage
from controller_inbox.store import Store


class Mailbox(Protocol):
    def list_messages(self, received_after: datetime | None = None): ...

    def get_attachments(self, message_id: str): ...

    def apply_categories(self, message_id: str, categories: list[str], flag: bool) -> str: ...


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
    as_of = as_of or now.astimezone(settings.tz).date()
    attachments_raw = explode_archives(list(raw.attachments))
    if mailbox is not None and not attachments_raw and raw.has_attachments:
        attachments_raw = list(mailbox.get_attachments(raw.id))

    att_records: list[AttachmentRecord] = []
    att_classifications: list[Classification] = []
    merged_fields = extract_fields(
        f"{raw.subject}\n{raw.body_text}",
        as_of=as_of,
        extra_vendor=raw.sender_name,
    )

    for raw_att in attachments_raw:
        text = extract_text_from_bytes(raw_att.filename, raw_att.content_type, raw_att.content)
        text = redact_financial_secrets(text)
        fields = extract_fields(f"{raw_att.filename}\n{text}", as_of=as_of, extra_vendor=raw.sender_name)
        merged_fields = merged_fields.merged_with(fields)
        classified = classify_document(
            subject=raw.subject,
            body=raw.body_text,
            filename=raw_att.filename,
            sender=raw.sender_email,
            extracted_text=text,
            content_type=raw_att.content_type,
            has_text=bool(text.strip()),
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
                extracted_text=text[:20_000],
                document_type=classified.document_type,
                document_confidence=classified.confidence,
                extracted_fields=fields,
                classification_reasons=classified.reasons,
            )
        )

    duplicate = False
    if merged_fields.primary_invoice:
        dupes = store.find_duplicate_invoices(merged_fields.primary_invoice, raw.id)
        duplicate = bool(dupes)

    classified_email = classify_email(
        subject=raw.subject,
        body=raw.body_text,
        sender=raw.sender_email,
        outlook_importance=raw.outlook_importance,
        attachments=att_classifications,
        fields=merged_fields,
        as_of=as_of,
        high_amount=settings.high_amount,
        vip_senders=settings.vip_list,
        has_attachments=bool(attachments_raw) or raw.has_attachments,
        duplicate_invoice=duplicate,
    )
    classified_email = _refine(
        classified_email,
        raw,
        store,
        settings,
        filenames=[att.filename for att in att_records],
    )

    # If Graph said there were attachments but we still have none after fetch.
    has_files = bool(att_records)
    if merged_fields.mentions_attachment and not has_files and "missing_attachment" not in classified_email.flags:
        classified_email.flags.append("missing_attachment")

    actions = extract_actions(
        email_id=raw.id,
        subject=raw.subject,
        body=raw.body_text,
        category=classified_email.document_type,
        importance=classified_email.importance,
        fields=merged_fields,
        flags=classified_email.flags,
        as_of=as_of,
        now=now,
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
        body_text=redact_financial_secrets(raw.body_text)[:50_000],
        body_preview=(raw.body_preview or raw.body_text[:240])[:500],
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
        attachments=att_records,
        actions=actions,
    )
    store.upsert_email(record)
    return store.get_email(record.id) or record


def ingest_mailbox(
    mailbox: Mailbox,
    store: Store,
    settings: Settings,
    *,
    received_after: datetime | None = None,
    now: datetime | None = None,
) -> list[EmailRecord]:
    processed: list[EmailRecord] = []
    for raw in mailbox.list_messages(received_after=received_after):
        processed.append(process_message(raw, store, settings, mailbox, now=now))
    last = now or datetime.now(timezone.utc)
    store.set_state("last_sync_at", last.astimezone(timezone.utc).isoformat())
    return processed


def ingest_demo(store: Store, settings: Settings, *, now: datetime | None = None) -> list[EmailRecord]:
    from controller_inbox.demo import DemoMailbox

    mailbox = DemoMailbox(now=now)
    return ingest_mailbox(mailbox, store, settings, now=now)


def _refine(classified, raw: RawMessage, store: Store, settings: Settings, filenames: list[str] | None = None):
    from controller_inbox.learn import apply_learned, match_correction
    from controller_inbox.local_llm import suggest_category
    from controller_inbox.models import DocumentType

    learned = match_correction(store, sender_email=raw.sender_email, subject=raw.subject)
    if learned:
        return apply_learned(classified, learned)
    if not settings.llm or classified.confidence >= 0.75 or "fraud_risk" in classified.flags:
        return classified
    hint = suggest_category(
        settings,
        subject=raw.subject,
        body=raw.body_text,
        filenames=filenames or [att.filename for att in raw.attachments],
        examples=store.list_corrections(),
    )
    if not hint:
        return classified
    classified.document_type = DocumentType(hint["category"])
    classified.confidence = max(classified.confidence, 0.6)
    classified.reasons.insert(0, f"Local model: {hint['why']}")
    classified.flags.append("local_model")
    return classified
