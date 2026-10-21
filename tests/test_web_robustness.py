"""Web robustness: per-app render state, the workspace poll loop, opening originals and workspace links."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from controller_inbox import web
from controller_inbox.config import Settings
from controller_inbox.store import Store
from controller_inbox.web import create_app

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


UI = Path(__file__).resolve().parents[1] / "src" / "controller_inbox" / "static" / "ui"


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
