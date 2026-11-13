"""The workspace (/app) in a real browser: the calendar, the back arrow and Esc, a slow email, held mail, Ask
CloseDesk on a phone, the Page tab's card, and long subjects. Skipped without Playwright's Chromium."""

from __future__ import annotations

import pytest

pytest.importorskip("playwright")

from controller_inbox import web  # noqa: E402
from liveserver import serving  # noqa: E402

PHONE = {"width": 390, "height": 844}
WIDE = {"width": 1280, "height": 900}


def _path(page) -> str:
    return page.evaluate("location.pathname + location.search")


# The calendar ------------------------------------------------------------------------------------------------


def test_a_meeting_opens_beside_the_calendar_without_stray_text(site, page):
    for size in (WIDE, PHONE):
        page.set_viewport_size(size)
        page.goto(f"{site}/app/calendar?view=week&date=2026-09-22")
        page.click(".cal-chip[aria-label*='Northwind invoice INV-10482 review']")
        page.wait_for_selector(".cal-panel .cal-panel-title")
        panel = page.inner_text(".cal-panel")
        assert "null" not in panel and "When" in panel, panel
        # Only the sample is loaded: the note says these are the sample's meetings, not invitations in the mail.
        note = page.inner_text(".cal-note")
        assert "sample calendar" in note and "drop folder" not in note, note


def test_a_calendar_address_with_a_wrong_date_shows_the_calendar(site, page):
    for query in ("view=week&date=abc", "view=month&date=2026-11"):
        page.goto(f"{site}/app/calendar?{query}")
        page.wait_for_selector(".cal-title")
        assert "date=abc" not in _path(page) and "date=2026-11&" not in _path(page) + "&"


def test_esc_goes_back_to_the_meeting_after_its_email_s_file_was_opened(site, page):
    page.set_viewport_size(WIDE)
    page.goto(f"{site}/app/calendar?view=week&date=2026-09-22")
    page.click(".cal-chip[aria-label*='Northwind invoice INV-10482 review']")
    page.click(".cal-related a.rel-link[href='/app/mail/demo-inv-10482']")
    page.wait_for_selector(".rd[data-email='demo-inv-10482']")
    page.click(".file-actions a[href*='/file/1']")
    page.wait_for_function("() => location.pathname.includes('/file/1')")
    page.keyboard.press("Escape")
    page.wait_for_function("() => location.pathname === '/app/mail/demo-inv-10482'")
    page.wait_for_selector(".rd[data-email='demo-inv-10482']")
    page.keyboard.press("Escape")
    page.wait_for_function("() => location.pathname === '/app/calendar'")
    assert "meeting=" in _path(page)
    page.wait_for_selector(".cal-panel .cal-panel-title")


# The top bar's back arrow, and a slow email ---------------------------------------------------------------


def test_the_back_arrow_leaves_the_calendar_settings_digest_and_whole_matter(site, page):
    page.set_viewport_size(PHONE)
    page.goto(f"{site}/app/all")
    page.wait_for_selector("#list .item")
    for rail in ("settings", "digest", "calendar"):
        page.click(f".rail-item[data-id='{rail}']")
        page.wait_for_function("id => location.pathname === '/app/' + id", arg=rail)
        assert page.is_visible(".top-back")
        page.click(".top-back")
        page.wait_for_function("() => location.pathname === '/app/all'")
    page.goto(f"{site}/app/trace/demo-inv-10482")
    page.wait_for_selector(".top-back")
    page.click(".top-back")
    page.wait_for_function("() => location.pathname === '/app/mail/demo-inv-10482'")


@pytest.mark.parametrize("rail", ["calendar", "settings", "digest"])
def test_an_email_that_answers_late_doesnt_cover_the_page_opened_meanwhile(site, page, rail):
    page.set_viewport_size(WIDE)
    page.add_init_script(
        """(() => {
          const real = window.fetch;
          window.fetch = (url, options) => /\\/api\\/mail\\/demo-pbc$/.test(String(url))
            ? new Promise((done) => setTimeout(done, 1200)).then(() => real(url, options))
            : real(url, options);
        })()"""
    )
    page.goto(f"{site}/app/all")
    page.click("#list a[href^='/app/mail/demo-pbc?']")
    page.click(f".rail-item[data-id='{rail}']")
    page.wait_for_timeout(2000)
    assert page.evaluate("location.pathname") == f"/app/{rail}"
    assert page.query_selector("#reader .rd[data-email]") is None
    assert "PBC" not in page.title()


