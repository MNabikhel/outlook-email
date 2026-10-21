"""The workspace page at /app: it loads at every address the workspace uses, its scripts come from this
computer with a version in their address, the classic pages link to it, and its scripts never put text
from the server into the page as HTML."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from controller_inbox import web
from controller_inbox.pipeline import ingest_demo

UI = Path(web.__file__).parent / "static" / "ui"


def test_the_workspace_page_loads_at_every_address_with_versioned_scripts(client):
    for path in ("/app", "/app/folder/important", "/app/mail/demo-payroll", "/app/mail/demo-payroll/file/1?tab=tables", "/app/digest"):
        page = client.get(path)
        assert page.status_code == 200, path
        assert '<script type="importmap">' in page.text and "/static/ui/main.js?v=" in page.text
        assert page.headers["cache-control"] == "no-store"
    page = client.get("/app").text
    assert '"/static/ui/api.js": "/static/ui/api.js?v=' in page.replace("\\u002f", "/")
    for name in ("main.js", "api.js", "dom.js", "lists.js", "reader.js", "chat.js", "format.js", "pages.js", "settings.js", "palette.js", "theme.js", "app.css"):
        assert client.get(f"/static/ui/{name}").status_code == 200, name
    # Nothing is fetched from outside this computer.
    assert not re.search(r"""(src|href)=["']?(https?:)?//""", page)


def test_the_classic_pages_link_to_the_workspace_and_back(client):
    assert 'href="/app"' in client.get("/").text
    assert 'href="/app"' in client.get("/inbox").text
    script = client.get("/static/ui/main.js").text
    assert 'href: "/"' in script and "Classic view" in script


def test_scripts_never_put_server_text_into_html_unescaped():
    folder = UI
    for path in folder.glob("*.js"):
        text = path.read_text(encoding="utf-8")
        uses = text.count(".innerHTML")
        if path.name == "chat.js":
            # The one use: an answer, escaped first by formatAnswer (see format.js).
            assert uses == 1 and "innerHTML = formatAnswer(" in text
        else:
            assert uses == 0, path.name
    assert "escapeHtml(sentence)" in (folder / "format.js").read_text(encoding="utf-8")


def test_a_file_name_with_a_bracketed_number_stays_one_link(tmp_path):
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js isn't installed")
    sources = [{"n": 1, "id": "e1", "subject": "Remittance", "files": [{"n": 1, "name": "Invoice [1].pdf", "text": True}]}]
    script = tmp_path / "answer.mjs"
    script.write_text(
        f"import {{ formatAnswer }} from {json.dumps((UI / 'format.js').as_uri())};\n"
        f"console.log(formatAnswer('See Invoice [1].pdf for the total [1].', {json.dumps(sources)}));\n"
    )
    html = subprocess.run([node, str(script)], capture_output=True, text=True, check=True).stdout
    assert html.count("<a ") == 2 and "<a" not in html.split("Invoice [1].pdf</a>")[0].split("<a ", 2)[-1].split(">", 1)[1]
    assert 'title="Open Invoice [1].pdf">Invoice [1].pdf</a> for the total <a class="cite"' in html


def test_a_cell_cited_in_a_csv_links_to_the_cell(settings, store, tmp_path):
    # A CSV's one sheet is named after the file, blanked out of the sentence: the link asks for the cell alone.
    import json
    import shutil
    import subprocess
    from urllib.parse import parse_qs, urlparse

    from controller_inbox.folder_mail import ingest_folder
    from msgfactory import write_msg

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js isn't installed")
    settings.trusted_domains = "taz.com"
    settings.ensure_data_dir()
    write_msg(settings.inbox_incoming / "q4.msg", "Q4 numbers", "Attached.", sender_name="Maya", sender_email="maya@taz.com",
              attachments=[("q4.csv", b"Line,Q3,Q4\nAds,1000,1500\nTravel,250,300\nTotal,1250,1800\n", "text/csv")])
    [email] = ingest_folder(store, settings)
    source = {"n": 1, "id": email.id, "subject": email.subject, "files": [{"n": 1, "name": "q4.csv", "text": True}]}
    script = tmp_path / "href.mjs"
    script.write_text(
        f"import {{ fileHref }} from {json.dumps((UI / 'format.js').as_uri())};\n"
        f"const [source, file] = [{json.dumps(source)}, {json.dumps(source['files'][0])}];\n"
        "console.log(fileHref(source, file, 'Travel in sheet \"q4.csv\", cell C3 is 300 [1].'));\n"
        "console.log(fileHref(source, file, \"Travel in 'q4.csv'!C3 is 300 [1].\"));\n"
    )
    hrefs = subprocess.run([node, str(script)], capture_output=True, text=True, check=True).stdout.split()
    assert len(hrefs) == 2
    client = TestClient(web.create_app(settings, store))
    for href in hrefs:
        at = parse_qs(urlparse(href).query)["at"][0]
        assert at == "C3", href
        assert "file-part target" in client.get(f"/inbox/{email.id}/files/1", params={"at": at}).text, href


