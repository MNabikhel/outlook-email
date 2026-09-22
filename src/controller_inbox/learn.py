"""Corrections the user makes become the next classification for that sender.

Rules still win when a message looks like a payment-instruction change. A saved
correction cannot silence that. Everything else the user teaches is applied on
the next similar message and written to a JSONL file for a local model.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from controller_inbox.actions import extract_actions
from controller_inbox.classify import Classification
from controller_inbox.config import Settings
from controller_inbox.models import DOCUMENT_LABELS, DocumentType, Importance
from controller_inbox.store import Store


def match_correction(store: Store, *, sender_email: str, subject: str) -> dict | None:
    sender = (sender_email or "").strip().lower()
    if sender:
        row = store.latest_correction(sender_email=sender)
        if row:
            return row
    normalized = _norm(subject)
    if not normalized:
        return None
    for row in store.list_corrections():
        if _norm(row["subject"]) == normalized:
            return row
    return None


def apply_learned(classification: Classification, correction: dict) -> Classification:
    corrected = DocumentType(correction["corrected_category"])
    reason = (correction.get("reason") or "").strip()
    if (
        "fraud_risk" in classification.flags
        and corrected != DocumentType.PAYMENT_INSTRUCTION_CHANGE
    ):
        classification.reasons.append("Kept the payment-instruction warning. A saved correction cannot clear it.")
        return classification

    classification.document_type = corrected
    classification.confidence = 0.99
    note = f"Learned from you: {reason}" if reason else "Learned from a correction you saved"
    classification.reasons.insert(0, note)
    if corrected == DocumentType.PAYMENT_INSTRUCTION_CHANGE:
        classification.flags = list(dict.fromkeys([*classification.flags, "fraud_risk", "do_not_process", "user_trained"]))
        classification.importance = Importance.CRITICAL
        classification.importance_score = 100
        classification.importance_reasons.insert(0, "You taught this pattern as a payment-instruction change")
        return classification

    classification.flags = [flag for flag in classification.flags if flag not in {"fraud_risk", "do_not_process"}]
    classification.flags.append("user_trained")
    if corrected == DocumentType.NEWSLETTER:
        classification.importance = Importance.LOW
        classification.importance_score = min(classification.importance_score, 15)
    return classification


def record_correction(
    store: Store,
    settings: Settings,
    *,
    email_id: str,
    corrected_category: str,
    reason: str,
) -> dict:
    email = store.get_email(email_id)
    if email is None:
        raise KeyError(email_id)
    corrected = DocumentType(corrected_category)
    reason = reason.strip()
    if len(reason) < 3:
        raise ValueError("Say why this category was wrong so the next message can learn from it.")
    row = {
        "id": str(uuid.uuid4()),
        "email_id": email.id,
        "previous_category": email.category.value,
        "corrected_category": corrected.value,
        "reason": reason[:500],
        "sender_email": (email.sender_email or "").lower(),
        "subject": email.subject,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    store.add_correction(row)
    export_training(store, settings)

    learned = Classification(
        document_type=email.category,
        confidence=email.category_confidence,
        reasons=list(email.importance_reasons),
        flags=list(email.flags),
        importance=email.importance,
        importance_score=email.importance_score,
        importance_reasons=list(email.importance_reasons),
    )
    # Manual correction is explicit, including clearing a fraud flag on THIS message.
    learned.flags = [flag for flag in learned.flags if flag not in {"fraud_risk", "do_not_process"}]
    learned = apply_learned(learned, row)
    if corrected != DocumentType.PAYMENT_INSTRUCTION_CHANGE:
        learned.flags = [flag for flag in learned.flags if flag not in {"fraud_risk", "do_not_process"}]
        learned.flags.append("user_trained")

    received = datetime.fromisoformat(email.received_at)
    as_of = received.astimezone(settings.tz).date()
    email.category = learned.document_type
    email.category_confidence = learned.confidence
    email.flags = list(dict.fromkeys(learned.flags))
    email.importance = learned.importance
    email.importance_score = learned.importance_score
    email.importance_reasons = [f"You corrected this to {DOCUMENT_LABELS[corrected]}: {reason}"] + [
        item for item in email.importance_reasons if not item.startswith("You corrected")
    ]
    email.actions = extract_actions(
        email_id=email.id,
        subject=email.subject,
        body=email.body_text,
        category=email.category,
        importance=email.importance,
        fields=email.extracted,
        flags=email.flags,
        as_of=as_of,
    )
    from controller_inbox.reading import assign_script_draft

    assign_script_draft(email)
    email.model_status = "corrected"
    store.upsert_email(email)
    return row


def export_training(store: Store, settings: Settings) -> None:
    settings.training_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for row in store.list_corrections():
        lines.append(
            json.dumps(
                {
                    "sender": row["sender_email"],
                    "subject": row["subject"],
                    "previous_category": row["previous_category"],
                    "category": row["corrected_category"],
                    "reason": row["reason"],
                },
                ensure_ascii=False,
            )
        )
    settings.training_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _norm(value: str) -> str:
    return " ".join((value or "").casefold().split())
