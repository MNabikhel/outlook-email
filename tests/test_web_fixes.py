"""Regression tests for the original-file download, task priority and rebuilt .eml fixes (one block per bug)."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from fastapi.testclient import TestClient

from controller_inbox import web
from controller_inbox.actions import extract_actions
from controller_inbox.folder_mail import ingest_folder
from controller_inbox.models import DocumentType, ExtractedFields, Importance

# --- A file dropped loose in the inbox downloaded as the "original" although its type is blocked ------------------


def test_original_of_a_dropped_macro_file_does_not_download(settings, store):
    # The workbook becomes its own email. Its attachment download was refused, but /original served the same file.
    settings.ensure_data_dir()
    (settings.inbox_incoming / "payment run.xlsm").write_bytes(b"PK\x03\x04 macro workbook")
    (email,) = ingest_folder(store, settings)
    client = TestClient(web.create_app(settings, store))

    assert client.get(f"/inbox/{email.id}/files/1/download").status_code == 403
    response = client.get(f"/inbox/{email.id}/original")
    assert response.status_code == 403
    assert "doesn't download" in response.text and "Outlook" not in response.text


def test_opening_a_dropped_macro_file_points_to_the_folder_it_is_in(settings, store):
    # "Only opens from Outlook" was wrong: the file was dropped in the inbox folder, not mailed.
    settings.ensure_data_dir()
    (settings.inbox_incoming / "payment run.xlsm").write_bytes(b"PK\x03\x04 macro workbook")
    (email,) = ingest_folder(store, settings)
    reply = TestClient(web.create_app(settings, store)).post(f"/inbox/{email.id}/open", headers={"X-CloseDesk": "1"}).json()
    assert reply["ok"] is False and "download" not in reply
    assert "Outlook" not in reply["message"] and str(settings.inbox_processed) in reply["message"], reply


def test_a_dropped_macro_files_pages_offer_no_download_of_it(settings, store):
    # Its original doesn't download, so the email page, the preview and the workspace don't offer to.
    settings.ensure_data_dir()
    (settings.inbox_incoming / "payment run.xlsm").write_bytes(b"PK\x03\x04 macro workbook")
    (settings.inbox_incoming / "statement.pdf").write_bytes(b"%PDF-1.4 statement")
    emails = {e.subject: e for e in ingest_folder(store, settings)}
    client = TestClient(web.create_app(settings, store))
    macro, pdf = (next(e for name, e in emails.items() if word in name) for word in ("payment", "statement"))
    for path in (f"/inbox/{macro.id}", f"/inbox/{macro.id}/preview"):
        assert f"/inbox/{macro.id}/original" not in client.get(path).text
    assert f"/inbox/{pdf.id}/original" in client.get(f"/inbox/{pdf.id}").text
    api = {"X-CloseDesk": "1"}
    assert client.get(f"/api/mail/{macro.id}", headers=api).json()["original_downloads"] is False
    assert client.get(f"/api/mail/{pdf.id}", headers=api).json()["original_downloads"] is True


def test_original_of_a_dropped_pdf_still_downloads(settings, store):
    settings.ensure_data_dir()
    (settings.inbox_incoming / "statement.pdf").write_bytes(b"%PDF-1.4 statement")
    (email,) = ingest_folder(store, settings)
    response = TestClient(web.create_app(settings, store)).get(f"/inbox/{email.id}/original")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"


# --- A Critical invoice due within a week was lowered to High -----------------------------------------------------

_ORDER = [Importance.LOW, Importance.MEDIUM, Importance.HIGH, Importance.CRITICAL]


def _invoice_task(due: str, importance: Importance = Importance.CRITICAL):
    fields = ExtractedFields(invoice_numbers=["INV-1"], amounts=[250000.0], due_dates=[due])
    (task,) = extract_actions(
        email_id="e1", subject="Invoice INV-1", body="", category=DocumentType.AP_INVOICE,
        importance=importance, fields=fields, flags=[], as_of=date(2026, 10, 8),
    )
    return task


def test_a_closer_due_date_never_lowers_the_task_priority():
    for importance in (Importance.CRITICAL, Importance.HIGH):
        later = _invoice_task("2026-11-08", importance).priority
        for due in ("2026-10-09", "2026-10-13"):
            soon = _invoice_task(due, importance).priority
            assert _ORDER.index(soon) >= _ORDER.index(later), (importance, due, soon, later)
    assert _invoice_task("2026-10-09", Importance.CRITICAL).priority == Importance.CRITICAL


# --- A sender address with a non-ASCII domain crashed the rebuilt .eml (500) --------------------------------------


def test_rebuilt_eml_with_an_international_sender_address():
    email = SimpleNamespace(
        subject="Rechnung 4711", sender_name="Jörg Müller", sender_email="joerg@müller.de",
        received_at="2026-10-01T09:00:00Z", body_text="Anbei die Rechnung.",
    )
    data = web._rebuilt_eml(email)
    assert b"Rechnung 4711" in data
    assert b"joerg@xn--mller-kva.de" in data
    # A non-ASCII local part can't go in an address formataddr writes; the address goes in on its own.
    data = web._rebuilt_eml(SimpleNamespace(**{**vars(email), "sender_email": "jörg@muller.de"}))
    assert b"Rechnung 4711" in data
