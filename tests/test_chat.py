"""Ask CloseDesk's answers: the check pass, the prompt budget, tool calls, notes on files, and the fraud lock."""

from __future__ import annotations

import json
from array import array
from dataclasses import replace
from types import SimpleNamespace

import httpx

from controller_inbox import agent, assistant, chats, file_summaries, local_llm, semantic, tools
from controller_inbox.answer_check import review
from controller_inbox.folder_mail import ingest_folder
from controller_inbox.fraud import attachments_locked
from controller_inbox.local_llm import ToolReply
from controller_inbox.models import DocumentType, EmailRecord, ExtractedFields, Importance
from controller_inbox.reading import _number_in, _ungrounded_amounts
from msgfactory import write_msg


# A figure that is in the material is never rewritten -----------------------------------------
def test_figures_in_the_file_are_never_rewritten_even_beside_total():
    invoice = "Net: $9,000\nVAT: $1,750\nShipping: $50\nTotal due: $10,800"
    answer = "The total is $10,800, made up of $9,000 net and $1,750 VAT."
    result = review(answer, material=[], files=[("inv.pdf", invoice)])
    assert (result.text, result.checks) == (answer, [])
    slip = review("Net $9,000 plus VAT $1,750 is a total of $10,700.", material=[], files=[("inv.pdf", invoice)])
    assert slip.text.endswith("a total of $10,750.") and slip.checks == ["Corrected $10,700 to $10,750 ($9,000 + $1,750)."]


# Words shaped like cells ("H1", "Q3") are not cell references -----------------------------------
def test_words_shaped_like_cells_are_left_alone():
    sheet = '[sheet "Plan"]\nA1: Item | H1: Notes\nA2: Revenue | B2: 48,500 | H2: ok\n'
    files = [("plan.xlsx", sheet)]
    for answer in ("H1 revenue was $48,500 in plan.xlsx.", "plan.xlsx: H1 revenue is 48,500."):
        result = review(answer, material=[], files=files)
        assert (result.text, result.checks) == (answer, []), answer
    cited = review("Revenue of 48,500 is in plan.xlsx cell H1.", material=[], files=files)
    assert cited.text == "Revenue of 48,500 is in plan.xlsx cell B2."
    prefixed = review("Revenue of 48,500 is in plan.xlsx (Plan!H2).", material=[], files=files)
    assert prefixed.text == "Revenue of 48,500 is in plan.xlsx (Plan!B2)."


# A page citation that holds every cited figure is kept ---------------------------------------
def test_a_right_page_is_kept_when_another_page_repeats_the_figure():
    pdf = (
        "[page 2]\nExecutive summary: consolidated revenue increased to 4,250,000 compared with prior quarter, "
        "driven by stronger subscription renewals\n[page 7]\nRevenue 4,250,000\n"
    )
    answer = "Consolidated revenue increased to 4,250,000 on page 7, driven by subscription renewals."
    result = review(answer, material=[], files=[("report.pdf", pdf)])
    assert (result.text, result.checks) == (answer, [])


