"""Notes the model writes on a conversation's files are stored on the stand-in email "chat-<id>". Replacing a file
(same name, new version) or removing it clears its page readings (store.add_chat_file / remove_chat_file) but not
those notes, so the old version's figures come back as "Notes from earlier reading" for the new file, and
answer_check counts them as read (_read_material includes earlier_findings): a stale figure isn't flagged."""

from __future__ import annotations

from controller_inbox import agent, assistant, chats


def test_notes_on_a_replaced_file_do_not_ground_the_new_answer(store, settings, monkeypatch):
    chat = chats.new_id()
    store.create_chat(chat)
    chats.add_file(store, settings, chat, "budget.txt", "text/plain", b"Q4 budget\nMarketing total: $10,000.00\n")
    v1 = chats.chat_mail(store, chat)
    ws = agent.Workspace(store, settings, [v1], question="What is the marketing total?", current_id=v1.id)
    assert agent.run_tool(ws, "note", {"text": "Marketing total is $10,000.00 (budget.txt)."}, limit=600) == "Noted."

    # The user uploads the corrected budget under the same name.
    chats.add_file(store, settings, chat, "budget.txt", "text/plain", b"Q4 budget (revised)\nMarketing total: $12,500.00\n")
    v2 = chats.chat_mail(store, chat)

    prompts = []

    def stream(_s, messages, **_k):
        prompts.append(messages[-1]["content"])
        yield "The marketing total is $10,000.00 (budget.txt) [1]."

    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "needs_more_context", lambda _s: False)
    monkeypatch.setattr(assistant, "context_length", lambda _s: 16384)
    monkeypatch.setattr(assistant, "complete_text", lambda *_a, **_k: "SQL: NONE")
    monkeypatch.setattr(assistant, "stream_text", stream)
    events = list(assistant.answer_stream(store, settings, "What is the marketing total?", uploads=v2))
    assert "$12,500.00" in prompts[0]
    assert "$10,000.00" not in prompts[0], "the replaced file's figure is given to the model as a note"
    checks = [item for e in events if e["type"] == "check" for item in e["items"]]
    assert any("10,000.00" in c for c in checks), f"a figure only in the replaced file isn't flagged: {checks}"
