"""Regression tests for the Ask CloseDesk fixes: answer checks, prompt budget, tool calls, notes, the fraud lock."""

from __future__ import annotations

import json
from array import array
from types import SimpleNamespace

from controller_inbox import agent, assistant, chats, file_summaries, semantic, tools
from controller_inbox.answer_check import review
from controller_inbox.local_llm import ToolReply


# 1. A figure that is in the material is never rewritten -----------------------------------------


def test_figures_in_the_file_are_never_rewritten_even_beside_total():
    invoice = "Net: $9,000\nVAT: $1,750\nShipping: $50\nTotal due: $10,800"
    answer = "The total is $10,800, made up of $9,000 net and $1,750 VAT."
    result = review(answer, material=[], files=[("inv.pdf", invoice)])
    assert (result.text, result.checks) == (answer, [])
    slip = review("Net $9,000 plus VAT $1,750 is a total of $10,700.", material=[], files=[("inv.pdf", invoice)])
    assert slip.text.endswith("a total of $10,750.") and slip.checks == ["Corrected $10,700 to $10,750 ($9,000 + $1,750)."]


# 2. Words shaped like cells ("H1", "Q3") are not cell references -----------------------------------


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


# 3. A page citation that holds every cited figure is kept ---------------------------------------


def test_a_right_page_is_kept_when_another_page_repeats_the_figure():
    pdf = (
        "[page 2]\nExecutive summary: consolidated revenue increased to 4,250,000 compared with prior quarter, "
        "driven by stronger subscription renewals\n[page 7]\nRevenue 4,250,000\n"
    )
    answer = "Consolidated revenue increased to 4,250,000 on page 7, driven by subscription renewals."
    result = review(answer, material=[], files=[("report.pdf", pdf)])
    assert (result.text, result.checks) == (answer, [])


# 4. The prompt fits its budget, and tools still run on a small context -----------------------------


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
        return ToolReply("The memo covers venue, travel and catering [1].")

    final = {}

    def fake_stream(_settings, messages, *, max_tokens):
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


# 5. An answer that says what day it is is not mistaken for an echo --------------------------------


def test_an_answer_that_starts_with_today_is_kept():
    answer = "Today is 2026-10-06, so INV-10482 [1] is 6 days overdue."
    assert "".join(assistant.without_echo(iter([answer]))) == answer
    echoed = "INV-10482 is overdue [1].\nToday is 2026-10-06.\nFocus list (most important first):\n- Overdue"
    assert "".join(assistant.without_echo(iter([echoed]))) == "INV-10482 is overdue [1]."


# 6. Tool-call arguments that aren't a JSON object ------------------------------------------------


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


# 7. Overnight summaries: spaced suffixes and whole-figure dropping ---------------------------------


def test_summary_lines_go_by_their_own_figures():
    text = "[page 1]\nRevenue 1,000,000. Balance $2,140."
    draft = "- Revenue was 2.5 million (page 1)\n- Growth was 5 percent (page 1)\n- 40 staff (page 1)\n- Balance $2,140 (page 1)"
    assert file_summaries._checked(draft, "f.pdf", text) == "- Balance $2,140 (page 1)"


# 8. The answer check sees everything the model was shown -------------------------------------------


def test_figures_from_the_focus_list_summaries_and_past_answers_are_not_flagged(store, settings, mail):
    budget = mail["Q4 budget draft"]
    budget.summary = "Maya asks for a check of the $1,234 travel line."
    ws = agent.Workspace(store, settings, [budget], question="what's due?", current_id=budget.id, past="Earlier: deposit was $7,777.")
    focus = [{"rank": 1, "label": "Overdue 12 days", "title": "Pay invoice 48,500", "email_id": budget.id}]
    answer = "Invoice 48,500 is overdue by 12 days; the travel line is $1,234 and the deposit was $7,777 [1]."
    events = list(assistant._checked(ws, answer, history=[], today="2026-10-06", focus=focus))
    assert events == []


# 9. Notes only on emails that were read, and labelled as data -------------------------------------


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


# 10. The fraud lock covers notes, earlier answers and the meaning index -----------------------------


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


# 11. The Bionic tool CLI answers a bad date in JSON ---------------------------------------------


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
