"""The page's own script (static/app.js) in a real browser, one block per fixed bug.

Skipped where Playwright or its Chromium isn't installed. The server runs in this process, so tests
fake what they need on it as the other web tests do.
"""

from __future__ import annotations

import time
from email.message import EmailMessage

import pytest

pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright  # noqa: E402

from controller_inbox import assistant, chats, web  # noqa: E402
from controller_inbox.folder_mail import ingest_folder  # noqa: E402
from controller_inbox.web import create_app  # noqa: E402
from liveserver import chromium_path, serving  # noqa: E402

CHROMIUM = chromium_path()
ROSTER = b"Employee,Department,Manager\nJonathan Reyes,,Priya Raman\nLi Wei,Finance,Dana Cole\n"
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
    context.set_default_timeout(15000)
    context.errors = []
    context.on("weberror", lambda error: context.errors.append(str(error.error)))
    yield context
    context.close()


@pytest.fixture
def page(context):
    return context.new_page()


def _saved_answer(store, text: str, sources: list[dict], chat_id: str = "") -> str:
    """A conversation whose answer is already written, as the model would have streamed it."""
    chat_id = chat_id or chats.new_id()
    store.create_chat(chat_id, "A saved answer")
    store.add_chat_turn(chat_id, "user", "A question")
    store.add_chat_turn(chat_id, "assistant", text, {"sources": sources, "mode": "model"})
    return chat_id


def _open_saved(page, url: str, chat_id: str):
    """The page with the chat panel open on the saved conversation; gives the answer's bubble."""
    page.goto(url)
    page.evaluate("id => localStorage.setItem('closedesk-chat-id', id)", chat_id)
    page.goto(url)
    if page.is_visible("#chat-toggle"):
        page.click("#chat-toggle")
    page.wait_for_function(ANSWERED, arg=2)
    return page.query_selector_all("#chat .msg.assistant")[-1]


def _links(bubble) -> list[tuple[str, str]]:
    """The links in the answer itself (not the list of sources under it)."""
    return [(a.inner_text(), a.get_attribute("href")) for a in bubble.query_selector_all(":scope > div a")]


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


# 4. A citation of a CSV file opens it at the cited cell (a CSV's sheet is named after the file).


def test_a_csv_citation_opens_the_cited_cell(site, page, context, store, settings):
    chat_id = chats.new_id()
    store.create_chat(chat_id)
    chats.add_file(store, settings, chat_id, "staff list.csv", "text/csv", ROSTER)
    sources = assistant.source_cards([chats.chat_mail(store, chat_id)], settings)
    text = (
        'Li Wei is in Finance (staff list.csv, sheet "staff list.csv", cell B3) [1].\n'
        "Jonathan Reyes has no department ('staff list.csv'!B2) [1]."
    )
    bubble = _open_saved(page, f"{site}/", _saved_answer(store, text, sources, chat_id))
    base = f"/inbox/chat-{chat_id}/files/1"
    links = _links(bubble)
    assert [name for name, _href in links] == ["staff list.csv", "staff list.csv", "[1]", "staff list.csv", "[1]"]
    assert [href for _name, href in links] == [f"{base}?at=B3"] * 3 + [f"{base}?at=B2"] * 2
    cited = context.new_page()
    cited.goto(site + base + "?at=B3")
    assert "B3 (Department): Finance" in cited.inner_text(".file-part.target mark.cited")
    assert context.errors == []


# 5. A file name with [n] in it stays one link: the citation pass doesn't run inside the file's link.


def test_a_file_name_with_a_bracketed_number_keeps_the_answer_whole(site, page, context, store, settings):
    hostile = 'inv<img src=x onerror="alert(1)">.pdf'
    message = EmailMessage()
    message["Subject"] = "Invoice and report"
    message["From"] = "Eve <eve@taz.com>"
    message["Date"] = "Mon, 28 Sep 2026 14:30:00 +0000"
    message.set_content("Both attached.")
    message.add_attachment(b"%PDF-1.4 not really", maintype="application", subtype="pdf", filename=hostile)
    message.add_attachment(b"a,b\n1,2\n", maintype="text", subtype="csv", filename="report[1].csv")
    (settings.inbox_incoming / "eve.eml").write_bytes(bytes(message))
    [email] = ingest_folder(store, settings)
    assert [att.filename for att in email.attachments] == [hostile, "report[1].csv"]
    text = f"Eve sent report[1].csv with the totals [1]. The invoice is {hostile} [1]."
    dialogs: list[str] = []
    page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
    bubble = _open_saved(page, f"{site}/", _saved_answer(store, text, assistant.source_cards([email], settings)))

    links = _links(bubble)
    assert [name for name, _href in links] == ["report[1].csv", "[1]", hostile, "[1]"]
    assert all(href.startswith(f"/inbox/{email.id}/files/") for _name, href in links)
    assert bubble.query_selector_all("a a") == [] and bubble.query_selector_all("img") == []
    assert [a.get_attribute("title") for a in bubble.query_selector_all(":scope > div a.cite-name")] == [
        "Open report[1].csv",
        f"Open {hostile}",
    ]
    assert bubble.query_selector(":scope > div").inner_text() == text
    assert dialogs == [] and context.errors == []


