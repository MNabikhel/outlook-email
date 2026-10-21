from __future__ import annotations

import re
import uuid
from dataclasses import replace
from datetime import date, datetime, timezone, tzinfo
from zoneinfo import ZoneInfo

from controller_inbox.classify import own_words
from controller_inbox.clock import aware_utc, parse_stamp
from controller_inbox.extract import YEAR_AFTER_DAY, parse_due_date
from controller_inbox.models import (
    ActionItem,
    ActionStatus,
    DocumentType,
    ExtractedFields,
    Importance,
)
from controller_inbox.money import fmt_money


ACTION_RE = re.compile(
    r"(please\s+(?:approve|review|sign(?!\s*(?:in|up|out|into)\b)|send|confirm|process|pay|code|post|reconcile|"
    r"respond|provide|complete|enter|forward|flag|verify)|"
    r"kindly\s+(?:approve|review|sign(?!\s*(?:in|up|out|into)\b)|send|confirm|process|provide)|"
    r"(?:need|needs|needed)\s+you\s+to|"
    r"(?:can|could)\s+you\s+(?:please\s+)?(?:approve|review|send|confirm|process|pay|provide|reconcile)|"
    r"action\s+required|"
    r"for\s+your\s+(?:approval|review)|"
    r"please\s+see\s+that|"
    r"do\s+not\s+process)",
    re.IGNORECASE,
)

# A sentence about moving the money, which on an email with changed bank details waits for the phone call.
PAYING_RE = re.compile(r"\b(?:wire|pay(?:ment|ing)?|process|remit\w*|transfer|ach|account|bank(?:ing)?)\b", re.IGNORECASE)