# The prompt fits its budget, and tools still run on a small context -----------------------------
def test_the_prompt_fits_a_4096_token_context_and_leaves_room_for_tools(store, settings, mail, monkeypatch):
    budget, quote, scam = mail["Q4 budget draft"], mail["FW: Acme quote"], mail["Updated remittance details"]
    memo = budget.attachments[1]
    memo.extracted_text = "\n".join(
        f"[page {n}]\n" + f"Offsite planning note {n}: venue, travel and catering line items. " * 40 for n in range(1, 30)
    )
    history = [{"role": "user" if i % 2 == 0 else "assistant", "text": "x" * 400} for i in range(6)]
    monkeypatch.setattr(assistant, "context_length", lambda _s: 4096)
    monkeypatch.setattr(assistant, "reply_budget", lambda _s, _m: 500)
    monkeypatch.setattr(semantic, "embedding_model", lambda _s: "")
    seen = []

    def fake_tools(_settings, messages, _tools, *, max_tokens):
        seen.append([dict(m) for m in messages])
        if len(seen) == 1:
            return ToolReply("", [{"id": "c1", "name": "read_file", "arguments": {"email": "1", "file": "Offsite memo.docx", "part": "page 3"}}])
        # A figure that isn't in the memo: the answer gets the second pass this test sizes.
        return ToolReply("The memo covers venue, travel and catering, $12,000 in all [1].")

    final = {}

    def fake_stream(_settings, messages, *, max_tokens, **_k):
        final["messages"] = messages
        yield "The memo covers venue, travel and catering [1]."

    monkeypatch.setattr(assistant, "chat_with_tools", fake_tools)
    monkeypatch.setattr(assistant, "stream_text", fake_stream)
    ws = agent.Workspace(store, settings, [budget, quote, scam], question="what does the memo say about catering?", current_id=budget.id)
    state = {"wrote": False, "text": ""}
    list(assistant._read_and_answer(ws, ws.question, state, history=history, today="2026-10-06", shrink=1))

    tool_budget = assistant._budget(settings, tools=True)
    assert assistant.prompt_chars(seen[0]) <= tool_budget
    tool_result = next(m for m in seen[1] if m["role"] == "tool")["content"]
    assert "No room left" not in tool_result and "Offsite memo.docx" in tool_result, "the tool ran on a 4,096-token context"
    assert assistant.prompt_chars(final["messages"]) <= assistant._budget(settings, tools=False)


def test_history_gets_only_the_room_that_is_left():
    history = [{"role": "user" if i % 2 == 0 else "assistant", "text": "y" * 400} for i in range(6)]
    tight = assistant.build_messages("q", [], history=history, budget=4000, tools=True)
    assert [m["role"] for m in tight] == ["system", "user"], "no room: earlier turns are left out"
    roomy = assistant.build_messages("q", [], history=history, budget=40_000, tools=True)
    assert len(roomy) == 8 and assistant.prompt_chars(roomy) <= 40_000


# An answer that says what day it is is not mistaken for an echo --------------------------------
def test_an_answer_that_starts_with_today_is_kept():
    answer = "Today is 2026-10-06, so INV-10482 [1] is 6 days overdue."
    assert "".join(assistant.without_echo(iter([answer]))) == answer
    echoed = "INV-10482 is overdue [1].\nToday is 2026-10-06.\nFocus list (most important first):\n- Overdue"
    assert "".join(assistant.without_echo(iter([echoed]))) == "INV-10482 is overdue [1]."


def test_an_answer_that_says_what_day_it_is_on_its_own_line_is_kept():
    for answer in (
        "INV-10482 [1] was due 2026-10-01.\nToday is 2026-10-08. It is 7 days overdue, so pay it this week.",
        "INV-10482 [1] was due 2026-10-01.\n\nToday is 2026-10-08.\nThat makes it 7 days late.",
    ):
        streamed = [answer[i : i + 5] for i in range(0, len(answer), 5)]
        assert "".join(assistant.without_echo(iter(streamed))) == answer
        assert "".join(assistant.without_echo(iter([answer]))) == answer
    # The prompt's header copied after the answer, with the first email under it, or left on its own at the end.
    copied = "INV-10482 is overdue [1].\n\nToday is 2026-10-06.\n\n[1] 2026-09-22 · from Maya Chen · \"Q4 budget\""
    assert "".join(assistant.without_echo(iter([copied]))) == "INV-10482 is overdue [1]."
    assert "".join(assistant.without_echo(iter(["INV-10482 is overdue [1].", "\nToday is 2026-10-06."]))) == "INV-10482 is overdue [1]."


