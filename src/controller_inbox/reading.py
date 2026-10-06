"""Fast script drafts, and the reading the local model is allowed to file.

Scripts pull text, amounts, dates, and file types. They also place every
message in a folder so the morning board works before Bionic has run.
The model is the reader when it is on. A payment-instruction warning cannot
be filed as informational or reference, and it cannot lose the phone-verify
action.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta, timezone, tzinfo

from controller_inbox import documents
from controller_inbox.actions import received_day
from controller_inbox.classify import QUOTE_START_RE
from controller_inbox.fraud import attachments_locked
from controller_inbox.models import (
    DOCUMENT_LABELS,
    EVERYDAY_CATEGORIES,
    FOLDERS,
    ActionItem,
    ActionStatus,
    DocumentType,
    EmailRecord,
    Importance,
)

# Per attachment in the overnight packet: the section list, the opening, and what matches the email.
PACKET_FILE_CHARS = 1500

REFERENCE_CATEGORIES = {
    DocumentType.BANK_STATEMENT,
    DocumentType.BANK_RECONCILIATION,
    DocumentType.PURCHASE_ORDER,
    DocumentType.CONTRACT,
    DocumentType.INSURANCE,
    DocumentType.SPREADSHEET,
    DocumentType.IMAGE_SCAN,
    DocumentType.PACKING_SLIP,
    DocumentType.NOTIFICATION,
}

ACTION_CATEGORIES = {
    DocumentType.REPLY_NEEDED,
    DocumentType.APPROVAL_REQUEST,
    DocumentType.AP_INVOICE,
    DocumentType.AR_INVOICE,
    DocumentType.CREDIT_MEMO,
    DocumentType.TAX_DOCUMENT,
    DocumentType.AUDIT_REQUEST,
    DocumentType.WIRE_ACH_REQUEST,
    DocumentType.EXPENSE_REPORT,
    DocumentType.PAYROLL,
    DocumentType.REMITTANCE_ADVICE,
    DocumentType.PAYMENT_INSTRUCTION_CHANGE,
}

INFO_CATEGORIES = {DocumentType.NEWSLETTER, DocumentType.INTERNAL_FYI, DocumentType.MEETING}

VERIFY_TITLE = "Verify payment-instruction change by phone before doing anything"

FRAUD_SUMMARY = (
    "Possible payment-instruction fraud — verify by phone before anything else. "
    "Do not change bank details or pay from this email."
)


class ReadingRefused(ValueError):
    """The message was corrected by the user, so a model reading may not replace it."""


def script_folder(category: DocumentType, importance: Importance, flags: list[str]) -> str:
    if "fraud_risk" in flags or category == DocumentType.PAYMENT_INSTRUCTION_CHANGE:
        return "important"
    if category == DocumentType.MEETING and importance in {Importance.CRITICAL, Importance.HIGH}:
        return "important"
    if category in INFO_CATEGORIES:
        return "informational"
    if category == DocumentType.WORKPAPER and "month_end" in flags:
        return "important"
    if category in REFERENCE_CATEGORIES or category == DocumentType.WORKPAPER:
        return "reference"
    if category in ACTION_CATEGORIES or importance in {Importance.CRITICAL, Importance.HIGH}:
        return "important"
    return "informational"


def script_summary(email: EmailRecord) -> str:
    if "fraud_risk" in email.flags or email.category == DocumentType.PAYMENT_INSTRUCTION_CHANGE:
        return "Payment instructions may have changed. Confirm by phone before any payment."
    label = DOCUMENT_LABELS.get(email.category, email.category.value)
    lead = lead_sentence(email.body_text)
    has_facts = bool(email.extracted.primary_invoice or email.extracted.primary_amount is not None)
    if email.category in {DocumentType.NEWSLETTER, DocumentType.NOTIFICATION}:
        who = email.sender_name or email.sender_email or "an automated sender"
        return f"{label} from {who}: {email.subject}".strip()
    if lead and (email.category in EVERYDAY_CATEGORIES or not has_facts):
        if email.category in {DocumentType.OTHER, DocumentType.INTERNAL_FYI}:
            return lead
        return f"{label}: {lead}"
    bits = [label]
    if email.extracted.primary_invoice:
        bits.append(email.extracted.primary_invoice)
    amount = email.extracted.primary_amount
    if amount is not None:
        bits.append(f"${amount:,.2f}")
    if email.extracted.primary_due:
        bits.append(f"due {email.extracted.primary_due}")
    line = " · ".join(bits)
    if email.folder == "informational":
        return f"{line}. Nothing required unless you want it."
    if email.folder == "reference":
        return f"{line}. Filed for reference, not an overnight task."
    if "duplicate_invoice" in email.flags:
        return f"{line}. Possible duplicate — check before posting."
    if "missing_attachment" in email.flags:
        return f"{line}. The note says a file was attached, and none arrived."
    return line


_GREETING = re.compile(
    r"^(?:(?:hi|hello|hey|dear|good\s+(?:morning|afternoon|evening))\b[^.!?,\n]{0,40}[,!:.]?|"
    r"(?:team|all|folks|everyone|everybody)\s*[,!:.]?)\s*$",
    re.I,
)
_FILLER = re.compile(
    r"^(?:please\s+(?:find|see)\s+(?:the\s+)?attached|(?:see|find)\s+attached|"
    r"(?:i\s+)?hope\s+(?:you|this|all)|thanks?(?:\s+you)?(?:\s+(?:so\s+much|again))?[,.!]|"
    r"thank\s+you\s+for\s+your\s+(?:email|message|note)|happy\s+(?:monday|friday))",
    re.I,
)


def lead_sentence(body: str, limit: int = 160) -> str:
    """The first real sentence of a message, without the greeting or the quoted thread."""
    text = body or ""
    quote = QUOTE_START_RE.search(text)
    if quote:
        text = text[: quote.start()]
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line and not _GREETING.match(line)]
    flat = re.sub(r"\s+", " ", " ".join(lines)).strip()
    flat = re.sub(
        r"^(?:(?:hi|hello|hey|dear)(?:\s+[\w.'-]+){0,3}?|team|all|folks|everyone|everybody)\s*[,!:\u2014\u2013-]+\s*",
        "",
        flat,
        flags=re.I,
    )
    for sentence in re.split(r"(?<=[.!?])\s+", flat):
        sentence = sentence.strip()
        if len(sentence) >= 15 and not _FILLER.match(sentence):
            sentence = sentence[0].upper() + sentence[1:]
            return sentence if len(sentence) <= limit else sentence[: limit - 1].rstrip() + "…"
    flat = flat[:limit]
    return flat[:1].upper() + flat[1:]


def assign_script_draft(email: EmailRecord) -> EmailRecord:
    email.folder = script_folder(email.category, email.importance, email.flags)
    email.summary = script_summary(email)
    if "user_trained" in email.flags:
        email.model_status = "corrected"
    else:
        email.model_status = "script_draft"
        if email.category_confidence < 0.55 and "needs_model" not in email.flags:
            email.flags.append("needs_model")
    return email


def build_packet(email: EmailRecord, corrections: list[dict] | None = None) -> dict:
    sender = (email.sender_email or "").lower()
    matched = []
    for row in corrections or []:
        if (row.get("sender_email") or "").lower() == sender and sender:
            matched.append(
                {
                    "category": row.get("corrected_category"),
                    "reason": row.get("reason"),
                    "subject": row.get("subject"),
                }
            )
    attachments = []
    locked = attachments_locked(email)
    focus = f"{email.subject} {(email.body_text or '')[:600]}"
    for att in email.attachments[:4]:
        text = (att.extracted_text or "").strip()
        note = "" if text else "No extractable text. Treat this as a scan and do not invent its contents."
        if locked:
            text, note = "", "Not read: this email is flagged as possible payment fraud."
        attachments.append(
            {
                "filename": att.filename,
                "script_type": att.document_type.value,
                "text": documents.excerpt(text, focus, PACKET_FILE_CHARS) if text else "",
                "note": note,
            }
        )
    return {
        "email_id": email.id,
        "subject": email.subject,
        "sender_name": email.sender_name,
        "sender_email": email.sender_email,
        "received_at": email.received_at,
        "body": (email.body_text or "")[:4000],
        "filenames": [att.filename for att in email.attachments],
        "attachments": attachments,
        "extracted": {
            "invoice_numbers": email.extracted.invoice_numbers[:8],
            "po_numbers": email.extracted.po_numbers[:8],
            "amounts": email.extracted.amounts[:8],
            "due_dates": email.extracted.due_dates[:8],
            "account_last4": email.extracted.account_last4[:4],
        },
        "script_draft": {
            "category": email.category.value,
            "folder": email.folder,
            "importance": email.importance.value,
            "summary": email.summary,
            "flags": email.flags,
            "note": "Hint from the fast tools. You decide the reading unless a fraud flag is present.",
        },
        "saved_corrections_for_sender": matched[:6],
    }


def overlay_reading(
    email: EmailRecord, parsed: dict, *, now: datetime | None = None, tz: tzinfo | None = None
) -> EmailRecord:
    """Apply a model reading onto a message.

    Fraud cannot be talked out of Important. A small model's summary that
    quotes a dollar amount not in the message is replaced by the script
    summary, a due date it made up is dropped, and a message with a task due
    within a week stays in Important.
    """
    now = now or datetime.now(timezone.utc)
    today = _today(now, tz)
    script_line = email.summary
    category = _category(parsed.get("category"), email.category)
    importance = _importance(parsed.get("importance"), email.importance)
    folder = parsed.get("folder") if parsed.get("folder") in FOLDERS else email.folder or "informational"
    summary = _clean_sentence(parsed.get("summary") or email.summary or "")
    why = _clean_sentence(parsed.get("why") or "Local model reading", limit=300)
    guard_notes: list[str] = []
    invented = _ungrounded_amounts(summary, email)
    if invented:
        guard_notes.append(
            f"Kept the script summary: the model quoted {', '.join(invented)}, which is not in the message."
        )
        summary = script_line

    fraud = _is_fraud(email)
    if category == DocumentType.PAYMENT_INSTRUCTION_CHANGE and not fraud:
        # The model alone cannot block an email: the fraud check found no bank-change request.
        category = email.category
        if "fraud_cleared" not in email.flags:
            email.flags = list(dict.fromkeys([*email.flags, "model_said_bank_change", "payment_caution"]))
            guard_notes.append(
                "The model read a bank-detail change that the fraud check did not flag. "
                "Confirm any bank change by phone before acting on it."
            )
    if fraud:
        if folder != "important" or importance != Importance.CRITICAL:
            guard_notes.append(
                f"Kept in Important as critical: possible payment-instruction fraud (the model said {folder}, {importance.value})."
            )
        folder = "important"
        importance = Importance.CRITICAL
        if category in INFO_CATEGORIES | {DocumentType.OTHER, DocumentType.MIXED}:
            category = (
                email.category
                if email.category == DocumentType.PAYMENT_INSTRUCTION_CHANGE
                else DocumentType.PAYMENT_INSTRUCTION_CHANGE
            )
        email.flags = list(dict.fromkeys([*email.flags, "fraud_risk", "do_not_process"]))
    else:
        email.flags = [flag for flag in email.flags if flag != "needs_model"]

    email.category = category
    email.importance = importance
    email.importance_score = _score(importance, fraud)
    email.folder = folder
    email.summary = summary or script_summary(email)
    email.model_status = "bionic"
    email.category_confidence = max(email.category_confidence, 0.7)
    email.flags = [flag for flag in email.flags if flag != "needs_model"]
    if "bionic" not in email.flags:
        email.flags.append("bionic")
    as_of = _as_of(email, tz)
    previous_actions = list(email.actions)
    provided = parsed.get("actions")
    if isinstance(provided, list) and provided:
        email.actions = _actions_from_model(email.id, provided, as_of, now, known_dates=_known_dates(email))
        dropped = [item for item in email.actions if item.detail.startswith(_DATE_NOTE)]
        if dropped:
            guard_notes.append(
                f"Replaced {len(dropped)} due date(s) the model gave that are not in the message"
                + (" with the message's own date." if all(item.due_date for item in dropped) else ".")
            )
    if fraud:
        # Only the verify-by-phone task is safe on a suspected bank change: anything else the
        # model suggests ("call them, then update the vendor record") could act on the new details.
        verify = [item for item in email.actions if item.title == VERIFY_TITLE] or [
            item for item in previous_actions if item.title == VERIFY_TITLE
        ]
        dropped = [item for item in email.actions if item.title != VERIFY_TITLE]
        if dropped:
            guard_notes.append(
                f"Removed {len(dropped)} task(s): on a possible bank-detail change the only task is to verify by phone."
            )
        email.actions = verify[:1] or [_verify_action(email.id, today, now)]
    elif email.folder != "important":
        soon = (today + timedelta(days=7)).isoformat()
        pressing = [
            item
            for item in email.actions
            if item.status == ActionStatus.OPEN and item.due_date and item.due_date <= soon
        ]
        if pressing:
            email.folder = "important"
            if email.importance == Importance.LOW:
                email.importance = Importance.MEDIUM
                email.importance_score = _score(Importance.MEDIUM, False)
            guard_notes.append(f"Kept in Important: “{pressing[0].title}” is due {pressing[0].due_date}.")
    if fraud and not _warns(email.summary):
        email.summary = FRAUD_SUMMARY
        guard_notes.append("Replaced the model's summary with a fraud warning: it did not warn about the bank change.")

    reasons = [item for item in email.importance_reasons if not item.startswith(("Bionic:", "Guard:"))]
    email.importance_reasons = [f"Bionic: {why}"] + [f"Guard: {note}" for note in guard_notes] + reasons
    return email


def apply_bionic_reading(store, email_id: str, parsed: dict, tz: tzinfo | None = None) -> EmailRecord:
    """Save a model reading. A message the user corrected is never overwritten (``ReadingRefused``)."""
    email = store.get_email(email_id)
    if email is None:
        raise KeyError(email_id)
    if is_corrected(email):
        raise ReadingRefused(f"Message {email_id} was corrected by you; the model reading was not saved.")
    overlay_reading(email, parsed, tz=tz)
    store.upsert_email(email)
    return store.get_email(email_id) or email


def is_corrected(email: EmailRecord) -> bool:
    return email.model_status == "corrected" or "user_trained" in email.flags


def _is_fraud(email: EmailRecord) -> bool:
    return "fraud_risk" in email.flags or email.category == DocumentType.PAYMENT_INSTRUCTION_CHANGE


def _category(value, fallback: DocumentType) -> DocumentType:
    try:
        return DocumentType(str(value))
    except ValueError:
        return fallback


def _importance(value, fallback: Importance) -> Importance:
    try:
        return Importance(str(value))
    except ValueError:
        return fallback


def _score(importance: Importance, fraud: bool) -> int:
    if fraud:
        return 100
    return {
        Importance.CRITICAL: 90,
        Importance.HIGH: 72,
        Importance.MEDIUM: 48,
        Importance.LOW: 18,
    }[importance]


def _as_of(email: EmailRecord, tz: tzinfo | None = None) -> date:
    """The day the message arrived where the user is. Evening mail west of UTC is already tomorrow in UTC."""
    return received_day(email.received_at, tz)


def _today(now: datetime, tz: tzinfo | None) -> date:
    """Today where the user is: what due dates and "within a week" are measured against."""
    zone = tz or timezone.utc
    current = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return current.astimezone(zone).date()


_DATE_NOTE = "Model suggested due"
_MONEY = re.compile(r"\$\s?((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)(\s?[kKmM]\b)?")


def _clean_sentence(value, *, limit: int = 240) -> str:
    text = re.sub(r"[*_`#>]+", "", str(value or ""))
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return text


_WARN_PHONE = re.compile(r"\b(?:phone|call|known\s+number|number\s+on\s+file)\b", re.I)
_WARN_HOLD = re.compile(
    r"\b(?:do\s+not|don'?t|never)\s+(?:pay|change|update|release|send|wire|act)\b|\bbefore\s+(?:any|paying|a)\s+payment\b",
    re.I,
)


def _warns(summary: str) -> bool:
    """A fraud summary must say to phone and not to pay or change anything; "verify" alone is not a warning."""
    text = summary or ""
    return text == FRAUD_SUMMARY or bool(_WARN_PHONE.search(text) and _WARN_HOLD.search(text))


def _haystack(email: EmailRecord) -> str:
    parts = [email.subject, email.body_text] + [att.extracted_text for att in email.attachments]
    return "\n".join(part or "" for part in parts)


def _ungrounded_amounts(summary: str, email: EmailRecord) -> list[str]:
    """Dollar figures in a model summary that the message itself does not contain."""
    known = [float(value) for value in email.extracted.amounts]
    for att in email.attachments:
        known += [float(value) for value in att.extracted_fields.amounts]
    haystack = _haystack(email).replace(",", "")
    invented = []
    for match in _MONEY.finditer(summary or ""):
        raw = match.group(1).replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        if match.group(2):
            value *= 1_000_000 if match.group(2).strip().lower() == "m" else 1_000
            if any(abs(value - item) <= max(0.06 * item, 1) for item in known):
                continue
        elif any(abs(value - item) < 0.01 for item in known) or _number_in(raw, haystack):
            continue
        invented.append(match.group(0).strip())
    return invented


def _number_in(raw: str, haystack: str) -> bool:
    """``raw`` appears as a whole number: "4850" is not in "48500", and "500" is not in "1500"."""
    return re.search(rf"(?<![\d.]){re.escape(raw)}(?!\d)", haystack) is not None


def _known_dates(email: EmailRecord) -> set[str]:
    dates = set(email.extracted.due_dates)
    for att in email.attachments:
        dates.update(att.extracted_fields.due_dates)
    return dates


def _actions_from_model(
    email_id: str,
    raw_actions: list,
    as_of: date,
    now: datetime,
    *,
    known_dates: set[str] | None = None,
) -> list[ActionItem]:
    created = now.replace(microsecond=0).isoformat()
    items: list[ActionItem] = []
    seen: set[str] = set()
    for raw in raw_actions[:8]:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()[:180]
        key = title.lower()
        if not title or key in seen:
            continue
        seen.add(key)
        due = raw.get("due") or raw.get("due_date")
        due_text = str(due).strip() if due else None
        if due_text in {"", "null", "none", "None"}:
            due_text = None
        if due_text and len(due_text) >= 10:
            due_text = due_text[:10]
        if due_text and not _iso_date(due_text):
            due_text = None
        detail = str(raw.get("detail") or "")[:500]
        if due_text and known_dates is not None and due_text not in known_dates:
            only = next(iter(known_dates)) if len(known_dates) == 1 else None
            used = f" Using {only}, the date in the message." if only else ""
            detail = f"{_DATE_NOTE} {due_text}; that date is not in the message.{used} {detail}".strip()
            due_text = only
        priority = _importance(raw.get("priority"), Importance.MEDIUM)
        items.append(
            ActionItem(
                id=str(uuid.uuid4()),
                email_id=email_id,
                title=title,
                detail=detail,
                due_date=due_text,
                priority=priority,
                status=ActionStatus.OPEN,
                source="bionic",
                created_at=created,
            )
        )
    return items


def _verify_action(email_id: str, as_of: date, now: datetime) -> ActionItem:
    return ActionItem(
        id=str(uuid.uuid4()),
        email_id=email_id,
        title=VERIFY_TITLE,
        detail="Do not update vendor master data or release a payment from this email. Call a known number on file.",
        due_date=as_of.isoformat(),
        priority=Importance.CRITICAL,
        status=ActionStatus.OPEN,
        source="fraud_rule",
        created_at=now.replace(microsecond=0).isoformat(),
    )


def _iso_date(text: str) -> bool:
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True
