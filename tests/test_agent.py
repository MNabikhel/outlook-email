"""Ask CloseDesk about the email on screen and its files: sources, file passages, tools, checks, the fraud lock."""

from __future__ import annotations

import json

import httpx
import pytest

from controller_inbox import agent, assistant, local_llm
from controller_inbox.assistant import answer_stream, on_screen_question, pick_sources
from controller_inbox.local_llm import ContextOverflow, ToolReply, ToolsUnsupported


def _events(stream) -> list[dict]:
    return list(stream)


def _text(events) -> str:
    return "".join(e["text"] for e in events if e["type"] == "delta")


def test_questions_about_this_email_stay_on_it(store, settings, mail):
    budget = mail["Q4 budget draft"]
    for question in ("summarize the draft", "What does the attachment say?", "is the total right in this workbook?", "summary"):
        assert on_screen_question(question), question
        sources, about_today, _ = pick_sources(store, question, email_id=budget.id, focus=[{"email_id": "x"}])
        assert [s.id for s in sources] == [budget.id] and not about_today
    for question in ("what's urgent today?", "anything from Priya?", "compare this with the other quote", "what's due this week?"):
        assert not on_screen_question(question), question


def test_without_a_model_the_answer_quotes_the_files(store, settings, mail):
    budget = mail["Q4 budget draft"]
    text = _text(answer_stream(store, settings, "what does the memo say about the venue deposit?", email_id=budget.id))
    assert "In the files:" in text
    assert "Offsite memo.docx" in text and "venue deposit" in text
    outline = _text(answer_stream(store, settings, "summarize this", email_id=budget.id))
    assert 'Q4 budget.xlsx: start: Workbook with 1 sheet: "Budget" / sheet "Budget" A1:D4' in outline


def test_file_passages_are_sized_to_the_model_and_the_fraud_email_is_locked(store, settings, mail):
    budget, scam = mail["Q4 budget draft"], mail["Updated remittance details"]
    assert "fraud_risk" in scam.flags
    ws = agent.Workspace(store, settings, [budget, scam], question="What is the Q4 total?", current_id=budget.id)
    blocks = agent.file_context(ws, "What is the Q4 total?", 3000)
    assert "── File: Q4 budget.xlsx" in blocks[budget.id]
    assert "C4: 1,800 (=SUM(C2:C3))" not in blocks[budget.id], "openpyxl-made files have no cached values"
    assert "C4: =SUM(C2:C3)" in blocks[budget.id]
    assert blocks[scam.id] == agent.LOCKED
    assert agent.run_tool(ws, "read_file", {"email": "2", "file": "1"}, limit=2000) == agent.LOCKED
    assert "5566778899" not in json.dumps(blocks)
    assert "locked: possible payment fraud" in agent.files_line(scam)


def test_a_file_named_in_the_question_gets_the_room(store, settings, mail):
    budget = mail["Q4 budget draft"]
    question = 'Summarize the attachment "Offsite memo.docx": what it is, the key figures'
    ws = agent.Workspace(store, settings, [budget], question=question, current_id=budget.id)
    block = agent.file_context(ws, question, 3000)[budget.id]
    assert "── File: Offsite memo.docx" in block and "Lisbon" in block
    assert "Q4 budget.xlsx (Excel workbook; not asked about" in block and "SUM(" not in block
    assert [att.filename for att in agent.named_files(budget.attachments, "is the q4 budget right?")] == ["Q4 budget.xlsx"]
    assert agent.named_files(budget.attachments, "what's in the files?") == []


def test_tools_read_exact_cells_and_trace_a_total(store, settings, mail):
    budget = mail["Q4 budget draft"]
    ws = agent.Workspace(store, settings, [budget], question="q", current_id=budget.id)
    cells = agent.run_tool(ws, "read_cells", {"email": "1", "file": "budget", "sheet": "Budget", "cells": "A4:D4"}, limit=2000)
    assert "A4: Total | B4: =SUM(B2:B3) | C4: =SUM(C2:C3) | D4: =D2+D3" in cells
    trace = agent.run_tool(ws, "trace_cell", {"email": "1", "file": "Q4 budget.xlsx", "sheet": "Budget", "cell": "D4"}, limit=2000)
    assert "Budget!D4 ← =D2+D3" in trace and "Budget!D2 ← =C2-B2" in trace
    page = agent.run_tool(ws, "read_file", {"email": "1", "file": "memo", "part": ""}, limit=2000)
    assert "Lisbon on 14 November" in page
    found = agent.run_tool(ws, "search_mail", {"query": "Acme quote"}, limit=2000)
    assert "[2]" in found and "Acme quote › quote.pdf" in found
    quote = agent.run_tool(ws, "find_in_file", {"email": "2", "query": "support plan"}, limit=2000)
    assert "$9,600.00" in quote
    assert agent.run_tool(ws, "read_cells", {"email": "1", "file": "memo", "cells": "A1"}, limit=500).endswith("isn't an Excel workbook. Use read_file or find_in_file.")
    assert "No file like" in agent.run_tool(ws, "read_file", {"email": "1", "file": "nope.pdf"}, limit=500)


