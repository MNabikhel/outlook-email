"""Which mail a chat question is answered from: the email on screen, its conversation, or a search of the inbox,
and how the answer without a model lays that out. The sample's today is Tuesday September 22, 2026."""

from __future__ import annotations

from datetime import date

import pytest
from test_links import _record

from controller_inbox import assistant, services

TODAY = "2026-09-22"


def _ask(store, settings, question, email_id=None, today=TODAY):
    focus = services.digest_now(store, settings, date.fromisoformat(today))["focus"] if store.get_email("demo-po") else []
    events = list(assistant.answer_stream(store, settings, question, email_id=email_id, focus=focus, today=today))
    return "".join(e.get("text", "") for e in events if e["type"] == "delta")


def _ids(store, question, email_id=None):
    return [email.id for email in assistant.pick_sources(store, question, email_id=email_id)[0]]


@pytest.fixture
def model(monkeypatch):
    """A model that answers, keeping each prompt it was sent and whether it was offered tools."""
    asked, tools = [], []

    def stream(_s, messages, **_k):
        asked.append("\n".join(str(m.get("content")) for m in messages))
        yield "Answer [1]."

    def with_tools(*_a, **_k):
        tools.append(1)
        raise assistant.ToolsUnsupported("no tools")

    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "stream_text", stream)
    monkeypatch.setattr(assistant, "needs_more_context", lambda _s: False)
    monkeypatch.setattr(assistant, "complete_text", lambda _s, messages, **_k: asked.append("\n".join(str(m.get("content")) for m in messages)) or "SQL: NONE")
    monkeypatch.setattr(assistant, "chat_with_tools", with_tools)
    return asked, tools


def test_summarize_my_day_is_the_focus_list(loaded, settings):
    text = _ask(loaded, settings, "summarize my day")
    assert "focus list" in text.lower() and "password" not in text.lower(), text


def test_a_code_inside_a_longer_word_is_not_found_there(loaded, settings):
    current = loaded.get_email("demo-inv-10482-dup")
    assert assistant._found_in(current, ["irs"]) == [], "'irs' is only inside 'first' here"
    assert assistant._found_in(current, ["invoice"]) == ["invoice"]
    assert assistant._found_in(loaded.get_email("demo-pbc"), ["workpaper"]) == ["workpaper"], "a plural form still counts"
    assert assistant._distinctive("irs", "What does the IRS want?"), "a word in capitals is a name"
    assert "demo-tax" in _ids(loaded, "What does the IRS want?", "demo-inv-10482-dup")
    assert not assistant.body_lines(current, "What does the IRS want?")


def test_a_history_question_naming_other_mail_searches_for_it(loaded, settings):
    assert "demo-tax" in _ids(loaded, "What is the history of the CP2000 notice?", "demo-inv-10482-dup")
    assert "demo-missing-att" in _ids(loaded, "What happened with Harbor Packaging so far?", "demo-inv-10482-dup")
    assert _ids(loaded, "What happened so far?", "demo-inv-10482-dup")[:2] == ["demo-inv-10482-dup", "demo-inv-10482"]


def test_a_question_about_this_naming_someone_the_email_lacks_also_searches(loaded, settings, model):
    asked, tools = model
    ids = _ids(loaded, "Did Purchasing issue a PO for this?", "demo-inv-10482")
    assert ids[0] == "demo-inv-10482" and "demo-po" in ids, ids
    assert _ids(loaded, "Summarize this invoice", "demo-inv-10482") == ["demo-inv-10482"]
    assert _ids(loaded, "Who is Northwind's contact on this?", "demo-inv-10482") == ["demo-inv-10482"], "a name it has stays on it"


def test_only_history_wording_asks_for_the_conversation(loaded, settings):
    for question in ("Do I need to follow up on this?", "Is this a supply chain issue?", "What is the payment history with Northwind?"):
        assert not assistant._HISTORY.search(question), question
    for question in ("What happened so far?", "Show me the email chain", "Any follow-ups on this?", "What is the history of this?",
                     "Show the thread", "How did this start?", "Trace this back"):
        assert assistant._HISTORY.search(question), question
    text = _ask(loaded, settings, "Do I need to follow up on this?", "demo-inv-10482")
    assert "How this came about" not in text, text