# ---------- The workspace's own scripts in a real browser, one test per fixed bug ----------


def _draft(page, words: str) -> str:
    """Opens the reply draft on the email shown, adds words to it and gives what it then says."""
    page.click("button:has-text('Draft reply')")
    page.wait_for_function("() => { const t = document.querySelector('.draft-text'); return t && !t.placeholder; }")
    page.click(".draft-text")
    page.keyboard.press("End")
    page.keyboard.type(words)
    return page.input_value(".draft-text")


def test_next_leaves_an_email_with_two_tasks_in_the_task_list(site, page):
    # Next looked for the email's first row, so on an email with two tasks in a row it came back to itself.
    page.goto(f"{site}/app/tasks")
    page.wait_for_selector(".items li")
    rows = page.eval_on_selector_all(".items li a.item-main", "els => els.map(e => new URL(e.href).pathname)")
    twice = next(i for i in range(len(rows) - 1) if rows[i] == rows[i + 1])
    page.click(f".items li:nth-child({twice + 1}) a.item-main")
    email = rows[twice].rsplit("/", 1)[-1]
    page.wait_for_selector(f'.rd[data-email="{email}"]')
    page.click('button[aria-label="Next email"]')
    page.wait_for_function("id => document.querySelector('.rd') && document.querySelector('.rd').dataset.email !== id", arg=email)
    assert page.get_attribute(".rd", "data-email") == rows[twice + 2].rsplit("/", 1)[-1]


def test_the_search_box_shows_the_filter_still_on_after_settings(site, page):
    # Back from Settings, the list was still filtered but its search box came back empty.
    page.goto(f"{site}/app/all")
    page.wait_for_selector(".items li")
    page.fill(".list-search", "payroll")
    filtered = page.text_content(".list-count")
    page.click("body")
    page.keyboard.press("g")
    page.keyboard.press("s")
    page.wait_for_selector(".page-title")
    page.keyboard.press("g")
    page.keyboard.press("a")
    page.wait_for_selector(".list-search")
    page.wait_for_function("n => document.querySelector('.list-count').textContent === n", arg=filtered)
    assert page.input_value(".list-search") == "payroll"


def test_escape_in_the_reply_draft_keeps_the_email_and_the_draft(site, page):
    # Esc closed the email (and the draft with it) even while typing in one of the reading pane's fields.
    page.goto(f"{site}/app/mail/demo-inv-10482?in=all")
    page.wait_for_selector('.rd[data-email="demo-inv-10482"]')
    typed = _draft(page, " Thanks, I will pay on Friday.")
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    assert "/app/mail/demo-inv-10482" in page.url
    assert page.input_value(".draft-text") == typed
    assert not page.evaluate("document.activeElement.classList.contains('draft-text')"), "Esc leaves the field"
    page.keyboard.press("Escape")
    page.wait_for_url(f"{site}/app/all")


def test_escape_after_ticking_a_checkbox_closes_the_email(site, page):
    # Any input counted as typing, so Esc after ticking a cost-code checkbox only took focus off it.
    page.goto(f"{site}/app/mail/demo-inv-10482?in=all")
    page.wait_for_selector('.rd[data-email="demo-inv-10482"]')
    page.evaluate("""() => {
        const box = Object.assign(document.createElement('input'), {type: 'checkbox', id: 'esc-box'});
        document.querySelector('.rd').append(box);
    }""")
    page.click("#esc-box")
    page.keyboard.press("Escape")
    page.wait_for_url(f"{site}/app/all")


