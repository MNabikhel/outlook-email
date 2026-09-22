from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from controller_inbox.models import DocumentType, ExtractedFields, Importance


@dataclass
class Classification:
    document_type: DocumentType
    confidence: float
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    importance: Importance = Importance.MEDIUM
    importance_score: int = 40
    importance_reasons: list[str] = field(default_factory=list)


@dataclass
class Rule:
    document_type: DocumentType
    weight: int
    keywords: tuple[str, ...] = ()
    regexes: tuple[re.Pattern[str], ...] = ()
    filename_keywords: tuple[str, ...] = ()
    sender_keywords: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    reason: str = ""


PAYMENT_CHANGE_RE = re.compile(
    r"(new\s+(?:bank(?:ing)?|routing|account|wire)\s+instruct|"
    r"updated\s+(?:bank(?:ing)?|wire|account|payment)\s+instruct|"
    r"(?<!not a )(?<!not )change(?:d|s)?\s+(?:in|of|to)\s+(?:bank|account|wiring|payment)|"
    r"please\s+use\s+(?:the\s+)?following\s+(?:account|routing|bank)|"
    r"wire\s+instructions\s+have\s+changed|"
    r"our\s+bank(?:ing)?\s+details\s+have\s+changed|"
    r"do\s+not\s+use\s+(?:the\s+)?previous\s+account)",
    re.IGNORECASE,
)

NEWSLETTER_RE = re.compile(
    r"(unsubscribe|view\s+in\s+browser|you\s+are\s+receiving\s+this|"
    r"weekly\s+roundup|this\s+week\s+in|newsletter)",
    re.IGNORECASE,
)