def test_how_do_i_about_the_open_email_or_accounting_is_no_setup_help(loaded, settings, model):
    asked, _tools = model
    for email_id, question in (("demo-question", "How do I answer this?"), ("demo-approval", "How do I approve it?"),
                               ("demo-tax", "How to dispute it?"), (None, "How do I calculate straight-line depreciation?")):
        asked.clear()
        events = list(assistant.answer_stream(loaded, settings, question, email_id=email_id, today=TODAY))
        modes = [e.get("mode") for e in events if e["type"] == "sources"]
        assert modes != ["help"] and asked, (question, modes)
    for question in ("How do I set up LM Studio?", "How do I install the local model?", "How do I export mail from Outlook?"):
        events = list(assistant.answer_stream(loaded, settings, question, today=TODAY))
        assert [e.get("mode") for e in events if e["type"] == "sources"] == ["help"], question


def test_when_is_it_due_on_the_open_email_is_answered_from_it(loaded, settings):
    text = _ask(loaded, settings, "when is the response due?", "demo-tax")
    assert "October 12" in text or "2026-10-12" in text, text
    assert _ids(loaded, "when is the response due?", "demo-tax") == ["demo-tax"]
    ids = _ids(loaded, "When is payment due?", "demo-inv-10482")
    assert ids == ["demo-inv-10482"], ids
    assert "demo-inv-10482" not in _ids(loaded, "what's due today?", "demo-inv-10482")[1:]


def test_first_or_tasks_on_the_open_email_stay_on_it(loaded, settings):
    assert _ids(loaded, "What is the first item on the PBC list?", "demo-pbc") == ["demo-pbc"]
    assert _ids(loaded, "What are the PBC tasks?", "demo-pbc") == ["demo-pbc"]
    assert _ids(loaded, "Which close items are behind?", "demo-close") == ["demo-close"]


def test_focus_list_mail_is_not_called_the_open_emails_conversation(loaded, settings):
    text = _ask(loaded, settings, "any other emails about this?", "demo-po")
    assert "In its conversation" not in text, text


def test_mail_linked_only_by_an_invoice_number_is_not_its_conversation(store, settings):
    invoice = _record("inv", invoices=["INV-55120"], received="2026-10-01T14:00:00+00:00")
    invoice.subject, invoice.body_text = "Invoice INV-55120", "Please find invoice INV-55120 for $4,200.00."
    remit = _record("remit", sender="ar@customer.com", invoices=["INV-55120"], received="2026-10-03T14:00:00+00:00")
    remit.subject, remit.body_text = "Remittance for INV-55120", "We paid INV-55120 today by ACH."
    store.upsert_email(invoice)
    store.upsert_email(remit)
    text = _ask(store, settings, "What happened so far?", "inv", today="2026-10-05")
    assert "In its conversation" not in text and "Linked to it" in text and "We paid INV-55120" in text, text


def test_a_long_conversation_keeps_the_replies_nearest_the_open_email(store, settings):
    for n in range(12):
        threading = {"in_reply_to": f"m{n - 1}@n.com", "references": ["m0@n.com"]} if n else {}
        record = _record(f"e{n:02d}", mid=f"<m{n}@n.com>", threading=threading, received=f"2026-10-{1 + n:02d}T14:00:00+00:00")
        record.subject, record.body_text = f"Re: rebate step {n}", f"Step {n} of the rebate talks."
        store.upsert_email(record)
    text = _ask(store, settings, "What happened so far?", "e11", today="2026-10-12")
    assert "Step 10" in text and "Step 5" in text and "Step 4" not in text, text
    assert "5 more emails of this conversation aren't shown here" in text, text
    lines = [line for line in text.splitlines() if "Step" in line]
    assert lines == sorted(lines, key=lambda line: int(line.split("Step ")[1].split()[0])), "oldest first"
