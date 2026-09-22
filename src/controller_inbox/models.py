from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime
from enum import Enum
from typing import Any


class DocumentType(str, Enum):
    AP_INVOICE = "ap_invoice"
    AR_INVOICE = "ar_invoice"
    CREDIT_MEMO = "credit_memo"
    PURCHASE_ORDER = "purchase_order"
    PACKING_SLIP = "packing_slip"
    REMITTANCE_ADVICE = "remittance_advice"
    BANK_STATEMENT = "bank_statement"
    BANK_RECONCILIATION = "bank_reconciliation"
    WIRE_ACH_REQUEST = "wire_ach_request"
    PAYMENT_INSTRUCTION_CHANGE = "payment_instruction_change"
    PAYROLL = "payroll"
    EXPENSE_REPORT = "expense_report"
    TAX_DOCUMENT = "tax_document"
    CONTRACT = "contract"
    INSURANCE = "insurance"
    AUDIT_REQUEST = "audit_request"
    WORKPAPER = "workpaper"
    SPREADSHEET = "spreadsheet"
    IMAGE_SCAN = "image_scan"
    NEWSLETTER = "newsletter"
    INTERNAL_FYI = "internal_fyi"
    MIXED = "mixed"
    OTHER = "other"


class Importance(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ActionStatus(str, Enum):
    OPEN = "open"
    DONE = "done"
    SNOOZED = "snoozed"


DOCUMENT_LABELS = {
    DocumentType.AP_INVOICE: "AP invoice",
    DocumentType.AR_INVOICE: "AR / customer invoice",
    DocumentType.CREDIT_MEMO: "Credit memo",
    DocumentType.PURCHASE_ORDER: "Purchase order",
    DocumentType.PACKING_SLIP: "Packing slip",
    DocumentType.REMITTANCE_ADVICE: "Remittance / cash application",
    DocumentType.BANK_STATEMENT: "Bank statement",
    DocumentType.BANK_RECONCILIATION: "Bank reconciliation",
    DocumentType.WIRE_ACH_REQUEST: "Wire / ACH request",
    DocumentType.PAYMENT_INSTRUCTION_CHANGE: "Payment instruction change",
    DocumentType.PAYROLL: "Payroll",
    DocumentType.EXPENSE_REPORT: "Expense report",
    DocumentType.TAX_DOCUMENT: "Tax document",
    DocumentType.CONTRACT: "Contract / agreement",
    DocumentType.INSURANCE: "Insurance",
    DocumentType.AUDIT_REQUEST: "Audit / PBC request",
    DocumentType.WORKPAPER: "Workpaper",
    DocumentType.SPREADSHEET: "Spreadsheet / data file",
    DocumentType.IMAGE_SCAN: "Scanned image",
    DocumentType.NEWSLETTER: "Newsletter / FYI",
    DocumentType.INTERNAL_FYI: "Internal FYI",
    DocumentType.MIXED: "Mixed attachments",
    DocumentType.OTHER: "Other / uncategorized",
}

IMPORTANCE_LABELS = {
    Importance.CRITICAL: "Critical",
    Importance.HIGH: "High",
    Importance.MEDIUM: "Medium",
    Importance.LOW: "Low",
}


def json_safe(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


@dataclass
class ExtractedFields:
    invoice_numbers: list[str] = field(default_factory=list)
    po_numbers: list[str] = field(default_factory=list)
    amounts: list[float] = field(default_factory=list)
    due_dates: list[str] = field(default_factory=list)
    vendor_candidates: list[str] = field(default_factory=list)
    account_last4: list[str] = field(default_factory=list)
    mentions_routing_or_account: bool = False
    mentions_attachment: bool = False

    @property
    def primary_amount(self) -> float | None:
        return max(self.amounts) if self.amounts else None

    @property
    def primary_invoice(self) -> str | None:
        return self.invoice_numbers[0] if self.invoice_numbers else None

    @property
    def primary_due(self) -> str | None:
        return self.due_dates[0] if self.due_dates else None

    def merged_with(self, other: "ExtractedFields") -> "ExtractedFields":
        def uniq(items: list) -> list:
            seen: set[str] = set()
            out = []
            for item in items:
                key = str(item)
                if key not in seen:
                    seen.add(key)
                    out.append(item)
            return out

        return ExtractedFields(
            invoice_numbers=uniq(self.invoice_numbers + other.invoice_numbers),
            po_numbers=uniq(self.po_numbers + other.po_numbers),
            amounts=uniq(self.amounts + other.amounts),
            due_dates=uniq(self.due_dates + other.due_dates),
            vendor_candidates=uniq(self.vendor_candidates + other.vendor_candidates),
            account_last4=uniq(self.account_last4 + other.account_last4),
            mentions_routing_or_account=self.mentions_routing_or_account
            or other.mentions_routing_or_account,
            mentions_attachment=self.mentions_attachment or other.mentions_attachment,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ExtractedFields":
        if not data:
            return cls()
        allowed = {item.name for item in fields(cls)}
        return cls(**{key: data[key] for key in allowed if key in data})


@dataclass
class AttachmentRecord:
    id: str
    email_id: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    extracted_text: str
    document_type: DocumentType
    document_confidence: float
    extracted_fields: ExtractedFields = field(default_factory=ExtractedFields)
    classification_reasons: list[str] = field(default_factory=list)


@dataclass
class ActionItem:
    id: str
    email_id: str
    title: str
    detail: str
    due_date: str | None
    priority: Importance
    status: ActionStatus = ActionStatus.OPEN
    source: str = "body"
    created_at: str = ""


@dataclass
class EmailRecord:
    id: str
    subject: str
    sender_name: str
    sender_email: str
    received_at: str
    body_text: str
    body_preview: str
    has_attachments: bool
    outlook_importance: str
    is_read: bool
    category: DocumentType
    category_confidence: float
    importance: Importance
    importance_score: int
    importance_reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    extracted: ExtractedFields = field(default_factory=ExtractedFields)
    source: str = "graph"
    conversation_id: str = ""
    internet_message_id: str = ""
    writeback_status: str = "skipped"
    created_at: str = ""
    attachments: list[AttachmentRecord] = field(default_factory=list)
    actions: list[ActionItem] = field(default_factory=list)


@dataclass
class RawAttachment:
    id: str
    filename: str
    content_type: str
    size_bytes: int
    content: bytes = b""


@dataclass
class RawMessage:
    id: str
    subject: str
    sender_name: str
    sender_email: str
    received_at: datetime
    body_text: str
    body_preview: str
    has_attachments: bool
    outlook_importance: str = "normal"
    is_read: bool = False
    conversation_id: str = ""
    internet_message_id: str = ""
    source: str = "graph"
    attachments: list[RawAttachment] = field(default_factory=list)