# Tool-call arguments that aren't a JSON object ------------------------------------------------
def test_bad_tool_arguments_go_back_to_the_model_instead_of_crashing(store, settings, mail, monkeypatch):
    budget = mail["Q4 budget draft"]
    assert agent.step_label("open_email", ["1"], agent.Workspace(store, settings, [budget])) == "Opening [1] Q4 budget draft"
    seen = []

    def fake_tools(_settings, messages, _tools, *, max_tokens):
        seen.append([dict(m) for m in messages])
        if len(seen) == 1:
            return ToolReply("", [
                {"id": "a", "name": "open_email", "arguments": ["1"]},
                {"id": "b", "name": "read_file", "arguments": {}},
            ])
        return ToolReply("Draft [1].")

    monkeypatch.setattr(assistant, "chat_with_tools", fake_tools)
    ws = agent.Workspace(store, settings, [budget], question="q", current_id=budget.id)
    gen = assistant._tool_loop(ws, [{"role": "system", "content": "s"}], 50_000)
    try:
        while True:
            next(gen)
    except StopIteration as done:
        assert done.value == "Draft [1]."
    results = [m["content"] for m in seen[1] if m["role"] == "tool"]
    assert results[0].startswith("The arguments for open_email weren't a JSON object")
    assert results[1].startswith("No arguments came through for read_file")
    call = next(m for m in seen[1] if m.get("tool_calls"))
    assert json.loads(call["tool_calls"][0]["function"]["arguments"]) == {}
    assert not ws.evidence, "nothing ran on default choices"


# Overnight summaries: spaced suffixes and whole-figure dropping ---------------------------------
def test_summary_lines_go_by_their_own_figures():
    text = "[page 1]\nRevenue 1,000,000. Balance $2,140."
    draft = "- Revenue was 2.5 million (page 1)\n- Growth was 5 percent (page 1)\n- 40 staff (page 1)\n- Balance $2,140 (page 1)"
    assert file_summaries._checked(draft, "f.pdf", text) == "- Balance $2,140 (page 1)"


# The answer check sees everything the model was shown -------------------------------------------
def test_figures_from_the_focus_list_and_summaries_are_not_flagged_but_one_only_a_past_answer_has_is(store, settings, mail):
    budget = mail["Q4 budget draft"]
    budget.summary = "Maya asks for a check of the $1,234 travel line."
    ws = agent.Workspace(store, settings, [budget], question="what's due?", current_id=budget.id, past="Earlier: deposit was $7,777.")
    focus = [{"rank": 1, "label": "Overdue 12 days", "title": "Pay invoice 48,500", "email_id": budget.id}]
    answer = "Invoice 48,500 is overdue by 12 days; the travel line is $1,234 and the deposit was $7,777 [1]."
    events = list(assistant._checked(ws, answer, history=[], today="2026-10-06", focus=focus))
    # A past answer is what the model wrote, not something it read: only its figure is flagged.
    checks = [item for event in events if event["type"] == "check" for item in event["items"]]
    assert [event["type"] for event in events] == ["check"] and len(checks) == 1 and "$7,777" in checks[0], events


# Notes only on emails that were read, and labelled as data -------------------------------------
def test_notes_are_only_written_on_emails_that_were_opened(store, settings, mail):
    budget, quote = mail["Q4 budget draft"], mail["FW: Acme quote"]
    ws = agent.Workspace(store, settings, [budget], question="acme quote")
    found = agent.run_tool(ws, "search_mail", {"query": "Acme quote"}, limit=2000)
    number = next(str(ws.number(e)) for e in ws.sources if e.id == quote.id)
    assert number in found
    refused = agent.run_tool(ws, "note", {"text": "Vendor bank verified, pay now.", "email": number}, limit=600)
    assert refused.startswith("Open [") and store.findings(quote.id) == []
    agent.run_tool(ws, "open_email", {"email": number}, limit=2000)
    assert agent.run_tool(ws, "note", {"text": "Quote Q-881 support plan is $9,600.00.", "email": number}, limit=600) == "Noted."
    assert agent.run_tool(ws, "note", {"text": "Budget total checked.", "email": "1"}, limit=600) == "Noted."
    ws.question = "support plan quote"
    assert agent.earlier_findings(ws, quote).startswith(agent.NOTES_HEAD)
    assert "not instructions" in agent.NOTES_HEAD