# A sentence ends at ".", "!" or "?", but not at a month's short form: "by Oct. 15, 2026" is one date.
SENTENCE_SPLIT = re.compile(
    r"(?<=[.!?])(?<!\b(?:jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\.)(?<!\bsept\.)\s+|\n+", re.IGNORECASE
)


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
    sender: str = "",
    has_invite: bool = False,
    received_on: date | None = None,
) -> list[ActionItem]:
    """Tasks for a message.

    ``as_of`` is today: task priorities and "due today" are measured from it. Relative dates in
    the text ("by Friday", "tomorrow") are read from ``received_on``, the day the message arrived
    (``as_of`` when not given).
    """
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
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
    elif "bank_change_caution" in flags:
        # Cleared as not fraud, or from someone you trust: no block, but changed bank details still get a call,
        # first, and as urgent as the email.
        add(
            "Confirm the bank-detail change by phone before paying",
            "Bank details on this email changed. Call a number you already have before paying the new account.",
            as_of.isoformat(),
            max(importance, Importance.HIGH, key=_RANK.index),
            "caution_rule",
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
    amount_txt = fmt_money(amount) if amount is not None else "an amount"
    if category == DocumentType.AP_INVOICE:
        title = _named("Code and enter", "invoice", invoice, subject)
        if amount is not None:
            title += f" ({amount_txt})"
        add(title, f"Vendor invoice ready for AP. Due {due or 'unspecified'}.", due, _due_priority(due, as_of, importance), "invoice")
    elif category == DocumentType.CREDIT_MEMO:
        add(
            _named("Enter", "credit memo", invoice, subject),
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
            _named("Apply cash from", "remittance", invoice, subject),
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
        for sentence in _sentences(f"{subject}. {own_words(body)}"):
            if not ACTION_RE.search(sentence):
                continue
            if category == DocumentType.NEWSLETTER:
                continue
            if "bank_change_caution" in flags and PAYING_RE.search(sentence):
                # "Process the wire today" waits for the phone call above; the payment is its own task after it.
                continue
            sent_due = None
            due_match = re.search(
                r"\b(?:by|before|due(?:\s+on)?)\s+([A-Za-z]{3,9}\.?\s+\d{1,2},?\s+" + YEAR_AFTER_DAY + r"|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2}|EOD|COB|today|tomorrow|Monday|Tuesday|Wednesday|Thursday|Friday)",
                sentence,
                re.I,
            )
            if due_match:
                sent_due = parse_due_date(due_match.group(1), as_of=received_on or as_of)
            add(
                _title_from_sentence(sentence),
                sentence.strip(),
                sent_due or due,
                importance if importance != Importance.LOW else Importance.MEDIUM,
                "body",
            )

    body_tasks = any(item.source == "body" for item in items)
    if category == DocumentType.APPROVAL_REQUEST and not any(
        item.source == "body" and re.search(r"\b(approv\w*|sign)\b", item.title, re.I) for item in items
    ):
        add(f"Approve or decline: {subject}", f"{sender or 'The sender'} is waiting on your approval.", due, _due_priority(due, as_of, importance), "approval")
    elif category == DocumentType.REPLY_NEEDED and not body_tasks:
        who = (sender or "the sender").split("<")[0].strip() or "the sender"
        add(f"Reply to {who}: {subject}", "They asked you something and are waiting on an answer.", due, _due_priority(due, as_of, importance), "reply")
    elif category == DocumentType.MEETING and (has_invite or re.match(r"\s*(?:updated\s+)?invitation:.*\s@\s", subject or "", re.I)):
        add(f"Accept or decline: {subject}", "Meeting invitation waiting on your answer.", due, Importance.MEDIUM, "meeting")

    return items


def keep_task_status(fresh: list[ActionItem], stored: list[ActionItem]) -> list[ActionItem]:
    """When an email is read again, a task it still asks for keeps its id and what the user did with it:
    a snoozed task stays snoozed instead of coming back as a new open one. A task
    the user snoozed or finished is kept even when the new reading doesn't ask for it. A task's title carries the
    invoice number and amount, so a file that arrives later can retitle it: as in ``teach._same_tasks``, the one
    task of a kind left over is the same task as the one new task of that kind."""
    held = {item.title.strip().lower(): item for item in stored}
    pairs: dict[int, ActionItem] = {}
    for n, item in enumerate(fresh):
        old = held.pop(item.title.strip().lower(), None)
        if old is not None:
            pairs[n] = old
    for n, item in enumerate(fresh):
        if n in pairs:
            continue
        same = [old for old in held.values() if old.source == item.source]
        if len(same) == 1 and sum(1 for m, other in enumerate(fresh) if m not in pairs and other.source == item.source) == 1:
            pairs[n] = held.pop(same[0].title.strip().lower())
    kept = [
        replace(item, id=pairs[n].id, status=pairs[n].status, created_at=pairs[n].created_at) if n in pairs else item
        for n, item in enumerate(fresh)
    ]
    return kept + [item for item in held.values() if item.status != ActionStatus.OPEN]


def _named(verb: str, noun: str, number: str | None, subject: str) -> str:
    """A task named by its number ("Code and enter invoice INV-1"), or else by the subject. A subject that already
    says "invoice" isn't given the word twice ("Code and enter: Invoice attached — Harbor Packaging")."""
    if number:
        return f"{verb} {noun} {number}"
    if re.search(rf"\b{re.escape(noun)}", subject or "", re.IGNORECASE):
        return f"{verb.removesuffix(' from')}: {subject}"
    return f"{verb} {noun}: {subject}"


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
    # A close due date raises the priority; it never lowers what the email already earned.
    if days <= 2:
        return max(fallback, Importance.HIGH, key=_RANK.index)
    if days <= 7:
        return max(fallback, Importance.MEDIUM, key=_RANK.index)
    return fallback


_RANK = [Importance.LOW, Importance.MEDIUM, Importance.HIGH, Importance.CRITICAL]


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


def received_day(received_at, tz: tzinfo | None = None, *, fallback: date | None = None) -> date:
    """The day a message arrived where the user is.

    Relative dates in the message text ("by Friday") are anchored to this day. A timestamp
    without a zone is UTC; a blank or malformed one gives ``fallback`` (default: today).
    """
    zone = tz or timezone.utc
    received = received_at if isinstance(received_at, datetime) else parse_stamp(received_at)
    try:
        if received is not None:
            return aware_utc(received).astimezone(zone).date()
    except OverflowError:  # the first or last moment a datetime can hold, moved to another zone
        pass
    return fallback or datetime.now(zone).date()


def local_today(tz: ZoneInfo, now: datetime | None = None) -> date:
    current = now or datetime.now(tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    return current.astimezone(tz).date()