def test_ticking_a_task_keeps_the_reply_draft(site, page):
    # Ticking a task redrew the whole email and dropped the reply being written.
    page.goto(f"{site}/app/mail/demo-pbc?in=all")
    page.wait_for_selector('.rd[data-email="demo-pbc"]')
    typed = _draft(page, "Hi, attaching the August bank recs now. ")
    page.click(".rd-tasks .task-check >> nth=0")
    page.wait_for_selector('.rd-tasks .task-check[aria-checked="true"]')
    assert page.input_value(".draft-text") == typed


def test_a_late_digest_answer_leaves_the_email_opened_since(site, page):
    # A digest that answered (here: failed) after you'd moved on replaced the email you were reading.
    page.add_init_script(
        """(() => {
          const real = window.fetch;
          window.fetch = (url, options) => /\\/api\\/digest\\?date=1999-01-01/.test(String(url))
            ? new Promise((done) => setTimeout(done, 800)).then(() => real(url, options))
            : real(url, options);
        })()"""
    )
    page.goto(f"{site}/app/folder/important")
    page.wait_for_selector(".items li")
    go = "url => { history.pushState({}, '', url); dispatchEvent(new PopStateEvent('popstate')); }"
    page.evaluate(go, "/app/digest?date=1999-01-01")
    page.evaluate(go, "/app/mail/demo-pbc?in=folder:important")
    page.wait_for_selector('.rd[data-email="demo-pbc"]')
    page.wait_for_timeout(1500)
    assert page.query_selector('.rd[data-email="demo-pbc"]')


def test_ask_closedesk_reopened_on_load_says_so_on_its_button(site, page):
    # The panel came back open after a reload while its button said it was closed.
    page.goto(f"{site}/app")
    page.wait_for_selector("body.ready")
    page.click("#chat-btn")
    assert page.get_attribute("#chat-btn", "aria-expanded") == "true"
    page.reload()
    page.wait_for_selector("body.ready")
    assert page.eval_on_selector("#chat", "e => e.hidden") is False
    assert page.get_attribute("#chat-btn", "aria-expanded") == "true"
    assert page.eval_on_selector("#chat-btn", "e => e.classList.contains('on')")



def test_settings_save_each_change_and_keep_one_not_saved_yet(site, page, settings, loaded):
    # The Setup page's settings work in the workspace: each Save says what happened under it, and drawing the
    # page again after one change leaves another section's change that isn't saved yet as it was.
    page.goto(f"{site}/app/settings")
    page.wait_for_selector("#set-timezone select")
    page.select_option("#set-tz", "Europe/London")
    assert "London is UTC+" in page.text_content("#set-timezone .set-preview")
    page.fill("#set-context-slider", "4")
    assert "32,768 tokens" in page.text_content("#set-context label")
    page.click("#set-timezone button[type=submit]")
    page.wait_for_selector("#set-timezone .set-status.ok")
    assert settings.timezone == "Europe/London" and page.evaluate("document.body.dataset.tz") == "Europe/London"
    assert page.input_value("#set-context-slider") == "4", "the slider not saved yet kept its place"
    page.click("#set-context button[type=submit]")
    page.wait_for_selector("#set-context .set-status.ok")
    assert settings.min_context_tokens == 32768
    page.check("#set-profile input[value=finance]")
    page.click("#set-profile button[type=submit]")
    page.wait_for_selector("#set-profile .set-status.ok")
    assert loaded.get_state("profile") == "finance"
    page.check("#set-vision input[value=off]")
    page.click("#set-vision button[type=submit]")
    page.wait_for_selector("#set-vision .set-status.ok")
    assert settings.vision_mode == "off"
    # A change the server refuses says why under it, and stays as it was chosen.
    page.check("#set-profile input[value=general]")
    page.evaluate("document.querySelector('#set-profile input[value=general]').value = 'sales'")
    page.click("#set-profile button[type=submit]")
    page.wait_for_selector("#set-profile .set-status.error")
    assert "General or Finance" in page.text_content("#set-profile .set-status") and page.is_checked("#set-profile input[value=sales]")
    assert loaded.get_state("profile") == "finance"
    # Nothing on the page links back to the classic Setup page for a setting.
    assert page.locator('#reader a[href^="/settings"]').count() == 0


