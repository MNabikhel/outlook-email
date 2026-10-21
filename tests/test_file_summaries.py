"""Long attachments are summarized overnight, checked against the file, and used to answer "summarize this file" at once."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from controller_inbox import agent, assistant, file_summaries
from controller_inbox.assistant import answer_stream
from controller_inbox.config import Settings
from controller_inbox.local_llm import ContextOverflow
from controller_inbox.models import AttachmentRecord, DocumentType, RawAttachment, RawMessage
from controller_inbox.pipeline import process_message
from controller_inbox.store import Store
from test_reasoning_models import FakeLMStudio, _serve

REPORT = "\n\n".join(
    f"[page {n}]\nSection {n}.1: The team reviewed supplier onboarding and payment runs and noted no exceptions."
    + ("\nFINDING 4 (HIGH): 3 vendor bank-detail changes in July were approved without a call-back. Fix by 30 November 2026." if n == 17 else "")
    for n in range(1, 25)
)
DRAFT = (
    "Here is the summary:\n"
    "- 3 vendor bank-detail changes in July were approved without a call-back (page 12).\n"
    "- The fix is due 30 November 2026 (page 17).\n"
    "- Unpaid invoices total $48,200 (page 3).\n"
    "- 24 pages reviewed with no other exceptions."
)


@pytest.fixture
def report(store, mail):
    email = mail["Q4 budget draft"]
    memo = next(att for att in email.attachments if att.filename == "Offsite memo.docx")
    memo.filename, memo.extracted_text = "Audit report.pdf", REPORT
    store.upsert_email(email)
    return store.get_email(email.id)


def _model(monkeypatch, reply=DRAFT):
    calls = []

    def complete(settings, messages, **_kwargs):
        calls.append(messages)
        return reply

    monkeypatch.setattr(file_summaries, "complete_text", complete)
    return calls


def test_long_files_are_summarized_and_every_figure_is_checked(store, settings, report, monkeypatch):
    calls = _model(monkeypatch)
    assert file_summaries.summarize_files(store, settings, limit=10, model="m") == 1
    assert "Audit report.pdf" in calls[0][1]["content"] and "FINDING 4" in calls[0][1]["content"]
    att = next(a for a in store.get_email(report.id).attachments if a.filename == "Audit report.pdf")
    summary = agent.overnight_summary(store, att)
    assert summary.splitlines() == [
        "- 3 vendor bank-detail changes in July were approved without a call-back (page 17).",
        "- The fix is due 30 November 2026 (page 17).",
        "- 24 pages reviewed with no other exceptions.",
    ], "the page is corrected and the made-up $48,200 line is dropped"
    assert file_summaries.summarize_files(store, settings, limit=10) == 0, "not summarized twice"

    att.extracted_text = REPORT + "\n[page 25]\nAppendix."
    assert agent.overnight_summary(store, att) == "", "a summary of older text isn't used"


def test_summarize_this_file_is_answered_at_once_from_the_overnight_summary(store, settings, report, monkeypatch):
    _model(monkeypatch)
    file_summaries.summarize_files(store, settings, limit=10)
    monkeypatch.setattr(assistant, "llm_active", lambda _settings: True)
    monkeypatch.setattr(assistant, "_model_answer", lambda *a, **k: pytest.fail("the model shouldn't be asked"))
    events = list(answer_stream(store, settings, "Summarize the audit report", email_id=report.id))
    text = "".join(e["text"] for e in events if e["type"] == "delta")
    assert text.startswith("**Audit report.pdf**\n- 3 vendor bank-detail changes in July") and "Written overnight" in text
    assert [e["text"] for e in events if e["type"] == "step"][-1] == "Used the summary of Audit report.pdf written during the overnight reading"

    ws = agent.Workspace(store, settings, [report], question="summarize this email", current_id=report.id)
    assert agent.summary_request(ws, "summarize this email") is None, "the email itself, not a file"
    ws.question = "what was finding 4?"
    block = agent.file_context(ws, ws.question, 3000)[report.id]
    assert "Summary written overnight (checked against the file):\n- 3 vendor" in block, "a map of a file too long to show"


def test_files_on_flagged_mail_are_never_summarized(store, settings, report, monkeypatch):
    calls = _model(monkeypatch)
    monkeypatch.setattr(file_summaries, "attachments_locked", lambda email: email.id == report.id)
    assert file_summaries.summarize_files(store, settings, limit=10) == 0 and calls == []


def test_the_overnight_run_summarizes_after_reading(store, settings, report, monkeypatch):
    from controller_inbox.overnight import run_overnight
    from test_bionic import AgreeingReader

    _model(monkeypatch)
    result = run_overnight(store, settings, limit=1, sync_graph=False, reader=AgreeingReader())
    assert result["files_summarized"] == 1
    assert "Attachments summarized for Ask CloseDesk: 1" in open(result["log_path"], encoding="utf-8").read()


@pytest.fixture
def two_files(store, mail):
    email = mail["Q4 budget draft"]
    for att in email.attachments:
        att.extracted_text = f"{att.filename} body. " + "The team reviewed onboarding and payment runs. " * 60
    store.upsert_email(email)
    return store.get_email(email.id)


def _failing_on_first(monkeypatch, store, email, error):
    # The most important file comes first in the queue every night.
    first_id = store.files_to_summarize(min_chars=agent.SUMMARY_MIN_CHARS, limit=10)[0][1]
    first = next(att.filename for att in email.attachments if att.id == first_id)
    calls = []

    def complete(settings, messages, **_kwargs):
        calls.append(messages[1]["content"].split("\n")[0])
        if first in calls[-1]:
            raise error
        return "- The team reviewed onboarding and payment runs."

    monkeypatch.setattr(file_summaries, "complete_text", complete)
    return calls


def _status(code):
    request = httpx.Request("POST", "http://127.0.0.1:11434/v1/chat/completions")
    return httpx.HTTPStatusError(f"{code}", request=request, response=httpx.Response(code, request=request))


@pytest.mark.parametrize("error", [ContextOverflow("exceeds the context"), _status(400)])
def test_a_file_the_model_fails_on_is_remembered_and_the_others_still_get_summaries(store, settings, two_files, monkeypatch, error):
    calls = _failing_on_first(monkeypatch, store, two_files, error)
    assert file_summaries.summarize_files(store, settings, limit=10, model="m") == 1, "the other file is summarized"
    assert len(calls) == 2
    calls.clear()
    assert file_summaries.summarize_files(store, settings, limit=1, model="m") == 0
    assert calls == [], "the failed file isn't tried every night, holding the nightly slot"
    assert store.files_to_summarize(min_chars=agent.SUMMARY_MIN_CHARS, limit=10, model="m") == []


def test_a_file_that_times_out_is_tried_again_another_night(store, settings, two_files, monkeypatch):
    calls = _failing_on_first(monkeypatch, store, two_files, httpx.ReadTimeout("timed out"))
    assert file_summaries.summarize_files(store, settings, limit=10, model="m") == 1, "the other file is summarized"
    assert len(calls) == 2
    assert len(store.files_to_summarize(min_chars=agent.SUMMARY_MIN_CHARS, limit=10, model="m")) == 1, "not marked"


@pytest.mark.parametrize("error", [httpx.ConnectError("refused"), _status(503)])
def test_summaries_stop_when_the_model_server_is_down(store, settings, two_files, monkeypatch, error):
    calls = _failing_on_first(monkeypatch, store, two_files, error)
    assert file_summaries.summarize_files(store, settings, limit=10, model="m") == 0
    assert len(calls) == 1, "no point asking for the next file"
    assert len(store.files_to_summarize(min_chars=agent.SUMMARY_MIN_CHARS, limit=10, model="m")) == 2, "nothing saved"


# Guards against: Overnight summaries size the prompt for a 450-token reply (prompt_budget(ctx, MAX_TOKENS)), but
# complete_text asks a reasoning model for THINKING_ROOM (2,048) tokens. The chat sizes its prompts with
# reply_budget(); the summaries don't, so on a 4,096-token model the request is ~1,600 tokens past the context and
# the model's thinking runs out of room (EmptyReply -> the file is remembered with an empty summary and never
# retried).
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


NOW = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)


def _raw(body: str, *, received: datetime, attachments=None, preview: str = "") -> RawMessage:
    return RawMessage(
        id="raw-1",
        subject="Docs",
        sender_name="A Vendor",
        sender_email="a@vendor.com",
        received_at=received,
        body_text=body,
        body_preview=preview,
        has_attachments=bool(attachments),
        source="folder",
        attachments=attachments or [],
    )


# Overnight file summaries: flagged mail doesn't use up the night, empty ones aren't retried -----
LONG_FILE = ("Line item freight services rendered in September per contract. " * 60).encode()


def _mail_with_long_file(store: Store, settings: Settings, n: int, *, scam: bool):
    raw = _raw(
        "Our bank details have changed. Please use the new account for all payments from today."
        if scam else "Please find the monthly report attached.",
        received=NOW,
        attachments=[RawAttachment(id="f.txt", filename=f"file{n}.txt", content_type="text/plain", size_bytes=len(LONG_FILE), content=LONG_FILE)],
    )
    raw.id, raw.subject = f"m{n}", "Updated remittance details" if scam else f"Monthly report {n}"
    raw.sender_email = f"billing@acme-pay{n}.net" if scam else "maya@taz.com"
    return process_message(raw, store, settings, now=NOW)


def test_files_on_flagged_mail_do_not_use_up_the_nights_summaries(store: Store, settings: Settings, monkeypatch):
    from controller_inbox import file_summaries

    # Flagged mail scores highest, so it would fill a night's quota of 3 and nothing else would ever be summarized.
    assert all("fraud_risk" in _mail_with_long_file(store, settings, n, scam=True).flags for n in range(3))
    _mail_with_long_file(store, settings, 9, scam=False)
    calls: list[str] = []
    monkeypatch.setattr(file_summaries, "summarize_file", lambda _settings, att: calls.append(att.filename) or "- Freight for September")
    assert file_summaries.summarize_files(store, settings, limit=3, model="m") == 1
    assert calls == ["file9.txt"]


def test_a_file_the_model_cannot_summarize_is_not_tried_every_night(store: Store, settings: Settings, monkeypatch):
    from controller_inbox import file_summaries

    _mail_with_long_file(store, settings, 1, scam=False)
    calls: list[str] = []
    monkeypatch.setattr(file_summaries, "summarize_file", lambda _settings, att: calls.append(att.id) or "")
    for _night in range(3):
        assert file_summaries.summarize_files(store, settings, limit=5, model="small") == 0
    assert calls == ["m1:f.txt"]
    file_summaries.summarize_files(store, settings, limit=5, model="bigger")
    assert len(calls) == 2, "another model gets a try"


# A NUL character in a file's text is not stored -------------------------------------------
NUL_EXPORT = b"2026-09-01  Wire to Alpha Freight   1,250.00\n" * 80 + b"END\x00\x00\n"


def test_a_file_with_a_nul_in_its_text_is_summarized_once(store: Store, settings: Settings, monkeypatch):
    from controller_inbox import file_summaries

    # SQLite's length() stops at a NUL and Python's len() doesn't, so the summary never matched its text.
    export = RawAttachment(id="export.txt", filename="export.txt", content_type="text/plain", size_bytes=len(NUL_EXPORT), content=NUL_EXPORT)
    record = process_message(_raw("Export attached", received=NOW, attachments=[export]), store, settings, now=NOW)
    assert "\x00" not in record.attachments[0].extracted_text
    calls: list[str] = []
    monkeypatch.setattr(file_summaries, "summarize_file", lambda _settings, att: calls.append(att.id) or "- Wires to Alpha Freight")
    for _night in range(3):
        file_summaries.summarize_files(store, settings, limit=20, model="m")
    assert calls == ["raw-1:export.txt"]


def test_text_stored_with_a_nul_before_is_cleaned_once(store: Store, settings: Settings):
    import sqlite3

    export = RawAttachment(id="export.txt", filename="export.txt", content_type="text/plain", size_bytes=3, content=b"abc")
    process_message(_raw("Export attached", received=NOW, attachments=[export]), store, settings, now=NOW)
    with sqlite3.connect(settings.db_path) as conn:  # as an older version left it
        conn.execute("UPDATE attachments SET extracted_text = ?", ("a\x00b",))
        conn.execute("DELETE FROM sync_state WHERE key = 'attachment_text_without_nul'")
    assert Store(settings.db_path).get_email("raw-1").attachments[0].extracted_text == "ab"
