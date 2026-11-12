"""Mail held as possible payment fraud is not interacted with: the model never reads it (its text no more than its
files, whether asked about on screen, found by a search, opened by the model or read overnight), and it doesn't
open in a mail app or download, where its links and files would work. Marking it safe undoes all of that."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from controller_inbox import agent, assistant, overnight
from controller_inbox.fraud import attachments_locked, record_fraud_verdict
from helpers import PAGE

HELD = "demo-bec-wire"
SAYS = ("9988776612", "previous account", "rest of the day")  # what only the held email's text says


@pytest.fixture
def model(monkeypatch):
    """A model that records every prompt it is given."""
    asked: list[str] = []

    def stream(_settings, messages, **_kwargs):
        asked.append("\n".join(str(m.get("content")) for m in messages))
        yield "Answer [1]."

    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "stream_text", stream)
    monkeypatch.setattr(assistant, "chat_with_tools", lambda *_a, **_k: (_ for _ in ()).throw(assistant.ToolsUnsupported("no tools")))
    return asked


def _answer(loaded, settings, question, email_id=None) -> str:
    events = list(assistant.answer_stream(loaded, settings, question, email_id=email_id, today="2026-09-22"))
    return "".join(e["text"] for e in events if e["type"] == "delta")


def test_asked_about_while_it_is_open_the_answer_says_why_it_is_held_and_the_model_isnt_asked(loaded, settings, model):
    assert attachments_locked(loaded.get_email(HELD))
    text = _answer(loaded, settings, "What does this email want me to do?", HELD)
    assert "held as possible payment fraud" in text and "phone" in text and "Not fraud" in text
    assert not any(word in text.lower() for word in SAYS) and not model, "neither the model nor the answer reads it"


def test_found_among_other_mail_the_model_is_told_it_is_held_and_never_what_it_says(loaded, settings, model):
    events = list(assistant.answer_stream(loaded, settings, "Any emails about updated wiring instructions or bank details?", today="2026-09-22"))
    assert HELD in {card["id"] for e in events if e["type"] == "sources" for card in e["sources"]}, "it is listed, to be verified"
    assert model and not any(word in prompt.lower() for prompt in model for word in SAYS)
    assert any("held as possible payment fraud" in prompt for prompt in model)


def test_the_models_tools_dont_open_or_list_it(loaded, settings):
    ws = agent.Workspace(loaded, settings, [loaded.get_email(HELD)], question="what is this")
    assert agent.run_tool(ws, "open_email", {"email": 1}, limit=4000) == agent.HELD
    found = agent.run_tool(ws, "search_mail", {"query": "wiring instructions"}, limit=4000)
    assert not any(word in found.lower() for word in SAYS) and "held as possible payment fraud" in found


def test_the_overnight_reading_passes_it_by(loaded):
    waiting = overnight.waiting_for_reading(loaded, 50)
    assert HELD not in {email.id for email in waiting}
    with loaded.connect() as conn:
        held_waiting = conn.execute("SELECT model_status FROM emails WHERE id = ?", (HELD,)).fetchone()[0] == "script_draft"
    if held_waiting:
        assert loaded.counts()["waiting_on_bionic"] == len(overnight.waiting_for_reading(loaded, 10_000))


def test_it_doesnt_open_in_a_mail_app_or_download_until_it_is_marked_safe(loaded, settings):
    from controller_inbox.web import create_app

    client = TestClient(create_app(settings, loaded), headers=PAGE)
    assert client.get(f"/inbox/{HELD}/original").status_code == 403
    assert client.post(f"/inbox/{HELD}/open").status_code == 403
    detail = client.get(f"/api/mail/{HELD}").json()
    assert detail["locked"] and not detail["has_original"] and not detail["original_downloads"]
    record_fraud_verdict(loaded, settings, HELD, verdict="safe")
    assert not attachments_locked(loaded.get_email(HELD))
    assert client.get(f"/inbox/{HELD}/original").status_code == 200


def test_an_earlier_answer_quoting_its_text_is_left_out_once_it_is_held_again(loaded, settings):
    from controller_inbox import chats

    record_fraud_verdict(loaded, settings, HELD, verdict="safe")
    assert not loaded.get_email(HELD).attachments, "an email with no files: only its text is held"
    loaded.create_chat("c1")
    loaded.add_chat_turn("c1", "user", "What account does the wiring email say to use?")
    loaded.add_chat_turn(
        "c1", "assistant", "It says to use account 9988776612 [1].", {"mode": "model", "sources": [{"n": 1, "id": HELD}]}
    )
    record_fraud_verdict(loaded, settings, HELD, verdict="fraud")
    history = chats.history_for_model(loaded, loaded.chat_turns("c1"))
    assert not any("9988776612" in turn["text"] for turn in history)
    assert chats.LOCKED_ANSWER in [turn["text"] for turn in history]
    assert "9988776612" not in chats.past_context(loaded, "Which account does the wiring email say to use?", exclude="c2")