# Held mail -----------------------------------------------------------------------------------------------------


def test_a_held_email_without_files_isnt_marked_files_locked(site, page):
    page.set_viewport_size(WIDE)
    page.goto(f"{site}/app/all")
    row = page.locator("#list .item", has_text="Urgent: Updated wiring instructions for Apex Vendor")
    row.wait_for()
    chips = row.locator(".chip").all_inner_texts()
    assert "Files locked" not in chips and "Held: possible fraud" in chips, chips


def test_a_held_email_s_file_gives_a_way_to_its_fraud_check_not_try_again(settings, store, mail, page):
    scam = mail["Updated remittance details"]
    with serving(web.create_app(settings, store)) as base:
        page.set_viewport_size(PHONE)
        page.goto(f"{base}/app/mail/{scam.id}/file/1")
        page.wait_for_selector("#reader .rd-empty")
        assert page.locator("#reader button", has_text="Try again").count() == 0
        page.click("#reader a:has-text('See the fraud check')")
        page.wait_for_function("id => location.pathname === '/app/mail/' + id", arg=scam.id)
        page.wait_for_function("() => document.activeElement && document.activeElement.id === 'rd-fraud'")


# Today ---------------------------------------------------------------------------------------------------------


def test_the_open_the_calendar_link_keeps_its_arrow_on_its_line(site, page):
    for size in (WIDE, PHONE):
        page.set_viewport_size(size)
        page.goto(f"{site}/app")
        shown = "links => links.filter((a) => a.offsetParent && a.textContent.includes('Open the calendar'))"
        page.wait_for_function(f"() => ({shown})([...document.querySelectorAll('a')]).length > 0")
        lines = page.eval_on_selector_all("a", f"links => ({shown})(links).map((a) => a.getClientRects().length)")
        assert lines and set(lines) == {1}, lines


# Ask CloseDesk on a phone ----------------------------------------------------------------------------------------


def test_ask_closedesk_on_a_phone_keeps_tab_and_esc_inside_it(site, page):
    page.set_viewport_size(PHONE)
    page.goto(f"{site}/app/mail/demo-pbc")
    page.wait_for_selector(".rd[data-email='demo-pbc']")
    page.click("#chat-btn")
    page.wait_for_selector("#chat:not([hidden])")
    for _ in range(8):
        page.keyboard.press("Tab")
        assert page.evaluate("() => Boolean(document.activeElement.closest('#chat'))"), page.evaluate("document.activeElement.outerHTML.slice(0, 120)")
    page.evaluate("() => document.activeElement.blur()")
    page.keyboard.press("Escape")
    page.wait_for_selector("#chat", state="hidden")
    assert page.evaluate("location.pathname") == "/app/mail/demo-pbc"
    assert not page.evaluate("() => document.querySelector('#reader').inert")
    # Beside the page in a wide window: the page stays usable.
    page.set_viewport_size(WIDE)
    page.click("#chat-btn")
    page.wait_for_selector("#chat:not([hidden])")
    assert not page.evaluate("() => document.querySelector('#reader').inert")


# The Page tab's card ---------------------------------------------------------------------------------------------


