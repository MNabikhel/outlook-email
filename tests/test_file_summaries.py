"""Long attachments are summarized overnight, checked against the file, and used to answer "summarize this file" at once."""

from __future__ import annotations

import pytest

from controller_inbox import agent, assistant, file_summaries
from controller_inbox.assistant import answer_stream

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
    assert [e["text"] for e in events if e["type"] == "step"] == ["Used the summary of Audit report.pdf written during the overnight reading"]

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
