"""File summaries and AI linking wait as long as the model needs, and a run stops at the first one that runs out.

Reported in real use: Gemma 4 12B wrote 1,844 tokens at 6.77 a second (about 4.5 minutes of writing alone, after
reading a long file), and CloseDesk gave up first ("client disconnected, stopping generation" in LM Studio). The
summaries waited 5 to 6 minutes whatever the model, not the wait worked out for it or the one set in Settings, and
after a timeout went on to the next long file, which timed out the same way: the run looked stuck.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from controller_inbox import agent, file_summaries, link_judge, local_llm, model_fit
from controller_inbox.local_llm import CHAT_TIMEOUT, ModelStatus, reply_budget
from test_file_summaries import REPORT

MODEL = "gemma-4-12b"


@pytest.fixture
def status(monkeypatch):
    found = ModelStatus(mode="on", reachable=True, model=MODEL)
    monkeypatch.setattr(file_summaries, "check_model", lambda *_a, **_k: found, raising=False)
    monkeypatch.setattr(link_judge, "check_model", lambda *_a, **_k: found, raising=False)
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


def _timeout(waited: float, written: int = 2048) -> httpx.ReadTimeout:
    exc = httpx.ReadTimeout("timed out")
    exc.waited, exc.written = waited, written  # as local_llm._post_chat marks a request that ran out of time
    return exc


def _given(settings, kwargs) -> tuple[float | None, int]:
    """The wait a call was given and the reply room it asked for, as ``complete_text`` works them out: ``limit`` is
    a number, or a function of the reply room the try asks for."""
    budget = reply_budget(settings, kwargs.get("max_tokens", 400))
    limit = kwargs.get("limit")
    return (limit(budget) if callable(limit) else limit), budget


def _model(monkeypatch, *, fail=None, slow=()):
    """The summary model: records each call's file and the wait it was given (``limit``); raises ``fail`` for every
    file when given, and runs out of time on each file in ``slow`` whatever it waits."""
    calls: list[dict] = []

    def complete(settings_, messages, **kwargs):
        limit, budget = _given(settings_, kwargs)
        name = messages[1]["content"].split("\n")[0].removeprefix("File: ")
        calls.append({"file": name, "limit": limit})
        if fail is not None:
            raise fail
        if name in slow:
            raise _timeout(max(local_llm._timeout(settings_, budget), limit or CHAT_TIMEOUT), budget)
        return "- The team reviewed onboarding and payment runs."

    monkeypatch.setattr(file_summaries, "complete_text", complete)
    return calls


def _waiting(store):
    return store.files_to_summarize(min_chars=agent.SUMMARY_MIN_CHARS, limit=10, model=MODEL)


def _first_and_other(store, email) -> tuple[str, str]:
    """The file first in line for a summary (the same one every run), and the other."""
    first = _waiting(store)[0][1]
    names = {att.id: att.filename for att in email.attachments}
    return names[first], next(name for att_id, name in names.items() if att_id != first)


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


# Measured on this computer: prompts read fast, but only one token written a second. The 450-token summary alone takes
# 7.5 minutes to write, more than the wait worked out from reading the model's whole context.
FAST_READ_SLOW_WRITE = {MODEL: {"read": 5000.0, "write": 1.0}}
WRITING = file_summaries.MAX_TOKENS / 1.0 * model_fit.MARGIN + model_fit.SPARE_SECONDS


def test_the_worked_out_wait_covers_reading_and_writing_at_the_models_measured_speed(store, settings, two_files, status, monkeypatch):
    store.set_state(model_fit.RATES_KEY, json.dumps(FAST_READ_SLOW_WRITE))
    calls = _model(monkeypatch)
    file_summaries.summarize_files(store, settings, limit=10, model=MODEL)
    # Nothing comes back from a call that isn't streamed until the reply, thinking included, is all written.
    reading = model_fit.reading_wait(store, settings, MODEL, status)
    assert reading < WRITING, "the reading alone doesn't cover it"
    assert calls and all(call["limit"] >= WRITING for call in calls)


def test_a_wait_saved_from_the_chat_never_cuts_a_summary_shorter_than_the_models_speed_needs(store, settings, two_files, status, monkeypatch):
    # "Save and ask again" in the chat keeps how long to wait for the first word of an answer: 6 minutes here.
    store.set_state(model_fit.RATES_KEY, json.dumps(FAST_READ_SLOW_WRITE))
    model_fit.set_wait(store, MODEL, 6)
    calls = _model(monkeypatch)
    assert file_summaries.summarize_files(store, settings, limit=10, model=MODEL) == 2
    assert calls and all(call["limit"] >= WRITING for call in calls), "the whole summary must be written in its wait"


def test_a_summary_that_runs_out_of_time_stops_the_runs_summaries_and_says_so(store, settings, two_files, status, monkeypatch):
    calls = _model(monkeypatch, fail=_timeout(360.0))
    notes: list[str] = []
    assert file_summaries.summarize_files(store, settings, limit=10, model=MODEL, notes=notes) == 0
    assert len(calls) == 1, "the next long file isn't tried, to time out the same way"
    assert notes == [
        f"Stopped summarizing files: {MODEL} took longer than 6 minutes on {calls[0]['file']}. CloseDesk waits up to "
        "12 minutes next time, or Settings → Local model → Wait up to … minutes can give it longer now."
    ]
    assert len(_waiting(store)) == 2, "no empty summary is saved: both files are tried again next run"


def test_after_a_timeout_the_next_run_waits_longer(store, settings, two_files, status, monkeypatch):
    calls = _model(monkeypatch, fail=_timeout(360.0))
    file_summaries.summarize_files(store, settings, limit=10, model=MODEL)
    calls = _model(monkeypatch)
    assert file_summaries.summarize_files(store, settings, limit=10, model=MODEL) == 2
    assert all(call["limit"] >= 720.0 for call in calls), "twice the wait that ran out"


def test_a_file_that_ran_out_of_time_waits_behind_the_others_next_run(store, settings, two_files, status, monkeypatch):
    first, other = _first_and_other(store, two_files)
    calls = _model(monkeypatch, slow={first})
    assert file_summaries.summarize_files(store, settings, limit=10, model=MODEL) == 0
    assert [call["file"] for call in calls] == [first]
    calls = _model(monkeypatch, slow={first})
    assert file_summaries.summarize_files(store, settings, limit=10, model=MODEL) == 1, "the other file gets its turn"
    assert [call["file"] for call in calls] == [other, first], "the one that ran out is tried after it, waiting longer"
    assert calls[1]["limit"] == 2 * CHAT_TIMEOUT


def test_a_file_that_runs_out_at_the_longest_wait_is_left_and_the_others_still_get_their_turn(store, settings, two_files, status, monkeypatch):
    store.set_state(model_fit.BACKGROUND_RAN_OUT_KEY, json.dumps({MODEL: model_fit.WAIT_CEILING}))  # 30 minutes now
    first, other = _first_and_other(store, two_files)
    calls = _model(monkeypatch, slow={first})
    notes: list[str] = []
    file_summaries.summarize_files(store, settings, limit=10, model=MODEL, notes=notes)
    assert [(call["file"], call["limit"]) for call in calls] == [(first, model_fit.WAIT_CEILING)]
    assert notes == [
        f"Stopped summarizing files: {MODEL} took longer than 30 minutes on {first}, the most CloseDesk gives it by "
        "itself. The next run goes on with the rest and leaves it until the model can wait longer: Settings → Local "
        "model → Wait up to … minutes can give it longer, or a smaller model is quicker."
    ], "no longer wait is promised: the next one would be 30 minutes again"
    for _run in range(3):
        calls = _model(monkeypatch, slow={first})
        file_summaries.summarize_files(store, settings, limit=10, model=MODEL)
        assert [call["file"] for call in calls] in ([other], []), "the file that can't get longer isn't tried again"
    assert len(_waiting(store)) == 1, "only that one still waits for a summary"
    model_fit.set_wait(store, MODEL, 60)
    calls = _model(monkeypatch)
    assert file_summaries.summarize_files(store, settings, limit=10, model=MODEL) == 1, "given longer, it is tried again"
    assert [(call["file"], call["limit"]) for call in calls] == [(first, 3600.0)]


def test_a_wait_worked_out_from_measured_speeds_that_runs_out_promises_no_longer_one(store, settings, two_files, status, monkeypatch):
    # Writing a third of a token a second: the worked-out wait is past 30 minutes, and doubling it stops at 30.
    store.set_state(model_fit.RATES_KEY, json.dumps({MODEL: {"read": 5000.0, "write": 0.3}}))
    first, _other = _first_and_other(store, two_files)
    calls = _model(monkeypatch, slow={first})
    notes: list[str] = []
    file_summaries.summarize_files(store, settings, limit=10, model=MODEL, notes=notes)
    assert calls[0]["limit"] > model_fit.WAIT_CEILING
    assert "next time" not in notes[0] and "the most CloseDesk gives it by itself" in notes[0]


def test_a_wait_set_in_settings_that_runs_out_says_so(store, settings, two_files, status, monkeypatch):
    model_fit.set_wait(store, MODEL, 10)
    first, other = _first_and_other(store, two_files)
    calls = _model(monkeypatch, slow={first})
    notes: list[str] = []
    file_summaries.summarize_files(store, settings, limit=10, model=MODEL, notes=notes)
    assert notes == [
        f"Stopped summarizing files: {MODEL} took longer than 10 minutes on {first}, the wait you set. The next run "
        "goes on with the rest and leaves it until the model can wait longer: Settings → Local model → Wait up to … "
        "minutes can give it longer."
    ]
    calls = _model(monkeypatch)
    file_summaries.summarize_files(store, settings, limit=10, model=MODEL)
    assert [(call["file"], call["limit"]) for call in calls] == [(other, 600.0)], "the user's own wait is kept as they set it"


def test_a_wait_typed_after_a_summary_ran_out_never_shortens_the_summaries_wait(store, settings, two_files, status, monkeypatch):
    first, _other = _first_and_other(store, two_files)
    _model(monkeypatch, slow={first})
    file_summaries.summarize_files(store, settings, limit=10, model=MODEL)  # ran out at 5 minutes: 10 next time
    # Settings shows the chat's wait, 5 minutes; the note said 10 for the summaries. 8 is more than the 5 shown.
    line = model_fit.describe(store, settings, status)["line"]
    assert "waits up to 5 minutes (set automatically), and up to 10 minutes for a file's summary." in line
    model_fit.set_wait(store, MODEL, 8)
    calls = _model(monkeypatch)
    file_summaries.summarize_files(store, settings, limit=10, model=MODEL)
    assert calls and all(call["limit"] >= 600.0 for call in calls), "not the 8 minutes typed: CloseDesk gave it 10"


def test_a_second_try_with_more_room_to_think_waits_as_long_as_that_room_needs(store, settings, two_files, monkeypatch):
    """A model that thinks though it wasn't known to: the first try asks for the summary's 450 tokens, it thinks
    them all away, and it is asked again with room to think (2,048 tokens). That try waits for its own room."""
    found = ModelStatus(mode="on", reachable=True, model=MODEL)
    for module in (local_llm, file_summaries):
        monkeypatch.setattr(module, "check_model", lambda *_a, **_k: found)
    store.set_state(model_fit.RATES_KEY, json.dumps(FAST_READ_SLOW_WRITE))
    replies = iter([
        {"choices": [{"message": {"content": "", "reasoning_content": "Let me read the file."}, "finish_reason": "length"}]},
        {"choices": [{"message": {"content": "- The team reviewed onboarding and payment runs."}, "finish_reason": "stop"}]},
    ])
    sent: list[tuple[int, float]] = []

    def post(url, json=None, timeout=None, **_kwargs):
        sent.append((json["max_tokens"], timeout))
        return httpx.Response(200, json=next(replies), request=httpx.Request("POST", url))

    monkeypatch.setattr(local_llm.httpx, "post", post)
    att = two_files.attachments[0]
    assert file_summaries.summarize_file(settings, att, store=store, model=MODEL, status=found)
    assert [tokens for tokens, _timeout in sent] == [450, local_llm.THINKING_ROOM]
    assert sent[1][1] >= local_llm.THINKING_ROOM / 1.0 * model_fit.MARGIN, "the 2,048 tokens take 34 minutes to write"


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


# --- AI linking ---------------------------------------------------------------------------------------------------


def _mail(store, id_, subject, *, sender="sales@harbor.com", body="", received="2026-10-05T09:00:00+00:00"):
    from test_links import _record

    record = _record(id_, sender=sender, received=received)
    record.subject, record.body_text = subject, body
    store.upsert_email(record)
    return store.get_email(id_)


@pytest.fixture
def harbor(store):
    for n in range(4):
        _mail(store, f"q{n}", f"Harbor quote number {n}", body="Harbor quote.", received=f"2026-10-0{n + 1}T09:00:00+00:00")
        _mail(store, f"a{n}", f"Question on the Harbor quote {n}", sender="ap@taz.com", body="About the Harbor quote.",
              received=f"2026-10-0{n + 1}T10:00:00+00:00")


def _judge(monkeypatch, *, fail=None, slow=""):
    """The model asked about related mail: records each question's first email (``about``) and the wait it was given;
    raises ``fail`` for every question when given, and runs out of time on each about the email titled ``slow``."""
    calls: list[dict] = []

    def complete(settings_, messages, **kwargs):
        limit, budget = _given(settings_, kwargs)
        about = messages[-1]["content"].split("Email B:", 1)[0]
        calls.append({"limit": limit, "slow": bool(slow) and slow in about})
        if fail is not None:
            raise fail
        if calls[-1]["slow"]:
            raise _timeout(max(local_llm._timeout(settings_, budget), limit or CHAT_TIMEOUT), budget)
        return json.dumps({"same_matter": True, "why": "Both are about the Harbor quote."})

    monkeypatch.setattr(link_judge, "complete_text", complete)
    monkeypatch.setattr(link_judge, "llm_active", lambda _settings: True)
    return calls


NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


def test_linking_waits_as_long_as_the_wait_set_in_settings(store, settings, harbor, status, monkeypatch):
    settings.llm_model = MODEL
    model_fit.set_wait(store, MODEL, 12)
    calls = _judge(monkeypatch)
    link_judge.judge_recent(store, settings, limit=4, now=NOW)
    assert calls and all(call["limit"] == 720.0 for call in calls)


def test_linking_that_runs_out_of_time_stops_for_the_run_and_says_so(store, settings, harbor, status, monkeypatch):
    settings.llm_model = MODEL
    calls = _judge(monkeypatch, fail=_timeout(300.0))
    notes: list[str] = []
    link_judge.judge_recent(store, settings, limit=10, now=NOW, notes=notes)
    assert len(calls) == 1
    assert notes == [
        f"Stopped looking for related mail: {MODEL} took longer than 5 minutes on one question. CloseDesk waits up to "
        "10 minutes next time, or Settings → Local model → Wait up to … minutes can give it longer now."
    ]


def test_an_email_a_question_ran_out_on_at_the_longest_wait_does_not_hold_up_the_rest(store, settings, harbor, status, monkeypatch):
    settings.llm_model = MODEL
    store.set_state(model_fit.BACKGROUND_RAN_OUT_KEY, json.dumps({MODEL: model_fit.WAIT_CEILING}))
    # The newest email: asked about first in every run.
    calls = _judge(monkeypatch, slow="Question on the Harbor quote 3")
    notes: list[str] = []
    assert link_judge.judge_recent(store, settings, limit=10, now=NOW, notes=notes) == 0
    assert [call["slow"] for call in calls] == [True]
    assert "the most CloseDesk gives it by itself" in notes[0] and "next time" not in notes[0]
    calls = _judge(monkeypatch, slow="Question on the Harbor quote 3")
    assert link_judge.judge_recent(store, settings, limit=10, now=NOW) > 0, "the rest of the mail is weighed"
    assert calls and not any(call["slow"] for call in calls), "the email that can't get longer isn't asked about again"
    assert store.ai_checked("a3") != link_judge.PROMPT_VERSION