def test_the_page_tab_s_card_closes_with_the_file_and_gives_focus_back(settings, store, page):
    from msgfactory import PDF
    from test_page_view import _detailed_invoice
    from test_workspace import _harbor, _open_page_tab

    email = _harbor(settings, store, ("HS-10482.pdf", _detailed_invoice(), PDF))
    with serving(web.create_app(settings, store)) as base:
        # A box pointed at with the keyboard, then Esc back to the email: its card goes with the file.
        _open_page_tab(page, base, email)
        page.focus(".pv-box")
        page.wait_for_selector("#pv-tip:not([hidden])")
        page.keyboard.press("Escape")
        page.wait_for_function("id => location.pathname === '/app/mail/' + id", arg=email.id)
        page.wait_for_selector(".rd[data-email]")
        assert page.is_hidden("#pv-tip")

        # The fix form: Tab stays inside it, and Esc or Cancel gives the focus back to its box.
        _open_page_tab(page, base, email)
        page.focus(".pv-box")
        page.keyboard.press("Enter")
        page.wait_for_selector("#pv-tip.pinned .pv-fix-input")
        for key in ("Tab", "Tab", "Tab", "Tab", "Shift+Tab", "Shift+Tab", "Shift+Tab"):
            page.keyboard.press(key)
            assert page.evaluate("() => document.getElementById('pv-tip').contains(document.activeElement)"), key
        page.keyboard.press("Escape")
        assert page.evaluate("() => document.activeElement.classList.contains('pv-box')")
        assert page.query_selector("#pv-tip.pinned") is None
        page.keyboard.press("Enter")
        page.wait_for_selector("#pv-tip.pinned .pv-fix-input")
        page.click("#pv-tip button:text-is('Cancel')")
        assert page.evaluate("() => document.activeElement.classList.contains('pv-box')")


# Long subjects on a phone ----------------------------------------------------------------------------------------


def test_a_long_unbroken_subject_wraps_on_a_phone(settings, store, page):
    from controller_inbox.folder_mail import ingest_folder
    from msgfactory import write_msg

    subject = "Emailing: Statement_of_account_ACCT0000123456789_for_period_2026-10-01_to_2026-10-31_final.pdf"
    settings.trusted_domains = "taz.com"
    settings.ensure_data_dir()
    write_msg(settings.inbox_incoming / "statement.msg", subject, "Please reply by Friday with the signed statement.",
              sender_name="Scanner", sender_email="scanner@taz.com")
    [email] = ingest_folder(store, settings)
    with serving(web.create_app(settings, store)) as base:
        page.set_viewport_size(PHONE)
        page.goto(f"{base}/app/mail/{email.id}")
        page.wait_for_selector(".rd-subject")
        wide = page.evaluate("() => { const r = document.querySelector('#reader'); return [r.scrollWidth, r.clientWidth]; }")
        assert wide[0] <= wide[1], wide
        for classic in (f"/inbox/{email.id}", "/inbox", "/digest"):
            page.goto(base + classic)
            wide = page.evaluate("() => [document.documentElement.scrollWidth, window.innerWidth]")
            assert wide[0] <= wide[1], (classic, wide)


# Settings ------------------------------------------------------------------------------------------------------


def test_choosing_a_model_too_big_for_this_computer_says_so_on_both_settings_pages(settings, store, page, monkeypatch):
    from test_lm_studio_models import CHAT, CODER, LMStudio, _serve

    from controller_inbox import model_roles

    gb = 1024**3
    settings.llm = None
    server = LMStudio(loaded=[CHAT])
    server.sizes = {CODER: 19 * gb, CHAT: 5 * gb}
    _serve(monkeypatch, server)
    monkeypatch.setattr(model_roles, "computer_memory", lambda: 16 * gb)
    with serving(web.create_app(settings, store)) as base:
        for size in (WIDE, PHONE):
            page.set_viewport_size(size)
            page.goto(f"{base}/app/settings")
            page.wait_for_selector("#set-chat")
            note = page.locator("#set-chat-too-big")
            assert not note.is_visible(), "the one loaded fits"
            page.select_option("#set-chat", CODER)
            assert note.is_visible() and "needs about 21 GB" in note.inner_text() and "3B to 8B" in note.inner_text()
            assert page.evaluate("() => document.documentElement.scrollWidth <= innerWidth"), "no sideways scroll"
            page.select_option("#set-chat", CHAT)
            assert not note.is_visible()
        page.goto(f"{base}/settings")
        note = page.locator("[data-too-big-note]")
        assert not note.is_visible()
        page.select_option("#chat-model select[name=model]", CODER)
        assert note.is_visible() and "needs about 21 GB" in note.inner_text()
