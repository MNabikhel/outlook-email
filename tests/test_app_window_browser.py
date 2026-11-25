"""What leaves CloseDesk from its window (static/ui/in_window.js), in both UIs, in a real Chromium standing in for the
window: its bridge (window.pywebview.api) is there as pywebview gives it, and what the pages ask it to open outside is
kept. A mail address opens the mail app, another website and a page meant for a new tab open in the default browser,
and a download stays with the window, which saves it. In an ordinary browser tab, none of it changes anything.

The real window (WebView2, WebKit) opens these pages in CI on Windows and macOS."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

# What pywebview puts in every page it shows, standing in: the window's bridge, whose calls give a Promise.
BRIDGE = """
window.pywebview = { api: {
  open_outside: (url) => window.closedeskOpened(url).then(() => true),
  page_ready: (path) => window.closedeskReady(path),
} };
"""
# pywebview adds its bridge once the page has loaded, then says so with "pywebviewready".
LATE_BRIDGE = """
window.addEventListener("load", () => setTimeout(() => {
  %s
  window.dispatchEvent(new Event("pywebviewready"));
}, 50));
""" % BRIDGE


def _context(browser, script: str | None):
    context = browser.new_context(accept_downloads=True)
    context.set_default_timeout(15000)
    context.errors = []
    context.on("weberror", lambda error: context.errors.append(str(error.error)))
    context.opened, context.ready = [], []
    context.expose_function("closedeskOpened", lambda url: context.opened.append(url))
    context.expose_function("closedeskReady", lambda path: context.ready.append(path))
    if script:
        context.add_init_script(script)
    return context


@pytest.fixture
def window(browser):
    """A tab that is the CloseDesk window as far as the pages can tell."""
    context = _context(browser, BRIDGE)
    page = context.new_page()
    yield page
    assert context.errors == []
    context.close()


@pytest.fixture
def tab(browser):
    """An ordinary browser tab, with nothing of the window in it."""
    context = _context(browser, None)
    page = context.new_page()
    yield page
    assert context.errors == []
    context.close()


def _with_a_digest(page, site: str) -> None:
    """Today's digest saved, as Rebuild saves it: its printable page is what is printed."""
    reply = page.request.post(f"{site}/digest/rebuild", max_redirects=0)
    assert reply.status == 303, reply.status


def _opened(page) -> list[str]:
    return page.context.opened


def _wait_opened(page, count: int = 1) -> list[str]:
    for _ in range(100):
        if len(_opened(page)) >= count:
            break
        page.wait_for_timeout(50)
    return _opened(page)


# A mail address opens the mail app -----------------------------------------------------------------------------------


def test_open_in_mail_app_opens_the_mail_app_from_the_workspace(site, window):
    window.goto(f"{site}/app/mail/demo-payroll")
    window.click("button:has-text('Draft reply')")
    link = window.locator(".draft a:has-text('Open in mail app')")
    link.wait_for(state="visible")
    assert link.get_attribute("href").startswith("mailto:")
    window.click(".draft a:has-text('Open in mail app')")
    (opened,) = _wait_opened(window)
    assert opened.startswith("mailto:") and "subject=" in opened
    assert window.url == f"{site}/app/mail/demo-payroll", "the window stays on CloseDesk"


def test_open_in_mail_app_opens_the_mail_app_from_a_classic_page(site, window):
    window.goto(f"{site}/inbox/demo-payroll")
    window.click(".mail-tools button[data-action='draft']")
    link = window.locator(".draft-box a.draft-mailto")
    link.wait_for(state="visible")
    window.wait_for_function("() => document.querySelector('.draft-box a.draft-mailto').href.startsWith('mailto:')")
    link.click()
    (opened,) = _wait_opened(window)
    assert opened.startswith("mailto:")
    assert window.url == f"{site}/inbox/demo-payroll"


# Another website, or a page meant for a new tab, opens in the default browser -------------------------------------------


def test_the_printable_digest_opens_in_the_browser_from_the_workspace(site, window):
    _with_a_digest(window, site)
    window.goto(f"{site}/app/digest")
    printable = window.locator("a:has-text('Printable page')")
    printable.wait_for()
    href = printable.get_attribute("href")
    printable.click()
    (opened,) = _wait_opened(window)
    assert opened == f"{site}{href}" and "/digest/" in opened, "the browser prints it, as from a browser tab"
    assert len(window.context.pages) == 1 and window.url == f"{site}/app/digest"


