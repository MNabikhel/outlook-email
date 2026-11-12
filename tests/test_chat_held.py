"""Held mail beside a chat answer: an email of the conversation, or about a meeting, held as possible payment fraud is
no source and its words never reach the model, but the answer says it is there, with or without a model."""

from __future__ import annotations

import pytest

from controller_inbox import assistant
from controller_inbox.fraud import record_fraud_verdict

MEETING = "What do I need to know before “PBC list walkthrough with the auditors” (Wednesday, September 23 · 11:00 AM–12:00 PM)?"


@pytest.fixture
def model(monkeypatch):
    asked = []

    def stream(_s, messages, **_k):
        asked.append("\n".join(str(m.get("content")) for m in messages))
        yield "Answer [1]."

    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "stream_text", stream)
    monkeypatch.setattr(assistant, "needs_more_context", lambda _s: False)
    monkeypatch.setattr(assistant, "complete_text", lambda *_a, **_k: "SQL: NONE")
    monkeypatch.setattr(assistant, "chat_with_tools", lambda *_a, **_k: (_ for _ in ()).throw(assistant.ToolsUnsupported("no tools")))
    return asked


def _events(store, settings, question, email_id=None):
    return list(assistant.answer_stream(store, settings, question, email_id=email_id, today="2026-09-22", focus=[]))


def test_the_model_is_told_of_a_held_email_in_the_conversation(loaded, settings, model):
    held = loaded.get_email("demo-inv-10482-dup")
    record_fraud_verdict(loaded, settings, held.id, verdict="fraud")
    events = _events(loaded, settings, "What happened so far?", "demo-inv-10482")
    sources = next(e for e in events if e["type"] == "sources")
    assert [card["id"] for card in sources["sources"]] == ["demo-inv-10482"], "held mail is no source"
    assert sources.get("warning") == assistant.FRAUD_WARNING
    prompt = model[-1]
    assert "1 email of this conversation is held as possible payment fraud" in prompt, prompt[-800:]
    assert "in case the first note was missed" not in prompt, "nothing of what it says"


def test_held_mail_about_a_meeting_is_said_to_be_there(loaded, settings, model, monkeypatch):
    record_fraud_verdict(loaded, settings, "demo-pbc", verdict="fraud")
    events = _events(loaded, settings, MEETING)
    assert next(e for e in events if e["type"] == "sources").get("warning") == assistant.FRAUD_WARNING
    assert "1 email about this meeting is held as possible payment fraud" in model[-1]
    assert "kicking off Q3 interim testing" not in model[-1]
    monkeypatch.setattr(assistant, "llm_active", lambda _s: False)
    text = "".join(e.get("text", "") for e in _events(loaded, settings, MEETING) if e["type"] == "delta")
    assert "No mail about it turned up" not in text and "1 email about it is held as possible payment fraud" in text, text
