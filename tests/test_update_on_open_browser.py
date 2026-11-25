"""The update question in a real Chromium, on the first page shown in both UIs: "A new version of CloseDesk is ready",
what's new, Update now and Not now. Not now asks again only the next time CloseDesk opens; Update now is refused while
mail is processed and offered again when that is done; offline, with the check turned off, or started without a
launcher, nothing is asked. Skipped without Playwright's Chromium."""

from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("playwright")

from controller_inbox import updates, web  # noqa: E402
from liveserver import serving  # noqa: E402
from test_update_on_open import NEWER, asked_at_start, download, github, no_github, offline  # noqa: E402,F401

DIALOG = "#update-ask[open]"
WHATS_NEW = "Wait as long as the model needs for file summaries, and stop instead of looking stuck"


class Stopper:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def opened(settings, store, monkeypatch, ask=None, *, failed: bool = False, newest: bool = False):
    """CloseDesk as it opens: GitHub asked (by ``ask``), and the app with its question; Update now stops it."""
    monkeypatch.setattr(updates, "_ask_github", ask or github())
    app = web.create_app(settings, store)
    app.state.update_question = asked_at_start(store, settings, failed=failed, newest=newest)
    app.state.stop_server = Stopper()
    return app


def answered(page, url: str) -> dict:
    """Opens ``url`` and gives the question's last answer to the page (once GitHub's answer was in)."""
    with page.expect_response(lambda r: "/api/update/question" in r.url and r.request.method == "GET" and not r.json()["checking"]) as reply:
        page.goto(url)
    page.wait_for_load_state("networkidle")
    return reply.value.json()


@pytest.mark.parametrize("first", ["/", "/app"])
def test_the_first_page_asks_and_not_now_isnt_asked_again_this_run(download, settings, loaded, page, monkeypatch, first):
    app = opened(settings, loaded, monkeypatch)
    with serving(app) as base:
        page.goto(base + first)
        page.wait_for_selector(DIALOG)
        text = page.inner_text("#update-ask")
        assert "A new version of CloseDesk is ready" in text
        assert "CloseDesk 2026.11.16 is out; you have 2026.11.13." in text
        assert WHATS_NEW in text
        assert page.evaluate("document.activeElement.textContent") == "Not now"
        page.click("#update-ask button:has-text('Not now')")
        page.wait_for_selector("#update-ask", state="detached")
        # CloseDesk carries on as normal, in this UI and the other, without asking again in this run.
        for other in ("/", "/inbox", "/app", "/app/settings"):
            assert answered(page, base + other)["ask"] is False
            assert page.query_selector("#update-ask") is None
        assert app.state.stop_server.calls == 0
    # The next time CloseDesk opens, it asks again.
    with serving(opened(settings, loaded, monkeypatch)) as base:
        page.goto(base + first)
        page.wait_for_selector(DIALOG)


def test_enter_pressed_as_the_question_comes_up_doesnt_update(download, settings, loaded, page, monkeypatch):
    # The question can come up while the person is typing in the page: an Enter meant for the chat closes it, as Not
    # now, and never updates CloseDesk.
    app = opened(settings, loaded, monkeypatch)
    with serving(app) as base:
        page.goto(base + "/app")
        page.wait_for_selector(DIALOG)
        page.keyboard.press("Enter")
        page.wait_for_selector("#update-ask", state="detached")
        assert app.state.stop_server.calls == 0
        assert answered(page, base + "/app")["ask"] is False


def test_escape_is_not_now(download, settings, loaded, page, monkeypatch):
    app = opened(settings, loaded, monkeypatch)
    with serving(app) as base:
        page.goto(base + "/app")
        page.wait_for_selector(DIALOG)
        page.keyboard.press("Escape")
        page.wait_for_selector("#update-ask", state="detached")
        # The key went to the dialog, not to the page behind it.
        assert page.url == base + "/app"
        assert answered(page, base + "/app")["ask"] is False