# 8. "Open in Outlook" and "Open workbook" say why when the server refuses (LAN mode), not that it's opening.


def test_open_buttons_say_why_when_the_server_refuses(site, page, context, mail, monkeypatch):
    def toast_says(text: str) -> None:
        page.wait_for_function("text => document.querySelector('#toast').textContent === text", arg=text)

    budget = mail["Q4 budget draft"]
    page.goto(f"{site}/inbox/{budget.id}")
    page.click(".detail [data-action='open-original']")
    toast_says("Opening budget.msg in your mail app.")

    monkeypatch.setattr(web, "LOCAL_CLIENTS", set())  # as if the page were on another computer
    page.click(".detail [data-action='open-original']")
    toast_says("Files open on the computer running CloseDesk only.")
    assert page.url == f"{site}/inbox/{budget.id}"
    page.goto(f"{site}/coding")
    page.click("[data-action='open-codes']")
    toast_says("Files open on the computer running CloseDesk only.")
    assert context.errors == []


# 9. A citation clicked in the pop-out chat opens beside it, and the pop-out keeps the conversation.


def test_a_citation_in_the_pop_out_opens_in_the_window_it_came_from(site, page, context, store, mail):
    budget = mail["Q4 budget draft"]
    chat_id = _saved_answer(store, "Maya Chen sent the Q4 budget draft [1].", assistant.source_cards([budget]))
    _open_saved(page, f"{site}/inbox", chat_id)
    with context.expect_page() as popped:
        page.click("[data-action='popout-chat']")
    popout = popped.value
    popout.wait_for_function(ANSWERED, arg=2)
    cite = "#chat .msg.assistant > div a.cite"
    assert popout.get_attribute(cite, "href") == f"/inbox/{budget.id}"

    popout.click(cite)
    page.wait_for_url(f"{site}/inbox/{budget.id}")
    assert popout.url == f"{site}/chat/window"

    page.close()  # the window it came from is gone: a new tab
    with context.expect_page() as opened:
        popout.click(cite)
    opened.value.wait_for_url(f"{site}/inbox/{budget.id}")
    assert popout.url == f"{site}/chat/window" and len(popout.query_selector_all("#chat .msg")) == 2
    assert context.errors == []


# 10. Opening the chat while a question waits for the saved conversation to load still shows the answer.


def test_opening_the_chat_while_a_question_waits_for_its_conversation_keeps_the_answer(site, page, context, store, mail, monkeypatch):
    def slow_model(*_args, **_kwargs):
        for i in range(12):
            yield f"part{i} "
            time.sleep(0.02)

    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "needs_more_context", lambda _s: False)
    monkeypatch.setattr(assistant, "stream_text", slow_model)
    budget = mail["Q4 budget draft"]
    chat_id = _saved_answer(store, "An earlier answer.", [])
    # The saved conversation takes half a second to load (a busy disk, or the page used from another computer).
    page.add_init_script(
        """(() => {
          const real = window.fetch;
          window.fetch = (url, options) => /\\/chats\\/[0-9a-f]+$/.test(String(url))
            ? new Promise((done) => setTimeout(done, 500)).then(() => real(url, options))
            : real(url, options);
        })()"""
    )
    page.goto(f"{site}/inbox/{budget.id}")
    page.evaluate("id => localStorage.setItem('closedesk-chat-id', id)", chat_id)
    page.goto(f"{site}/inbox/{budget.id}")
    # "Ask about this email", then the chat button while the conversation is still loading.
    page.evaluate(
        """() => {
          document.querySelector(".detail [data-action='ask']").click();
          setTimeout(() => document.querySelector("#chat-toggle").click(), 200);
        }"""
    )
    page.wait_for_function(ANSWERED, arg=4, timeout=20000)
    messages = _chat_messages(page)
    assert messages[2] == "msg user | What does this email need from me?"
    assert "part11" in messages[3] and "Stopped" not in messages[3]
    turns = store.chat_turns(chat_id)
    assert [turn["role"] for turn in turns] == ["user", "assistant"] * 2
    assert turns[3]["text"].startswith("part0 ") and "Stopped" not in turns[3]["text"] and not turns[3].get("failed")
    assert not page.is_disabled("#chat button[type=submit]")
    assert context.errors == []
