from datetime import datetime, timezone

from controller_inbox.models import DocumentType, Importance, TriageBin
from controller_inbox.pipeline import ingest_demo
from controller_inbox.triage import (
    coerce_bin,
    derive_triage_bin,
    deterministic_summary,
    reconcile_bin,
)


def test_fraud_always_maps_to_fraud_review():
    assert (
        derive_triage_bin(
            category=DocumentType.PAYMENT_INSTRUCTION_CHANGE,
            importance=Importance.CRITICAL,
            flags=["fraud_risk"],
            has_actions=True,
        )
        == TriageBin.FRAUD_REVIEW
    )


def test_newsletter_is_read_later():
    assert (
        derive_triage_bin(
            category=DocumentType.NEWSLETTER,
            importance=Importance.LOW,
            flags=[],
            has_actions=False,
        )
        == TriageBin.READ_LATER
    )


def test_actions_force_action_required():
    assert (
        derive_triage_bin(
            category=DocumentType.AP_INVOICE,
            importance=Importance.MEDIUM,
            flags=[],
            has_actions=True,
        )
        == TriageBin.ACTION_REQUIRED
    )


def test_medium_no_action_is_review():
    assert (
        derive_triage_bin(
            category=DocumentType.PURCHASE_ORDER,
            importance=Importance.MEDIUM,
            flags=[],
            has_actions=False,
        )
        == TriageBin.REVIEW
    )


def test_reconcile_keeps_fraud_and_actions_locked():
    # Fraud is never overridden by the model.
    assert reconcile_bin(TriageBin.FRAUD_REVIEW, TriageBin.FYI, has_actions=False) == TriageBin.FRAUD_REVIEW
    # A real action cannot be downgraded to read-later by the model.
    assert reconcile_bin(TriageBin.ACTION_REQUIRED, TriageBin.READ_LATER, has_actions=True) == TriageBin.ACTION_REQUIRED
    # Soft bins may be refined.
    assert reconcile_bin(TriageBin.REVIEW, TriageBin.FYI, has_actions=False) == TriageBin.FYI


def test_coerce_bin_aliases():
    assert coerce_bin("action") == TriageBin.ACTION_REQUIRED
    assert coerce_bin("Read Later") == TriageBin.READ_LATER
    assert coerce_bin("info") == TriageBin.FYI
    assert coerce_bin("garbage") is None
    assert coerce_bin(None) is None


def test_demo_bins_are_assigned(store, settings, as_of_now):
    records = ingest_demo(store, settings, now=as_of_now)
    by_id = {r.id: r for r in records}
    assert by_id["demo-bec-wire"].triage_bin == TriageBin.FRAUD_REVIEW
    assert by_id["demo-newsletter"].triage_bin == TriageBin.READ_LATER
    assert by_id["demo-inv-10482"].triage_bin == TriageBin.ACTION_REQUIRED
    # Every email gets a non-empty deterministic summary with rules source by default.
    for rec in records:
        assert rec.summary
        assert rec.ai_source == "rules"


def test_deterministic_summary_mentions_amount(store, settings, as_of_now):
    records = ingest_demo(store, settings, now=as_of_now)
    invoice = {r.id: r for r in records}["demo-inv-10482"]
    text = deterministic_summary(invoice)
    assert "INV-10482" in text
    assert "12,850" in text