def test_the_printable_digest_opens_in_the_browser_from_a_classic_page(site, window):
    _with_a_digest(window, site)
    window.goto(f"{site}/digest")
    printable = window.locator("a:has-text('Printable page')")
    href = printable.get_attribute("href")
    printable.click()
    assert _wait_opened(window) == [f"{site}{href}"]
    assert len(window.context.pages) == 1


def test_another_website_opens_in_the_browser_not_in_the_window(site, window):
    # The window has no back button: a website opened in it would leave CloseDesk behind for good.
    window.goto(f"{site}/inbox")
    window.evaluate(
        "() => { const a = document.createElement('a'); a.href = 'https://lmstudio.ai/'; a.id = 'away'; a.textContent = 'LM Studio';"
        " document.body.prepend(a); }"
    )
    window.click("#away")
    assert _wait_opened(window) == ["https://lmstudio.ai/"]
    assert window.url == f"{site}/inbox"


def test_the_chat_in_its_own_window_opens_in_the_browser(site, window):
    # A classic page's chat pop-out (window.open): the window opens it in the browser, and says nothing was blocked.
    window.goto(f"{site}/inbox")
    window.evaluate("() => document.querySelector(\"[data-action='popout-chat']\").click()")
    assert _wait_opened(window) == [f"{site}/chat/window"]
    assert len(window.context.pages) == 1
    assert "blocked" not in (window.locator("#toast").text_content() or "")
    assert window.evaluate("() => window.open('/digest', '_blank') !== null"), "the page is told it opened"
    assert _wait_opened(window, 2)[-1] == f"{site}/digest"


# A download stays with the window ------------------------------------------------------------------------------------


def test_downloads_stay_with_the_window_which_saves_them(site, window):
    window.goto(f"{site}/actions")
    with window.expect_download() as got:
        window.click("a:has-text('Download CSV')")
    assert got.value.suggested_filename == "closedesk-actions.csv"
    window.goto(f"{site}/app/mail/demo-payroll")
    with window.expect_download() as got:
        window.click(".rd-bar a[title^='Download']")
    assert got.value.suggested_filename.endswith(".eml")
    window.goto(f"{site}/app/fraud")
    with window.expect_download() as got:
        window.click("a:has-text('Download the log (CSV)')")
    assert got.value.suggested_filename == "closedesk-fraud-log.csv"
    assert _opened(window) == [], "nothing was sent to the browser"


# The page says it loaded (CI's check of the real window waits for it) ---------------------------------------------------


def test_the_page_tells_the_window_it_loaded_in_both_uis(site, browser):
    context = _context(browser, LATE_BRIDGE)
    page = context.new_page()
    for path, count in (("/app", 1), ("/inbox", 2)):
        page.goto(f"{site}{path}")
        for _ in range(100):
            if len(context.ready) >= count:
                break
            page.wait_for_timeout(50)
    assert context.ready == ["/app", "/inbox"]
    assert context.errors == []
    context.close()


# In a browser tab nothing changes --------------------------------------------------------------------------------------


def test_in_a_browser_tab_links_meant_for_a_new_tab_still_open_one(site, tab):
    _with_a_digest(tab, site)
    tab.goto(f"{site}/digest")
    with tab.context.expect_page() as opened:
        tab.click("a:has-text('Printable page')")
    assert "/digest/" in opened.value.url
    assert tab.context.opened == []
    assert tab.evaluate("() => { const w = window.open('about:blank'); const real = !!(w && w.document); w && w.close(); return real; }")


@pytest.mark.parametrize("which", ["opening", "updating"])
def test_back_onto_the_windows_own_page_goes_forward_again(browser, which):
    # On Windows the window's browser keys and its mouse's Back button work (app_window.browser_keys): Back from
    # CloseDesk's first page went to the window's own "Opening CloseDesk", which waits for good, with nothing opening.
    from controller_inbox import app_window

    shown = {"opening": app_window.opening_page(), "updating": app_window.updating_page()}[which]
    context = browser.new_context()
    context.set_default_timeout(15000)
    page = context.new_page()
    page.route("http://window.test/own", lambda route: route.fulfill(content_type="text/html", body=shown))
    page.route("http://window.test/closedesk", lambda route: route.fulfill(content_type="text/html", body="<title>CloseDesk</title><p>Today</p>"))
    try:
        page.goto("http://window.test/own")
        assert page.inner_text("h1") in ("Opening CloseDesk", "Updating CloseDesk"), "shown as the window shows it"
        page.goto("http://window.test/closedesk")
        page.go_back()
        page.wait_for_url("http://window.test/closedesk")
        assert page.inner_text("p") == "Today"
    finally:
        context.close()
