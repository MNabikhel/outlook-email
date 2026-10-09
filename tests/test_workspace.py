"""The workspace page at /app: it loads at every address the workspace uses, its scripts come from this
computer with a version in their address, the classic pages link to it, and its scripts never put text
from the server into the page as HTML."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from controller_inbox import web
from controller_inbox.pipeline import ingest_demo

UI = Path(web.__file__).parent / "static" / "ui"


@pytest.fixture
def client(store, settings, as_of_now):
    ingest_demo(store, settings, now=as_of_now)
    return TestClient(web.create_app(settings, store))


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
def site(settings, loaded):
    from liveserver import serving

    with serving(web.create_app(settings, loaded)) as base:
        yield base


@pytest.fixture
def page(browser):
    context = browser.new_context()
    context.set_default_timeout(15000)
    context.errors = []
    context.on("weberror", lambda error: context.errors.append(str(error.error)))
    yield context.new_page()
    assert context.errors == []
    context.close()


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
        page.goto(f"{base}/app/mail/{email.id}/file/1")
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


def _open_page_tab(page, base, email):
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base}/app/mail/{email.id}/file/1")
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
