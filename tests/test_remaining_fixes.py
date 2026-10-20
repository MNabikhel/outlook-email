"""The last three things the end-to-end check left open: a cleared bank-change request keeps a sensible category,
the Page tab's total on a schedule is its grand total, and a credit note's total keeps its minus sign."""

from __future__ import annotations

from datetime import date

from controller_inbox import fraud, teach
from controller_inbox.models import DocumentType
from controller_inbox.pipeline import ingest_demo


def test_a_cleared_bank_change_request_is_a_wire_request_not_other(store, settings, as_of_now):
    ingest_demo(store, settings, now=as_of_now)
    fraud.record_fraud_verdict(store, settings, "demo-bec-wire", verdict="safe", scope="email", now=as_of_now)
    email = store.get_email("demo-bec-wire")
    assert email.category == DocumentType.WIRE_ACH_REQUEST, "a request to send money, cleared but still checked by phone"
    assert "fraud_risk" not in email.flags and "payment_caution" in email.flags


def test_a_credit_notes_total_keeps_its_minus_sign():
    as_of = date(2026, 10, 20)
    assert teach.value_of("total", "Credit total (450.00)", as_of=as_of) == "-450.0"
    assert teach.value_of("total", "Amount due -1,250.00", as_of=as_of) == "-1250.0"
    assert teach.value_of("total", "Balance 1,250.00 CR", as_of=as_of) == "-1250.0"
    assert teach.value_of("total", "Total due $2,118.40", as_of=as_of) == "2118.4"


def _details(settings, data: bytes) -> dict:
    from controller_inbox import page_details, page_view

    settings.ensure_data_dir()
    found = page_view.regions(settings, data, "file.pdf", 1)["regions"]
    return {d["label"]: d["value"] for d in page_details.key_details(found, "text", None, {"invoices": [], "pos": [], "due_dates": [], "vendors": []})}


def test_a_schedules_total_due_is_its_grand_total(settings):
    from pathlib import Path

    data = (Path(__file__).parent / "fixtures" / "excel_schedules" / "AP Aging 9-30-26.pdf").read_bytes()
    # The total row lists Current, each aging bucket and the Total column: the grand total is under "Total".
    assert _details(settings, data)["Total due"].replace(" ", "") == "$267,159.31"


def test_an_invoices_total_due_is_unchanged(settings):
    from test_page_view import _detailed_invoice

    assert _details(settings, _detailed_invoice())["Total due"] == "$6,327.59"
