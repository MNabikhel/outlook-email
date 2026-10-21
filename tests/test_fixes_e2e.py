"""Regressions for the defects a walkthrough of the app found (one or more tests per defect)."""

from __future__ import annotations

import io
import os
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from controller_inbox import fraud, web
from controller_inbox.folder_mail import ingest_folder
from controller_inbox.models import ActionStatus, DocumentType
from controller_inbox.pipeline import ingest_demo
from msgfactory import XLSX, write_msg

PAGE = {"X-CloseDesk": "1"}
NOW = datetime(2026, 9, 22, 13, 0, tzinfo=timezone.utc)


def _age(path: Path) -> None:
    """Files written just now look like they are still being copied; date them a minute back."""
    old = time.time() - 60
    os.utime(path, (old, old))


# 1. "Not fraud" on a bank-detail change ---------------------------------------------------------------


def test_not_fraud_on_a_bank_change_lifts_the_block_but_keeps_the_caution(store, settings, as_of_now):
    ingest_demo(store, settings, now=as_of_now)
    fraud.record_fraud_verdict(store, settings, "demo-bec-wire", verdict="safe", scope="email", now=as_of_now)
    email = store.get_email("demo-bec-wire")
    check = store.fraud_check("demo-bec-wire")
    assert "fraud_risk" not in email.flags and "fraud_cleared" in email.flags
    # The bank details still changed: a phone call before paying stays on the list.
    assert check["level"] == "caution" and "payment_caution" in email.flags
    assert "phone" in email.summary.lower() and "meetings" not in email.summary.lower()
    titles = [item.title for item in email.actions if item.status == ActionStatus.OPEN]
    assert titles and "phone" in titles[0].lower()

    page = TestClient(web.create_app(settings, store)).get("/inbox/demo-bec-wire").text
    assert "nothing suspicious" not in page.lower() and "double-check before paying" in page.lower()


def test_a_cleared_email_with_a_high_score_does_not_say_nothing_suspicious(store, settings, as_of_now):
    ingest_demo(store, settings, now=as_of_now)
    client = TestClient(web.create_app(settings, store))
    # Payment wording without a bank change: the verdict clears it entirely, and the panel says who cleared it.
    email = store.get_email("demo-bec-wire")
    email.subject = "Urgent wire today"
    email.body_text = "Please process the outstanding $48,500.00 wire today to account 9988776612. Urgent, I am in meetings."
    store.upsert_email(email)
    fraud.record_fraud_verdict(store, settings, "demo-bec-wire", verdict="safe", scope="email", now=as_of_now)
    check = store.fraud_check("demo-bec-wire")
    assert check["level"] == "none"
    page = client.get("/inbox/demo-bec-wire").text.lower()
    assert "nothing suspicious" not in page and "marked not fraud" in page


# 2. A close pack with a workbook is not an AP invoice --------------------------------------------------


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


def _close_pack(settings) -> Path:
    book = Workbook()
    summary = book.active
    summary.title = "Summary"
    summary.append(["Account", "Sep", "Oct", "Var"])
    summary.append(["Revenue", 120000, 131000, "=C2-B2"])
    detail = book.create_sheet("Detail")
    detail.append(["Vendor", "Invoice", "Amount"])
    detail.append(["Acme", "INV-88123", 4319.5])
    detail.append(["Northwind", "INV-10482", 12850])
    data = io.BytesIO()
    book.save(data)
    csv = b"date,vendor,amount\n2026-09-01,Acme,4319.50\n2026-09-03,Northwind,12850.00\n"
    path = write_msg(
        settings.inbox_incoming / "maya-close.msg",
        "September close pack",
        "Hi,\n\nAttached are the close workbook, the AP export and a receipt photo.\n\nMaya",
        attachments=[("Close workbook.xlsx", data.getvalue(), XLSX), ("AP export.csv", csv, "text/csv")],
        sent=NOW + timedelta(minutes=10),
    )
    _age(path)
    return path


def test_a_close_pack_workbook_listing_an_invoice_is_not_filed_as_that_invoice(store, settings):
    settings.ensure_data_dir()
    _invoice_eml(settings)
    _close_pack(settings)
    records = {record.subject: record for record in ingest_folder(store, settings, now=NOW)}
    pack = store.get_email(records["September close pack"].id)
    invoice = store.get_email(records["Invoice INV-88123 from Acme Industrial Supply"].id)
    assert pack.category not in {DocumentType.AP_INVOICE, DocumentType.AR_INVOICE}
    assert "INV-88123" not in pack.extracted.invoice_numbers
    assert "duplicate_invoice" not in pack.flags and not any("duplicate" in a.title.lower() for a in pack.actions)
    # The real invoice is not taken for a repeat of the workbook's row either.
    assert store.find_duplicate_invoices("INV-88123", invoice.id) == []
    # The workbook's text is still there to search and ask about.
    workbook = next(att for att in pack.attachments if att.filename == "Close workbook.xlsx")
    assert "INV-88123" in workbook.extracted_text


