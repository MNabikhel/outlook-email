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
    for name in ("main.js", "api.js", "dom.js", "lists.js", "reader.js", "chat.js", "format.js", "pages.js", "palette.js", "theme.js", "app.css"):
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