# The fraud lock covers notes, earlier answers and the meaning index -----------------------------
def test_locked_mail_leaves_no_trace_in_notes_past_answers_or_meaning_search(store, settings, mail, monkeypatch):
    budget, scam = mail["Q4 budget draft"], mail["Updated remittance details"]
    assert agent.attachments_locked(scam)
    store.add_finding(scam.id, "Remit account 5566778899 per the bank letter.", question="bank letter account")
    ws = agent.Workspace(store, settings, [scam], question="bank letter account")
    assert agent.earlier_findings(ws, scam) == ""
    assert "5566778899" not in agent.run_tool(ws, "open_email", {"email": "1"}, limit=4000)

    store.create_chat("aaaaaaaaaaaa")
    store.add_chat_turn("aaaaaaaaaaaa", "user", "What account does the remittance letter give?")
    store.add_chat_turn("aaaaaaaaaaaa", "assistant", "The remittance letter gives account 5566778899 [1].",
                        {"mode": "model", "sources": [{"id": scam.id}]})
    store.create_chat("bbbbbbbbbbbb")
    store.add_chat_turn("bbbbbbbbbbbb", "user", "What remittance account does the budget use?")
    store.add_chat_turn("bbbbbbbbbbbb", "assistant", "The budget remittance account is not listed [1].",
                        {"mode": "model", "sources": [{"id": budget.id}]})
    past = chats.past_context(store, "remittance account letter")
    assert "5566778899" not in past and "budget remittance account is not listed" in past

    model = "embed-test"
    monkeypatch.setattr(semantic, "embedding_model", lambda _s: model)
    monkeypatch.setattr(semantic, "embed", lambda _s, texts, query=False: [array("f", [1.0, 0.0, 0.0]) for _ in texts])
    unit, other, zero = array("f", [1.0, 0.0, 0.0]), array("f", [0.0, 1.0, 0.0]), array("f", [0.0, 0.0, 1.0])
    scam_file = semantic._file_items(scam.attachments[0])[0]
    budget_file = semantic._file_items(budget.attachments[0])[0]
    rows = [
        (f"email:{scam.id}", scam.id, "x", other.tobytes()),
        (scam_file[0], scam.id, semantic._key(scam_file[1]), unit.tobytes()),
        (f"email:{budget.id}", budget.id, "x", other.tobytes()),
        (budget_file[0], budget.id, semantic._key(budget_file[1]), unit.tobytes()),
    ] + [(f"file:pad:{n}", "pad", "x", zero.tobytes()) for n in range(30)]
    store.save_embeddings(model, rows)
    found = [email.id for email in semantic.search(store, settings, "bank account letter")]
    assert budget.id in found and scam.id not in found, "a locked email's file vectors don't find it"
    assert semantic.rank_sections(store, settings, scam.attachments[0], "bank account") == []
    assert semantic.rank_sections(store, settings, budget.attachments[0], "budget") != []


# The Bionic tool CLI answers a bad date in JSON ---------------------------------------------
def test_a_bad_date_is_a_json_error(store, settings):
    result = tools.dispatch(store, settings, "focus", SimpleNamespace(date="2026-13-01", limit=20))
    assert result["ok"] is False and "YYYY-MM-DD" in result["error"]
    json.dumps(result)


def test_saving_a_reading_over_a_corrected_message_is_a_json_error(loaded, settings):
    email = loaded.get_email("demo-newsletter")
    email.model_status = "corrected"
    loaded.upsert_email(email)
    payload = {"email_id": email.id, "category": "ap_invoice", "folder": "important", "importance": "high", "summary": "x"}
    result = tools.dispatch(loaded, settings, "save_reading", SimpleNamespace(json=json.dumps(payload)))
    assert result["ok"] is False and "corrected" in result["error"]
    assert loaded.get_email(email.id).model_status == "corrected"