def test_the_open_mail_list_shows_a_new_time_zone_saved_in_settings(site, page, settings):
    # Saving a time zone asks again for the list left open behind Settings, so going back to it doesn't show
    # its times in the old zone.
    first_time = "() => { const el = document.querySelector('.item-when[title]'); return el && el.title; }"
    page.goto(f"{site}/app/all")
    page.wait_for_function(first_time)
    before = page.evaluate(first_time)
    page.click('a[href="/app/settings"]')
    page.wait_for_selector("#set-tz")
    page.select_option("#set-tz", "Asia/Tokyo")
    page.click("#set-timezone button[type=submit]")
    page.wait_for_selector('[data-status="timezone"].ok')
    assert settings.timezone == "Asia/Tokyo"
    page.click('a[href="/app/all"]')
    page.wait_for_function(f"() => ({first_time})() !== {json.dumps(before)}")
    fresh = page.context.new_page()
    fresh.goto(f"{site}/app/all")
    fresh.wait_for_function(first_time)
    assert page.evaluate(first_time) == fresh.evaluate(first_time)


def test_check_again_comes_back_with_a_chat_model_chosen_and_not_loaded_yet(site, page):
    # Picking another chat model without loading it leaves that choice as it is, but Check again still
    # finishes: its button comes back and the section says it checked.
    def with_chat_models(route):
        response = route.fetch()
        data = response.json()
        data["model"].update(mode="auto", active=True, reachable=True, describe="LM Studio: qwen", loaded=["qwen"], server="LM Studio")
        data["chat"] = {"pinned": False, "env_model": "", "current": "qwen", "models": [{"key": "qwen", "loaded": True}, {"key": "gemma", "loaded": False}], "min_context_tokens": 16384}
        route.fulfill(response=response, body=json.dumps(data), headers={**response.headers, "content-type": "application/json"})

    page.route("**/api/settings*", lambda route: with_chat_models(route) if route.request.method == "GET" else route.continue_())
    page.goto(f"{site}/app/settings")
    page.wait_for_selector("#set-chat")
    page.select_option("#set-chat", "gemma")
    button = page.locator("#set-model .set-banner-row button")
    button.click()
    page.wait_for_selector('#set-model [data-status="model"].ok')
    assert button.is_enabled() and "Check again" in button.inner_text()
    assert page.input_value("#set-chat") == "gemma", "the choice not loaded yet kept its place"


def test_the_page_tab_marks_where_text_was_read_and_says_what_on_hover(settings, store, page):
    # A PDF opens on its page, a box over each piece of text read there; pointing at one says what was read.
    from controller_inbox.folder_mail import ingest_folder
    from liveserver import serving
    from msgfactory import PDF, write_msg
    from test_page_view import _invoice

    settings.trusted_domains = "taz.com"
    settings.ensure_data_dir()
    write_msg(settings.inbox_incoming / "invoice.msg", "Invoice INV-1042", "Our invoice is attached.", sender_name="Harbor Steel",
              sender_email="ar@taz.com", attachments=[("invoice.pdf", _invoice(pages=2), PDF)])
    [email] = ingest_folder(store, settings)
    with serving(web.create_app(settings, store)) as base:
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto(f"{base}/app/mail/{email.id}/file/1?tab=page")
        page.wait_for_selector(".tab.on:has-text('Page')")
        page.wait_for_selector(".pv-sheet img")
        page.wait_for_function("() => document.querySelector('.pv-sheet img').naturalWidth > 0")
        assert page.text_content(".pv-where") == "Page 1 of 2"
        assert "Read from the file's own text (exact)" in page.text_content(".pv-about")
        total = page.locator(".pv-box[aria-label='1,560.50']")
        assert page.locator(".pv-box").count() >= 10
        total.hover()
        page.wait_for_selector(".pv-tip:not([hidden])")
        card = page.text_content(".pv-tip")
        assert "From the file's own text" in card and "exact" in card and "1,560.50" in card
        box = page.evaluate("() => { const r = document.querySelector('.pv-tip').getBoundingClientRect(); return [r.left, r.right, r.top, r.bottom]; }")
        assert box[0] >= 0 and box[1] <= 1440 and box[2] >= 0 and box[3] <= 900, "the card stays in the window"
        page.click("button[aria-label='Next page']")
        page.wait_for_function("() => document.querySelector('.pv-where').textContent === 'Page 2 of 2'")
        page.wait_for_selector(".pv-box[aria-label='Terms and conditions, page 2']")