def test_a_workbook_named_as_an_invoice_is_still_an_invoice(store, settings):
    from controller_inbox.classify import classify_document

    named = classify_document(subject="Files", body="See attached.", filename="Invoice 4410.xlsx",
                              extracted_text="Invoice INV-4410 amount due 1,200.00", content_type=XLSX)
    said = classify_document(subject="Invoice INV-4410", body="Our invoice is attached.", filename="book.xlsx",
                             extracted_text="Invoice INV-4410 amount due 1,200.00", content_type=XLSX)
    assert named.document_type == DocumentType.AP_INVOICE and said.document_type == DocumentType.AP_INVOICE


# 3. Files that can't be read are said so ---------------------------------------------------------------


def _eml_with(settings, name: str, filename: str, data: bytes, mime: str = "application/pdf") -> Path:
    message = EmailMessage()
    message["From"] = "Vendor AP <ap@vendor.example>"
    message["To"] = "ap@horizongoods.example"
    message["Subject"] = name
    message["Date"] = format_datetime(NOW)
    message["Message-ID"] = f"<{name.replace(' ', '-')}@vendor.example>"
    message.set_content("Scan attached.")
    maintype, subtype = mime.split("/")
    message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    path = settings.inbox_incoming / f"{name}.eml"
    path.write_bytes(bytes(message))
    _age(path)
    return path


def test_a_damaged_pdf_keeps_its_email_but_offers_no_page_or_text(store, settings):
    settings.ensure_data_dir()
    _eml_with(settings, "Broken scan", "broken-scan.pdf", b"%PDF-1.4\n garbage garbage")
    [record] = ingest_folder(store, settings, now=NOW)
    client = TestClient(web.create_app(settings, store))
    detail = client.get(f"/api/mail/{record.id}", headers=PAGE).json()
    [card] = detail["attachments"]
    assert card["preview"] is False and card["has_text"] is False
    assert "extraction error" not in card["clip"] and "couldn't be read" in card["clip"]
    assert card["download"] is True
    view = client.get(f"/api/mail/{record.id}/files/1", headers=PAGE).json()
    assert view["file"]["preview"] is False and view["parts"] == []


# The workspace in a real browser ------------------------------------------------------------------------


@pytest.fixture(scope="module")
def browser():
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    from liveserver import chromium_path

    path = chromium_path()
    if path is None:
        pytest.skip("no Chromium for Playwright on this machine")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=path)
        yield browser
        browser.close()


@pytest.fixture
def serve(settings, store):
    """Starts the app on the store as it is when called and gives its address."""
    from contextlib import ExitStack

    from liveserver import serving

    with ExitStack() as stack:
        yield lambda: stack.enter_context(serving(web.create_app(settings, store)))


def _page(browser, *, width=1280, height=900, dark=False):
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme="dark" if dark else "light")
    context.set_default_timeout(15000)
    return context.new_page()


def test_process_new_mail_says_how_many_files_could_not_be_read(browser, serve, settings):
    settings.ensure_data_dir()
    _invoice_eml(settings)
    broken = settings.inbox_incoming / "broken.msg"
    broken.write_bytes(b"this is not an outlook message at all" * 10)
    _age(broken)
    base = serve()
    page = _page(browser)
    page.goto(f"{base}/app")
    page.wait_for_selector("body.ready")
    page.click("#process-btn")
    page.wait_for_selector(".toast:has-text('Processed')", timeout=60000)
    text = page.inner_text(".toast:has-text('Processed')")
    assert "1 file couldn't be read" in text and str(settings.inbox_failed) in text, text
    page.context.close()


# 4. Toolbars fit the reading pane -----------------------------------------------------------------------

OUTSIDE = """() => {
  const pane = document.querySelector('.reader-pane').getBoundingClientRect();
  const out = [];
  for (const el of document.querySelectorAll('.reader-pane .rd-bar > *, .reader-pane .rd-bar .btn, .reader-pane .tabs .tab')) {
    const box = el.getBoundingClientRect();
    if (box.width && (box.right > pane.right + 0.5 || box.left < pane.left - 0.5)) out.push((el.textContent || el.className).trim());
  }
  const scroller = document.querySelector('.reader-pane');
  if (scroller.scrollWidth > scroller.clientWidth + 1) out.push('the pane scrolls sideways');
  if (document.documentElement.scrollWidth > innerWidth + 1) out.push('the page scrolls sideways');
  return out;
}"""


