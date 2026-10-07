"""Chat-path bugs found in review: budgets, reasoning tags, figures written with K/M, runaway arithmetic."""

from __future__ import annotations

import copy
import time

from controller_inbox import agent, assistant, file_summaries, local_llm
from controller_inbox.models import AttachmentRecord, DocumentType


def test_a_nested_power_is_refused_at_once(store, settings):
    ws = agent.Workspace(store, settings, [])
    started = time.monotonic()
    result = agent.run_tool(ws, "calculate", {"expression": "((((((9**12)**12)**12)**12)**12)**12)"}, limit=600)
    assert time.monotonic() - started < 0.5 and result.startswith("Couldn't work that out")
    assert "= 1,024" in agent.calculate("2**10")


def test_a_figure_written_with_k_or_m_in_the_email_is_the_emails(loaded, settings, monkeypatch):
    email = copy.deepcopy(loaded.get_email("demo-question"))
    email.body_text = "Hi, the Q4 headcount plan adds $1.2M in salaries and a $250K recruiting fee. Can you confirm by Friday?"
    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "complete_text", lambda *_a, **_k: "Hi Maya,\n\nI can confirm the $1.2M in salaries and the $250K fee by Friday.\n\nBest,\n[Your name]")
    assert assistant.draft_reply(settings, email)["mode"] == "model"


def test_an_overnight_summary_prompt_fits_the_context(settings, monkeypatch):
    rows = "\n".join(f"A{r}: Vendor {r} | B{r}: {10000 + r * 37:,}.00 | C{r}: {r * 913 % 9000:,}.50 | D{r}: =B{r}-C{r}" for r in range(2, 140))
    att = AttachmentRecord(
        id="a1", email_id="e1", filename="aging.xlsx", content_type="", size_bytes=1, sha256="x",
        extracted_text='[sheet "AP aging"]\n' + rows, document_type=DocumentType.SPREADSHEET, document_confidence=1.0,
    )
    monkeypatch.setattr(file_summaries, "context_length", lambda _s: 4096)
    sent = {}
    monkeypatch.setattr(file_summaries, "complete_text", lambda _s, messages, **_k: sent.setdefault("m", messages) and "- x (A1)")
    file_summaries.summarize_file(settings, att)
    used = sum(agent.prompt_size(m["content"]) for m in sent["m"])
    assert used <= agent.prompt_budget(4096, file_summaries.MAX_TOKENS, tools=False)


def test_a_body_padded_with_spaces_keeps_to_its_budget(mail):
    email = copy.deepcopy(mail["Q4 budget draft"])
    lines = [f"{'Invoice':<28}{'Date':<20}{'Amount':>16}"] + [
        f"{'INV-' + str(10400 + n):<28}{'2026-09-' + format(n % 28 + 1, '02d'):<20}{'$' + format(1000 + n * 37, ',') + '.00':>16}"
        for n in range(250)
    ]
    email.body_text = "Remittance advice\r\n\r\n" + "\r\n".join(lines)  # plain-text bodies are stored as sent
    email.attachments = []
    for budget in (6000, 10000):
        assert assistant.prompt_chars(assistant.build_messages("what was paid?", [email], budget=budget, current_id=email.id)) <= budget


def test_reasoning_before_a_lone_closing_think_tag_is_not_the_answer(store, settings, mail, monkeypatch):
    # A chat template that opens <think> in the prompt leaves only "</think>" in the reply.
    reply = "Okay, the user asks about the offsite. Maybe it costs $9,999? Let me check.\n</think>\n\nThe offsite is in Lisbon [1]."
    assert local_llm.strip_thinking(reply) == "The offsite is in Lisbon [1]."
    budget = mail["Q4 budget draft"]
    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "complete_text", lambda *_a, **_k: "SQL: NONE")
    monkeypatch.setattr(assistant, "stream_text", lambda *_a, **_k: iter([reply[i : i + 7] for i in range(0, len(reply), 7)]))
    events = list(assistant.answer_stream(store, settings, "Where is the offsite in this memo?", email_id=budget.id))
    final = [e["text"] for e in events if e["type"] == "revise"][-1]
    assert final == "The offsite is in Lisbon [1]."


def test_a_reply_that_is_only_reasoning_falls_back_instead_of_an_empty_answer(store, settings, mail, monkeypatch):
    reply = "Okay, the user asks about the offsite. Let me think about where it is.\n</think>\n"
    budget = mail["Q4 budget draft"]
    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "complete_text", lambda *_a, **_k: "SQL: NONE")
    monkeypatch.setattr(assistant, "stream_text", lambda *_a, **_k: iter([reply]))
    events = list(assistant.answer_stream(store, settings, "Where is the offsite in this memo?", email_id=budget.id))
    assert any(e["type"] == "mode" and e["mode"] == "lookup" for e in events), "the lookup answer takes its place"
    assert "".join(e["text"] for e in events if e["type"] == "delta").strip()
