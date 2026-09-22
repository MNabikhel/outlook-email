from datetime import datetime, timezone

from controller_inbox.actions import local_today
from controller_inbox.digest import build_digest
from controller_inbox.models import DocumentType, Importance
from controller_inbox.pipeline import ingest_demo


def test_demo_pipeline_categorizes_controller_mailbox(store, settings, as_of_now):
    records = ingest_demo(store, settings, now=as_of_now)
    by_id = {item.id: item for item in records}
    assert len(records) == 14

    invoice = by_id["demo-inv-10482"]
    assert invoice.category == DocumentType.AP_INVOICE
    assert invoice.extracted.primary_invoice == "INV-10482"
    assert invoice.extracted.primary_amount == 12850.0
    assert invoice.attachments[0].document_type == DocumentType.AP_INVOICE

    duplicate = by_id["demo-inv-10482-dup"]
    assert duplicate.category == DocumentType.AP_INVOICE
    assert "duplicate_invoice" in duplicate.flags
    assert "tax" not in duplicate.flags

    bec = by_id["demo-bec-wire"]
    assert bec.category == DocumentType.PAYMENT_INSTRUCTION_CHANGE
    assert bec.importance == Importance.CRITICAL
    assert "fraud_risk" in bec.flags
    assert "9988776612" not in bec.body_text

    assert by_id["demo-chase-stmt"].category == DocumentType.BANK_STATEMENT
    assert by_id["demo-payroll"].category == DocumentType.PAYROLL
    assert by_id["demo-pbc"].category == DocumentType.AUDIT_REQUEST
    assert by_id["demo-expense"].category == DocumentType.EXPENSE_REPORT
    assert by_id["demo-tax"].category == DocumentType.TAX_DOCUMENT
    assert by_id["demo-newsletter"].category == DocumentType.NEWSLETTER
    assert by_id["demo-remittance"].category == DocumentType.REMITTANCE_ADVICE
    assert by_id["demo-po"].category == DocumentType.PURCHASE_ORDER
    assert "missing_attachment" in by_id["demo-missing-att"].flags
    assert by_id["demo-wire-legit"].category == DocumentType.WIRE_ACH_REQUEST

    action_titles = " ".join(a.title.lower() for a in by_id["demo-bec-wire"].actions)
    assert "verify" in action_titles and "phone" in action_titles
    assert "please process the outstanding" not in action_titles


def test_daily_digest_lists_fraud_and_actions(loaded, settings, as_of_now):
    as_of = local_today(settings.tz, as_of_now)
    payload = build_digest(loaded, as_of=as_of, generated_at=as_of_now)
    assert payload["kpis"]["emails"] == 14
    assert payload["kpis"]["fraud_alerts"] >= 1
    assert payload["kpis"]["open_actions"] >= 8
    assert any("wiring" in item["subject"].lower() or "fraud" in ",".join(item["flags"]) for item in payload["critical_alerts"])
    assert all(
        "fraud_risk" in item["flags"] or item["category"] == "payment_instruction_change"
        for item in payload["critical_alerts"]
    )
    assert payload["invoices_to_enter"]
    assert payload["cash_to_apply"]
    assert "CloseDesk daily digest" in payload["markdown"]
    assert "INV-10482" in payload["markdown"] or "invoice" in payload["markdown"].lower()