def _harbor(settings, store, *attachments):
    from controller_inbox.folder_mail import ingest_folder
    from msgfactory import write_msg

    settings.trusted_domains = "taz.com"
    settings.ensure_data_dir()
    write_msg(settings.inbox_incoming / "harbor.msg", "Invoice HS-10482", "Our invoice is attached.", sender_name="Harbor Steel",
              sender_email="ar@taz.com", attachments=list(attachments))
    [email] = ingest_folder(store, settings)
    return email


def test_a_file_opens_on_its_preview_and_the_email_shows_a_picture_of_it(page, store, settings):
    from controller_inbox.folder_mail import ingest_folder
    from liveserver import serving
    from msgfactory import PDF, write_msg
    from test_page_view import _invoice

    settings.trusted_domains = "taz.com"
    settings.ensure_data_dir()
    write_msg(settings.inbox_incoming / "invoice.msg", "Invoice INV-1042", "Our invoice is attached.", sender_name="Harbor Steel",
              sender_email="ar@taz.com", attachments=[("invoice.pdf", _invoice(pages=2), PDF)])
    [email] = ingest_folder(store, settings)
    with serving(web.create_app(settings, store)) as base:
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto(f"{base}/app/mail/{email.id}")
        page.wait_for_function("() => { const img = document.querySelector('.file-thumb img'); return img && img.naturalWidth > 0; }")
        page.click(".file-thumb")
        page.wait_for_selector(".tab.on:has-text('Preview')")
        frame = page.wait_for_selector("iframe.pvw-pdf")
        assert frame.get_attribute("src").endswith("/files/1/view#view=FitH")
        page.click(".tab:has-text('Page')")
        page.wait_for_selector(".pv-teach-pick")


def _open_page_tab(page, base, email):
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base}/app/mail/{email.id}/file/1?tab=page")
    page.wait_for_selector(".tab.on:has-text('Page')")
    page.wait_for_function("() => { const i = document.querySelector('.pv-sheet img'); return i && i.naturalWidth > 0; }")


def test_the_page_tab_shows_tables_and_key_details_where_they_are_printed(settings, store, page):
    from liveserver import serving
    from msgfactory import PDF
    from test_page_view import _detailed_invoice

    email = _harbor(settings, store, ("HS-10482.pdf", _detailed_invoice(), PDF))
    with serving(web.create_app(settings, store)) as base:
        _open_page_tab(page, base, email)
        # Every box is the file's own text: nothing to check.
        assert page.is_disabled(".pv-next") and page.text_content(".pv-next") == "Nothing to check"
        # Key details, found on the page.
        assert page.text_content(".pv-field[data-label='Invoice no.'] .pv-field-text") == "HS-10482"
        page.click(".pv-field[data-label='Total due']")
        total = page.locator(".pv-box[aria-label='$6,327.59']")
        assert "pv-flash" in total.get_attribute("class") and "ok" in total.get_attribute("class")
        # A table's outline opens it as a grid; pointing at a cell marks its box on the page.
        outline = page.locator(".pv-tbl:has(.pv-tbl-tag:text-is('Line items'))")
        outline.locator(".pv-tbl-tag").click()
        page.wait_for_selector(".pv-panel:not([hidden]) .pv-grid")
        assert page.text_content(".pv-panel-head b") == "Line items"
        assert page.get_attribute(".pv-chip[aria-pressed='true']", "data-table") == outline.get_attribute("data-table")
        cell = page.locator(".pv-grid td:text-is('1,874.40')")
        cell.hover()
        region = cell.get_attribute("data-region")
        box = page.locator(f".pv-box[data-region='{region}']")
        assert box.get_attribute("aria-label") == "1,874.40" and "pv-hl" in box.get_attribute("class")
        # And pointing at the box marks its cell.
        page.mouse.move(5, 5)
        assert "pv-hl" not in box.get_attribute("class")
        page.locator(".pv-box[aria-label='412.50']").hover()
        assert "pv-hl" in page.get_attribute(".pv-grid td:text-is('412.50')", "class")
        # Esc closes the table and stays on the page.
        page.keyboard.press("Escape")
        page.wait_for_selector(".pv-panel", state="hidden")
        assert "/file/1" in page.url and page.locator(".pv-box").count() > 20
        # The size it is drawn at is kept, and the boxes stay on their words.
        page.click(".pv-zoom .seg:text-is('150%')")
        width = page.evaluate("() => document.querySelector('.pv-sheet').getBoundingClientRect().width")
        assert width > 1100
        sheet = page.evaluate("() => document.querySelector('.pv-sheet').getBoundingClientRect().left")
        left = page.evaluate("() => document.querySelector(\".pv-box[aria-label='$6,327.59']\").getBoundingClientRect().left")
        assert 0.8 < (left - sheet) / width < 0.85
        page.reload()
        page.wait_for_function("() => { const i = document.querySelector('.pv-sheet img'); return i && i.naturalWidth > 0; }")
        assert page.get_attribute(".pv-zoom .seg[aria-pressed='true']", "data-zoom") == "150"


