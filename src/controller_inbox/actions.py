from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

from controller_inbox.extract import parse_due_date
from controller_inbox.models import (
    ActionItem,
    ActionStatus,
    DocumentType,
    EmailRecord,
    ExtractedFields,
    Importance,
)


ACTION_RE = re.compile(
    r"(please\s+(?:approve|review|sign|send|confirm|process|pay|code|post|reconcile|"
    r"respond|provide|complete|enter|forward|flag|verify)|"
    r"kindly\s+(?:approve|review|sign|send|confirm|process|provide)|"
    r"(?:need|needs|needed)\s+you\s+to|"
    r"(?:can|could)\s+you\s+(?:please\s+)?(?:approve|review|send|confirm|process|pay|provide|reconcile)|"
    r"action\s+required|"
    r"for\s+your\s+(?:approval|review)|"
    r"please\s+see\s+that|"
    r"do\s+not\s+process)",
    re.IGNORECASE,
)

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def extract_actions(
    *,
    email_id: str,
    subject: str,
    body: str,
    category: DocumentType,
    importance: Importance,
    fields: ExtractedFields,
    flags: list[str],
    as_of: date,
    now: datetime | None = None,
) -> list[ActionItem]:
    now = now or datetime.utcnow()
    created = now.replace(microsecond=0).isoformat()
    items: list[ActionItem] = []
    seen: set[str] = set()

    def add(title: str, detail: str, due: str | None, priority: Importance, source: str) -> None:
        key = title.strip().lower()
        if not key or key in seen:
            return
        seen.add(key)
        items.append(
            ActionItem(
                id=str(uuid.uuid4()),
                email_id=email_id,
                title=title.strip()[:180],
                detail=detail.strip()[:500],
                due_date=due,
                priority=priority,
                status=ActionStatus.OPEN,
                source=source,
                created_at=created,
            )
        )

    if "fraud_risk" in flags or category == DocumentType.PAYMENT_INSTRUCTION_CHANGE:
        add(
            "Verify payment-instruction change by phone before doing anything",
            "Do not update vendor master data or release a payment from this email. Call a known number on file.",
            as_of.isoformat(),
            Importance.CRITICAL,
            "fraud_rule",
        )

    if "duplicate_invoice" in flags and fields.primary_invoice:
        add(
            f"Review possible duplicate invoice {fields.primary_invoice}",
            "This invoice number was already seen on another message. Confirm it is not a double-entry.",
            as_of.isoformat(),
            Importance.HIGH,
            "duplicate_rule",
        )

    if "missing_attachment" in flags:
        add(
            f"Request the missing attachment: {subject}",
            "The sender said something was attached, but no file arrived.",
            as_of.isoformat(),
            Importance.MEDIUM,
            "missing_attachment",
        )

    due = fields.primary_due
    amount = fields.primary_amount
    invoice = fields.primary_invoice
    amount_txt = f"${amount:,.2f}" if amount is not None else "an amount"
    if category == DocumentType.AP_INVOICE:
        title = f"Code and enter invoice {invoice or subject}"
        if amount is not None:
            title += f" ({amount_txt})"
        add(title, f"Vendor invoice ready for AP. Due {due or 'unspecified'}.", due, _due_priority(due, as_of, importance), "invoice")
    elif category == DocumentType.CREDIT_MEMO:
        add(
            f"Enter credit memo {invoice or subject}",
            "Apply the credit against the vendor account.",
            due,
            importance,
            "credit_memo",
        )
    elif category == DocumentType.BANK_STATEMENT:
        add(
            "Reconcile bank statement",
            subject,
            _close_due(as_of),
            Importance.HIGH if _days_to_month_end(as_of) <= 10 else Importance.MEDIUM,
            "bank_statement",
        )
    elif category == DocumentType.BANK_RECONCILIATION:
        add("Review / finish bank reconciliation", subject, _close_due(as_of), Importance.HIGH, "bank_rec")
    elif category == DocumentType.PAYROLL:
        add("Review payroll register and post payroll JE", subject, due or as_of.isoformat(), Importance.HIGH, "payroll")
    elif category == DocumentType.REMITTANCE_ADVICE:
        add(
            f"Apply cash from remittance {invoice or subject}",
            "Match the remittance to open AR and post cash.",
            due or as_of.isoformat(),
            Importance.HIGH,
            "cash_app",
        )
    elif category == DocumentType.EXPENSE_REPORT:
        add("Review and approve expense report", subject, due, importance if importance != Importance.LOW else Importance.MEDIUM, "expense")
    elif category == DocumentType.AUDIT_REQUEST:
        add("Complete auditor PBC / document request", subject, due or _plus_days(as_of, 3), Importance.HIGH, "audit")
    elif category == DocumentType.TAX_DOCUMENT:
        add("Review tax notice / filing and calendar the deadline", subject, due or _plus_days(as_of, 5), Importance.CRITICAL, "tax")
    elif category == DocumentType.WIRE_ACH_REQUEST:
        add(
            f"Approve or reject outgoing payment {amount_txt}",
            subject,
            due or as_of.isoformat(),
            Importance.HIGH,
            "wire",
        )
    elif category == DocumentType.WORKPAPER:
        add("Complete close workpaper", subject, _close_due(as_of), Importance.HIGH, "close")

    if "fraud_risk" not in flags and category != DocumentType.PAYMENT_INSTRUCTION_CHANGE:
        for sentence in _sentences(f"{subject}. {body}"):
            if not ACTION_RE.search(sentence):
                continue
            if category == DocumentType.NEWSLETTER:
                continue
            sent_due = None
            due_match = re.search(
                r"\b(?:by|before|due)\s+([A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{2,4}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2}|EOD|COB|today|tomorrow|Monday|Tuesday|Wednesday|Thursday|Friday)",
                sentence,
                re.I,
            )
            if due_match:
                sent_due = parse_due_date(due_match.group(1), as_of=as_of)
            add(
                _title_from_sentence(sentence),
                sentence.strip(),
                sent_due or due,
                importance if importance != Importance.LOW else Importance.MEDIUM,
                "body",
            )

    return items