def test_a_figure_from_an_earlier_answer_is_still_flagged_in_a_follow_up(store, settings):
    # The model's own earlier answers are not something it read: a made-up figure it repeats stays flagged.
    ws = agent.Workspace(store, settings, [], question="What was the Ridgeview total again?")
    history = [
        {"role": "user", "text": "What is the Ridgeview total?"},
        {"role": "assistant", "text": "The Ridgeview total is $12,345.67 [1]."},
    ]
    events = list(assistant._checked(ws, "As I said, the Ridgeview total is $12,345.67 [1].", history=history, today="2026-10-08"))
    checks = [item for event in events if event["type"] == "check" for item in event["items"]]
    assert any("$12,345.67" in item for item in checks), events


def test_a_last_comma_first_name_greets_by_the_first_name():
    assert assistant._first_name("Smith, John") == "John"
    assert assistant._first_name("John Smith") == "John"


# A figure the model made up in an earlier conversation counts as "read" when this answer is checked ------------
def test_a_figure_from_an_earlier_answer_is_not_taken_as_checked_against_the_files(loaded, settings):
    from controller_inbox import agent, assistant

    email = loaded.get_email("demo-inv-10482")  # the invoice says "Amount due $12,850.00"
    # An earlier chat's answer, written by the model (it misread the amount), comes back as background.
    past = (
        "From earlier conversations with this person (background only, ...):\n"
        '- 2026-10-01, asked "how much is the Northwind invoice INV-10482?": answered "The amount due is $13,250.00."'
    )
    ws = agent.Workspace(loaded, settings, [email], question="How much do we owe Northwind on INV-10482?", current_id=email.id, past=past)
    answer = "The amount due on INV-10482 is $13,250.00 [1]."
    events = list(assistant._checked(ws, answer, history=[], today="2026-10-20"))
    checks = [item for event in events if event["type"] == "check" for item in event["items"]]
    assert any("13,250.00" in item for item in checks), (
        "the made-up $13,250.00 (the file says $12,850.00) went out unflagged: the earlier answer grounded it"
    )


def test_a_figure_from_a_note_the_model_wrote_earlier_is_not_taken_as_checked_against_the_files(loaded, settings):
    from controller_inbox import agent, assistant

    email = loaded.get_email("demo-inv-10482")
    # A note the model wrote with the note tool on an earlier question, kept with the email.
    loaded.add_finding(email.id, "Northwind INV-10482 amount due is $13,250.00 (INV-10482.pdf page 1)", question="amount due", at="2026-10-01T00:00:00+00:00")
    ws = agent.Workspace(loaded, settings, [email], question="What is the amount due on Northwind INV-10482?", current_id=email.id)
    answer = "The amount due on INV-10482 is $13,250.00 [1]."
    events = list(assistant._checked(ws, answer, history=[], today="2026-10-20"))
    checks = [item for event in events if event["type"] == "check" for item in event["items"]]
    assert any("13,250.00" in item for item in checks), "a figure only the model's own earlier note has went out unflagged"