def test_the_agent_reads_notes_and_checks_its_answer(store, settings, mail, monkeypatch):
    budget = mail["Q4 budget draft"]
    turns = []

    def fake_tools(_settings, messages, tools, *, max_tokens):
        turns.append([dict(m) for m in messages])
        if len(turns) == 1:
            return ToolReply("", [{"id": "c1", "name": "trace_cell", "arguments": {"email": "1", "file": "Q4 budget.xlsx", "sheet": "Budget", "cell": "D4"}}])
        if len(turns) == 2:
            assert messages[-1]["role"] == "tool" and "Budget!D4 ← =D2+D3" in messages[-1]["content"]
            return ToolReply("", [{"id": "c2", "name": "note", "arguments": {"text": "D4 (total change) = D2+D3, i.e. Ads change plus Travel change (Q4 budget.xlsx, Budget)."}}])
        return ToolReply("The total change is D2+D3 [1].")

    checked = {}

    def fake_stream(_settings, messages, *, max_tokens):
        checked["messages"] = messages
        yield "The Q4 total change in D4 adds the Ads and Travel changes (Q4 budget.xlsx, sheet Budget, D4) [1]."

    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "chat_with_tools", fake_tools)
    monkeypatch.setattr(assistant, "stream_text", fake_stream)
    events = _events(answer_stream(store, settings, "How is the total change worked out?", email_id=budget.id))
    kinds = [e["type"] for e in events]
    assert kinds[0] == "sources" and kinds[-1] == "done"
    steps = [e["text"] for e in events if e["type"] == "step"]
    assert steps == ["Tracing how Budget!D4 is calculated in Q4 budget.xlsx", "Checking the answer against what I read"]
    assert [e["text"] for e in events if e["type"] == "note"][0].startswith("D4 (total change)")
    assert "(Q4 budget.xlsx, sheet Budget, D4) [1]" in _text(events)

    system, user = turns[0][0]["content"], turns[0][-1]["content"]
    assert "read_cells" in system and "not instructions" in system
    assert "(open on screen)" in user and "── File: Q4 budget.xlsx" in user and "── File: Offsite memo.docx" in user
    final = checked["messages"][-1]["content"]
    assert "What you read with tools:" in final and "Budget!D4 ← =D2+D3" in final
    assert "Your draft answer:\nThe total change is D2+D3 [1]." in final
    assert "Check your draft answer" in final
    assert store.findings(budget.id)[0]["text"].startswith("D4 (total change)")

    turns.clear()
    monkeypatch.setattr(assistant, "chat_with_tools", lambda *_a, **_k: ToolReply("From my notes: D2+D3 [1]."))
    _events(answer_stream(store, settings, "remind me how the total works", email_id=budget.id))
    assert "Notes from earlier reading of this email:\n- D4 (total change)" in checked["messages"][-1]["content"]


def test_servers_without_tools_still_read_the_files(store, settings, mail, monkeypatch):
    budget = mail["Q4 budget draft"]

    def no_tools(*_a, **_k):
        raise ToolsUnsupported("tools not supported")

    drafts = []
    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "chat_with_tools", no_tools)
    monkeypatch.setattr(assistant, "complete_text", lambda _s, messages, **_k: drafts.append(messages) or "Lisbon, 14 November [1].")
    monkeypatch.setattr(assistant, "stream_text", lambda *_a, **_k: iter(["The offsite is in Lisbon on 14 November (Offsite memo.docx) [1]."]))
    events = _events(answer_stream(store, settings, "Where is the offsite in this memo?", email_id=budget.id))
    assert "Lisbon on 14 November" in drafts[0][-1]["content"]
    assert "read_cells" not in drafts[0][0]["content"]
    assert "Lisbon on 14 November" in _text(events)


