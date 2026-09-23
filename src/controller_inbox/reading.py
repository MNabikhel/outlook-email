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
from datetime import date, datetime, timedelta, timezone

from controller_inbox.models import (
    DOCUMENT_LABELS,
    FOLDERS,
    ActionItem,
    ActionStatus,
    DocumentType,
    EmailRecord,
    Importance,
)

REFERENCE_CATEGORIES = {
    DocumentType.BANK_STATEMENT,
    DocumentType.BANK_RECONCILIATION,
    DocumentType.PURCHASE_ORDER,
    DocumentType.CONTRACT,
    DocumentType.INSURANCE,
    DocumentType.SPREADSHEET,
    DocumentType.IMAGE_SCAN,
    DocumentType.PACKING_SLIP,
}

ACTION_CATEGORIES = {
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

INFO_CATEGORIES = {DocumentType.NEWSLETTER, DocumentType.INTERNAL_FYI}

VERIFY_TITLE = "Verify payment-instruction change by phone before doing anything"

_PAY_WORDS = ("pay the", "process the wire", "release the", "new account", "updated wiring", "wire the funds")


def script_folder(category: DocumentType, importance: Importance, flags: list[str]) -> str:
    if "fraud_risk" in flags or category == DocumentType.PAYMENT_INSTRUCTION_CHANGE:
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
    for att in email.attachments[:4]:
        text = (att.extracted_text or "").strip()
        attachments.append(
            {
                "filename": att.filename,
                "script_type": att.document_type.value,
                "text": text[:1200] if text else "",
                "note": "" if text else "No extractable text. Treat this as a scan and do not invent its contents.",
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


def overlay_reading(email: EmailRecord, parsed: dict, *, now: datetime | None = None) -> EmailRecord:
    """Apply a model reading onto a message.

    Fraud cannot be talked out of Important. A small model's summary that
    quotes a dollar amount not in the message is replaced by the script
    summary, a due date it made up is dropped, and a message with a task due
    within a week stays in Important.
    """
    now = now or datetime.now(timezone.utc)
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

    fraud = _is_fraud(email, category)
    if fraud:
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
    as_of = _as_of(email)
    provided = parsed.get("actions")
    if isinstance(provided, list) and provided:
        email.actions = _actions_from_model(email.id, provided, as_of, now, known_dates=_known_dates(email))
        dropped = [item for item in email.actions if item.detail.startswith(_DATE_NOTE)]
        if dropped:
            guard_notes.append(f"Dropped {len(dropped)} due date(s) the model gave that are not in the message.")
    if fraud:
        email.actions = [item for item in email.actions if not _looks_like_payment(item.title)]
        if not any("phone" in item.title.lower() for item in email.actions):
            email.actions.insert(0, _verify_action(email.id, as_of, now))
    elif email.folder != "important":
        soon = (as_of + timedelta(days=7)).isoformat()
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
        email.summary = "Possible payment-instruction fraud — verify by phone before anything else. " + email.summary

    reasons = [item for item in email.importance_reasons if not item.startswith(("Bionic:", "Guard:"))]
    email.importance_reasons = [f"Bionic: {why}"] + [f"Guard: {note}" for note in guard_notes] + reasons
    return email


def apply_bionic_reading(store, email_id: str, parsed: dict) -> EmailRecord:
    email = store.get_email(email_id)
    if email is None:
        raise KeyError(email_id)
    overlay_reading(email, parsed)
    store.upsert_email(email)
    return store.get_email(email_id) or email


def _is_fraud(email: EmailRecord, category: DocumentType) -> bool:
    return (
        "fraud_risk" in email.flags
        or email.category == DocumentType.PAYMENT_INSTRUCTION_CHANGE
        or category == DocumentType.PAYMENT_INSTRUCTION_CHANGE
    )


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


def _as_of(email: EmailRecord) -> date:
    try:
        return datetime.fromisoformat(email.received_at).date()
    except ValueError:
        return datetime.now(timezone.utc).date()


_DATE_NOTE = "Model suggested due"
_MONEY = re.compile(r"\$\s?((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)(\s?[kKmM]\b)?")


def _clean_sentence(value, *, limit: int = 240) -> str:
    text = re.sub(r"[*_`#>]+", "", str(value or ""))
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return text


def _warns(summary: str) -> bool:
    text = (summary or "").lower()
    return any(word in text for word in ("verify", "phone", "fraud", "do not pay", "confirm by"))


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
        elif any(abs(value - item) < 0.01 for item in known) or raw in haystack:
            continue
        invented.append(match.group(0).strip())
    return invented


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
            detail = f"{_DATE_NOTE} {due_text}; that date is not in the message. {detail}".strip()
            due_text = None
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


def _looks_like_payment(title: str) -> bool:
    text = title.lower()
    return any(word in text for word in _PAY_WORDS)
