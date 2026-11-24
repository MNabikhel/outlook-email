"""The offer to load a model again smaller after it ran out of memory, in a real browser: both chats and both Settings
pages. The answers are scripted (``answer_stream`` replaced) and the reload recorded (``step_back_context`` replaced),
so the pages are what is tested; tests/test_step_back_ask.py tests the events and routes themselves. Skipped without
Playwright's Chromium."""

from __future__ import annotations

import pytest

pytest.importorskip("playwright")

from controller_inbox import local_llm, model_fit, web  # noqa: E402
from liveserver import serving  # noqa: E402
from test_chat_time_browser import ABOUT, WIDE, _ask, _scripted  # noqa: E402

MODEL = "google/gemma-3-27b"
TEXT = (
    f"{MODEL} ran out of memory with a 16,384-token context. Reload it with 8,192? "
    "Long attachments are then read a section at a time."
)


def _offer(question: str, email_id: str = "") -> list:
    return [
        {"type": "sources", "sources": [], "mode": "model"},
        {"type": "mode", "mode": "lookup", "note": "The local model ran out of memory before it could answer."},
        {"type": "delta", "text": "Here's a straight lookup instead."},
        {"type": "step_back", "model": MODEL, "loaded": 16384, "size": 8192, "text": TEXT, "question": question, "email_id": email_id},
    ]


def _reloads(monkeypatch) -> list:
    """``step_back_context`` replaced: records the model and size the page asked for."""
    done: list = []

    def reload(_settings, key, size):
        done.append((key, size))
        return f"Loaded {key} again with an {size:,}-token context: 16,384 didn't fit in this computer's memory."

    monkeypatch.setattr(local_llm, "step_back_context", reload)
    return done


def test_the_chat_offers_the_reload_and_asks_again(settings, store, page, monkeypatch):
    asked = _scripted(monkeypatch, _offer("What is due?"), [{"type": "delta", "text": "The Northwind invoice is due Friday."}])
    done = _reloads(monkeypatch)
    with serving(web.create_app(settings, store)) as base:
        _ask(page, base, "What is due?")
        box = page.locator(".msg-step-back")
        box.wait_for()
        assert TEXT in box.inner_text()
        assert done == [], "nothing reloaded before the user says so"
        box.locator("button", has_text="Reload and ask again").click()
        page.wait_for_selector(".msg.bot:last-child .msg-text:has-text('due Friday')")
        assert done == [(MODEL, 8192)]
        assert asked == ["What is due?", "What is due?"] and ABOUT == [None, None]
        assert "Loaded google/gemma-3-27b again with an 8,192-token context" in page.inner_text(".msg-step-back")
        assert page.locator(".msg-step-back button").count() == 0


def test_keep_as_is_puts_the_offer_away_and_reloads_nothing(settings, store, page, monkeypatch):
    asked = _scripted(monkeypatch, _offer("What is due?"))
    done = _reloads(monkeypatch)
    with serving(web.create_app(settings, store)) as base:
        _ask(page, base, "What is due?")
        box = page.locator(".msg-step-back")
        box.wait_for()
        box.locator("button", has_text="Keep as is").click()
        page.wait_for_selector(".msg-step-back", state="detached")
        assert done == [] and asked == ["What is due?"]


def test_the_chat_says_why_the_reload_was_refused(settings, store, page, monkeypatch):
    asked = _scripted(monkeypatch, _offer("What is due?"))

    def refused(_settings, key, _size):
        raise local_llm.StepBackRefused(f"{key} is answering another request. Once it is done, try again.")

    monkeypatch.setattr(local_llm, "step_back_context", refused)
    with serving(web.create_app(settings, store)) as base:
        _ask(page, base, "What is due?")
        page.locator(".msg-step-back button", has_text="Reload and ask again").click()
        page.wait_for_selector(".step-back-status.error")
        assert "is answering another request" in page.inner_text(".step-back-status")
        assert asked == ["What is due?"], "not asked again"
        assert page.locator(".msg-step-back button", has_text="Reload and ask again").is_enabled()