def test_update_now_in_the_workspace_updates_and_opens_the_new_version(download, settings, loaded, page, monkeypatch):
    app = opened(settings, loaded, monkeypatch)
    with serving(app) as base:
        page.goto(base + "/app")
        page.wait_for_selector(DIALOG)
        page.click("#update-ask button:has-text('Update now')")
        page.wait_for_selector("#updating")
        assert updates.UPDATING in page.inner_text("#updating") and app.state.stop_server.calls == 1
        # Gone as the updating page shows (it was taken out only by its close event, a moment later: a look in
        # between found it).
        assert page.query_selector("#update-ask") is None
        # The launcher has put the new version in place and started it: the page loads again by itself.
        (download / ".git_archival.txt").write_text(f"commit: {NEWER}\ndate: 2026-11-16\n", encoding="utf-8")
        page.wait_for_function("() => (document.querySelector('.rail-version') || {}).textContent === 'CloseDesk 2026.11.16 (bbbbbbb)'", timeout=20000)


def test_the_workspace_keeps_saying_it_updates_whatever_is_clicked_meanwhile(download, settings, loaded, page, monkeypatch):
    # A click on Today (or any folder) while CloseDesk was down replaced "Updating CloseDesk" with a normal-looking
    # workspace, its top bar still offering "Update ready" and Process new mail, which then failed with the browser's
    # "Failed to fetch".
    app = opened(settings, loaded, monkeypatch)
    with serving(app) as base:
        page.goto(base + "/app/inbox")
        page.wait_for_selector(DIALOG)
        page.click("#update-ask button:has-text('Update now')")
        page.wait_for_selector("#updating")
        today = page.query_selector("#rail-nav a[href='/app']").bounding_box()
        page.mouse.click(today["x"] + today["width"] / 2, today["y"] + today["height"] / 2)  # where a person clicks
        page.evaluate("history.pushState({}, '', '/app/all')")
        page.go_back()  # the browser's Back, within the workspace
        page.keyboard.press("g")
        page.keyboard.press("t")
        page.wait_for_timeout(500)
        assert page.query_selector("#reader #updating") is not None, "the updating page stays until CloseDesk is back"
        assert page.query_selector("#top-update") is None, "nothing offers the update that runs"
        assert page.is_disabled("#process-btn") and "Updating" in page.inner_text("#process-btn")
        assert page.query_selector(".toast") is None, "nothing failed: nothing was tried"
        (download / ".git_archival.txt").write_text(f"commit: {NEWER}\ndate: 2026-11-16\n", encoding="utf-8")
        page.wait_for_function("() => (document.querySelector('.rail-version') || {}).textContent === 'CloseDesk 2026.11.16 (bbbbbbb)'", timeout=20000)
        assert page.query_selector("#updating") is None and page.is_enabled("#process-btn")


def test_update_now_on_a_classic_page_updates_and_opens_the_new_version(download, settings, loaded, page, monkeypatch):
    app = opened(settings, loaded, monkeypatch)
    with serving(app) as base:
        page.goto(base + "/inbox")
        page.wait_for_selector(DIALOG)
        page.click("#update-ask button:has-text('Update now')")
        page.wait_for_selector("#update-ask.updating")
        assert updates.UPDATING in page.inner_text("#update-ask") and app.state.stop_server.calls == 1
        assert page.query_selector("#update-ask button") is None, "nothing to click while it updates"
        # In the classic pages' own colours, not the browser's bright blue <progress>.
        assert page.query_selector("#update-ask progress") is None
        bar = page.evaluate("getComputedStyle(document.querySelector('#update-ask .update-ask-bar span')).backgroundColor")
        assert bar == "rgb(27, 54, 93)", bar  # --navy
        assert page.get_attribute("#update-ask .update-ask-bar", "role") == "progressbar"
        page.keyboard.press("Escape")
        assert page.query_selector("#update-ask.updating[open]") is not None, "it stays up until CloseDesk is back"
        (download / ".git_archival.txt").write_text(f"commit: {NEWER}\ndate: 2026-11-16\n", encoding="utf-8")
        page.wait_for_function("() => document.body.innerText.includes('CloseDesk 2026.11.16 (bbbbbbb)')", timeout=20000)
        assert page.url == base + "/inbox"