@pytest.mark.parametrize("width,chat", [(390, False), (1280, True)])
def test_the_reader_and_file_toolbars_fit(browser, serve, store, settings, as_of_now, width, chat):
    ingest_demo(store, settings, now=as_of_now)
    base = serve()
    page = _page(browser, width=width)
    page.goto(f"{base}/app")
    page.wait_for_selector("body.ready")
    if chat:
        page.click("#chat-btn")
        page.wait_for_selector("#chat:not([hidden])")
    for path, ready in (
        ("/app/mail/demo-bec-wire?in=all", '.rd[data-email="demo-bec-wire"]'),
        ("/app/mail/demo-payroll/file/1?in=all&tab=tables", ".file-view .tabs"),
    ):
        page.goto(f"{base}{path}")
        page.wait_for_selector(ready)
        page.wait_for_timeout(300)
        assert page.evaluate(OUTSIDE) == [], path
    page.context.close()


# 5. A first start says how to get mail in --------------------------------------------------------------


def test_today_with_no_mail_shows_how_to_get_started(browser, serve, store, settings):
    settings.ensure_data_dir()
    base = serve()
    page = _page(browser)
    page.goto(f"{base}/app")
    page.wait_for_selector(".get-started")
    text = page.inner_text(".get-started")
    assert str(settings.inbox_incoming) in text and "Nothing new since" not in page.inner_text("#reader")
    assert page.is_visible(".get-started button:has-text('Process new mail')")
    page.click(".get-started button:has-text('Load sample mailbox')")
    page.wait_for_function("() => document.querySelector('.kpis')", timeout=30000)
    assert store.counts()["emails"] > 0
    assert not page.query_selector(".get-started")
    page.context.close()


# 11. The top bar at 1280 pixels -------------------------------------------------------------------------

CUT = "sel => { const e = document.querySelector(sel); return e ? e.scrollWidth > e.clientWidth + 1 : 'missing'; }"