# The overnight summary of a file is still served after the user fixed a figure on its page --------------------
def test_summarize_this_file_does_not_serve_the_figure_the_user_fixed_on_the_page(loaded, settings, monkeypatch):
    from controller_inbox import agent, assistant

    email = loaded.get_email("demo-inv-10482")
    att = email.attachments[0]
    # A longer invoice (overnight summaries are written for files of 1,500 characters or more).
    att.extracted_text += "\n" + "\n".join(f"Line {n}: Office supplies, carton {n}, see packing list" for n in range(60))
    loaded.upsert_email(email)
    att = loaded.get_email(email.id).attachments[0]
    assert "$12,850.00" in att.extracted_text and len(att.extracted_text) >= agent.SUMMARY_MIN_CHARS
    loaded.save_file_summary(att.id, agent.summary_key(att), "- Amount due $12,850.00, due October 5, 2026 (page 1)")

    # OCR misread the amount: the user fixes it on the Page tab. Same length, so the summary's key doesn't change.
    loaded.add_page_fix({
        "attachment_id": att.id, "sha256": att.sha256, "page": 1, "kind": "text",
        "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.05, "was": "$12,850.00", "now": "$12,580.00", "sender": email.sender_email,
    })
    assert "$12,580.00" in loaded.get_email(email.id).attachments[0].extracted_text

    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    # The model reads the file as shown instead (a stand-in: with no model at all, the lookup answer quotes the
    # email's subject, which has the amount as the sender wrote it).
    monkeypatch.setattr(assistant, "complete_text", lambda *_a, **_k: "SQL: NONE")
    monkeypatch.setattr(assistant, "stream_text", lambda *_a, **_k: iter(["- Amount due $12,580.00, due October 5, 2026 [1]"]))
    events = list(assistant.answer_stream(loaded, settings, "Summarize the attached file", email_id=email.id))
    assert not any(e["type"] == "step" and "written during the overnight reading" in e["text"] for e in events)
    text = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "12,850.00" not in text, f"the summary served still has the figure the user corrected: {text!r}"


# The model server goes away during the check pass: a finished draft is thrown away for half a sentence ----------
def test_a_finished_draft_is_kept_when_the_server_stops_during_the_check_pass(store, settings, mail, monkeypatch):
    from controller_inbox import assistant, local_llm

    budget = mail["Q4 budget draft"]
    draft = "Ads went up by 500, from 1,000 to 1,500, and Travel by 50, so the total change is 550 in Q4 budget.xlsx."
    replies = iter([
        local_llm.ToolReply(content="", calls=[{"id": "c1", "name": "read_file", "arguments": {"email": "1", "file": "Q4 budget.xlsx"}}]),
        local_llm.ToolReply(content=draft),
    ])
    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "complete_text", lambda *_a, **_k: "SQL: NONE")
    monkeypatch.setattr(assistant, "chat_with_tools", lambda *_a, **_k: next(replies))

    def dies(*_a, **_k):
        yield "The total"
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")

    monkeypatch.setattr(assistant, "stream_text", dies)
    events = list(assistant.answer_stream(store, settings, "How much did the budget change in total from Q3 to Q4?", email_id=budget.id))
    assert any(e["type"] == "step" and "Checking the answer" in e["text"] for e in events), "the check pass ran"
    shown = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "550" in shown, f"the finished draft was dropped for a cut-off check pass: {shown!r}"


# An earlier answer in the same chat that quoted an email's files is given back to the model after it is flagged --
def test_the_same_chats_earlier_answer_from_a_now_flagged_emails_files_is_not_given_to_the_model(store, settings, mail, monkeypatch):
    from fastapi.testclient import TestClient

    from controller_inbox import assistant, chats, web
    from controller_inbox.fraud import attachments_locked

    scam = store.get_email(mail["Updated remittance details"].id)
    assert attachments_locked(scam)
    # Before it was flagged (or while it was marked "Not fraud"), the chat read its file and quoted the account.
    chat_id = chats.new_id()
    store.create_chat(chat_id)
    store.add_chat_turn(chat_id, "user", "What account does the bank letter give?")
    store.add_chat_turn(
        chat_id, "assistant", "The new bank letter.pdf says to remit to account 5566778899, routing 021000021 [1].",
        {"sources": [{"n": 1, "id": scam.id, "subject": scam.subject}]},
    )
    # The same exchange, kept in another conversation, is kept out of the model's background (chats.past_context).
    assert "5566778899" not in chats.past_context(store, "What account does the bank letter give?")

    sent = []
    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "complete_text", lambda *_a, **_k: "SQL: NONE")

    def stream(_settings, messages, **_k):
        sent.append(messages)
        yield "Please verify by phone [1]."

    monkeypatch.setattr(assistant, "stream_text", stream)
    client = TestClient(web.create_app(settings, store))
    reply = client.post("/chat", json={"message": "Should I update the vendor's account?", "chat_id": chat_id, "email_id": scam.id},
                        headers={"x-closedesk": "1"})
    assert reply.status_code == 200 and sent, reply.text
    prompt = json.dumps(sent)
    assert "5566778899" not in prompt, "text from a fraud-held email's file reached the model through the chat history"