def test_next_to_check_steps_through_a_scans_amber_and_red_boxes(settings, store, page, monkeypatch):
    from controller_inbox import ocr, page_view, vision
    from liveserver import serving
    from test_page_view import LINES, OVIS, _detailed_invoice

    pdf = _detailed_invoice()
    scan = vision.render(pdf, "HS-10482.pdf", 1, reader=page_view.VIEW)
    width, height = page_view.png_size(scan)
    # OCR as RapidOCR gives it, from where the words are printed: unsure of one description.
    lines = [
        {"left": r["x"] * width, "top": r["y"] * height, "right": (r["x"] + r["w"]) * width, "bottom": (r["y"] + r["h"]) * height,
         "text": r["text"], "score": 0.62 if r["text"].startswith("Anchor bolts") else 0.97}
        for r in page_view._text_layer(pdf, 1)
    ]
    monkeypatch.setattr(ocr, "image_text", lambda _data: "")
    monkeypatch.setattr(ocr, "line_boxes", lambda _png: lines)
    email = _harbor(settings, store, ("HS-10482 signed.png", scan, "image/png"))
    att = email.attachments[0]
    rows = "".join(f"<tr><td>{d}</td><td>{q}</td><td>{p:,.2f}</td><td>{'1,847.40' if q == 6 else f'{q * p:,.2f}'}</td></tr>" for d, q, p in LINES)
    # The vision model read the page the same, but for the plate's amount.
    reading = (
        "# Harbor Steel LLC\n88 Dockside Way, Tacoma WA 98421\n\nInvoice no.: HS-10482\nInvoice date: 09/28/2026\nDue date: 10/28/2026\n"
        f"PO number: 4500-1163\n\n<table><tr><td>Description</td><td>Qty</td><td>Unit price</td><td>Amount</td></tr>{rows}"
        "<tr><td>Subtotal</td><td></td><td></td><td>$5,941.40</td></tr><tr><td>Sales tax 6.5%</td><td></td><td></td><td>$386.19</td></tr>"
        "<tr><td>Total due</td><td></td><td></td><td>$6,327.59</td></tr></table>"
    )
    store.save_page_reading(att.id, 1, first="", model_text=reading, model=OVIS, seconds=30, comparison="{}", sha256=att.sha256)
    with serving(web.create_app(settings, store)) as base:
        _open_page_tab(page, base, email)
        assert page.locator(".pv-box.differs").count() == 1
        to_check = page.locator(".pv-box.check, .pv-box.differs").count()
        assert to_check >= 2
        assert page.text_content(".pv-count-check") == f"{to_check} boxes to check"
        seen = []
        for step in range(1, 3):
            page.click(".pv-next")
            page.wait_for_function(f"() => document.querySelector('.pv-count-check').textContent === '{step} of {to_check} to check'")
            page.wait_for_selector(".pv-tip:not([hidden])")
            flashed = page.locator(".pv-box.pv-flash")
            assert flashed.count() == 1
            seen.append(page.evaluate("() => parseFloat(document.querySelector('.pv-box.pv-flash').style.top)"))
        # In reading order: down the page.
        assert seen[0] <= seen[1]
        # The red box's card says the two read it differently.
        while "1,874.40" not in page.text_content(".pv-tip"):
            page.click(".pv-next")
            page.wait_for_timeout(150)
        assert "differs" in page.text_content(".pv-tip") and "1,847.40" in page.text_content(".pv-tip")
        # Only what needs checking: the green boxes hide.
        page.check(".pv-only input")
        assert page.locator(".pv-box.ok").first.is_hidden() and page.locator(".pv-box.differs").is_visible()