def test_the_top_bar_shows_the_date_and_the_progress_whole_at_1280(browser, serve, store, settings, as_of_now):
    import json as _json

    ingest_demo(store, settings, now=as_of_now)
    base = serve()
    page = _page(browser, width=1280)
    page.goto(f"{base}/app")
    page.wait_for_selector("body.ready")
    page.wait_for_timeout(300)
    assert page.evaluate(CUT, ".top-date") is False

    def running(route):
        reply = route.fetch()
        state = reply.json()
        state["job"].update(state="running", stage="rereading", stage_label="Re-reading attachments", done=6, total=6, note="Close workbook.xlsx")
        route.fulfill(response=reply, body=_json.dumps(state))

    page.route("**/api/state", running)
    page.evaluate("dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_selector(".top-job .job")
    page.wait_for_timeout(300)
    assert page.evaluate(CUT, ".top-job .job") is False
    page.context.close()


def test_past_digests_are_listed_in_the_workspace(browser, serve, store, settings):
    for day in ("2026-09-20", "2026-09-21", "2026-09-22"):
        store.save_digest(day, f"{day}T12:00:00+00:00", f"# {day}", "<p></p>", {"date": day, "headline": f"Digest {day}"})
    base = serve()
    page = _page(browser, width=1280)
    page.goto(f"{base}/app/digest?date=2026-09-22")
    page.wait_for_selector(".doc-nav")
    page.click(".doc-nav a:has-text('Past digests')")
    page.click("#past-digests a:has-text('2026-09-20')")
    page.wait_for_url(f"{base}/app/digest?date=2026-09-20")
    page.context.close()


# 6. Dark mode text keeps its contrast -------------------------------------------------------------------

CONTRAST = """sel => {
  const parse = (c) => c.match(/[\\d.]+/g).map(Number);
  const lum = ([r, g, b]) => [r, g, b].map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); })
    .reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i], 0);
  const background = (el) => { for (; el; el = el.parentElement) { const c = parse(getComputedStyle(el).backgroundColor); if (c.length < 4 || c[3] > 0.5) return c; } return [255, 255, 255]; };
  const el = document.querySelector(sel);
  if (!el) return 'missing';
  const [a, b] = [lum(parse(getComputedStyle(el).color)), lum(background(el))];
  return Math.round(((Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)) * 100) / 100;
}"""


@pytest.mark.parametrize("dark", [True, False])
def test_badges_and_buttons_keep_their_contrast_in_dark_mode(browser, serve, store, settings, as_of_now, dark):
    ingest_demo(store, settings, now=as_of_now)
    base = serve()
    page = _page(browser, width=1280, dark=dark)
    checks = {
        "/app/mail/demo-bec-wire?in=fraud": [".brand-mark", ".rd-head .pill.imp-critical", "#rd-fraud .btn-danger", "#rd-fraud .tag-fraud"],
        "/app/mail/demo-approval?in=all": [".rd-head .pill.imp-high"],
        "/app/fraud": [".items .tag-fraud"],
    }
    seen = {}
    for path, selectors in checks.items():
        page.goto(f"{base}{path}")
        page.wait_for_selector(selectors[-1])
        for selector in selectors:
            seen[selector] = page.evaluate(CONTRAST, selector)
    if dark:
        assert all(ratio >= 4.5 for ratio in seen.values()), seen
    else:
        # Light mode is as it was: white on the danger red and on the brand blue.
        assert seen["#rd-fraud .btn-danger"] >= 4.5 and seen[".brand-mark"] >= 4.5, seen
    page.context.close()


# 7. Open in Outlook without a mail app says so plainly -------------------------------------------------


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


# 8. A file dropped on its own --------------------------------------------------------------------------


def test_a_loose_dropped_file_has_no_empty_address_or_made_up_vendor(store, settings):
    settings.ensure_data_dir()
    path = settings.inbox_incoming / "Budget notes.txt"
    path.write_text("Budget vs actual for Q3. Travel is over by $1,200.00.", encoding="utf-8")
    _age(path)
    [record] = ingest_folder(store, settings, now=NOW)
    email = store.get_email(record.id)
    assert "Dropped file" not in email.extracted.vendor_candidates
    client = TestClient(web.create_app(settings, store))
    assert "&lt;&gt;" not in client.get(f"/inbox/{record.id}").text


def test_a_printed_grand_total_is_the_amount_of_a_long_schedule():
    from datetime import date

    from controller_inbox.extract import extract_fields

    rows = "\n".join(f"Vendor {n} | Current: ${1000 + n * 10:,.2f} | Over 90: $0 | Total: ${1000 + n * 10:,.2f}" for n in range(12))
    text = f"Accounts Payable Aging\n{rows}\nVendor: Total | Current: $12,660.00 | Over 90: $500.00 | Total: $13,160.00\n"
    fields = extract_fields(text, as_of=date(2026, 10, 20))
    assert fields.primary_amount == 13160.0
    # A short invoice reads as before.
    invoice = extract_fields("Subtotal $4,075.00\nSales tax $244.50\nTotal Due $4,319.50", as_of=date(2026, 10, 20))
    assert invoice.amounts == [4075.0, 244.5, 4319.5] and invoice.primary_amount == 4319.5


# 9. Task titles don't say a word twice ------------------------------------------------------------------


def test_task_titles_named_after_the_subject_do_not_repeat_the_kind_of_task(store, settings, as_of_now):
    ingest_demo(store, settings, now=as_of_now)
    titles = [item.title for email in store.list_emails(limit=100) for item in email.actions]
    assert titles
    for title in titles:
        words = title.lower().replace(":", " ").split()
        repeats = {word for a, word in zip(words, words[1:]) if a == word}
        assert not repeats, title
    assert any(title.startswith("Code and enter") and "Harbor Packaging" in title for title in titles)
    assert not any("invoice Invoice" in title or "remittance Payment remittance" in title for title in titles)


# 10. The classic pages: an icon, and no sideways scrolling on a phone ----------------------------------


def test_the_classic_pages_have_an_icon(store, settings, as_of_now):
    ingest_demo(store, settings, now=as_of_now)
    client = TestClient(web.create_app(settings, store))
    icon = client.get("/favicon.ico")
    assert icon.status_code == 200 and icon.headers["content-type"].startswith("image/")
    for path in ("/", "/inbox", "/settings", "/coding"):
        assert 'rel="icon"' in client.get(path).text, path


def test_the_classic_pages_fit_a_phone(browser, serve, store, settings, as_of_now):
    ingest_demo(store, settings, now=as_of_now)
    base = serve()
    page = _page(browser, width=390, height=844)
    wide = {}
    for path in ("/", "/inbox", "/coding", "/settings", "/actions", "/attachments"):
        page.goto(f"{base}{path}")
        page.wait_for_selector(".main")
        width = page.evaluate("document.documentElement.scrollWidth")
        if width > 390:
            wide[path] = page.evaluate("[...document.querySelectorAll('.main *')].filter(e => e.getBoundingClientRect().right > 391 && !e.closest('.table-wrap')).map(e => e.tagName + '.' + e.className + ' ' + e.textContent.slice(0, 50))")
    assert wide == {}
    page.context.close()