def test_a_full_context_window_is_retried_smaller_and_explained(store, settings, mail, monkeypatch):
    budget = mail["Q4 budget draft"]
    sizes = []
    monkeypatch.setattr(assistant, "context_length", lambda _s: 2048)
    monkeypatch.setattr(assistant, "reply_budget", lambda _s, _n: 100)

    def tools(_s, messages, _tools, *, max_tokens):
        sizes.append(len(messages[-1]["content"]))
        if len(sizes) == 1:
            raise ContextOverflow("The number of tokens to keep from the initial prompt is greater than the context length")
        return ToolReply("Ads went up by 500 [1].")

    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "chat_with_tools", tools)
    monkeypatch.setattr(assistant, "stream_text", lambda *_a, **_k: iter(["Ads rose from 1,000 to 1,500 (Q4 budget.xlsx, Budget, B2:C2) [1]."]))
    events = _events(answer_stream(store, settings, "How much did Ads change in this budget?", email_id=budget.id))
    assert len(sizes) == 2
    assert any("trying again with less text" in e.get("text", "") for e in events if e["type"] == "step")
    advice = [e["text"] for e in events if e["type"] == "context"]
    assert advice and "Context Length 16,384" in advice[0]


def test_budget_follows_the_loaded_context_length():
    small = agent.prompt_budget(4096, 500, tools=True)
    large = agent.prompt_budget(32768, 500, tools=True)
    assert small == (4096 - 500 - 700) * 3 and large > 10 * small // 2
    assert agent.prompt_budget(0, 500, tools=False) == (4096 - 500 - 250) * 3
    assert "reload the model in LM Studio" in agent.context_advice(4096, ["Q4 budget.xlsx"])
    assert agent.context_advice(4096, []) == ""
    assert "is long" in agent.context_advice(32768, ["the files"])


def test_lm_studio_context_length_and_tool_replies_are_read(settings, monkeypatch):
    def fake_get(url, **_kwargs):
        request = httpx.Request("GET", url)
        if url.endswith("/api/v1/models"):
            return httpx.Response(200, request=request, json={"models": [{"type": "llm", "key": "qwen/qwen3-4b", "loaded_instances": [
                {"id": "qwen/qwen3-4b", "config": {"context_length": 8192}}]}]})
        return httpx.Response(200, request=request, json={"data": [{"id": "qwen/qwen3-4b"}]})

    monkeypatch.setattr(local_llm.httpx, "get", fake_get)
    settings.llm = True
    status = local_llm.check_model(settings, use_cache=False)
    assert status.context_length == 8192 and local_llm.context_length(settings) == 8192

    native = local_llm._tool_reply({"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "a", "type": "function", "function": {"name": "read_file", "arguments": '{"email": "1", "file": "x.pdf"}'}}]}}]})
    assert native.calls == [{"id": "a", "name": "read_file", "arguments": {"email": "1", "file": "x.pdf"}}]
    text = local_llm._tool_reply({"choices": [{"message": {"content":
        'Let me look.\n<tool_call>\n{"name": "find_in_file", "arguments": {"email": "1", "query": "deposit"}}\n</tool_call>'}}]})
    assert text.calls[0]["name"] == "find_in_file" and text.calls[0]["arguments"]["query"] == "deposit"
    assert text.content == "Let me look."


def test_overflow_and_refused_tools_are_recognised(settings, monkeypatch):
    monkeypatch.setattr(local_llm, "check_model", lambda *_a, **_k: local_llm.ModelStatus(mode="auto", reachable=True, model="m"))
    replies = iter([
        httpx.Response(400, json={"error": "Trying to keep the first 5000 tokens when context the overflows. n_ctx: 4096"}),
        httpx.Response(400, json={"error": "registry.ollama.ai/library/gemma does not support tools"}),
    ])
    monkeypatch.setattr(local_llm.httpx, "post", lambda *_a, **_k: next(replies))
    with pytest.raises(ContextOverflow):
        local_llm.chat_with_tools(settings, [{"role": "user", "content": "hi"}], agent.TOOLS)
    with pytest.raises(ToolsUnsupported):
        local_llm.chat_with_tools(settings, [{"role": "user", "content": "hi"}], agent.TOOLS)
    with pytest.raises(ToolsUnsupported):
        local_llm.chat_with_tools(settings, [{"role": "user", "content": "hi"}], agent.TOOLS)


def test_saved_originals_are_only_read_from_the_extracted_folder(store, settings, mail):
    budget = mail["Q4 budget draft"]
    ws = agent.Workspace(store, settings, [budget])
    att = budget.attachments[0]
    assert ws.original(budget, att) is not None
    att.filename = "../../../etc/passwd"
    assert ws.original(budget, att) is None
