from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Protocol

from controller_inbox.actions import extract_actions, keep_task_status, received_day
from controller_inbox.classify import Classification, classify_document, classify_email, is_data_file, own_words
from controller_inbox.clock import aware_utc, parse_stamp
from controller_inbox.config import Settings
from controller_inbox.documents import MAX_TEXT
from controller_inbox.extract import (
    ATTACHMENT_MENTION_RE,
    explode_archives,
    extract_fields,
    extract_text_from_bytes,
    redact_financial_secrets,
    sha256_bytes,
)
from controller_inbox.models import AttachmentRecord, DocumentType, EmailRecord, ExtractedFields, RawMessage
from controller_inbox.calendar_events import invitation_method
from controller_inbox.fraud import assess, attachments_locked, domain_of, dropped_file_body, reassess_email, save_check, trust_context
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
) -> EmailRecord:
    now = now or datetime.now(timezone.utc)
    existing = store.get_email(raw.id)
    if existing is not None and existing.model_status in KEEP_READINGS and not raw.attachments:
        return existing
    if existing is not None and raw.from_file:
        raw = _as_stored(raw, existing)
    if existing is not None and existing.writeback_status == "written":
        # Versions before Outlook was read only marked the messages they flagged high importance in Outlook: read
        # again, that is CloseDesk's own mark, not the sender's. The importance the message came with is kept.
        raw = replace(raw, outlook_importance=existing.outlook_importance)
    # Two dates. "By Friday" and "October 15" in the text are read against the day the mail was
    # sent (``anchor``); how urgent or overdue it is now is judged against today (``as_of``).
    as_of = as_of or now.astimezone(settings.tz).date()
    anchor = sent_date(raw.received_at, settings, fallback=as_of)
    # The raw text is kept for field extraction (it needs the account digits to see that a bank
    # change is asked for); everything stored or shown is built from the masked text.
    clean = replace(
        raw,
        subject=redact_financial_secrets(raw.subject),
        body_text=redact_financial_secrets(raw.body_text),
        body_preview=redact_financial_secrets(raw.body_preview or raw.body_text[:240]),
    )
    files = list(raw.attachments)
    if mailbox is not None and not files and raw.has_attachments:
        files = list(mailbox.get_attachments(raw.id))
    if mailbox is not None and existing is None and files:
        # Mail read from a mailbox (Microsoft 365, the sample) keeps its files as they came, as the drop folder and
        # classic Outlook keep theirs: the Preview tab, Download and the page view open them.
        from controller_inbox.folder_mail import unique_attachments, write_extracted

        files = unique_attachments(files)
        write_extracted(settings, replace(raw, attachments=files))
    attachments_raw = explode_archives(files)
    # Another copy of a stored message: the files stored from earlier copies are kept, and this copy adds
    # only files they don't hold. A re-saved .eml often comes without the files dropped beside the first.
    kept = existing.attachments if existing is not None else []
    attachments_raw = _unique_ids(
        _unstored(attachments_raw, kept), taken={att.id.removeprefix(f"{raw.id}:") for att in kept}
    )
    if existing is not None and existing.model_status in KEEP_READINGS:
        return _add_files(store, settings, existing, raw, attachments_raw, anchor, now=now)

    att_records: list[AttachmentRecord] = []
    att_classifications: list[Classification] = []
    merged_fields = extract_fields(
        f"{raw.subject}\n{raw.body_text}",
        as_of=anchor,
        extra_vendor=_vendor_hint(raw),
    )
    for att in kept:
        att_records.append(att)
        att_classifications.append(_classify_file(raw, att.filename, att.content_type, att.extracted_text))
        merged_fields = merged_fields.merged_with(_email_fields(att, att_classifications[-1].document_type))
    for raw_att in attachments_raw:
        record, classified = _file_record(raw, raw_att, anchor)
        _read_as_taught(store, raw.sender_email, record, anchor)
        att_records.append(record)
        att_classifications.append(classified)
        merged_fields = merged_fields.merged_with(_email_fields(record, classified.document_type))

    body_text = clean.body_text
    verdicts = [flag for flag in (existing.flags if existing else []) if flag in VERDICT_FLAGS]
    files = [(att.filename, att.extracted_text) for att in att_records]
    check = assess(
        trust_context(store, settings),
        subject=clean.subject,
        body=dropped_file_body(raw.id, raw.sender_email, body_text, files),
        sender_name=raw.sender_name,
        sender_email=raw.sender_email,
        reply_to=raw.reply_to,
        attachments=files,
        history=store.sender_history(raw.sender_email, exclude=raw.id),
        flags=verdicts,
        domain_history=store.domain_history(domain_of(raw.sender_email), exclude=raw.id),
        reply_domain_history=store.domain_history(domain_of(raw.reply_to), exclude=raw.id),
    )
    from controller_inbox.links import conversation_members

    conversation = conversation_members(store, raw.id, raw.threading, raw.internet_message_id, raw.conversation_id)
    # The email being read, as the conversation checks see it (it isn't stored yet).
    stored = store.email_heads([raw.id]) if existing is not None else []
    me = SimpleNamespace(
        id=raw.id,
        stored=stored[0].stored if stored else None,  # read again: where it was stored first
        subject=clean.subject,
        sender_email=raw.sender_email,
        reply_to=raw.reply_to,
        internet_message_id=raw.internet_message_id,
        threading=dict(raw.threading),
        received_at=raw.received_at.astimezone(timezone.utc).isoformat(),
        flags=[],
        invoice_file=any(c.document_type == DocumentType.AP_INVOICE for c in att_classifications),
    )
    invitation = invitation_method([*raw.attachments, *att_records], raw.meetings)
    holders = _holders(store, merged_fields.primary_invoice)
    classified_email = _classify(
        store,
        settings,
        conversation=conversation,
        holders=holders,
        me=me,
        email_id=raw.id,
        subject=clean.subject,
        body=raw.body_text,
        sender_email=raw.sender_email,
        outlook_importance=raw.outlook_importance,
        attachments=att_classifications,
        fields=merged_fields,
        as_of=as_of,
        has_attachments=bool(att_records) or raw.has_attachments,
        fraud=check.level,
        invitation=invitation,
    )
    classified_email.flags.extend(verdicts + _bank_caution(check))

    # If Graph said there were attachments but we still have none after fetch.
    has_files = bool(att_records)
    # "Please find attached" in the quoted email a reply carries is no file this one forgot.
    if (
        merged_fields.mentions_attachment
        and not has_files
        and "missing_attachment" not in classified_email.flags
        and ATTACHMENT_MENTION_RE.search(f"{raw.subject}\n{own_words(raw.body_text)}")
    ):
        classified_email.flags.append("missing_attachment")

    actions = extract_actions(
        email_id=raw.id,
        subject=clean.subject,
        body=body_text,
        category=classified_email.document_type,
        importance=classified_email.importance,
        fields=merged_fields,
        flags=classified_email.flags,
        as_of=as_of,
        now=now,
        sender=raw.sender_name or raw.sender_email,
        has_invite=any(att.filename.lower().endswith(".ics") for att in att_records),
        invitation=invitation,
        received_on=anchor,
        entered_elsewhere=_entered_elsewhere(
            store, me, holders, conversation, own_file=me.invoice_file, fraud=check.level
        ),
    )
    if existing is not None:
        actions = keep_task_status(actions, existing.actions)

    # Nothing is written back to the mailbox: Outlook and Microsoft 365 are only read.
    writeback_status = existing.writeback_status if existing is not None and existing.writeback_status == "written" else "skipped"

    record = EmailRecord(
        id=raw.id,
        subject=clean.subject,
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
        threading=dict(raw.threading),
    )
    from controller_inbox.reading import assign_script_draft

    assign_script_draft(record)
    # Reading the files takes a while (a scan is read with OCR), and meanwhile the user may give a fraud verdict on
    # the email or snooze or finish its tasks. That is read again with the write lock held, so it is kept rather than
    # overwritten from the copy read at the start. (Done is never written here: upsert_email leaves it alone.)
    # An invitation on mail held as possible payment fraud is no meeting: its text never reaches the calendar, only
    # which meeting it named, so Outlook's own copy of that meeting isn't shown either. Read before the write lock is
    # taken, so a long one keeps nobody waiting.
    found = invites(raw, record.id, now)
    with store.transaction() as conn:
        latest = store.get_email(raw.id, conn=conn)
        verdict_changed = latest is not None and _keep_user_state(record, latest)
        store.upsert_email(record, conn=conn)
        store.save_invites(record.id, found, sender=raw.sender_email, received=record.received_at, held=attachments_locked(record),
                           sender_name=raw.sender_name,
                           expanded=now.astimezone(timezone.utc).isoformat(timespec="seconds"), conn=conn)
    from controller_inbox import cost_codes

    try:
        if verdict_changed:
            # Checked again under the verdict given meanwhile, as it would have been from the start.
            reassess_email(store, settings, store.get_email(record.id) or record, now=now)
        else:
            save_check(store, record, check, now=now)
        cost_codes.refresh(store, settings, email_ids=[record.id])
        _settle_conversation(store, settings, record, conversation, holders, now=now)
    except Exception:
        # The email is stored and on the board: a fraud check or cost code that couldn't be saved is logged, and the
        # message isn't reported as one that couldn't be read.
        log.warning("Stored %s, but couldn't finish filing it", record.id, exc_info=True)
    return store.get_email(record.id) or record


