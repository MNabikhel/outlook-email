"""Long lists stopped at 200 while the sidebar counted more: the store pages, the pages say so and load more."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from controller_inbox import web
from controller_inbox.models import DocumentType, EmailRecord, Importance

PAGE = {"X-CloseDesk": "1"}


def _fill(store, n: int, folder: str = "reference") -> None:
    for i in range(n):
        store.upsert_email(
            EmailRecord(
                id=f"m{i:03d}", subject=f"Statement {i:03d}", sender_name="Bank", sender_email="bank@bank.example",
                received_at=f"2026-09-{1 + i // 24:02d}T{i % 24:02d}:00:00+00:00", body_text="", body_preview="",
                has_attachments=False, outlook_importance="normal", is_read=False, category=DocumentType.OTHER,
                category_confidence=0.5, importance=Importance.LOW, importance_score=10, folder=folder,
            )
        )


def test_list_emails_pages_with_an_offset_and_counts_the_rest(store):
    _fill(store, 205)
    first = store.list_emails(folder="reference", limit=200)
    rest = store.list_emails(folder="reference", limit=200, offset=200)
    assert len(first) == 200 and len(rest) == 5
    assert not {e.id for e in first} & {e.id for e in rest}
    assert store.count_emails(folder="reference") == 205
    assert store.count_emails(q="statement 204") == 1


def test_classic_folder_and_all_mail_say_how_many_are_not_shown(settings, store):
    _fill(store, 205)
    client = TestClient(web.create_app(settings, store))
    for path in ("/folder/reference", "/inbox"):
        page = client.get(path).text
        assert "Showing 200 of 205" in page, path
        assert page.count('class="subj"') == 200
        assert "limit=400" in page
        more = client.get(f"{path}?limit=400").text
        assert more.count('class="subj"') == 205 and "Showing 200" not in more
    page = client.get("/inbox?q=statement&limit=400").text
    assert page.count('class="subj"') == 205


def test_mail_api_gives_the_total_and_the_next_page(settings, store):
    _fill(store, 205)
    client = TestClient(web.create_app(settings, store))
    for query in ("folder=reference", "q=statement", ""):
        data = client.get(f"/api/mail?{query}", headers=PAGE).json()
        assert len(data["items"]) == 200 and data["total"] == 205, query
        rest = client.get(f"/api/mail?{query}&offset=200", headers=PAGE).json()
        assert len(rest["items"]) == 5 and rest["total"] == 205, query


def test_the_workspace_list_loads_more_than_200(settings, store):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    from liveserver import chromium_path, serving

    path = chromium_path()
    if path is None:
        pytest.skip("no Chromium for Playwright on this machine")
    _fill(store, 205)
    with serving(web.create_app(settings, store)) as base, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=path)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"{base}/app/folder/reference")
        page.wait_for_selector(".items li")
        assert page.locator(".items li").count() == 200
        assert "Showing 200 of 205" in page.text_content(".list-more")
        page.click(".list-more button")
        page.wait_for_function("() => document.querySelectorAll('.items li').length === 205")
        assert page.locator(".list-more").count() == 0
        browser.close()
        assert errors == []


def test_load_more_does_not_loop_when_mail_arrives_between_pages(settings, store):
    # Mail filed between two pages repeats a row on the next; the list pages on by the rows sent, not those kept.
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    from liveserver import chromium_path, serving

    path = chromium_path()
    if path is None:
        pytest.skip("no Chromium for Playwright on this machine")
    _fill(store, 650)
    late = replace(store.get_email("m000"), id="m999", subject="Statement late", received_at="2026-09-29T23:00:00+00:00")
    asked = []

    def on_offset(route):
        asked.append(route.request.url)
        if len(asked) == 1:
            store.upsert_email(late)
        route.continue_()

    with serving(web.create_app(settings, store)) as base, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=path)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/api/mail?*offset=*", on_offset)
        page.goto(f"{base}/app/folder/reference")
        page.wait_for_selector(".items li")
        page.click(".list-more button")
        page.wait_for_function("() => document.querySelectorAll('.items li').length === 400")
        page.click(".list-more button")
        page.wait_for_function("() => document.querySelectorAll('.items li').length >= 600")
        page.wait_for_timeout(1000)
        assert len(asked) < 5, asked
        assert len(set(asked)) == len(asked), "the same page was asked for twice"
        browser.close()
        assert errors == []