RULES: tuple[Rule, ...] = (
    Rule(
        DocumentType.PAYMENT_INSTRUCTION_CHANGE,
        120,
        regexes=(PAYMENT_CHANGE_RE,),
        flags=("fraud_risk", "do_not_process"),
        reason="Banking / payment instructions appear to have changed",
    ),
    Rule(
        DocumentType.TAX_DOCUMENT,
        95,
        keywords=("cp2000", "irs", "form 941", "form 1099", "w-2", "w-9", "franchise tax", "notice of deficiency", "penalty notice"),
        filename_keywords=("w-9", "w9", "1099", "w-2", "w2", "941", "tax-notice"),
        sender_keywords=("irs.gov", "treasury.gov", "ftb.ca.gov"),
        flags=("tax", "deadline"),
        reason="Tax form or notice language",
    ),
    Rule(
        DocumentType.AUDIT_REQUEST,
        90,
        keywords=("pbc", "prepared by client", "audit request", "sample selection", "provided by client", "external audit", "testing request", "workpaper request"),
        filename_keywords=("pbc", "pbc-list", "audit"),
        sender_keywords=("deloitte", "pwc", "ey.com", "kpmg", "rsmus", "bdo.com"),
        flags=("audit",),
        reason="Audit / PBC request",
    ),
    Rule(
        DocumentType.WIRE_ACH_REQUEST,
        80,
        keywords=("wire transfer", "wire request", "please wire", "ach payment", "initiate ach", "outgoing wire", "beneficiary bank"),
        filename_keywords=("wire", "ach-request"),
        flags=("payment_approval",),
        reason="Wire or ACH payment request",
    ),
    Rule(
        DocumentType.AP_INVOICE,
        78,
        keywords=("invoice", "amount due", "remit to", "bill to", "invoice number", "payment terms", "net 30", "net 15"),
        filename_keywords=("invoice", "inv-", "inv_", "bill"),
        reason="Vendor invoice language",
    ),
    Rule(
        DocumentType.CREDIT_MEMO,
        76,
        keywords=("credit memo", "credit note", "cm #", "amount credited"),
        filename_keywords=("credit-memo", "credit_memo", "cm-"),
        reason="Credit memo language",
    ),
    Rule(
        DocumentType.PURCHASE_ORDER,
        74,
        keywords=("purchase order", "po number", "ship to", "please fulfill"),
        filename_keywords=("purchase-order", "po-", "po_"),
        reason="Purchase order language",
    ),
    Rule(
        DocumentType.PACKING_SLIP,
        62,
        keywords=("packing slip", "packing list", "shipped qty", "delivery note"),
        filename_keywords=("packing", "delivery-note", "bol"),
        reason="Packing / receiving document",
    ),
    Rule(
        DocumentType.REMITTANCE_ADVICE,
        86,
        keywords=("remittance advice", "payment remittance", "cash application", "we have paid", "payment notification", "ach credit"),
        filename_keywords=("remittance", "remit", "payment-advice"),
        reason="Customer remittance / cash application",
    ),
    Rule(
        DocumentType.BANK_STATEMENT,
        82,
        keywords=("account statement", "ending balance", "beginning balance", "statement period", "checking statement", "business checking"),
        filename_keywords=("statement", "stmt", "bank-stmt"),
        sender_keywords=("chase.com", "bankofamerica.com", "wellsfargo.com", "pnc.com", "usbank.com", "bofa.com"),
        flags=("reconcile",),
        reason="Bank statement",
    ),
    Rule(
        DocumentType.BANK_RECONCILIATION,
        80,
        keywords=("bank rec", "bank reconciliation", "outstanding checks", "deposits in transit", "reconciled balance"),
        filename_keywords=("bank-rec", "reconciliation", "bkrec"),
        flags=("month_end",),
        reason="Bank reconciliation workpaper",
    ),
    Rule(
        DocumentType.PAYROLL,
        84,
        keywords=("payroll register", "pay date", "gross pay", "net pay", "employer taxes", "direct deposit register"),
        filename_keywords=("payroll", "register", "pay-register"),
        sender_keywords=("adp.com", "paychex.com", "gusto.com", "ukg.com", "paylocity.com"),
        flags=("payroll",),
        reason="Payroll register / payroll provider",
    ),
    Rule(
        DocumentType.EXPENSE_REPORT,
        72,
        keywords=("expense report", "expense reimbursement", "out of pocket", "mileage"),
        filename_keywords=("expense", "er-", "t&e", "tande"),
        flags=("approval",),
        reason="Expense report",
    ),
    Rule(
        DocumentType.CONTRACT,
        60,
        keywords=("master services agreement", "statement of work", "msa", "please countersign", "terms and conditions"),
        filename_keywords=("msa", "sow", "agreement", "contract", "nda"),
        reason="Contract / agreement",
    ),
    Rule(
        DocumentType.INSURANCE,
        58,
        keywords=("certificate of insurance", "evidence of coverage", "additional insured", "coi"),
        filename_keywords=("coi", "insurance", "acord"),
        reason="Insurance / COI",
    ),
    Rule(
        DocumentType.WORKPAPER,
        55,
        keywords=("rollforward", "flux analysis", "tie-out", "accrual support", "prepaid schedule", "close checklist"),
        filename_keywords=("rollforward", "flux", "accrual", "prepaid", "tieout", "workpaper"),
        flags=("month_end",),
        reason="Close / accounting workpaper",
    ),
    Rule(
        DocumentType.NEWSLETTER,
        40,
        regexes=(NEWSLETTER_RE,),
        keywords=("roundup", "industry news"),
        reason="Looks like a newsletter",
    ),
)


VIP_DEFAULT = (
    "cfo",
    "controller",
    "ceo@",
    "irs.gov",
    "treasury.gov",
    "deloitte",
    "pwc.com",
    "ey.com",
    "kpmg",
)


def _keyword_hit(blob: str, keyword: str) -> bool:
    """Whole-word match so 'irs' does not fire inside 'first'."""
    if not keyword:
        return False
    pattern = r"(?<![\w.])" + re.escape(keyword.lower()) + r"s?(?![\w.])"
    return re.search(pattern, blob) is not None


def _haystack(subject: str, body: str, filename: str, sender: str, extracted_text: str) -> str:
    return "\n".join([subject or "", body or "", filename or "", sender or "", extracted_text or ""]).lower()


