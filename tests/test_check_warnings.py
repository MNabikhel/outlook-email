"""Which of an answer's checks are warnings. The workspace chat listed every check in a green box with a tick, "$12,480
isn't in the emails or files I read; check it before relying on it." too, which reads as "checked and right". Each
check now says whether it is a warning (a figure to check before relying on it) or a correction the check made,
decided by answer_check, which makes them; a conversation saved before then has its plain checks marked the same way
when it is opened. tests/test_check_warnings_browser.py shows them on the page."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from controller_inbox import answer_check, assistant, chats, vision, web
from controller_inbox.answer_check import review
from helpers import PAGE
from test_answer_check import _review
from test_vision import BALANCE, MODEL, OCR_TEXT

NOT_FOUND = "$12,480 isn't in the emails or files I read; check it before relying on it."
CORRECTED = "Corrected 31,200 to 31,500 (115,500 − 84,000)."


def _marks(result) -> list[tuple[str, bool]]:
    return [(item["text"], item["warning"]) for item in answer_check.check_items(result.checks)]


def test_a_correction_is_a_confirmation_and_a_figure_to_check_is_a_warning():
    invoice = "Net: $9,000\nVAT: $1,750\nShipping: $50\nTotal due: $10,800"
    # A worked-out figure recomputed and put right.
    assert _marks(review("Net $9,000 plus VAT $1,750 is a total of $10,700.", material=[], files=[("inv.pdf", invoice)])) == [
        ("Corrected $10,700 to $10,750 ($9,000 + $1,750).", False)
    ]
    # A figure that isn't in what was read.
    assert _marks(review("Travel is $12,480.", material=[], files=[("inv.pdf", invoice)])) == [(NOT_FOUND, True)]
    # A page and a cell put right.
    assert _marks(_review("FY26 Audit.pdf page 12 says 3 vendor bank-detail changes in July were approved without a call-back.")) == [
        ("Corrected the page for FY26 Audit.pdf: that is on page 17, not page 12.", False)
    ]
    assert _marks(_review("Marketing's change of 31,500 is in Q4 budget.xlsx cell D3.")) == [
        ("Corrected the cell for 31,500 in Q4 budget.xlsx: it is in Summary!D2, not D3.", False)
    ]
    # A figure cited to the wrong file.
    assert _marks(_review("[1] · page 2 · Section 2.25: Marketing's Q4 budget is 115,500.")) == [
        ("115,500 is in Q4 budget.xlsx, not FY26 Audit.pdf: check where that figure comes from.", True)
    ]
    # A figure only the vision model read.
    page = vision.merged_page(OCR_TEXT, BALANCE, vision.compare(OCR_TEXT, vision.page_text(BALANCE)), first_name="OCR", model=MODEL)
    assert _marks(review("Receivables were $30,250 [1].", material=[], files=[("balance sheet.pdf", page)])) == [
        ("$30,250 was read from the page by the vision model only (OCR read 30,256): check it against the file.", True)
    ]


def test_the_chat_says_which_checks_are_warnings(store, settings, mail, monkeypatch):
    budget = mail["Q4 budget draft"]
    answer = "Marketing went from 84,000 to 115,500, an increase of 31,200. Travel is $97,250."

    def model(ws, question, state, **_kwargs):
        state["wrote"], state["text"] = True, answer
        yield {"type": "delta", "text": answer}

    monkeypatch.setattr(assistant, "llm_active", lambda _settings: True)
    monkeypatch.setattr(assistant, "needs_more_context", lambda _settings: False)
    monkeypatch.setattr(assistant, "_model_answer", model)
    events = list(assistant.answer_stream(store, settings, "Did Marketing go from 84,000 to 115,500?", email_id=budget.id))
    assert next(e["items"] for e in events if e["type"] == "check") == [
        {"text": CORRECTED, "warning": False},
        {"text": "$97,250 isn't in the emails or files I read; check it before relying on it.", "warning": True},
    ]


def test_a_conversation_keeps_its_checks_marked_and_one_saved_before_has_them_marked_when_opened(store, settings, mail, monkeypatch):
    budget = mail["Q4 budget draft"]
    answer = "Marketing went from 84,000 to 115,500, an increase of 31,200. Travel is $12,480."

    def model(ws, question, state, **_kwargs):
        state["wrote"], state["text"] = True, answer
        yield {"type": "delta", "text": answer}

    monkeypatch.setattr(assistant, "llm_active", lambda _settings: True)
    monkeypatch.setattr(assistant, "needs_more_context", lambda _settings: False)
    monkeypatch.setattr(assistant, "_model_answer", model)
    client = TestClient(web.create_app(settings, store))
    marked = [{"text": CORRECTED, "warning": False}, {"text": NOT_FOUND, "warning": True}]
    reply = client.post("/chat", json={"message": "Did Marketing go from 84,000 to 115,500?", "email_id": budget.id}, headers=PAGE)
    lines = [json.loads(line) for line in reply.text.splitlines()]
    chat_id = lines[0]["id"]
    assert next(e["items"] for e in lines if e["type"] == "check") == marked
    assert client.get(f"/chats/{chat_id}").json()["turns"][1]["checks"] == marked, "opened again, as it was shown"

    # Saved before checks were marked: plain words, marked as the check marks them now.
    old = chats.new_id()
    store.create_chat(old, "Travel")
    store.add_chat_turn(old, "user", "What is travel?")
    store.add_chat_turn(old, "assistant", "Travel is $12,480.", {"mode": "model", "checks": [CORRECTED, NOT_FOUND]})
    assert client.get(f"/chats/{old}").json()["turns"][1]["checks"] == marked
