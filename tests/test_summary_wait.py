"""File summaries and AI linking wait as long as the model needs, and a run stops at the first one that runs out.

Reported in real use: Gemma 4 12B wrote 1,844 tokens at 6.77 a second (about 4.5 minutes of writing alone, after
reading a long file), and CloseDesk gave up first ("client disconnected, stopping generation" in LM Studio). The
summaries waited 5 to 6 minutes whatever the model, not the wait worked out for it or the one set in Settings, and
after a timeout went on to the next long file, which timed out the same way: the run looked stuck.
"""

from __future__ import annotations

import json

import httpx
import pytest

from controller_inbox import agent, file_summaries, model_fit
from controller_inbox.local_llm import CHAT_TIMEOUT, ModelStatus
from test_file_summaries import REPORT

MODEL = "gemma-4-12b"


@pytest.fixture
def status(monkeypatch):
    found = ModelStatus(mode="on", reachable=True, model=MODEL)
    monkeypatch.setattr(file_summaries, "check_model", lambda *_a, **_k: found, raising=False)
    return found


@pytest.fixture
def two_files(store, mail):
    email = mail["Q4 budget draft"]
    for att in email.attachments:
        att.extracted_text = f"{att.filename} body. " + "The team reviewed onboarding and payment runs. " * 60
    store.upsert_email(email)
    return store.get_email(email.id)


@pytest.fixture
def audit_report(store, mail):
    email = mail["Q4 budget draft"]
    memo = next(att for att in email.attachments if att.filename == "Offsite memo.docx")
    memo.filename, memo.extracted_text = "Audit report.pdf", REPORT
    store.upsert_email(email)
    return store.get_email(email.id)


def _timeout(waited: float) -> httpx.ReadTimeout:
    exc = httpx.ReadTimeout("timed out")
    exc.waited, exc.written = waited, 2498  # as local_llm._post_chat marks a request that ran out of time
    return exc


def _model(monkeypatch, *, fail=None):
    """The summary model: records each call's file and ``limit``; raises ``fail`` for every file when given."""
    calls: list[dict] = []

    def complete(_settings, messages, **kwargs):
        calls.append({"file": messages[1]["content"].split("\n")[0].removeprefix("File: "), "limit": kwargs.get("limit")})
        if fail is not None:
            raise fail
        return "- The team reviewed onboarding and payment runs."

    monkeypatch.setattr(file_summaries, "complete_text", complete)
    return calls


def _waiting(store):
    return store.files_to_summarize(min_chars=agent.SUMMARY_MIN_CHARS, limit=10, model=MODEL)


def test_summaries_wait_as_long_as_the_wait_set_in_settings(store, settings, two_files, status, monkeypatch):
    model_fit.set_wait(store, MODEL, 15)
    calls = _model(monkeypatch)
    assert file_summaries.summarize_files(store, settings, limit=10, model=MODEL) == 2
    assert [call["limit"] for call in calls] == [900.0, 900.0], "the user's 15 minutes, not the usual 5 to 6"


def test_a_short_wait_set_in_settings_is_never_shorter_than_before(store, settings, two_files, status, monkeypatch):
    model_fit.set_wait(store, MODEL, 2)
    calls = _model(monkeypatch)
    file_summaries.summarize_files(store, settings, limit=10, model=MODEL)
    assert calls and all(call["limit"] >= CHAT_TIMEOUT for call in calls)


def test_the_worked_out_wait_covers_reading_and_writing_at_the_models_measured_speed(store, settings, two_files, status, monkeypatch):
    # Measured on this computer: 40 prompt tokens a second, 6.77 written a second (the reported Gemma 4 12B).
    store.set_state(model_fit.RATES_KEY, json.dumps({MODEL: {"read": 40.0, "write": 6.77}}))
    calls = _model(monkeypatch)
    file_summaries.summarize_files(store, settings, limit=10, model=MODEL)
    from controller_inbox.local_llm import reply_budget

    # Nothing comes back from a call that isn't streamed until the reply, thinking included, is all written.
    writing = reply_budget(settings, file_summaries.MAX_TOKENS) / 6.77
    assert calls and all(call["limit"] > writing for call in calls)
    assert all(call["limit"] >= model_fit.reading_wait(store, settings, MODEL, status) for call in calls)


def test_a_summary_that_runs_out_of_time_stops_the_runs_summaries_and_says_so(store, settings, two_files, status, monkeypatch):
    calls = _model(monkeypatch, fail=_timeout(360.0))
    notes: list[str] = []
    assert file_summaries.summarize_files(store, settings, limit=10, model=MODEL, notes=notes) == 0
    assert len(calls) == 1, "the next long file isn't tried, to time out the same way"
    assert notes == [
        f"Stopped summarizing files: {MODEL} took longer than 6 minutes on {calls[0]['file']}. CloseDesk gives it "
        "longer next time, or Settings → Local model → Wait up to … minutes can give it longer now."
    ]
    assert len(_waiting(store)) == 2, "no empty summary is saved: both files are tried again next run"


def test_after_a_timeout_the_next_run_waits_longer(store, settings, two_files, status, monkeypatch):
    calls = _model(monkeypatch, fail=_timeout(360.0))
    file_summaries.summarize_files(store, settings, limit=10, model=MODEL)
    calls = _model(monkeypatch)
    assert file_summaries.summarize_files(store, settings, limit=10, model=MODEL) == 2
    assert calls[0]["limit"] >= 720.0, "twice the wait that ran out"


def test_a_wait_set_in_settings_that_runs_out_says_so(store, settings, two_files, status, monkeypatch):
    model_fit.set_wait(store, MODEL, 10)
    calls = _model(monkeypatch, fail=_timeout(600.0))
    notes: list[str] = []
    file_summaries.summarize_files(store, settings, limit=10, model=MODEL, notes=notes)
    assert notes == [
        f"Stopped summarizing files: {MODEL} took longer than 10 minutes on {calls[0]['file']}. "
        "Settings → Local model → Wait up to … minutes can give it longer."
    ]
    calls = _model(monkeypatch)
    file_summaries.summarize_files(store, settings, limit=10, model=MODEL)
    assert calls[0]["limit"] == 600.0, "the user's own wait is kept as they set it"


def test_the_overnight_run_says_the_summaries_stopped(store, settings, audit_report, status, monkeypatch):
    from controller_inbox.overnight import run_overnight
    from test_bionic import AgreeingReader

    reader = AgreeingReader()
    reader.model = MODEL
    calls = _model(monkeypatch, fail=_timeout(360.0))
    result = run_overnight(store, settings, limit=1, sync_graph=False, reader=reader)
    assert len(calls) == 1
    assert result["model"] and "Stopped summarizing files" in result["model_note"]
    assert "Stopped summarizing files" in open(result["log_path"], encoding="utf-8").read()
