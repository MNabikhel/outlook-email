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

    def fake(_store, _settings, question, **kw):
        asked.append(question)
        ABOUT.append(kw.get("email_id"))
        for event in queue.pop(0) if queue else [{"type": "delta", "text": "No more answers scripted."}]:
            if isinstance(event, (int, float)):
                time.sleep(event)
            else:
                yield event
        yield {"type": "done"}

    monkeypatch.setattr(web, "answer_stream", fake)
    ABOUT.clear()
    return asked


ABOUT: list[str | None] = []  # the email each scripted question was about, in order


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


def _offer_without_an_email() -> list:
    offer = {
        "type": "more_time", "model": MODEL, "minutes": 5, "suggest": 9, "tokens": 6000, "question": "What needs a reply?",
        "email_id": "", "text": "The local model needed more time than CloseDesk gives it (5 minutes).",
    }
    return [{"type": "delta", "text": "Here's a straight lookup instead."}, offer]


def test_asked_again_a_question_about_all_mail_stays_about_all_mail(settings, loaded, page, monkeypatch):
    """Asked with no email open, then an email opened before "Save and ask again": the question went again about
    that email, and was answered about it instead of all the mail."""
    asked = _scripted(monkeypatch, _offer_without_an_email(), [{"type": "delta", "text": "Two need a reply."}])
    with serving(web.create_app(settings, loaded)) as base:
        _ask(page, base, "What needs a reply?")
        form = page.locator(".msg-more-time")
        form.wait_for()
        page.click("#list a[href^='/app/mail/demo-pbc?']")
        page.wait_for_selector(".scope-chip")
        form.locator("button", has_text="Save and ask again").click()
        page.wait_for_selector(".msg.bot:last-child .msg-text:has-text('Two need a reply')")
    assert asked == ["What needs a reply?", "What needs a reply?"] and ABOUT == [None, None]


def test_asked_again_in_the_classic_chat_a_question_about_all_mail_stays_about_all_mail(settings, loaded, page, monkeypatch):
    asked = _scripted(monkeypatch, _offer_without_an_email(), [{"type": "delta", "text": "Two need a reply."}])
    with serving(web.create_app(settings, loaded)) as base:
        page.set_viewport_size(WIDE)
        page.goto(f"{base}/chat/window")
        page.fill("#chat textarea", "What needs a reply?")
        page.press("#chat textarea", "Enter")
        form = page.locator("form.more-time")
        form.wait_for()
        page.evaluate("document.body.dataset.emailId = 'demo-pbc'")  # an email opened meanwhile
        form.locator("button").click()
        page.wait_for_selector(".msg:has-text('Two need a reply.')")
    assert asked == ["What needs a reply?", "What needs a reply?"] and ABOUT == [None, None]


# The estimate while the model reads ---------------------------------------------------------------------------------


def test_the_estimate_fills_a_bar_then_says_it_is_taking_longer_then_writing(settings, store, page, monkeypatch):
    reading = {"type": "progress", "state": "reading", "text": "Reading 3 emails and 2 files", "tokens": 6000, "seconds": 1.5, "about": "about 5 seconds"}
    _scripted(
        monkeypatch,
        [{"type": "sources", "sources": [], "mode": "model"}, reading, 3.5,
         {"type": "progress", "state": "writing", "text": "Writing the answer"}, 1.0,
         {"type": "delta", "text": "The Northwind invoice is due Friday."}],
    )
    with serving(web.create_app(settings, store)) as base:
        _ask(page, base, "What is due?")
        line = page.locator(".msg-progress")
        line.wait_for()
        assert line.inner_text().startswith("Reading 3 emails and 2 files… about 5 seconds")
        bar = line.locator(".progress-bar")
        assert "busy" not in (bar.get_attribute("class") or "")
        assert "animation-duration:1.5s" in (bar.locator("i").get_attribute("style") or "").replace(" ", "")
        # Past the estimate: said, and the bar keeps moving instead of sitting full.
        page.wait_for_selector(".msg-progress.late", timeout=4000)
        assert "Taking longer than usual…" in page.inner_text(".msg-progress")
        assert "busy" in page.get_attribute(".msg-progress .progress-bar", "class")
        page.wait_for_selector(".msg.bot .typing:has-text('Writing the answer…')", timeout=5000)
        page.wait_for_selector(".msg.bot .msg-text:has-text('due Friday')")
        assert page.locator(".msg-progress").count() == 0


def test_with_no_estimate_the_chat_says_what_it_reads_and_shows_a_moving_bar(settings, store, page, monkeypatch):
    reading = {"type": "progress", "state": "reading", "text": "Reading 1 email", "tokens": 900, "seconds": None, "about": ""}
    _scripted(monkeypatch, [reading, 1.5, {"type": "delta", "text": "Nothing is due."}])
    with serving(web.create_app(settings, store)) as base:
        _ask(page, base, "What is due?")
        line = page.locator(".msg-progress")
        line.wait_for()
        assert line.inner_text().strip() == "Reading 1 email…"
        assert "busy" in line.locator(".progress-bar").get_attribute("class")
        page.wait_for_selector(".msg.bot .msg-text:has-text('Nothing is due.')")


def test_the_classic_chat_says_what_it_reads(settings, store, page, monkeypatch):
    reading = {"type": "progress", "state": "reading", "text": "Reading 2 emails", "tokens": 3000, "seconds": 60, "about": "about 1 minute"}
    _scripted(monkeypatch, [reading, 1.5, {"type": "delta", "text": "Done."}])
    with serving(web.create_app(settings, store)) as base:
        page.set_viewport_size(WIDE)
        page.goto(f"{base}/chat/window")
        page.fill("#chat textarea", "What is due?")
        page.press("#chat textarea", "Enter")
        page.wait_for_selector(".msg .typing:has-text('Reading 2 emails… about 1 minute')")
        page.wait_for_selector(".msg:has-text('Done.')")
