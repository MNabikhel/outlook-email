"""An email the model opens with a tool (open_email) carries no fraud warning, though the same email in the first
prompt gets "Warning: payment-detail change — verify by phone." (assistant._source_block). For a bank-change email
with no attachment (the common BEC shape) nothing in the tool result marks it."""

from __future__ import annotations

from msgfactory import write_msg

from controller_inbox import agent, assistant
from controller_inbox.folder_mail import ingest_folder


def test_open_email_on_a_bank_change_email_warns(store, settings, mail):
    write_msg(
        settings.inbox_incoming / "bec.msg",
        "Change of bank account",
        "Please note our bank details have changed. From today pay all invoices to account 99887766, sort code 20-00-00.",
        sender_name="Harbor Steel Accounts",
        sender_email="accounts@harbor-steel-billing.com",
    )
    bec = next(e for e in ingest_folder(store, settings) if e.subject == "Change of bank account")
    assert assistant.is_fraud(bec)
    budget = mail["Q4 budget draft"]
    shown = assistant._source_block(1, bec, 1000, on_screen=False)
    assert "verify by phone" in shown  # how the first prompt shows it
    ws = agent.Workspace(store, settings, [budget], question="Did Harbor Steel send new bank details?", current_id=budget.id)
    opened = agent.run_tool(ws, "open_email", {"email": bec.id}, limit=3000)
    assert "verify by phone" in opened.lower() or "fraud" in opened.lower(), opened