def test_update_now_waits_while_mail_is_processed_and_is_offered_when_done(download, settings, loaded, page, monkeypatch):
    app = opened(settings, loaded, monkeypatch)
    release = threading.Event()
    app.state.job.start(lambda progress: release.wait(30))
    try:
        with serving(app) as base:
            page.goto(base + "/app")
            page.wait_for_selector(DIALOG)
            page.click("#update-ask button:has-text('Update now')")
            page.wait_for_function(f"() => document.querySelector('#update-ask').innerText.includes({updates.BUSY!r})")
            assert page.is_disabled("#update-ask button:has-text('Update now')")
            assert app.state.stop_server.calls == 0
            # The keyboard's place stays in the question (Update now, pressed, can't keep it): on Not now.
            assert page.evaluate("document.activeElement.textContent") == "Not now"
            release.set()
            page.wait_for_function("() => document.querySelector('#update-ask').innerText.includes('The mail is done: CloseDesk can update now.')")
            assert page.is_enabled("#update-ask button:has-text('Update now')")
            # As on a page loaded again meanwhile: the focus stays on Not now (a key pressed then must not update).
            assert page.evaluate("document.activeElement.textContent") == "Not now"
            page.click("#update-ask button:has-text('Update now')")
            page.wait_for_selector("#updating")
            assert app.state.stop_server.calls == 1
    finally:
        release.set()


def test_classic_setups_update_button_is_offered_once_the_mail_is_done(download, settings, loaded, page, monkeypatch):
    # Classic Setup has no look at the mail being processed: its Update button stayed disabled, "It can update once the
    # mail being processed now is done" with it, after the mail was done, until the page was loaded again.
    app = opened(settings, loaded, monkeypatch)
    release = threading.Event()
    app.state.job.start(lambda progress: release.wait(30))
    try:
        with serving(app) as base:
            page.goto(base + "/settings#update")
            page.wait_for_selector(DIALOG)
            page.click("#update-ask button:has-text('Not now')")
            page.wait_for_selector("#update-ask", state="detached")
            button = "#update form[action='/settings/update'] button"
            assert page.is_disabled(button)
            assert "It can update once the mail being processed now is done." in page.inner_text("#update")
            release.set()
            page.wait_for_function(f"() => !document.querySelector({button!r}).disabled", timeout=10000)
            said = page.inner_text("#update")
            assert updates.DONE in said and "It can update once" not in said
            page.click(button)
            page.wait_for_selector("[data-update-line]")
            assert app.state.stop_server.calls == 1
    finally:
        release.set()


@pytest.mark.parametrize("first", ["/", "/app"])
def test_offline_nothing_is_asked_and_nothing_is_in_the_way(download, settings, loaded, page, monkeypatch, first):
    with serving(opened(settings, loaded, monkeypatch, offline)) as base:
        assert answered(page, base + first)["ask"] is False
        assert page.query_selector("#update-ask") is None


def test_a_slow_github_asks_once_its_answer_is_in(download, settings, loaded, page, monkeypatch):
    slow = threading.Event()
    monkeypatch.setattr(updates, "_ask_github", github(hold=slow))
    app = web.create_app(settings, loaded)
    app.state.update_question = question = updates.Question()
    question.begin(loaded, settings)
    try:
        with serving(app) as base:
            page.goto(base + "/")
            page.wait_for_selector("h1")
            time.sleep(1)
            assert page.query_selector("#update-ask") is None
            slow.set()
            page.wait_for_selector(DIALOG)
    finally:
        slow.set()


def test_with_the_check_turned_off_nothing_is_asked(download, settings, loaded, page, monkeypatch):
    settings.update_check = False
    ask = github()
    with serving(opened(settings, loaded, monkeypatch, ask)) as base:
        for first in ("/", "/app"):
            assert answered(page, base + first)["ask"] is False
            assert page.query_selector("#update-ask") is None
    assert ask.asked == []


def test_started_without_a_launcher_nothing_is_asked_and_settings_say_how(download, settings, loaded, page, monkeypatch):
    monkeypatch.delenv(updates.LAUNCHER_ENV)
    with serving(opened(settings, loaded, monkeypatch)) as base:
        for first in ("/", "/app"):
            assert answered(page, base + first)["ask"] is False
            assert page.query_selector("#update-ask") is None
        page.goto(base + "/app/settings#set-about")
        page.wait_for_selector("#set-update")
        assert "double-click CloseDesk-Update" in page.inner_text("#set-update")