def _keep_user_state(record: EmailRecord, latest: EmailRecord) -> bool:
    """Give ``record`` what the user did to the stored email ``latest``: its fraud verdict and what they did with
    each task. Returns whether the verdict differs from the one ``record`` was read with."""
    verdicts = [flag for flag in latest.flags if flag in VERDICT_FLAGS]
    changed = verdicts != [flag for flag in record.flags if flag in VERDICT_FLAGS]
    record.flags = [flag for flag in record.flags if flag not in VERDICT_FLAGS] + verdicts
    record.actions = keep_task_status(record.actions, latest.actions)
    return changed


def _read_as_taught(store: Store, sender: str, record: AttachmentRecord, anchor) -> None:
    """Fill the fields its sender's earlier files were taught (teach.py), read where those labels are printed."""
    from controller_inbox import teach

    if not sender:
        return
    learnt = teach.templates(store, sender, not_attachment=record.id)
    if not learnt:
        return
    for field, got in teach.read_with(record.extracted_text or "", learnt, as_of=anchor).items():
        if (record.extracted_fields.taught.get(field) or {}).get("by") == "you":
            continue
        record.extracted_fields = teach.with_taught(record.extracted_fields, field, got["value"], by="learnt", label=got["label"])


def _as_stored(raw: RawMessage, stored: EmailRecord) -> RawMessage:
    """Another copy of a stored email that has no subject or sent time of its own keeps the stored ones, so the
    email isn't renamed after the copy's file or dated the day that file was saved."""
    received = raw.received_at
    if "sent" in raw.from_file:
        received = parse_stamp(stored.received_at) or aware_utc(raw.received_at)
    subject = stored.subject if "subject" in raw.from_file and stored.subject else raw.subject
    return replace(raw, subject=subject, received_at=received)


