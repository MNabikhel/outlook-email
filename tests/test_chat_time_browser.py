"""Ask CloseDesk with a big, slow model, in a real browser: the offer of more time, the estimate while the model
reads, and its thinking as it comes. The answers are scripted (``answer_stream`` replaced) so the page is what is
tested; tests/test_chat_time.py tests the events themselves. Skipped without Playwright's Chromium."""

from __future__ import annotations

import time

import pytest

pytest.importorskip("playwright")

from controller_inbox import model_fit, web  # noqa: E402
from liveserver import serving  # noqa: E402

WIDE = {"width": 1280, "height": 900}
MODEL = "qwen/qwen3-32b"


def _scripted(monkeypatch, *answers):
    """``answer_stream`` replaced: each question gets the next list of events (a number in it is a pause in seconds).
    Returns the questions asked, in order."""
    asked: list[str] = []
    queue = list(answers)

    def fake(_store, _settings, question, **_kw):
        asked.append(question)
        for event in queue.pop(0) if queue else [{"type": "delta", "text": "No more answers scripted."}]:
            if isinstance(event, (int, float)):
                time.sleep(event)
            else:
                yield event
        yield {"type": "done"}

    monkeypatch.setattr(web, "answer_stream", fake)
    return asked


def _ask(page, base: str, question: str) -> None:
    page.set_viewport_size(WIDE)
    page.goto(f"{base}/app/all")
    page.click("#chat-btn")
    page.fill("#chat textarea", question)
    page.press("#chat textarea", "Enter")


def test_out_of_time_offers_more_minutes_and_asks_again(settings, store, page, monkeypatch):
    offer = {
        "type": "more_time", "model": MODEL, "minutes": 5, "suggest": 9, "tokens": 6000, "question": "What is due?", "email_id": "",
        "text": "The local model needed more time than CloseDesk gives it (5 minutes). qwen3-32b usually needs about 9 minutes for a question like this on this computer.",
    }
    asked = _scripted(
        monkeypatch,
        [{"type": "sources", "sources": [], "mode": "model"},
         {"type": "mode", "mode": "lookup", "note": "The local model needed more time than CloseDesk gives it, so this answer comes from looking things up."},
         {"type": "delta", "text": "Here's a straight lookup instead."}, offer],
        [{"type": "sources", "sources": [], "mode": "model"}, {"type": "delta", "text": "The Northwind invoice is due Friday."}],
    )
    with serving(web.create_app(settings, store)) as base:
        _ask(page, base, "What is due?")
        form = page.locator(".msg-more-time")
        form.wait_for()
        assert "qwen3-32b usually needs about 9 minutes" in form.inner_text()
        box = form.locator("input[type=number]")
        assert box.input_value() == "9", "prefilled with the suggestion"
        # Nonsense is refused with a plain message, and the question isn't asked again.
        box.fill("500")
        form.locator("button", has_text="Save and ask again").click()
        page.wait_for_selector(".more-time-status.error")
        assert "Type a number of minutes" in page.inner_text(".more-time-status") and asked == ["What is due?"]
        # A number: saved for the model, and the same question asked again in the same conversation.
        page.locator(".msg-more-time input[type=number]").fill("15")
        page.locator(".msg-more-time button", has_text="Save and ask again").click()
        page.wait_for_selector(".msg.bot:last-child .msg-text:has-text('due Friday')")
        assert asked == ["What is due?", "What is due?"]
        assert model_fit.chosen_wait(store, MODEL) == 900
        assert "15 minutes" in page.inner_text(".msg-more-time .more-time-status")


def test_the_classic_chat_offers_more_time_too(settings, store, page, monkeypatch):
    offer = {
        "type": "more_time", "model": MODEL, "minutes": 5, "suggest": 8, "tokens": 6000, "question": "What is due?", "email_id": "",
        "text": "The local model needed more time than CloseDesk gives it (5 minutes). qwen3-32b usually needs about 8 minutes for a question like this on this computer.",
    }
    asked = _scripted(
        monkeypatch,
        [{"type": "delta", "text": "Here's a straight lookup instead."}, offer],
        [{"type": "delta", "text": "Due Friday."}],
    )
    with serving(web.create_app(settings, store)) as base:
        page.set_viewport_size(WIDE)
        page.goto(f"{base}/chat/window")
        page.fill("#chat textarea", "What is due?")
        page.press("#chat textarea", "Enter")
        form = page.locator("form.more-time")
        form.wait_for()
        assert form.locator("input").input_value() == "8"
        form.locator("input").fill("10")
        form.locator("button").click()
        page.wait_for_selector(".msg:has-text('Due Friday.')")
        assert asked == ["What is due?", "What is due?"] and model_fit.chosen_wait(store, MODEL) == 600