@pytest.mark.parametrize("first", ["/", "/app"])
def test_an_update_that_didnt_finish_is_said(download, settings, loaded, page, monkeypatch, first):
    with serving(opened(settings, loaded, monkeypatch, offline, failed=True)) as base:
        page.goto(base + first)
        page.wait_for_selector(DIALOG)
        text = page.inner_text("#update-ask")
        assert "CloseDesk wasn't updated" in text and updates.UNFINISHED in text
        assert page.query_selector("#update-ask button:has-text('Update now')") is None, "GitHub can't be reached now"
        page.click("#update-ask button:has-text('OK')")
        page.wait_for_selector("#update-ask", state="detached")
        assert answered(page, base + first)["ask"] is False


@pytest.mark.parametrize("first", ["/", "/app"])
def test_after_update_now_found_nothing_newer_to_download_the_page_says_so_once(download, settings, loaded, page, monkeypatch, first):
    # GitHub still lists the newer version: the page that loads again after Update now says why nothing changed,
    # instead of asking the same question again.
    with serving(opened(settings, loaded, monkeypatch, newest=True)) as base:
        page.goto(base + first)
        page.wait_for_selector(DIALOG)
        text = page.inner_text("#update-ask")
        assert updates.NEWEST_TITLE in text and updates.NEWEST_SAID in text
        assert "is out; you have" not in text
        assert page.query_selector("#update-ask button:has-text('Update now')") is None
        page.click("#update-ask button:has-text('OK')")
        page.wait_for_selector("#update-ask", state="detached")
        assert answered(page, base + first)["ask"] is False


# Waiting for CloseDesk to come back ------------------------------------------------------------------------------------


@pytest.mark.parametrize("first", ["/inbox", "/app"])
def test_an_update_that_fails_at_once_is_said_though_closedesk_was_back_between_two_looks(download, settings, loaded, page, monkeypatch, first):
    # No internet: the launcher says so and starts CloseDesk again as it was, in about a second and a half, between two
    # of the page's looks. The page saw neither CloseDesk go nor a new version, and said "Updating CloseDesk" for good.
    app = opened(settings, loaded, monkeypatch)
    with serving(app) as base:
        page.goto(base + first)
        page.wait_for_selector(DIALOG)
        page.click("#update-ask button:has-text('Update now')")
        page.wait_for_selector("#updating" if first == "/app" else "#update-ask.updating")
        # Started again as it was: another start, which asks "CloseDesk wasn't updated".
        app.state.update_question = asked_at_start(loaded, settings, failed=True)
        app.state.run = "started-again"
        page.wait_for_function("() => (document.querySelector('#update-ask[open]') || {}).innerText?.includes(\"CloseDesk wasn't updated\")", timeout=20000)


def test_setups_update_page_goes_back_to_setup_once_closedesk_started_again_as_it_was(download, settings, loaded, page, monkeypatch):
    app = opened(settings, loaded, monkeypatch)
    with serving(app) as base:
        page.goto(base + "/settings#update")
        page.wait_for_selector(DIALOG)
        page.click("#update-ask button:has-text('Not now')")
        page.click("#update form[action='/settings/update'] button")
        page.wait_for_selector("[data-update-line]")
        assert page.inner_text("#update").count("Updating CloseDesk") == 1
        app.state.run = "started-again"
        page.wait_for_url(base + "/settings#update", timeout=20000)
        page.wait_for_selector("#update form[action='/settings/update']")
        # The page that said "Updating CloseDesk" isn't left in the tab's history: Back doesn't say it again, with
        # nothing updating.
        page.go_back()
        page.wait_for_load_state()
        assert "Updating CloseDesk" not in page.inner_text("body")


def test_setups_update_page_keeps_saying_it_updates_whatever_is_clicked_meanwhile(download, settings, loaded, page, monkeypatch):
    # The rail (Today, the folders, "Update ready"), the search and the chat stayed live while the launcher downloaded:
    # one click left the page for the browser's "can't be reached" (or a page from the CloseDesk still stopping),
    # and nothing brought the tab back once CloseDesk was. The workspace's updating page keeps them out of reach.
    app = opened(settings, loaded, monkeypatch)
    with serving(app) as base:
        page.goto(base + "/settings#update")
        page.wait_for_selector(DIALOG)
        page.click("#update-ask button:has-text('Not now')")
        page.wait_for_selector("#update-ask", state="detached")
        page.click("#update form[action='/settings/update'] button")
        page.wait_for_selector("[data-update-line]")
        address = page.url
        assert page.query_selector(".rail-foot a[href='/settings#update']") is None, "nothing offers the update that runs"
        today = page.query_selector(".rail nav a[href='/']").bounding_box()
        page.mouse.click(today["x"] + today["width"] / 2, today["y"] + today["height"] / 2)  # where a person clicks
        page.keyboard.press("/")
        page.keyboard.type("invoice")
        page.keyboard.press("Enter")
        fab = page.query_selector("#chat-toggle").bounding_box()
        page.mouse.click(fab["x"] + fab["width"] / 2, fab["y"] + fab["height"] / 2)
        page.wait_for_timeout(500)
        assert page.url == address and page.query_selector("[data-update-line]") is not None, "the page left 'Updating CloseDesk'"
        assert page.is_hidden("#chat"), "the chat would only fail: CloseDesk is down"
        app.state.run = "started-again"
        page.wait_for_url(base + "/settings#update", timeout=20000)
        page.wait_for_selector("#update form[action='/settings/update']")