def sent_date(received_at, settings: Settings, *, fallback):
    """The day a message was sent, in the user's time zone: what "tomorrow" in its text means. Kept for the
    modules that import it: it is ``actions.received_day``."""
    return received_day(received_at, settings.tz, fallback=fallback)


def _vendor_hint(raw: RawMessage) -> str:
    """The sender's name as a vendor to look for, when there is a sender: a file dropped on its own has none."""
    return raw.sender_name if raw.sender_email else ""


def _file_record(raw: RawMessage, raw_att, anchor) -> tuple[AttachmentRecord, Classification]:
    """One attachment read, classified and its fields pulled, as stored."""
    unmasked = extract_text_from_bytes(raw_att.filename, raw_att.content_type, raw_att.content)
    text = redact_financial_secrets(unmasked)[:MAX_TEXT]
    fields = extract_fields(f"{raw_att.filename}\n{text}", as_of=anchor, extra_vendor=_vendor_hint(raw))
    # The masked text holds no bank number to read, so a bank letter's account is read before masking. Only its
    # last four are kept; every other field comes from the masked text, so no whole number is stored.
    bank = extract_fields(unmasked[:MAX_TEXT], as_of=anchor)
    fields = replace(
        fields,
        account_last4=bank.account_last4,
        mentions_routing_or_account=fields.mentions_routing_or_account or bank.mentions_routing_or_account,
    )
    classified = _classify_file(raw, raw_att.filename, raw_att.content_type, text)
    record = AttachmentRecord(
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
    return record, classified


def _email_fields(att: AttachmentRecord, document_type: DocumentType) -> ExtractedFields:
    """What a file adds to its email's fields. A workbook or CSV that is no invoice of its own (a close workbook,
    an AP export) lists invoice numbers that were already seen, so they don't make the email about that invoice. Nor
    does one listing several invoices, whatever it was filed as: a subledger beside an invoice is still a list."""
    fields = att.extracted_fields
    listing = document_type in {DocumentType.SPREADSHEET, DocumentType.OTHER} or len(set(fields.invoice_numbers)) > 1
    if listing and is_data_file(att.filename, att.content_type):
        return replace(fields, invoice_numbers=[])
    return fields


def _classify_file(raw: RawMessage, filename: str, content_type: str, text: str) -> Classification:
    return classify_document(
        subject=raw.subject,
        body=raw.body_text,
        filename=filename,
        sender=raw.sender_email,
        extracted_text=text,
        content_type=content_type,
        has_text=bool(text.strip()),
        payment_rule=False,
    )


def _unstored(attachments: list, kept: list[AttachmentRecord]) -> list:
    """The files not stored with the message yet. The same bytes under any name count as stored."""
    if not kept:
        return attachments
    hashes = {att.sha256 for att in kept if att.sha256}
    names = {att.filename for att in kept}
    return [att for att in attachments if (sha256_bytes(att.content) not in hashes if att.content else att.filename not in names)]


def _add_files(
    store: Store, settings: Settings, email: EmailRecord, raw: RawMessage, attachments_raw: list, anchor, *, now: datetime
) -> EmailRecord:
    """Another copy of a message the model read or the user corrected: the reading stays, the files this copy
    adds are stored with it, and the fraud check runs again with them, so a bank letter among them still counts.

    Reading the files takes a while (a scan is read with OCR), and meanwhile the user may correct the email or
    give a fraud verdict on it. So the files are read first and then added to the email as it is by then."""
    if not attachments_raw:
        return email
    records = [_file_record(raw, raw_att, anchor)[0] for raw_att in attachments_raw]
    for record in records:
        # Read with what the sender's earlier files were taught, as a file on a new email is.
        _read_as_taught(store, raw.sender_email, record, anchor)
    email = store.get_email(email.id) or email
    held = {att.id for att in email.attachments}
    for record in records:
        if record.id in held:
            continue
        email.attachments.append(record)
        email.extracted = email.extracted.merged_with(_email_fields(record, record.document_type))
    email.has_attachments = True
    email.flags = [flag for flag in email.flags if flag != "missing_attachment"]
    store.upsert_email(email)
    reassess_email(store, settings, email, now=now)
    from controller_inbox import cost_codes

    cost_codes.refresh(store, settings, email_ids=[email.id])
    return store.get_email(email.id) or email


def _unique_ids(attachments: list, taken: set[str] = frozenset()) -> list:
    """Attachment ids must be unique within a message (they key the stored record); a repeat gets a number.
    ``taken`` are ids already stored with the message."""
    seen: set[str] = set(taken)
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
# How many syncs in a row a message that fails (Outlook throttling, a dropped connection) holds the cursor back for.
MAX_SYNC_TRIES = 10


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
    ``report["failed"]`` and skipped; the rest are still read and the cursor still moves, but not past
    the earliest message that failed, so the next sync tries it again (up to ``MAX_SYNC_TRIES`` times).

    The cursor is the time the sync started, so mail that arrives while it runs is read next time.
    ``cursor=None`` leaves it alone (the sample mailbox is not a sync), and so does a sync of a window that
    starts after the cursor.
    """
    started = now or datetime.now(timezone.utc)
    report = report if report is not None else {}
    report.setdefault("failed", [])
    processed: list[EmailRecord] = []
    retry_key = f"{cursor}_retry"
    retry = json.loads(store.get_state(retry_key) or "{}") if cursor else {}
    tries: dict[str, int] = retry.get("tries", {})
    # Resuming from a cursor held back for a failed message: the mail around it was read by the last sync.
    resumed = bool(retry) and received_after is not None and aware_utc(received_after) == datetime.fromisoformat(retry["cursor"])
    read_before = datetime.fromisoformat(retry["until"]) if resumed else None
    failed: dict[str, int] = {}
    held: list[datetime] = []
    sample_checked = cursor is None  # the sample mailbox itself is read without a cursor
    # Read oldest first, as it came: a reply read before the email it answers would be taken for the one that
    # started their conversation (links.new_domains), and Microsoft 365 lists the newest first.
    listed = sorted(mailbox.list_messages(received_after=received_after), key=lambda raw: aware_utc(raw.received_at))
    for raw in listed:
        received = aware_utc(raw.received_at)
        if read_before and received < read_before and raw.id not in tries and store.has_email(raw.id):
            continue  # listed again only because the cursor waited for a message that failed
        if not store.has_email(raw.id) and store.email_with_message_id(raw.internet_message_id, raw.source):
            # Outlook gives a message a new id when it is moved to another folder; it was read under the old one.
            continue
        if not store.has_email(raw.id) and _filed_from_a_file(store, raw):
            continue
        if not sample_checked:
            # The first real message from Outlook replaces the sample mailbox, as one from the drop folder does.
            sample_checked = True
            if not store.real_mail_count():
                report["sample_cleared"] = store.clear_sample()
        try:
            processed.append(process_message(raw, store, settings, mailbox, now=now))
        except Exception as exc:
            log.warning("Couldn't read message %s (%s)", raw.id, raw.subject, exc_info=True)
            report["failed"].append({"id": raw.id, "subject": redact_financial_secrets(raw.subject), "error": str(exc)[:300]})
            failed[raw.id] = tries.get(raw.id, 0) + 1
            if failed[raw.id] < MAX_SYNC_TRIES:
                held.append(received)
    # A sync of a shorter window (``sync --hours 1``) didn't read all the mail since the cursor, or the message
    # it waits for: the cursor and the messages to try again stay as they are. With no cursor yet, the next sync
    # would look back ``lookback_hours``, so only a window reaching that far back covers it.
    previous = store.get_state(cursor) if cursor else None
    since = parse_stamp(previous) or started.astimezone(timezone.utc) - timedelta(hours=settings.lookback_hours)
    covered = received_after is None or aware_utc(received_after) <= since
    if cursor and covered:
        started = started.astimezone(timezone.utc)
        mark = min([started, *held]).isoformat()
        store.set_state(cursor, mark)
        store.set_state(retry_key, json.dumps({"cursor": mark, "until": started.isoformat(), "tries": failed}) if failed else "")
    return processed


def _filed_from_a_file(store: Store, raw: RawMessage) -> bool:
    """Whether this message is already filed from the drop folder or classic Outlook (the same Message-ID, and
    the same subject, sender and time, as the drop folder tells a reused Message-ID apart): it isn't filed twice,
    with a second set of tasks and its own invoice flagged as a duplicate of itself."""
    from controller_inbox.mail_identity import stored_from

    return stored_from(store, raw, ("folder", "outlook")) is not None


def invites(raw: RawMessage, email_id: str, now: datetime) -> list:
    """The meetings an email's invitations name: its .ics files, and the meeting of an Outlook meeting request, at
    most ``calendar_events.MAX_ROWS``, read within ``calendar_events.ICS_SECONDS`` for them all."""
    from time import monotonic

    from controller_inbox import calendar_events

    found = [replace(meeting, email_id=email_id) for meeting in raw.meetings]
    deadline = monotonic() + calendar_events.ICS_SECONDS
    for att in raw.attachments:
        name = (att.filename or "").lower()
        if (name.endswith(".ics") or "calendar" in (att.content_type or "").lower()) and att.content:
            if len(found) >= calendar_events.MAX_ROWS:
                break
            try:
                found += calendar_events.from_ics(att.content, email_id=email_id, now=now, limit=calendar_events.MAX_ROWS - len(found), deadline=deadline)
            except Exception:  # an invitation that can't be read adds no meeting; the email is still read
                log.warning("Couldn't read the invitation %s", att.filename, exc_info=True)
    return found


def ingest_demo(store: Store, settings: Settings, *, now: datetime | None = None) -> list[EmailRecord]:
    from controller_inbox.demo import DemoMailbox

    from controller_inbox import calendar_events
    from controller_inbox.demo import demo_meetings

    mailbox = DemoMailbox(now=now)
    records = ingest_mailbox(mailbox, store, settings, now=now, cursor=None)
    anchor = now or datetime(2026, 9, 22, 8, 15, tzinfo=timezone.utc)
    store.replace_meetings(
        "demo", demo_meetings(anchor),
        start=(anchor - calendar_events.WINDOW_BACK).isoformat(), end=(anchor + calendar_events.WINDOW_AHEAD).isoformat(),
    )
    return records


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
    conversation: set[str] | None = None,
    holders: list[str] | None = None,
    me=None,
    invitation: str = "",
) -> Classification:
    """Script classification, then any correction the user saved for this sender or subject.

    The model is the parser when it is enabled. That happens after the
    script draft is built, so it can see amounts and dates. A correction
    the user already saved is kept and is not sent back to the model.
    """
    from controller_inbox.learn import apply_learned, match_correction

    own_file = any(c.document_type == DocumentType.AP_INVOICE for c in attachments)
    me = me or SimpleNamespace(id=email_id, subject=subject, sender_email=sender_email, threading={}, received_at="", flags=[], invoice_file=own_file)
    duplicate = bool(
        fields.primary_invoice
        and _repeats(
            store,
            me,
            _holders(store, fields.primary_invoice) if holders is None else holders,
            conversation or set(),
            own_file=own_file,
            fraud=fraud,
        )
    )
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
        invitation=invitation,
    )
    learned = match_correction(store, sender_email=sender_email, subject=subject, email_id=email_id)
    return apply_learned(classified, learned) if learned else classified


_REPLY_SUBJECT = re.compile(r"^\s*(re|fw|fwd|aw|wg|tr|sv)\s*:", re.I)


def _replying(record) -> bool:
    """A reply or forward: it answers another email, or says so in its subject."""
    info = getattr(record, "threading", None) or {}
    return bool(info.get("in_reply_to") or info.get("references")) or bool(_REPLY_SUBJECT.match(getattr(record, "subject", "") or ""))


def _talk(record) -> bool:
    """A reply (or forward) that brings no invoice file: it writes about an invoice, and is no invoice itself."""
    return _replying(record) and not _has_invoice_file(record)


def _doubtful(record, fraud: str = "") -> bool:
    """An email not to be taken at its word: held, or cautioned (``fraud`` is the level of one still being read)."""
    return fraud in {"caution", "high"} or "payment_caution" in (getattr(record, "flags", []) or []) or (
        hasattr(record, "category") and attachments_locked(record)
    )


def _has_invoice_file(record) -> bool:
    if hasattr(record, "invoice_file"):
        return bool(record.invoice_file)
    return any(att.document_type == DocumentType.AP_INVOICE for att in getattr(record, "attachments", []) or [])


def _sent_to(record, other) -> bool:
    """Whether ``other`` came from ``record``'s own sender or from someone ``record`` went to: who answers about it."""
    sender = (getattr(other, "sender_email", "") or "").strip().lower()
    if not sender:
        return False
    return sender == (record.sender_email or "").strip().lower() or sender in {a.lower() for a in (record.threading or {}).get("to") or []}


def _holders(store: Store, invoice: str | None) -> list[str]:
    """Every stored email with this invoice number (``Store.find_duplicate_invoices``), looked up once per email read."""
    return store.find_duplicate_invoices(invoice, "") if invoice else []


def _repeats(store: Store, me, holders: list[str], conversation: set[str], *, own_file: bool, fraud: str = "") -> list[str]:
    """The other emails with ``me``'s invoice number (``holders``: all that have it), as possible duplicates.

    Each email is judged on itself, never by trusting another: a reply that brings no invoice file (a colleague's
    "which PO is this for?", the vendor's answer) is no second invoice, so it is never flagged; and an invoice read
    after such replies in its own conversation, from its sender or the people it went to, isn't flagged because of
    them. Everything else counts, as two copies of an invoice always did. Held or cautioned mail is never let off.
    Mail held as possible fraud is no duplicate of anything else: it is held, and flagged itself."""
    found = [other for other in holders if other != me.id]
    if not found:
        return []
    heads = {head.id: head for head in store.email_heads(found)}
    if _doubtful(me, fraud):
        return found
    found = [other for other in found if other not in heads or not attachments_locked(heads[other])]
    if _talk(me) and not own_file:
        return []
    return [
        other for other in found
        if not (other in conversation and other in heads and _talk(heads[other]) and _sent_to(me, heads[other]))
    ]


def _entered_elsewhere(store: Store, me, holders: list[str], conversation: set[str], *, own_file: bool, fraud: str = "") -> bool:
    """Whether a reply about an invoice needs no "Code and enter" task of its own: it brings no invoice file, and
    an earlier email of its conversation, not held or cautioned, is the invoice (it brings the file, or isn't a reply)
    and came from the same sender or went to the one replying. That email keeps its own task, always."""
    if own_file or not _talk(me) or _doubtful(me, fraud):
        return False
    mine = getattr(me, "stored", None)
    for head in store.email_heads([other for other in holders if other != me.id and other in conversation]):
        if _talk(head) or _doubtful(head) or head.category != DocumentType.AP_INVOICE:
            continue
        earlier = (mine is None or head.stored < mine) and (head.received_at or "") <= (me.received_at or "")
        if earlier and _sent_to(head, me):
            return True
    return False


def _settle_conversation(store: Store, settings: Settings, record: EmailRecord, conversation: set[str], holders: list[str], *, now: datetime) -> None:
    """An invoice read after mail about it (the drop folder reads files by name, not by date). The "Code and
    enter" task goes from replies that bring no invoice file, came later than the invoice, and are from its sender
    or someone it went to. And a reply from another domain that brought an invoice file of its own (a lookalike's
    "resent as a PDF" read first) is filed again as a possible duplicate. Nothing is taken from the invoice's own
    email, or from any email that brings an invoice file; a task the user already finished or snoozed stays."""
    if not conversation or _talk(record) or _doubtful(record) or record.category != DocumentType.AP_INVOICE:
        return
    domain = domain_of(record.sender_email)
    for head in store.email_heads([other for other in holders if other != record.id and other in conversation]):
        if _talk(head) and (head.received_at or "") >= (record.received_at or "") and _sent_to(record, head):
            store.drop_open_tasks(head.id, source="invoice")
        elif _replying(head) and _has_invoice_file(head) and "duplicate_invoice" not in head.flags and domain_of(head.sender_email) != domain:
            from controller_inbox.fraud import assess_email

            if (stored := store.get_email(head.id)) is not None:
                rescore_stored(store, settings, stored, assess_email(store, trust_context(store, settings), stored), now=now)


def _bank_caution(check) -> list[str]:
    """The flag that says a caution is about changed bank details (see ``FraudCheck.flags``)."""
    return [flag for flag in getattr(check, "flags", []) if flag == "bank_change_caution"]


def rescore_stored(
    store: Store, settings: Settings, email: EmailRecord, check, *, now: datetime | None = None
) -> EmailRecord:
    """Refile a stored email after its fraud level changed (a verdict, a trusted domain, a report).

    The script draft is rebuilt from the stored text, so an overnight reading made
    under the old level is read again. Tasks keep what the user did with them (``keep_task_status``).
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
    from controller_inbox.links import conversation_members

    conversation = conversation_members(store, email.id, email.threading, email.internet_message_id, email.conversation_id)
    # The email as the invoice checks see it: stored, with its files' kinds as just read again.
    heads = store.email_heads([email.id])
    me = heads[0] if heads else SimpleNamespace(id=email.id, stored=None, subject=email.subject, sender_email=email.sender_email,
                                                threading=email.threading, received_at=email.received_at, flags=[])
    me.invoice_file = any(c.document_type == DocumentType.AP_INVOICE for c in attachments)
    me.flags = []  # judged under ``check``, the level it is filed again under
    holders = _holders(store, email.extracted.primary_invoice)
    classified = _classify(
        store,
        settings,
        conversation=conversation,
        holders=holders,
        me=me,
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
        invitation=invitation_method(email.attachments),
    )
    kept = [flag for flag in email.flags if flag in VERDICT_FLAGS | {"model_said_bank_change"}]
    email.category = classified.document_type
    email.category_confidence = classified.confidence
    email.importance = classified.importance
    email.importance_score = classified.importance_score
    email.importance_reasons = classified.importance_reasons
    email.flags = list(dict.fromkeys(classified.flags + _bank_caution(check) + kept))
    fresh = extract_actions(
        email_id=email.id,
        subject=email.subject,
        body=email.body_text,
        category=email.category,
        importance=email.importance,
        fields=email.extracted,
        flags=email.flags,
        as_of=as_of,
        now=now,
        sender=email.sender_name or email.sender_email,
        has_invite=any(att.filename.lower().endswith(".ics") for att in email.attachments),
        invitation=invitation_method(email.attachments),
        received_on=sent_date(email.received_at, settings, fallback=as_of),
        entered_elsewhere=_entered_elsewhere(store, me, holders, conversation, own_file=me.invoice_file, fraud=check.level),
    )
    # As when the email is read again: the new reading's tasks, keeping what the user did with each.
    email.actions = keep_task_status(fresh, email.actions)
    from controller_inbox.reading import assign_script_draft

    assign_script_draft(email)
    store.upsert_email(email)
    return store.get_email(email.id) or email