def score_rules(
    *,
    subject: str,
    body: str,
    filename: str = "",
    sender: str = "",
    extracted_text: str = "",
) -> dict[DocumentType, tuple[int, list[str], list[str]]]:
    blob = _haystack(subject, body, filename, sender, extracted_text)
    file_low = (filename or "").lower()
    sender_low = (sender or "").lower()
    scores: dict[DocumentType, tuple[int, list[str], list[str]]] = {}
    for rule in RULES:
        hit = False
        reasons: list[str] = []
        matched_kw = next((k for k in rule.keywords if _keyword_hit(blob, k)), None)
        if matched_kw:
            hit = True
            reasons.append(f"keyword:{matched_kw}")
        for rx in rule.regexes:
            if rx.search(blob):
                hit = True
                reasons.append(rule.reason or rx.pattern[:40])
        if rule.filename_keywords and any(k in file_low for k in rule.filename_keywords):
            hit = True
            reasons.append("filename")
        if rule.sender_keywords and any(k in sender_low for k in rule.sender_keywords):
            hit = True
            reasons.append("sender")
        if not hit:
            continue
        prev = scores.get(rule.document_type, (0, [], []))
        flags = list(dict.fromkeys(list(prev[2]) + list(rule.flags)))
        scores[rule.document_type] = (
            prev[0] + rule.weight,
            list(dict.fromkeys(prev[1] + reasons + ([rule.reason] if rule.reason else []))),
            flags,
        )
    return scores


def classify_document(
    *,
    subject: str = "",
    body: str = "",
    filename: str = "",
    sender: str = "",
    extracted_text: str = "",
    content_type: str = "",
    has_text: bool | None = None,
) -> Classification:
    scores = score_rules(
        subject=subject,
        body=body,
        filename=filename,
        sender=sender,
        extracted_text=extracted_text,
    )
    if not scores:
        ctype = (content_type or "").lower()
        name = (filename or "").lower()
        if ctype.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".tif", ".tiff")):
            return Classification(DocumentType.IMAGE_SCAN, 0.45, ["image attachment"], importance=Importance.MEDIUM, importance_score=35)
        if name.endswith((".xlsx", ".xlsm", ".csv")) or "spreadsheet" in ctype:
            return Classification(DocumentType.SPREADSHEET, 0.4, ["spreadsheet without a stronger document type"], importance=Importance.MEDIUM, importance_score=40)
        if has_text is False:
            return Classification(DocumentType.IMAGE_SCAN, 0.3, ["no extractable text"], importance=Importance.LOW, importance_score=25)
        return Classification(DocumentType.OTHER, 0.2, ["no matching finance document pattern"], importance=Importance.LOW, importance_score=20)

    best_type, (weight, reasons, flags) = max(scores.items(), key=lambda item: item[1][0])
    confidence = min(0.98, 0.35 + weight / 180)
    return Classification(
        document_type=best_type,
        confidence=round(confidence, 2),
        reasons=reasons,
        flags=flags,
    )


