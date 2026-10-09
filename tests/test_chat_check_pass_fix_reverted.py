"""The check pass exists to fix the draft against the file. When it replaces a figure the draft took from the wrong
row with the right one (both in the file), _keep_stated_figures sees the draft's figure "dropped" and puts the
wrong draft back with a "revise" event."""

from __future__ import annotations

from controller_inbox import assistant, chats
from controller_inbox.local_llm import ToolReply

RENT = (
    "Rent schedule 2026\n"
    "September rent: $1,200.00\n"
    "October rent: $1,500.00\n"
).encode()


def _final(events):
    text = ""
    for event in events:
        if event["type"] == "delta":
            text += event["text"]
        elif event["type"] == "revise":
            text = event["text"]
    return text


def test_check_pass_correction_is_not_undone(store, settings, monkeypatch):
    chat = chats.new_id()
    store.create_chat(chat)
    chats.add_file(store, settings, chat, "rent.txt", "text/plain", RENT)
    uploads = chats.chat_mail(store, chat)

    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "needs_more_context", lambda _s: False)
    monkeypatch.setattr(assistant, "context_length", lambda _s: 16384)
    monkeypatch.setattr(assistant, "complete_text", lambda *_a, **_k: "SQL: NONE")
    # The draft picks September's line by mistake (no citation, so the check pass runs).
    monkeypatch.setattr(assistant, "chat_with_tools", lambda *_a, **_k: ToolReply("The October rent is $1,200.00."))
    # The check pass, as asked ("fix or remove anything that doesn't [match]"), gives the right line.
    monkeypatch.setattr(assistant, "stream_text", lambda *_a, **_k: iter(["The October rent is $1,500.00 (rent.txt) [1]."]))

    events = list(assistant.answer_stream(store, settings, "What is the October rent, and how did it change?", uploads=uploads))
    steps = [e["text"] for e in events if e["type"] == "step"]
    assert "Checking the answer against what I read" in steps
    final = _final(events)
    assert "$1,500.00" in final, f"the checked answer was replaced by the wrong draft: {final!r}"
