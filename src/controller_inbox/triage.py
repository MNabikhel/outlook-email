"""Pure, deterministic triage helpers.

This module decides which *bin* a message belongs in and produces a plain-English
summary without any network access. The local-LLM path (``llm.py``) can refine the
soft bins and replace the summary, but everything here is a safe fallback that runs
with no model available, which keeps the pipeline and the test-suite hermetic.
"""

from __future__ import annotations

from controller_inbox.models import (
    DOCUMENT_LABELS,
    DocumentType,
    EmailRecord,
    Importance,
    TriageBin,
)

# Bins the LLM is allowed to choose between. Fraud review and "there is a real
# action on this message" are decided by rules and never handed to the model.
_SOFT_BINS = {TriageBin.REVIEW, TriageBin.FYI, TriageBin.READ_LATER}

_READ_LATER_CATEGORIES = {DocumentType.NEWSLETTER}


def derive_triage_bin(
    *,
    category: DocumentType,
    importance: Importance,
    flags: list[str],
    has_actions: bool,
) -> TriageBin:
    """Rule-based bin. Deterministic and used as the guardrail for the LLM path."""
    if "fraud_risk" in flags or category == DocumentType.PAYMENT_INSTRUCTION_CHANGE:
        return TriageBin.FRAUD_REVIEW
    if category in _READ_LATER_CATEGORIES:
        return TriageBin.READ_LATER
    if has_actions:
        return TriageBin.ACTION_REQUIRED
    if importance in {Importance.CRITICAL, Importance.HIGH}:
        return TriageBin.ACTION_REQUIRED
    if importance == Importance.MEDIUM:
        return TriageBin.REVIEW
    return TriageBin.FYI


def reconcile_bin(rules_bin: TriageBin, llm_bin: TriageBin | None, *, has_actions: bool) -> TriageBin:
    """Combine the rule bin with an LLM suggestion, keeping rules authoritative.

    - Fraud review is never overridden.
    - A message with a concrete extracted action stays ``action_required``.
    - Otherwise the model may move it among the soft bins (review / fyi / read later).
    """
    if rules_bin == TriageBin.FRAUD_REVIEW:
        return TriageBin.FRAUD_REVIEW
    if has_actions:
        return TriageBin.ACTION_REQUIRED
    if llm_bin is None:
        return rules_bin
    if llm_bin == TriageBin.FRAUD_REVIEW:
        # The model wants to escalate; honour it as a review rather than a false fraud call.
        return TriageBin.REVIEW
    if llm_bin == TriageBin.ACTION_REQUIRED or llm_bin in _SOFT_BINS:
        return llm_bin
    return rules_bin


def coerce_bin(value: str | TriageBin | None) -> TriageBin | None:
    if value is None:
        return None
    if isinstance(value, TriageBin):
        return value
    token = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "fraud": TriageBin.FRAUD_REVIEW,
        "do_not_process": TriageBin.FRAUD_REVIEW,
        "action": TriageBin.ACTION_REQUIRED,
        "todo": TriageBin.ACTION_REQUIRED,
        "review": TriageBin.REVIEW,
        "needs_review": TriageBin.REVIEW,
        "info": TriageBin.FYI,
        "informational": TriageBin.FYI,
        "fyi": TriageBin.FYI,
        "read_later": TriageBin.READ_LATER,
        "newsletter": TriageBin.READ_LATER,
        "ignore": TriageBin.READ_LATER,
    }
    if token in aliases:
        return aliases[token]
    try:
        return TriageBin(token)
    except ValueError:
        return None


def deterministic_summary(record: EmailRecord) -> str:
    """A compact, human-readable one-liner built from the extracted facts."""
    label = DOCUMENT_LABELS.get(record.category, record.category.value)
    fields = record.extracted
    bits: list[str] = [label]
    if fields.primary_invoice:
        bits.append(fields.primary_invoice)
    sender = record.sender_name or record.sender_email
    if sender:
        bits.append(f"from {sender}")
    if fields.primary_amount is not None:
        bits.append(f"for ${fields.primary_amount:,.2f}")
    if fields.primary_due:
        bits.append(f"due {fields.primary_due}")
    sentence = " ".join(bits).strip()
    sentence = sentence[0].upper() + sentence[1:] if sentence else label
    if not sentence.endswith("."):
        sentence += "."
    tail = _flag_sentence(record.flags)
    if tail:
        sentence = f"{sentence} {tail}"
    return sentence


def deterministic_highlights(record: EmailRecord) -> list[str]:
    """Short bullet facts, reused when no model is available."""
    out: list[str] = []
    fields = record.extracted
    if fields.primary_amount is not None:
        out.append(f"Amount ${fields.primary_amount:,.2f}")
    if fields.primary_due:
        out.append(f"Due {fields.primary_due}")
    if fields.primary_invoice:
        out.append(f"Invoice {fields.primary_invoice}")
    if record.importance_reasons:
        out.append(record.importance_reasons[0])
    return out[:4]


def _flag_sentence(flags: list[str]) -> str:
    notes = {
        "fraud_risk": "Possible payment-instruction fraud — verify by phone.",
        "do_not_process": "Do not process from this email.",
        "duplicate_invoice": "Looks like a duplicate invoice.",
        "missing_attachment": "Sender referenced an attachment that did not arrive.",
        "overdue": "Past due.",
    }
    for key, note in notes.items():
        if key in flags:
            return note
    return ""