def test_updating_closedesk_is_said_once(download, settings, loaded, page, monkeypatch):
    app = opened(settings, loaded, monkeypatch)
    with serving(app) as base:
        page.goto(base + "/inbox")
        page.wait_for_selector(DIALOG)
        page.click("#update-ask button:has-text('Update now')")
        page.wait_for_selector("#update-ask.updating")
        assert page.inner_text("#update-ask").count("Updating CloseDesk") == 1
    app = opened(settings, loaded, monkeypatch)
    with serving(app) as base:
        page.goto(base + "/app")
        page.wait_for_selector(DIALOG)
        page.click("#update-ask button:has-text('Update now')")
        page.wait_for_selector("#updating")
        assert page.inner_text("#updating").count("Updating CloseDesk") == 1


# Update now from a page shown before CloseDesk was updated -------------------------------------------------------------


def test_update_now_in_a_tab_shown_before_closedesk_was_updated_says_so_and_stops_nothing(download, settings, loaded, page, monkeypatch):
    app = opened(settings, loaded, monkeypatch)
    with serving(app) as base:
        page.goto(base + "/inbox")
        page.wait_for_selector(DIALOG)
        # Updated meanwhile, from another tab: this one still shows the question.
        (download / ".git_archival.txt").write_text(f"commit: {NEWER}\ndate: 2026-11-16\n", encoding="utf-8")
        page.click("#update-ask button:has-text('Update now')")
        page.wait_for_function(f"() => document.querySelector('#update-ask').innerText.includes({updates.UPDATED_MEANWHILE!r})")
        assert app.state.stop_server.calls == 0
        # Update now isn't offered again (each press only repeated the refusal): the page is reloaded instead.
        assert page.query_selector("#update-ask button:has-text('Update now')") is None
        assert "is out; you have" not in page.inner_text("#update-ask")
        # Nor does its heading still say a new version is ready, above "nothing to update now".
        assert page.inner_text("#update-ask-title") == updates.MEANWHILE_TITLE
        assert page.evaluate("document.activeElement.textContent") == "Reload the page"
        with page.expect_navigation():
            page.click("#update-ask button:has-text('Reload the page')")
        assert page.url == base + "/inbox"


# A second tab -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("first", ["/inbox", "/app"])
def test_update_now_in_a_second_tab_while_closedesk_updates_says_so_and_comes_back(download, settings, loaded, page, monkeypatch, first):
    # Two tabs show the question; Update now in one, and CloseDesk stops to download the update. Update now in the
    # other found nothing answering: it said "Failed to fetch", offered both buttons again, and never loaded again.
    app = opened(settings, loaded, monkeypatch)
    with serving(app) as base:
        page.goto(base + first)
        page.wait_for_selector(DIALOG)
        # CloseDesk has stopped to update (from the first tab): nothing answers.
        page.route("**/api/update", lambda route: route.abort("connectionrefused"))
        page.route("**/health", lambda route: route.abort("connectionrefused"))
        page.click("#update-ask button:has-text('Update now')")
        shown = "#updating" if first == "/app" else "#update-ask.updating"
        page.wait_for_selector(shown)
        said = page.inner_text(shown)
        assert updates.UPDATING_ELSEWHERE in said and "Failed to fetch" not in said
        assert page.query_selector("#update-ask button:has-text('Update now')") is None
        # Back, as the new version: the tab loads again by itself.
        page.unroute("**/health")
        (download / ".git_archival.txt").write_text(f"commit: {NEWER}\ndate: 2026-11-16\n", encoding="utf-8")
        page.wait_for_function("() => !document.querySelector('#updating, #update-ask.updating')", timeout=20000)
        assert app.state.stop_server.calls == 0