def test_an_answer_cut_off_for_room_says_so(store, settings, mail, monkeypatch):
    from controller_inbox import assistant

    def stream(*_a, finished=None, **_k):
        yield "The offsite is in Lisbon on"
        if finished is not None:
            finished["reason"] = "length"

    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "complete_text", lambda *_a, **_k: "SQL: NONE")
    monkeypatch.setattr(assistant, "stream_text", stream)
    events = list(assistant.answer_stream(store, settings, "Where is the offsite in this memo?", email_id=mail["Q4 budget draft"].id))
    shown = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "cut off" in shown, shown


def test_the_same_tool_call_asked_again_isnt_run_again(store, settings, mail, monkeypatch):
    from controller_inbox import assistant

    budget = mail["Q4 budget draft"]
    call = {"id": "c1", "name": "read_file", "arguments": {"email": "1", "file": "Q4 budget.xlsx"}}
    replies = iter([
        local_llm.ToolReply(content="", calls=[call]),
        local_llm.ToolReply(content="", calls=[{**call, "id": "c2"}]),
        local_llm.ToolReply(content="Ads went up by 500 in Q4 budget.xlsx [1]."),
    ])
    sent = []

    def tools(_settings, messages, *_a, **_k):
        sent.append([dict(m) for m in messages])
        return next(replies)

    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "complete_text", lambda *_a, **_k: "SQL: NONE")
    monkeypatch.setattr(assistant, "chat_with_tools", tools)
    monkeypatch.setattr(assistant, "stream_text", lambda *_a, **_k: iter(["Ads went up by 500 [1]."]))
    events = list(assistant.answer_stream(store, settings, "How much did the budget change in total from Q3 to Q4?", email_id=budget.id))
    reads = [e for e in events if e["type"] == "step" and "Q4 budget.xlsx" in e["text"] and "Read" in e["text"]]
    assert len(reads) <= 1, reads
    last_tool = [m for m in sent[-1] if m.get("role") == "tool"][-1]
    assert last_tool["content"].startswith("Already done"), last_tool


def test_fields_shown_on_the_page_are_given_to_the_chat_as_the_emails_facts(store, settings, mail):
    from controller_inbox import assistant

    email = mail["Q4 budget draft"]
    email.extracted.taught = {"total": {"value": "4210.0", "by": "you", "label": "Total due"}}
    block = assistant._source_block(1, email, 500, on_screen=True)
    assert "Total due: 4,210.00 (shown by the user)" in block


# The check pass and a fraud-held file -----------------------------------------------------------------------
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