def test_enter_on_a_page_box_opens_its_fix_card_and_stays_on_the_file(settings, store, page):
    # The boxes are role=button: Enter is theirs, not the page-wide "open the selected email".
    from liveserver import serving
    from msgfactory import PDF
    from test_page_view import _invoice

    email = _harbor(settings, store, ("invoice.pdf", _invoice(pages=2), PDF))
    with serving(web.create_app(settings, store)) as base:
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto(f"{base}/app/folder/{email.folder}")
        page.click(f".item[data-key='{email.id}'] .item-main")
        page.click(".rd .file-actions a:has-text('Page')")
        page.wait_for_function("() => { const i = document.querySelector('.pv-sheet img'); return i && i.naturalWidth > 0; }")
        page.focus(".pv-box[aria-label='Amount']")
        page.keyboard.press("Enter")
        page.wait_for_selector(".pv-fix-input")
        page.wait_for_timeout(300)
        assert "/file/1" in page.url, page.url
        assert page.locator(".pv-fix-input").count() == 1


def test_a_fix_on_the_page_tab_shows_on_the_text_tab_and_its_undo_too(settings, store, page):
    # The file's reading was kept from before the fix; a fix (and its undo) fetch it again.
    from liveserver import serving
    from msgfactory import PDF
    from test_page_view import _invoice

    email = _harbor(settings, store, ("invoice.pdf", _invoice(pages=2), PDF))
    with serving(web.create_app(settings, store)) as base:
        _open_page_tab(page, base, email)
        page.click(".pv-box[aria-label='Amount']")
        page.fill(".pv-fix-input", "ZEBRAWORD")
        page.click(".pv-fix button[type=submit]")
        page.wait_for_selector(".pv-box[aria-label='ZEBRAWORD']")
        page.click(".tab:has-text('Text')")
        page.wait_for_selector(".text-view .parts")
        assert "ZEBRAWORD" in page.text_content(".text-view .parts")
        # Undone from its note while on the Text tab: the tab fetches the reading again when next shown.
        with page.expect_response(lambda response: response.url.endswith("/undo")):
            page.click(".toast-action:has-text('Undo')")
        page.click(".tab:has-text('Page')")
        page.wait_for_selector(".pv-box[aria-label='Amount']")
        page.click(".tab:has-text('Text')")
        page.wait_for_selector(".text-view .parts")
        assert "ZEBRAWORD" not in page.text_content(".text-view .parts")


def test_slash_on_the_digest_opens_an_empty_palette(site, page):
    page.goto(f"{site}/app/digest")
    page.wait_for_selector("body.ready")
    page.wait_for_selector(".doc-nav")
    page.keyboard.press("/")
    page.wait_for_selector(".palette input")
    assert page.input_value(".palette input") == ""


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


# The workspace in a real browser ------------------------------------------------------------------------
@pytest.fixture
def serve(settings, store):
    """Starts the app on the store as it is when called and gives its address."""
    from contextlib import ExitStack

    from liveserver import serving

    with ExitStack() as stack:
        yield lambda: stack.enter_context(serving(web.create_app(settings, store)))


_OPEN: list = []