def test_settings_update_while_closedesk_updates_from_another_tab_says_so(download, settings, loaded, page, monkeypatch):
    app = opened(settings, loaded, monkeypatch)
    with serving(app) as base:
        page.goto(base + "/app/settings")
        page.wait_for_selector(DIALOG)
        page.click("#update-ask button:has-text('Not now')")
        page.wait_for_selector("#set-update button:has-text('Update')")
        page.route("**/api/update", lambda route: route.abort("connectionrefused"))
        page.route("**/health", lambda route: route.abort("connectionrefused"))
        page.click("#set-update button:has-text('Update')")
        page.wait_for_selector("#updating")
        assert updates.UPDATING_ELSEWHERE in page.inner_text("#updating")
        assert "Failed to fetch" not in page.inner_text("body")


# Another person's CloseDesk on the port meanwhile -----------------------------------------------------------------------


@pytest.mark.parametrize("first", ["/inbox", "/app", "/settings"])
def test_the_page_waiting_for_the_update_never_loads_another_persons_closedesk(download, settings, loaded, page, monkeypatch, first):
    # A shared computer (Remote Desktop): this person's CloseDesk stopped to update, and another person's opened on the
    # same port meanwhile. Its /health said "closedesk", and the page waiting loaded that one: this person's browser
    # showed someone else's mail. Only this folder's CloseDesk, run by this person, is CloseDesk back.
    import json

    from controller_inbox import version

    app = opened(settings, loaded, monkeypatch)
    theirs = {"ok": True, "app": "closedesk", "commit": NEWER, "run": "theirs", **version.whose(), "user": "0" * 16}
    with serving(app) as base:
        page.goto(base + first)
        page.wait_for_selector(DIALOG)
        if first == "/settings":
            page.click("#update-ask button:has-text('Not now')")
            page.wait_for_selector("#update-ask", state="detached")
            page.click("#update form[action='/settings/update'] button")
            shown = "[data-update-line]"
        else:
            page.click("#update-ask button:has-text('Update now')")
            shown = "#updating" if first == "/app" else "#update-ask.updating"
        page.wait_for_selector(shown)
        address = page.url
        page.route("**/health", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(theirs)))
        page.wait_for_timeout(5000)  # two of the page's looks, and more
        assert page.url == address and page.query_selector(shown) is not None, "the page loaded another person's CloseDesk"
        # This person's, back as the new version: loaded.
        page.unroute("**/health")
        (download / ".git_archival.txt").write_text(f"commit: {NEWER}\ndate: 2026-11-16\n", encoding="utf-8")
        page.wait_for_function("() => !document.querySelector('#updating, #update-ask.updating, [data-update-line]')", timeout=20000)


def test_an_older_closedesk_that_doesnt_say_whose_it_is_is_still_taken_for_this_one(download, settings, loaded, page, monkeypatch):
    import json

    app = opened(settings, loaded, monkeypatch)
    older = {"ok": True, "app": "closedesk", "commit": NEWER, "run": "older"}
    with serving(app) as base:
        page.goto(base + "/inbox")
        page.wait_for_selector(DIALOG)
        page.click("#update-ask button:has-text('Update now')")
        page.wait_for_selector("#update-ask.updating")
        page.route("**/health", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(older)))
        page.wait_for_function("() => !document.querySelector('#update-ask.updating')", timeout=20000)


# The classic rail -------------------------------------------------------------------------------------------------------


def test_the_classic_rails_update_ready_can_be_read(download, settings, loaded, page, monkeypatch):
    # It took the pages' navy link colour on the rail's navy: 1.28:1, an empty gap before "· CloseDesk ...".
    from test_workspace import CONTRAST

    app = opened(settings, loaded, monkeypatch)
    with serving(app) as base:
        page.goto(base + "/inbox")
        page.wait_for_selector(DIALOG)
        page.click("#update-ask button:has-text('Not now')")
        link = ".rail-foot a[href='/settings#update']"
        assert page.inner_text(link) == "Update ready"
        assert page.evaluate(CONTRAST, link) >= 4.5


