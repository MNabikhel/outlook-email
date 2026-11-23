"""A model's reasoning in Ask CloseDesk, in a real browser: a small box that fills as it streams, closes when the
answer starts ("Thought for 40 s"), and shows the reasoning as text, never as HTML. Scripted answers, as in
tests/test_chat_time_browser.py. Skipped without Playwright's Chromium."""

from __future__ import annotations

import re

import pytest

pytest.importorskip("playwright")

from controller_inbox import web  # noqa: E402
from liveserver import serving  # noqa: E402
from test_chat_time_browser import WIDE, _ask, _scripted  # noqa: E402


# The thinking, as it comes ------------------------------------------------------------------------------------------

TRICK = "<img src=x onerror=\"window.__hacked = 1\"> is not markup here. "


def _thinking_script():
    return [
        {"type": "sources", "sources": [], "mode": "model"},
        {"type": "progress", "state": "reading", "text": "Reading 1 email", "tokens": 900, "seconds": None, "about": ""},
        {"type": "progress", "state": "thinking", "text": "Thinking"},
        {"type": "thinking", "text": "The user asks what is due. "}, 0.8,
        {"type": "thinking", "text": TRICK}, 0.8,
        {"type": "thinking", "text": "The invoice says Friday."}, 1.2,
        {"type": "progress", "state": "writing", "text": "Writing the answer"},
        {"type": "delta", "text": "The Northwind invoice is due Friday."},
    ]


def test_the_thinking_box_fills_then_closes_when_the_answer_starts(settings, store, page, monkeypatch):
    _scripted(monkeypatch, _thinking_script())
    with serving(web.create_app(settings, store)) as base:
        _ask(page, base, "What is due?")
        box = page.locator(".msg-thinking.live")
        box.wait_for()
        assert box.locator("summary").inner_text().startswith("Thinking")
        assert box.evaluate("el => el.open"), "open while it thinks, showing the last few lines"
        page.wait_for_selector(".msg-thinking.live .think-text:has-text('is not markup here')")
        # Model output is text: shown as typed, never run.
        assert TRICK.strip() in page.inner_text(".msg-thinking .think-text")
        assert page.locator(".msg-thinking img").count() == 0 and page.evaluate("window.__hacked") is None
        # The answer starts: the box closes and says how long the model thought.
        page.wait_for_selector(".msg.bot .msg-text:has-text('due Friday')")
        done = page.locator(".msg-thinking")
        assert not done.evaluate("el => el.open") and "live" not in (done.get_attribute("class") or "")
        assert re.fullmatch(r"Thought for \d+ s", done.locator("summary").inner_text().strip())
        assert "Friday" in page.inner_text(".msg.bot .msg-text") and "The user asks" not in page.inner_text(".msg.bot .msg-text")
        done.locator("summary").click()
        assert done.evaluate("el => el.open") and "The invoice says Friday." in done.inner_text()


def test_the_classic_chat_shows_the_thinking_escaped(settings, store, page, monkeypatch):
    _scripted(monkeypatch, _thinking_script())
    with serving(web.create_app(settings, store)) as base:
        page.set_viewport_size(WIDE)
        page.goto(f"{base}/chat/window")
        page.fill("#chat textarea", "What is due?")
        page.press("#chat textarea", "Enter")
        page.wait_for_selector(".msg-thinking .think-text:has-text('is not markup here')")
        assert page.locator(".msg-thinking img").count() == 0 and page.evaluate("window.__hacked") is None
        page.wait_for_selector(".msg:has-text('due Friday')")
        assert not page.locator(".msg-thinking").evaluate("el => el.open")
        assert re.fullmatch(r"Thought for \d+ s", page.locator(".msg-thinking summary").inner_text().strip())


def _long_thinking(pause: float = 0.0):
    lines = [{"type": "thinking", "text": f"Step {n}: checking the invoice dates and the amounts again.\n"} for n in range(1, 25)]
    return [
        {"type": "sources", "sources": [], "mode": "model"},
        {"type": "progress", "state": "thinking", "text": "Thinking"},
        *lines,
        {"type": "thinking", "text": "The last line of the reasoning."}, 0.6,
        *([pause] if pause else [{"type": "delta", "text": "The Northwind invoice is due Friday."}]),
    ]


def test_the_classic_chat_shows_the_latest_reasoning_and_all_of_it_once_done(settings, store, page, monkeypatch):
    """The classic page clipped every thinking box to its first three lines, live or done, with no way to scroll:
    the reasoning looked frozen while it came, and the rest couldn't be read after."""
    _scripted(monkeypatch, _long_thinking())
    with serving(web.create_app(settings, store)) as base:
        page.set_viewport_size(WIDE)
        page.goto(f"{base}/chat/window")
        page.fill("#chat textarea", "What is due?")
        page.press("#chat textarea", "Enter")
        page.wait_for_selector(".msg-thinking.live .think-text:has-text('The last line of the reasoning.')")
        live = page.locator(".msg-thinking.live .think-text")
        assert live.evaluate("el => el.scrollTop > 0 && el.scrollTop + el.clientHeight >= el.scrollHeight - 2"), "the latest lines show"
        page.wait_for_selector(".msg:has-text('due Friday')")
        done = page.locator(".msg-thinking")
        assert "live" not in (done.get_attribute("class") or "")
        done.locator("summary").click()
        text = done.locator(".think-text")
        assert text.evaluate("el => getComputedStyle(el).overflowY") == "auto", "the whole reasoning can be scrolled"
        assert text.evaluate("el => el.clientHeight") > live_height(page)


def live_height(page) -> float:
    """About three lines of the thinking box's text, in pixels."""
    return page.evaluate("parseFloat(getComputedStyle(document.querySelector('.msg-thinking .think-text')).fontSize) * 4.6")


def test_stopped_while_it_thinks_the_box_keeps_how_long_it_thought(settings, store, page, monkeypatch):
    """Stopped while the model thought, the box never got an end: each later repaint (the next question) said it
    had thought for longer, up to the moment of that repaint."""
    _scripted(monkeypatch, _long_thinking(pause=8), [{"type": "delta", "text": "Nothing else is due."}])
    with serving(web.create_app(settings, store)) as base:
        _ask(page, base, "What is due?")
        page.wait_for_selector(".msg-thinking.live .think-text:has-text('The last line of the reasoning.')")
        page.click("[aria-label='Stop answering']")
        page.wait_for_selector(".msg-stopped")
        first = page.locator(".msg-thinking").first
        said = first.locator("summary").inner_text().strip()
        assert re.fullmatch(r"Thought for \d+ s", said) and not first.evaluate("el => el.open")
        page.wait_for_timeout(2500)
        page.fill("#chat textarea", "Anything else?")
        page.press("#chat textarea", "Enter")
        page.wait_for_selector(".msg.bot .msg-text:has-text('Nothing else is due')")
        assert page.locator(".msg-thinking").first.locator("summary").inner_text().strip() == said