def _sentences(text: str) -> list[str]:
    parts = [p.strip() for p in SENTENCE_SPLIT.split(text or "") if p.strip()]
    return [p for p in parts if 8 <= len(p) <= 400][:40]


def _title_from_sentence(sentence: str) -> str:
    cleaned = re.sub(r"^(hi|hello|good\s+(?:morning|afternoon)|team)[,:\s]+", "", sentence, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > 140:
        cleaned = cleaned[:137] + "..."
    return cleaned[0].upper() + cleaned[1:] if cleaned else sentence


def _due_priority(due: str | None, as_of: date, fallback: Importance) -> Importance:
    if not due:
        return fallback if fallback != Importance.LOW else Importance.MEDIUM
    try:
        days = (date.fromisoformat(due) - as_of).days
    except ValueError:
        return fallback
    if days <= 2:
        return Importance.HIGH
    if days <= 7:
        return Importance.HIGH if fallback == Importance.CRITICAL else Importance.MEDIUM
    return fallback


def _plus_days(as_of: date, days: int) -> str:
    return date.fromordinal(as_of.toordinal() + days).isoformat()


def _month_end(as_of: date) -> date:
    if as_of.month == 12:
        nxt = date(as_of.year + 1, 1, 1)
    else:
        nxt = date(as_of.year, as_of.month + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1)


def _days_to_month_end(as_of: date) -> int:
    return (_month_end(as_of) - as_of).days


def _close_due(as_of: date) -> str:
    return _month_end(as_of).isoformat()


def local_today(tz: ZoneInfo, now: datetime | None = None) -> date:
    current = now or datetime.now(tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    return current.astimezone(tz).date()
