"""Fix 9522c9c clears *every* note of a conversation (findings for "chat-<id>") when any one of its files is
replaced or removed, so notes the model wrote from the conversation's other, unchanged files are lost too."""
from controller_inbox import agent, chats


def _note_on_vendors(store, settings, chat):
    mail = chats.chat_mail(store, chat)
    ws = agent.Workspace(store, settings, [mail], question="Who is the top vendor?", current_id=mail.id)
    assert agent.run_tool(ws, "note", {"text": "Top vendor is Northwind at $48,200.00 (vendors.txt)."}, limit=600) == "Noted."
    return mail.id


def test_replacing_one_file_keeps_notes_on_another(store, settings):
    chat = chats.new_id()
    store.create_chat(chat)
    chats.add_file(store, settings, chat, "budget.txt", "text/plain", b"Q4 budget\nMarketing total: $10,000.00\n")
    chats.add_file(store, settings, chat, "vendors.txt", "text/plain", b"Vendor spend\nNorthwind $48,200.00\n")
    mail_id = _note_on_vendors(store, settings, chat)
    chats.add_file(store, settings, chat, "budget.txt", "text/plain", b"Q4 budget (revised)\nMarketing total: $12,500.00\n")
    texts = [f["text"] for f in store.findings(mail_id)]
    assert any("vendors.txt" in t for t in texts), texts


def test_removing_one_file_keeps_notes_on_another(store, settings):
    chat = chats.new_id()
    store.create_chat(chat)
    chats.add_file(store, settings, chat, "budget.txt", "text/plain", b"Q4 budget\nMarketing total: $10,000.00\n")
    chats.add_file(store, settings, chat, "vendors.txt", "text/plain", b"Vendor spend\nNorthwind $48,200.00\n")
    mail_id = _note_on_vendors(store, settings, chat)
    store.remove_chat_file(chat, "budget.txt")
    texts = [f["text"] for f in store.findings(mail_id)]
    assert any("vendors.txt" in t for t in texts), texts