def classify_email(
    *,
    subject: str,
    body: str,
    sender: str,
    outlook_importance: str,
    attachments: list[Classification],
    fields: ExtractedFields,
    as_of: date,
    high_amount: float = 10_000,
    vip_senders: list[str] | None = None,
    has_attachments: bool = False,
    duplicate_invoice: bool = False,
) -> Classification:
    attachment_types = [item.document_type for item in attachments]
    combined_flags: list[str] = []
    for item in attachments:
        combined_flags.extend(item.flags)
    email_scores = score_rules(subject=subject, body=body, sender=sender, extracted_text="")
    if email_scores:
        email_type, (weight, reasons, flags) = max(email_scores.items(), key=lambda item: item[1][0])
        combined_flags.extend(flags)
    else:
        email_type, weight, reasons = DocumentType.OTHER, 0, []

    if attachment_types:
        ranked = _rank_types(attachment_types)
        primary = ranked[0]
        if len(set(attachment_types)) > 1 and ranked[0] != DocumentType.PAYMENT_INSTRUCTION_CHANGE:
            category = DocumentType.MIXED if len(set(t for t in attachment_types if t not in {DocumentType.OTHER, DocumentType.SPREADSHEET, DocumentType.IMAGE_SCAN})) > 1 else primary
            if category == DocumentType.MIXED:
                # Keep a useful primary if one attachment is clearly the point of the email.
                if _type_rank(primary) <= _type_rank(DocumentType.AP_INVOICE):
                    category = primary
        else:
            category = primary
        confidence = max((item.confidence for item in attachments), default=0.5)
        reasons = [f"attachment:{item.document_type.value}" for item in attachments] + reasons
    else:
        category = email_type
        confidence = min(0.9, 0.3 + weight / 180) if weight else 0.25

    if fields.mentions_attachment and not has_attachments:
        combined_flags.append("missing_attachment")
        reasons.append("Email says something is attached, but no attachment was found")

    if duplicate_invoice:
        combined_flags.append("duplicate_invoice")
        reasons.append("Invoice number already seen on another email")

    if category == DocumentType.PAYMENT_INSTRUCTION_CHANGE or "fraud_risk" in combined_flags:
        combined_flags.extend(["fraud_risk", "do_not_process"])
        category = DocumentType.PAYMENT_INSTRUCTION_CHANGE

    importance, score, imp_reasons = _importance(
        category=category,
        flags=combined_flags,
        fields=fields,
        outlook_importance=outlook_importance,
        sender=sender,
        as_of=as_of,
        high_amount=high_amount,
        vip_senders=vip_senders or [],
        subject=subject,
        body=body,
    )
    return Classification(
        document_type=category,
        confidence=round(confidence, 2),
        reasons=list(dict.fromkeys(reasons)),
        flags=list(dict.fromkeys(combined_flags)),
        importance=importance,
        importance_score=score,
        importance_reasons=imp_reasons,
    )


_TYPE_PRIORITY = [
    DocumentType.PAYMENT_INSTRUCTION_CHANGE,
    DocumentType.TAX_DOCUMENT,
    DocumentType.AUDIT_REQUEST,
    DocumentType.WIRE_ACH_REQUEST,
    DocumentType.AP_INVOICE,
    DocumentType.BANK_STATEMENT,
    DocumentType.BANK_RECONCILIATION,
    DocumentType.PAYROLL,
    DocumentType.REMITTANCE_ADVICE,
    DocumentType.CREDIT_MEMO,
    DocumentType.EXPENSE_REPORT,
    DocumentType.PURCHASE_ORDER,
    DocumentType.CONTRACT,
    DocumentType.WORKPAPER,
    DocumentType.AR_INVOICE,
    DocumentType.INSURANCE,
    DocumentType.PACKING_SLIP,
    DocumentType.SPREADSHEET,
    DocumentType.IMAGE_SCAN,
    DocumentType.NEWSLETTER,
    DocumentType.INTERNAL_FYI,
    DocumentType.OTHER,
]


def _type_rank(doc_type: DocumentType) -> int:
    try:
        return _TYPE_PRIORITY.index(doc_type)
    except ValueError:
        return len(_TYPE_PRIORITY)


def _rank_types(types: list[DocumentType]) -> list[DocumentType]:
    return sorted(types, key=_type_rank)