@pytest.fixture(autouse=True)
def _close_pages():
    """Every tab a test opened is closed, even when the test fails: a tab left open keeps its routes and timers
    running against a server that has stopped, and breaks the next test."""
    yield
    while _OPEN:
        try:
            _OPEN.pop().close()
        except Exception:
            pass


def _page(browser, *, width=1280, height=900, dark=False):
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme="dark" if dark else "light")
    context.set_default_timeout(15000)
    _OPEN.append(context)
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


# Toolbars fit the reading pane -----------------------------------------------------------------------
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


# A first start says how to get mail in --------------------------------------------------------------
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


# The top bar at 1280 pixels -------------------------------------------------------------------------
CUT = "sel => { const e = document.querySelector(sel); return e ? e.scrollWidth > e.clientWidth + 1 : 'missing'; }"


def test_the_top_bar_shows_the_date_and_the_progress_whole_at_1280(browser, serve, store, settings, as_of_now):
    import json as _json

    ingest_demo(store, settings, now=as_of_now)
    base = serve()
    page = _page(browser, width=1280)
    page.goto(f"{base}/app")
    page.wait_for_selector("body.ready")
    # A wide fallback font, as on a computer without the system font the page asks for (CI's Linux has DejaVu).
    page.add_style_tag(content="* { font-family: 'DejaVu Sans', sans-serif !important; }")
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
    # What is cut short is the note, with the whole line on hover; the stage and the count are always shown.
    assert page.evaluate(CUT, ".top-job .job-text b") is False
    assert "Close workbook.xlsx" in page.get_attribute(".top-job .job-text", "title")
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


# Dark mode text keeps its contrast -------------------------------------------------------------------
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


def test_coming_back_to_the_tab_while_a_status_check_is_out_keeps_one_poll_loop(site, page):
    """A visibilitychange during an in-flight status poll neither sends a second request nor starts a second loop."""
    page.clock.install()
    page.goto(f"{site}/app")
    page.wait_for_selector("body.ready")
    held = []
    page.route("**/api/state", lambda route: held.append(route))
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_timeout(300)
    assert len(held) == 1
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_timeout(300)
    assert len(held) == 1, "a second poll went out while the first was still on its way"
    for route in held:
        route.continue_()
    page.unroute("**/api/state")
    sent = []
    page.on("request", lambda request: sent.append(request.url) if request.url.endswith("/api/state") else None)
    page.wait_for_timeout(300)
    page.clock.run_for(16000)
    page.wait_for_timeout(300)
    assert len(sent) == 1, f"one poll loop runs, not {len(sent)}"


def test_open_in_outlook_shows_the_servers_refusal_when_it_is_not_json(site, page):
    """A non-JSON error from the open-original endpoint shows as an error, not a silent download."""
    asked = []
    page.on("request", lambda request: asked.append(request.url))
    page.route("**/inbox/*/open", lambda route: route.fulfill(status=500, body="Internal Server Error", content_type="text/plain"))
    page.goto(f"{site}/app/mail/demo-payroll")
    page.wait_for_selector('.rd[data-email="demo-payroll"]')
    page.click("button:has-text('Open in Outlook')")
    page.wait_for_selector(".toast.error")
    assert "500" in page.inner_text(".toast.error")
    page.wait_for_timeout(300)
    assert not [url for url in asked if url.endswith("/original")]


def test_open_in_outlook_shows_the_servers_message_on_a_refusal(site, page):
    """A JSON refusal from the open-original endpoint shows the server's own words."""
    page.route("**/inbox/*/open", lambda route: route.fulfill(status=403, body='{"detail": "Files open on the computer running CloseDesk only."}', content_type="application/json"))
    page.goto(f"{site}/app/mail/demo-payroll")
    page.wait_for_selector('.rd[data-email="demo-payroll"]')
    page.click("button:has-text('Open in Outlook')")
    page.wait_for_selector(".toast.error")
    assert "computer running CloseDesk only" in page.inner_text(".toast.error")


def test_workspace_scripts_link_to_the_workspace_settings_not_the_classic_page():
    """No workspace script links to the classic /settings page, which has a workspace equivalent."""
    found = [
        f"{path.name}:{n}"
        for path in sorted(UI.glob("*.js"))
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"""["'`]/settings\b""", line)
    ]
    assert found == []
