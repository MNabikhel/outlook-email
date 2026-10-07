"""The page's own script (static/app.js) in a real browser, one block per fixed bug.

Skipped where Playwright or its Chromium isn't installed. The server runs in this process, so tests
fake what they need on it as the other web tests do.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright  # noqa: E402

from controller_inbox import web  # noqa: E402
from controller_inbox.web import create_app  # noqa: E402
from liveserver import chromium_path, serving  # noqa: E402

CHROMIUM = chromium_path()
pytestmark = pytest.mark.skipif(CHROMIUM is None, reason="no Chromium for Playwright on this machine")

# The chat log once the last answer has finished streaming.
ANSWERED = """n => {
  const msgs = document.querySelectorAll('#chat .chat-log .msg');
  const last = msgs[msgs.length - 1];
  return msgs.length >= n && last.matches('.assistant:not(.pending)');
}"""


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        yield browser
        browser.close()


@pytest.fixture
def site(settings, store, mail, monkeypatch):
    monkeypatch.setattr(web, "open_file", lambda _path: None)
    with serving(create_app(settings, store)) as base:
        yield base


@pytest.fixture
def context(browser):
    context = browser.new_context()
    context.errors = []
    context.on("weberror", lambda error: context.errors.append(str(error.error)))
    yield context
    context.close()


@pytest.fixture
def page(context):
    return context.new_page()


def _chat_messages(page) -> list[str]:
    return page.eval_on_selector_all("#chat .chat-log .msg", "els => els.map(e => e.className + ' | ' + e.innerText)")


# 1. "Ask about this email" and Summarize answer even when the chat panel wasn't opened on the page yet.


def test_asking_from_an_email_page_shows_the_answer_and_keeps_the_conversation(site, page, context, store, mail):
    budget = mail["Q4 budget draft"]
    page.goto(f"{site}/inbox/{budget.id}")
    page.click(".detail [data-action='ask']")
    page.wait_for_function(ANSWERED, arg=2, timeout=20000)
    messages = _chat_messages(page)
    assert messages[0] == "msg user | What does this email need from me?"
    assert "Thinking" not in messages[1] and len(messages[1]) > 30
    chat_id = page.evaluate("localStorage.getItem('closedesk-chat-id')")
    assert chat_id, "the conversation is remembered for the next page"
    assert [turn["role"] for turn in store.chat_turns(chat_id)] == ["user", "assistant"]

    # A new page has the saved conversation to load first: Summarize answers after it.
    page.goto(f"{site}/inbox/{budget.id}")
    page.click(".detail [data-action='ask-file'][data-mode='summary']")
    page.wait_for_function(ANSWERED, arg=4, timeout=20000)
    messages = _chat_messages(page)
    assert len(messages) == 4 and messages[2].startswith("msg user | Summarize the attachment")
    assert page.evaluate("localStorage.getItem('closedesk-chat-id')") == chat_id
    assert [turn["role"] for turn in store.chat_turns(chat_id)] == ["user", "assistant"] * 2
    assert not page.is_disabled("#chat button[type=submit]")
    assert context.errors == []