def _importance(
    *,
    category: DocumentType,
    flags: list[str],
    fields: ExtractedFields,
    outlook_importance: str,
    sender: str,
    as_of: date,
    high_amount: float,
    vip_senders: list[str],
    subject: str,
    body: str,
) -> tuple[Importance, int, list[str]]:
    score = 30
    reasons: list[str] = []
    blob = f"{subject}\n{body}".lower()
    sender_low = (sender or "").lower()

    if "fraud_risk" in flags or category == DocumentType.PAYMENT_INSTRUCTION_CHANGE:
        return Importance.CRITICAL, 100, ["Possible payment-instruction / BEC fraud — verify by a known phone number"]

    if category == DocumentType.TAX_DOCUMENT:
        score += 40
        reasons.append("Tax notice / filing document")
    if category == DocumentType.AUDIT_REQUEST:
        score += 35
        reasons.append("Auditor request")
    if category == DocumentType.WIRE_ACH_REQUEST:
        score += 32
        reasons.append("Outgoing payment request")
    if category == DocumentType.AP_INVOICE:
        score += 18
        reasons.append("Vendor invoice to enter / schedule")
    if category == DocumentType.PAYROLL:
        score += 22
        reasons.append("Payroll")
    if category == DocumentType.BANK_STATEMENT:
        score += 12
        reasons.append("Bank statement to reconcile")
    if category == DocumentType.REMITTANCE_ADVICE:
        score += 14
        reasons.append("Cash to apply")
    if category in {DocumentType.NEWSLETTER, DocumentType.INTERNAL_FYI}:
        score -= 20
        reasons.append("Informational / newsletter")
    if "missing_attachment" in flags:
        score += 15
        reasons.append("Claimed attachment is missing")
    if "duplicate_invoice" in flags:
        score += 20
        reasons.append("Possible duplicate invoice")

    amount = fields.primary_amount
    if amount is not None and amount >= high_amount:
        score += 18
        reasons.append(f"Amount ${amount:,.2f} is at or above the review threshold")
    if amount is not None and amount >= high_amount * 5:
        score += 10
        reasons.append("Very large dollar amount")

    if fields.primary_due:
        try:
            due = date.fromisoformat(fields.primary_due)
            days = (due - as_of).days
            if days < 0:
                score += 25
                reasons.append(f"Past due ({due.isoformat()})")
                flags.append("overdue")
            elif days == 0:
                score += 22
                reasons.append("Due today")
            elif days <= 3:
                score += 16
                reasons.append(f"Due in {days} day(s)")
            elif days <= 7:
                score += 10
                reasons.append("Due within a week")
        except ValueError:
            pass

    last_day = _month_end(as_of)
    days_to_close = (last_day - as_of).days
    if days_to_close <= 7 and (
        category
        in {
            DocumentType.BANK_STATEMENT,
            DocumentType.BANK_RECONCILIATION,
            DocumentType.WORKPAPER,
            DocumentType.AP_INVOICE,
            DocumentType.PAYROLL,
        }
        or "month_end" in flags
        or "close" in blob
        or "month-end" in blob
        or "month end" in blob
    ):
        score += 12
        reasons.append(f"Month-end is in {days_to_close} day(s)")
        flags.append("month_end")

    if (outlook_importance or "").lower() == "high":
        score += 10
        reasons.append("Sender marked the message as high importance in Outlook")

    vip_needles = list(vip_senders) + list(VIP_DEFAULT)
    if any(needle in sender_low for needle in vip_needles):
        score += 12
        reasons.append("VIP / elevated sender")

    if re.search(r"\b(urgent|asap|immediately|eod|cob|today)\b", blob):
        score += 8
        reasons.append("Urgent timing language")

    score = max(0, min(99, score))
    if score >= 85:
        importance = Importance.CRITICAL
    elif score >= 62:
        importance = Importance.HIGH
    elif score >= 38:
        importance = Importance.MEDIUM
    else:
        importance = Importance.LOW
    return importance, score, list(dict.fromkeys(reasons))


def _month_end(as_of: date) -> date:
    if as_of.month == 12:
        nxt = date(as_of.year + 1, 1, 1)
    else:
        nxt = date(as_of.year, as_of.month + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1)


def month_end(as_of: date) -> date:
    return _month_end(as_of)


def outlook_categories(classification: Classification) -> list[str]:
    labels = ["CloseDesk", DOCUMENT_OUTLOOK.get(classification.document_type, classification.document_type.value)]
    if classification.importance in {Importance.CRITICAL, Importance.HIGH}:
        labels.append("CloseDesk-Important")
    if "fraud_risk" in classification.flags:
        labels.append("CloseDesk-FraudReview")
    return labels


DOCUMENT_OUTLOOK = {
    DocumentType.AP_INVOICE: "AP-Invoice",
    DocumentType.PAYMENT_INSTRUCTION_CHANGE: "Fraud-Review",
    DocumentType.BANK_STATEMENT: "Bank-Statement",
    DocumentType.PAYROLL: "Payroll",
    DocumentType.TAX_DOCUMENT: "Tax",
    DocumentType.AUDIT_REQUEST: "Audit",
    DocumentType.REMITTANCE_ADVICE: "Cash-App",
    DocumentType.EXPENSE_REPORT: "T&E",
    DocumentType.WIRE_ACH_REQUEST: "Payment-Approval",
    DocumentType.WORKPAPER: "Close",
}
