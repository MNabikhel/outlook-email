"""Overnight summaries size the prompt for a 450-token reply (prompt_budget(ctx, MAX_TOKENS)), but complete_text
asks a reasoning model for THINKING_ROOM (2,048) tokens. The chat sizes its prompts with reply_budget(); the
summaries don't, so on a 4,096-token model the request is ~1,600 tokens past the context and the model's thinking
runs out of room (EmptyReply -> the file is remembered with an empty summary and never retried)."""

from __future__ import annotations

from test_reasoning_models import FakeLMStudio, _serve

from controller_inbox import agent, file_summaries
from controller_inbox.models import AttachmentRecord, DocumentType


def test_summary_prompt_plus_requested_reply_fits_the_context(settings, monkeypatch):
    settings.llm = None
    server = FakeLMStudio()
    _serve(monkeypatch, server)
    rows = "\n".join(f"Vendor {r} owes {10000 + r * 37:,}.00 for invoice INV-{r:05d}, due in thirty days." for r in range(2, 92))
    att = AttachmentRecord(
        id="a1", email_id="e1", filename="ap notes.txt", content_type="", size_bytes=1, sha256="x",
        extracted_text=rows, document_type=DocumentType.OTHER, document_confidence=1.0,
    )
    monkeypatch.setattr(file_summaries, "context_length", lambda _s: 4096)
    file_summaries.summarize_file(settings, att)
    chats = [call for call in server.calls if "messages" in call]
    assert chats
    first = chats[0]
    prompt_tokens = sum(agent.prompt_size(m["content"]) for m in first["messages"]) / agent.CHARS_PER_TOKEN
    total = prompt_tokens + 250 + first["max_tokens"]
    assert total <= 4096, f"prompt {prompt_tokens:.0f} + 250 + max_tokens {first['max_tokens']} = {total:.0f} > 4096"