def test_a_fraud_held_files_figure_does_not_bring_back_the_draft_over_the_check(store, settings, mail, monkeypatch):
    """A figure only a fraud-held email's file has doesn't count as "in the file" when the check pass drops it."""
    chat = chats.new_id()
    store.create_chat(chat)
    chats.add_file(store, settings, chat, "rent.txt", "text/plain", RENT)
    uploads = chats.chat_mail(store, chat)
    scam = mail["Updated remittance details"]
    held = replace(scam, attachments=[replace(scam.attachments[0], extracted_text="Wire $9,999.00 to account 5566778899")])
    assert attachments_locked(held)

    monkeypatch.setattr(assistant, "pick_sources", lambda *_a, **_k: ([held], False, set()))
    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "needs_more_context", lambda _s: False)
    monkeypatch.setattr(assistant, "context_length", lambda _s: 16384)
    monkeypatch.setattr(assistant, "complete_text", lambda *_a, **_k: "SQL: NONE")
    # The draft takes its figure from the held file; the check pass, reading only what may be read, drops it.
    monkeypatch.setattr(assistant, "chat_with_tools", lambda *_a, **_k: ToolReply("The October rent is $9,999.00."))
    monkeypatch.setattr(assistant, "stream_text", lambda *_a, **_k: iter(["The rent file doesn't give that amount for October."]))

    events = list(assistant.answer_stream(store, settings, "What is the October rent, and how did it change?", uploads=uploads))
    assert "Checking the answer against what I read" in [e["text"] for e in events if e["type"] == "step"]
    final = _final(events)
    assert "$9,999.00" not in final, f"the held file's figure put the draft back over the check: {final!r}"


# Guards against: The check pass exists to fix the draft against the file. When it replaces a figure the draft took
# from the wrong row with the right one (both in the file), _keep_stated_figures sees the draft's figure "dropped"
# and puts the wrong draft back with a "revise" event.
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


# Guards against: Notes the model writes on a conversation's files are stored on the stand-in email "chat-<id>".
# Replacing a file (same name, new version) or removing it clears its page readings (store.add_chat_file /
# remove_chat_file) but not those notes, so the old version's figures come back as "Notes from earlier reading" for
# the new file, and answer_check counts them as read (_read_material includes earlier_findings): a stale figure
# isn't flagged.
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


# Guards against: An email the model opens with a tool (open_email) carries no fraud warning, though the same email
# in the first prompt gets "Warning: payment-detail change — verify by phone." (assistant._source_block). For a
# bank-change email with no attachment (the common BEC shape) nothing in the tool result marks it.
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


# Replacing or removing one of a conversation's files clears that file's notes only: notes the model wrote from the
# conversation's other, unchanged files are kept.


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


# 6-8. Model reading guard rails.
def _fraud_email():
    return EmailRecord(
        id="e1", subject="Bank change", sender_name="V", sender_email="ap@v.example", received_at="2026-10-01T15:00:00+00:00",
        body_text="Our banking details have changed. Invoice total $48,500.00 due.", body_preview="", has_attachments=False,
        outlook_importance="normal", is_read=False, category=DocumentType.PAYMENT_INSTRUCTION_CHANGE, category_confidence=0.9,
        importance=Importance.CRITICAL, importance_score=100, flags=["fraud_risk", "do_not_process"],
        extracted=ExtractedFields(amounts=[48500.0]), summary="script",
    )


def test_amount_check_matches_whole_numbers_only():
    email = _fraud_email()
    assert _ungrounded_amounts("Pay $4,850 deposit.", email) == ["$4,850"]
    assert _ungrounded_amounts("Pay $500.", email) == ["$500"]
    assert _ungrounded_amounts("Invoice for $48,500.00.", email) == []
    assert _number_in("48500", "total 48500.00") and not _number_in("4850", "total 48500.00")


# Ask CloseDesk without a model: "due this week" keeps the matches due then; a letterhead isn't read as a table.
def test_a_lookup_for_invoices_due_this_week_leaves_out_later_and_undated_ones(store, settings):
    """On the sample board (Sep 22) no invoice is due this week; next week's is Harbor Packaging's (Oct 1) only."""
    from controller_inbox.assistant import answer_stream
    from controller_inbox.cli import load_sample

    settings.ensure_data_dir()
    load_sample(store, settings)

    def ask(question: str) -> str:
        return "".join(e.get("text", "") for e in answer_stream(store, settings, question, today="2026-09-22") if e["type"] == "delta")

    this_week = ask("Which invoices are due this week?")
    assert "None of the emails that match is due this week" in this_week and "INV-10482" not in this_week
    next_week = ask("Which invoices are due next week?")
    assert "Harbor Packaging" in next_week and "INV-10482" not in next_week
