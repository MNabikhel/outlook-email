"""The email page with real .msg files: reading and downloading attachments, the fraud lock, verdicts, and the fraud page."""

from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from controller_inbox import fraud, web
from controller_inbox.local_llm import ModelStatus


@pytest.fixture
def client(store, settings, mail):
    return TestClient(web.create_app(settings, store))


def _n(email, filename: str) -> int:
    return [att.filename for att in email.attachments].index(filename) + 1


def test_email_page_lists_files_to_read_download_and_ask_about(client, mail):
    budget = mail["Q4 budget draft"]
    page = client.get(f"/inbox/{budget.id}")
    assert page.status_code == 200
    assert "Q4 budget.xlsx" in page.text and "Excel workbook" in page.text
    sheet = _n(budget, "Q4 budget.xlsx")
    assert f'href="/inbox/{budget.id}/files/{sheet}"' in page.text
    assert f'href="/inbox/{budget.id}/files/{sheet}/download"' in page.text
    assert 'data-action="ask-file"' in page.text and 'data-file="Offsite memo.docx"' in page.text
    assert "Fraud check" in page.text and "nothing suspicious" in page.text
    assert "Trust everyone at @taz.com" not in page.text, "taz.com is already trusted in settings"
    assert "Summarize this email" in page.text

    preview = client.get(f"/inbox/{budget.id}/preview")
    assert 'data-action="ask-file"' in preview.text and "Excel workbook" in preview.text


def test_file_page_shows_every_section_and_finds_words(client, mail):
    budget = mail["Q4 budget draft"]
    page = client.get(f"/inbox/{budget.id}/files/{_n(budget, 'Q4 budget.xlsx')}")
    assert page.status_code == 200
    assert "sheet &#34;Budget&#34;" in page.text
    assert "C4 (Q4): =SUM(C2:C3)" in page.text

    memo = client.get(f"/inbox/{budget.id}/files/{_n(budget, 'Offsite memo.docx')}", params={"q": "venue deposit"})
    assert "mention “venue deposit”" in memo.text
    assert 'file-part hit' in memo.text
    assert client.get(f"/inbox/{budget.id}/files/9").status_code == 404


def test_downloads_are_the_original_file_and_never_rendered(client, settings, mail):
    budget = mail["Q4 budget draft"]
    response = client.get(f"/inbox/{budget.id}/files/{_n(budget, 'Q4 budget.xlsx')}/download")
    assert response.status_code == 200
    saved = (settings.inbox_extracted / budget.id / "Q4 budget.xlsx").read_bytes()
    assert response.content == saved and response.content[:2] == b"PK"
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment")
    assert response.headers["x-content-type-options"] == "nosniff"


def test_fraud_email_files_are_readable_but_locked_until_cleared(client, store, mail):
    scam = mail["Updated remittance details"]
    page = client.get(f"/inbox/{scam.id}")
    assert "Files locked" in page.text and "Download locked" in page.text
    assert 'data-action="ask-file"' not in page.text
    assert f'/inbox/{scam.id}/original" download' not in page.text
    assert "possible payment fraud" in page.text and "Asks to change bank or payment details" in page.text

    text = client.get(f"/inbox/{scam.id}/files/1")
    assert text.status_code == 200 and "****8899" in text.text and "may be payment fraud" in text.text
    assert client.get(f"/inbox/{scam.id}/files/1/download").status_code == 403
    assert client.get(f"/inbox/{scam.id}/original").status_code == 403

    cleared = client.post(f"/inbox/{scam.id}/fraud", data={"choice": "safe:email", "note": "Called Acme on 555-0100"})
    assert cleared.status_code == 200 and "Saved as not fraud" in cleared.text
    assert "You marked this email as not fraud" in cleared.text
    assert client.get(f"/inbox/{scam.id}/files/1/download").status_code == 200
    event = store.fraud_log(limit=5, events=("marked_safe",))[0]
    assert event["email_id"] == scam.id and "Called Acme" in event["note"]