def test_the_offer_is_shown_again_with_the_conversation(settings, store, page, monkeypatch):
    _scripted(monkeypatch, _offer("What is due?"))
    with serving(web.create_app(settings, store)) as base:
        _ask(page, base, "What is due?")
        page.locator(".msg-step-back").wait_for()
        page.reload()
        page.locator(".msg-step-back").wait_for()
        assert TEXT in page.inner_text(".msg-step-back")


def test_the_classic_chat_offers_the_reload_too(settings, store, page, monkeypatch):
    asked = _scripted(monkeypatch, _offer("What is due?"), [{"type": "delta", "text": "Due Friday."}], _offer("And the total?"))
    done = _reloads(monkeypatch)
    with serving(web.create_app(settings, store)) as base:
        page.set_viewport_size(WIDE)
        page.goto(f"{base}/chat/window")
        page.fill("#chat textarea", "What is due?")
        page.press("#chat textarea", "Enter")
        box = page.locator(".step-back")
        box.wait_for()
        assert TEXT in box.inner_text() and done == []
        box.locator("button", has_text="Reload and ask again").click()
        page.wait_for_selector(".msg:has-text('Due Friday.')")
        assert done == [(MODEL, 8192)] and asked == ["What is due?", "What is due?"]

        # Keep as is: put away, nothing reloaded.
        page.fill("#chat textarea", "And the total?")
        page.press("#chat textarea", "Enter")
        page.locator(".step-back button", has_text="Keep as is").last.wait_for()
        page.locator(".step-back button", has_text="Keep as is").last.click()
        page.wait_for_function("document.querySelectorAll('.step-back [data-action=step-back-keep]').length === 0")
        assert done == [(MODEL, 8192)]


def _offered_in_settings(monkeypatch) -> list:
    """Settings' context line with the offer until the reload is done."""
    done = _reloads(monkeypatch)
    real = model_fit.context_line

    def line(settings, model):
        found = real(settings, model)
        if done:
            return {**found, "shown": True, "step_back": None, "label": "8,192 tokens", "advice": "Ask CloseDesk reads long attachments a section at a time with this."}
        offer = {"model": MODEL, "loaded": 16384, "size": 8192, "text": TEXT}
        return {**found, "shown": True, "tokens": 16384, "label": "16,384 tokens", "tone": "short", "step_back": offer,
                "advice": f"{TEXT} Press Reload with 8,192 to have it answer again."}

    monkeypatch.setattr(model_fit, "context_line", line)
    return done


def test_settings_offers_the_reload(settings, store, page, monkeypatch):
    done = _offered_in_settings(monkeypatch)
    with serving(web.create_app(settings, store)) as base:
        page.set_viewport_size(WIDE)
        page.goto(f"{base}/app/settings#set-context")
        button = page.locator("#set-step-back")
        button.wait_for()
        assert button.inner_text().strip() == "Reload with 8,192"
        assert TEXT in page.inner_text("#set-context")
        assert done == []
        button.click()
        page.wait_for_selector("#set-context .set-status.ok")
        assert done == [(MODEL, 8192)]
        assert "Loaded google/gemma-3-27b again" in page.inner_text("#set-context")
        assert page.locator("#set-step-back").count() == 0


def test_the_setup_page_offers_the_reload(settings, store, page, monkeypatch):
    done = _offered_in_settings(monkeypatch)
    with serving(web.create_app(settings, store)) as base:
        page.set_viewport_size(WIDE)
        page.goto(f"{base}/settings")
        button = page.locator("form[data-step-back] button")
        button.wait_for()
        assert button.inner_text().strip() == "Reload with 8,192"
        assert TEXT in page.inner_text("#context-line")
        button.click()
        page.wait_for_selector("[data-step-back-said]")
        assert done == [(MODEL, 8192)]
        assert "Loaded google/gemma-3-27b again" in page.inner_text("[data-step-back-said]")
        assert page.locator("form[data-step-back]").count() == 0