@pytest.mark.parametrize(
    "width, chat, whole",
    [(320, False, False), (390, False, True), (560, False, True), (720, False, True), (1280, False, True),
     (1280, True, True), (1181, True, False)],
)
def test_the_workspaces_update_ready_leaves_the_top_bar_on_screen(download, settings, loaded, page, monkeypatch, width, chat, whole):
    # At a phone's width (390px) "Update ready" pushed the help button and Ask CloseDesk past the right edge, and the
    # page scrolled sideways; so it did beside the open chat just wider than the chat's own window (1181px). Shortened
    # ("Update…") only where it would.
    app = opened(settings, loaded, monkeypatch)
    with serving(app) as base:
        page.set_viewport_size({"width": width, "height": 800})
        page.goto(base + "/app")
        page.wait_for_selector(DIALOG)
        page.click("#update-ask button:has-text('Not now')")
        page.wait_for_selector("#update-ask", state="detached")
        page.wait_for_selector("#top-update")
        if chat:
            page.click("#chat-btn")
            page.wait_for_selector("#chat:not([hidden])")
            page.wait_for_timeout(300)
        cut = page.evaluate("(() => { const e = document.querySelector('#top-update'); return e.scrollWidth > e.clientWidth; })()")
        assert cut is not whole, "said whole where it fits, shortened only where it doesn't"
        found = page.evaluate("""() => {
          const right = (sel) => document.querySelector(sel).getBoundingClientRect().right;
          return {
            page: document.documentElement.scrollWidth, help: right("[data-cmd='help']"), chat: right("#chat-btn"),
            update: document.querySelector("#top-update").getBoundingClientRect().width,
          };
        }""")
        assert found["page"] <= width, f"the page scrolls sideways: {found}"
        assert found["help"] <= width and found["chat"] <= width, f"the top bar's buttons are past the edge: {found}"
        assert found["update"] > 20, "Update ready is still there to click"


# A long what's new ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("first", ["/inbox", "/app"])
def test_a_long_whats_new_keeps_its_ellipsis_with_its_last_word(download, settings, loaded, page, monkeypatch, first):
    # Shortened with "…": at the window's smallest size (720x520) the line broke just before it, leaving "…" on a line
    # of its own. Tried at every width the line can have, from a phone's to the dialog's widest.
    title = "Read the totals on scanned invoices from every vendor faster and say which page each figure came from " * 3
    app = opened(settings, loaded, monkeypatch, github(message=title))
    with serving(app) as base:
        page.set_viewport_size({"width": 720, "height": 520})
        page.goto(base + first)
        page.wait_for_selector(DIALOG)
        alone = page.evaluate("""() => {
          const line = document.querySelector('.update-ask-new');
          const text = line.lastChild;
          const top = (i) => { const r = document.createRange(); r.setStart(text, i); r.setEnd(text, i + 1); return r.getBoundingClientRect().top; };
          const end = text.length - 1;
          const widths = [];
          for (let width = 120; width <= 640; width += 1) {
            line.style.width = `${width}px`;
            if (Math.abs(top(end) - top(end - 2)) > 2) widths.push(width);
          }
          return widths;
        }""")
        assert page.evaluate("document.querySelector('.update-ask-new').lastChild.textContent.endsWith('…')")
        assert alone == []


# The classic Today page, while mail is processed ----------------------------------------------------------------------


def test_on_the_classic_today_page_the_mail_done_is_said_once_the_page_loaded_again(download, settings, loaded, page, monkeypatch):
    # The Today page loads again when the mail is done, before the question's own look: asked afresh, it didn't say
    # the update it had been asked for could be done now.
    app = opened(settings, loaded, monkeypatch)
    release = threading.Event()
    app.state.job.start(lambda progress: release.wait(30) and {"ingested": 0, "already_read": 0})
    try:
        with serving(app) as base:
            page.goto(base + "/")
            page.wait_for_selector(DIALOG)
            page.click("#update-ask button:has-text('Update now')")
            page.wait_for_function(f"() => document.querySelector('#update-ask').innerText.includes({updates.BUSY!r})")
            release.set()
            page.wait_for_function("() => (document.querySelector('#update-ask[open]') || {}).innerText?.includes('The mail is done: CloseDesk can update now.')", timeout=20000)
            assert page.is_enabled("#update-ask button:has-text('Update now')")
            assert page.query_selector(".job.running") is None, "the page shows the mail it read"
    finally:
        release.set()