def test_reporting_and_trusting_from_the_email_page(client, store, mail):
    quote = mail["FW: Acme quote"]
    reported = client.post(f"/inbox/{quote.id}/fraud", data={"choice": "fraud:email"})
    assert "Reported as fraud" in reported.text
    assert "fraud_confirmed" in store.get_email(quote.id).flags
    assert client.get(f"/inbox/{quote.id}/files/1/download").status_code == 403

    scam = mail["Updated remittance details"]
    nonsense = client.post(f"/inbox/{scam.id}/fraud", data={"choice": "safe:nonsense"})
    assert "didn&#39;t save. Use the buttons" in nonsense.text


def test_fraud_page_manages_trusted_domains_and_exports_the_log(client, store, settings, mail):
    page = client.get("/fraud")
    assert page.status_code == 200
    assert "@taz.com" in page.text and "from settings" in page.text
    assert "Updated remittance details" in page.text, "the blocked email is listed"
    assert "What it has learned" in page.text
    assert "acme-payments.net" not in dict(fraud.suggested_domains(store, settings)), "never suggest a flagged sender"

    assert "free email service" in client.post("/fraud/domain", data={"domain": "gmail.com"}).text
    assert "enter a domain such as taz.com" in client.post("/fraud/domain", data={"domain": "not a domain"}).text
    added = client.post("/fraud/domain", data={"domain": "@Acme.com", "note": "vendor since 2019"})
    assert "Domain saved" in added.text and "vendor since 2019" in added.text
    assert any(row["value"] == "acme.com" and row["verdict"] == "safe" for row in store.trust_entries())
    removed = client.post("/fraud/domain/remove", data={"domain": "acme.com"})
    assert "Removed" in removed.text
    assert not store.trust_entries()

    rows = list(csv.reader(io.StringIO(client.get("/fraud/log.csv").text)))
    assert rows[0][:3] == ["at", "event", "level"]
    assert {"flagged", "trusted", "untrusted"} <= {row[1] for row in rows[1:]}


def test_log_export_keeps_spreadsheets_from_running_subjects(store):
    from controller_inbox.fraud import log_csv

    store.log_fraud({"at": "2026-09-01T00:00:00+00:00", "event": "flagged", "level": "none", "subject": "=HYPERLINK(\"x\")", "score": -30})
    row = list(csv.reader(io.StringIO(log_csv(store))))[1]
    assert row[5].startswith("'=") and row[3] == "-30"


def test_other_websites_cannot_post_to_the_dashboard(client, mail):
    scam = mail["Updated remittance details"]
    for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}):
        response = client.post(f"/inbox/{scam.id}/fraud", data={"choice": "safe:domain"}, headers=headers)
        assert response.status_code == 403, headers
        assert client.post("/fraud/domain", data={"domain": "evil.example"}, headers=headers).status_code == 403
    same = client.post("/fraud/domain", data={"domain": "acme.com"}, headers={"Origin": "http://testserver"})
    assert same.status_code == 200


def test_notes_from_reading_show_on_the_email_and_can_be_cleared(client, store, mail):
    budget = mail["Q4 budget draft"]
    store.add_finding(budget.id, "Q4 total is =SUM(C2:C3) on sheet Budget", question="is the total right?", at="2026-09-01T10:00:00+00:00")
    page = client.get(f"/inbox/{budget.id}")
    assert "Notes from Ask CloseDesk" in page.text and "Q4 total is =SUM(C2:C3)" in page.text
    cleared = client.post(f"/inbox/{budget.id}/findings/clear")
    assert "Notes cleared" in cleared.text and not store.findings(budget.id)


def test_setup_shows_the_context_window_and_how_to_raise_it(client, settings, monkeypatch):
    status = ModelStatus(mode="auto", reachable=True, models=["qwen2.5-7b"], model="qwen2.5-7b", base_url="x", context_length=4096)
    monkeypatch.setattr(web, "check_model", lambda *args, **kwargs: status)
    page = client.get("/settings")
    assert "4,096 tokens" in page.text and "Context Length 16,384" in page.text
    status.context_length = 32768
    assert "Enough to read whole attachments" in client.get("/settings").text