def test_the_setup_page_says_it_is_reloading_and_takes_one_press(settings, store, page, monkeypatch):
    import threading

    done = _offered_in_settings(monkeypatch)
    recorded = local_llm.step_back_context
    let_go = threading.Event()

    def slow(settings_, key, size):  # LM Studio takes its time
        let_go.wait(20)
        return recorded(settings_, key, size)

    monkeypatch.setattr(local_llm, "step_back_context", slow)
    with serving(web.create_app(settings, store)) as base:
        page.set_viewport_size(WIDE)
        page.goto(f"{base}/settings")
        button = page.locator("form[data-step-back] button")
        button.wait_for()
        button.click(no_wait_after=True)
        # The form is sent and its page not back yet: the button says what is happening and can't be pressed again.
        assert page.evaluate("document.querySelector('form[data-step-back] button').disabled")
        assert page.evaluate("document.querySelector('form[data-step-back] button').textContent.trim()") == "Reloading in LM Studio…"
        let_go.set()
        page.wait_for_selector("[data-step-back-said]")
        assert done == [(MODEL, 8192)]


def _slow_reloads(monkeypatch):
    """``step_back_context`` replaced by one that waits until let go, as LM Studio's reload takes minutes."""
    import threading

    done = _reloads(monkeypatch)
    recorded = local_llm.step_back_context
    let_go = threading.Event()
    started = threading.Event()

    def slow(settings_, key, size):
        started.set()
        let_go.wait(20)
        return recorded(settings_, key, size)

    monkeypatch.setattr(local_llm, "step_back_context", slow)
    return done, started, let_go


def test_the_chat_takes_no_other_question_while_it_reloads(settings, store, page, monkeypatch):
    asked = _scripted(monkeypatch, _offer("What is due?"), [{"type": "delta", "text": "The Northwind invoice is due Friday."}])
    done, started, let_go = _slow_reloads(monkeypatch)
    with serving(web.create_app(settings, store)) as base:
        try:
            _ask(page, base, "What is due?")
            page.locator(".msg-step-back button", has_text="Reload and ask again").click()
            assert started.wait(10)
            page.fill("#chat textarea", "Who sent it?")
            page.press("#chat textarea", "Enter")
            page.wait_for_timeout(300)
        finally:
            let_go.set()
        page.wait_for_selector(".msg.bot:last-child .msg-text:has-text('due Friday')")
        assert asked == ["What is due?", "What is due?"], "the question chosen is the one asked again"
        assert done == [(MODEL, 8192)]
        assert page.input_value("#chat textarea") == "Who sent it?", "the other question is kept to send once it is done"


def test_the_classic_chat_takes_no_other_question_while_it_reloads(settings, store, page, monkeypatch):
    asked = _scripted(monkeypatch, _offer("What is due?"), [{"type": "delta", "text": "Due Friday."}])
    done, started, let_go = _slow_reloads(monkeypatch)
    with serving(web.create_app(settings, store)) as base:
        page.set_viewport_size(WIDE)
        page.goto(f"{base}/chat/window")
        page.fill("#chat textarea", "What is due?")
        page.press("#chat textarea", "Enter")
        try:
            page.locator(".step-back button", has_text="Reload and ask again").click()
            assert started.wait(10)
            assert page.locator("#chat button[type='submit']").is_disabled()
            page.fill("#chat textarea", "Who sent it?")
            page.press("#chat textarea", "Enter")
            page.wait_for_timeout(300)
        finally:
            let_go.set()
        page.wait_for_selector(".msg:has-text('Due Friday.')")
        assert asked == ["What is due?", "What is due?"], "the question chosen is the one asked again"
        assert done == [(MODEL, 8192)]
        assert page.input_value("#chat textarea") == "Who sent it?"
