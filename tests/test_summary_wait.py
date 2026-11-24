"""File summaries and AI linking wait as long as the model needs, and a run stops at the first one that runs out.

Reported in real use: Gemma 4 12B wrote 1,844 tokens at 6.77 a second (about 4.5 minutes of writing alone, after
reading a long file), and CloseDesk gave up first ("client disconnected, stopping generation" in LM Studio). The
summaries waited 5 to 6 minutes whatever the model, not the wait worked out for it or the one set in Settings, and
after a timeout went on to the next long file, which timed out the same way: the run looked stuck.
"""

from __future__ import annotations

import json
import re
import select
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from controller_inbox import agent, file_summaries, link_judge, local_llm, model_fit, web_common
from controller_inbox.local_llm import CHAT_TIMEOUT, ModelStatus, reply_budget
from helpers import PAGE
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
        "itself. The next run goes on with the rest and leaves it until a longer wait is set in Settings → Local "
        "model → Wait up to … minutes, or a smaller model is quicker."
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


def test_a_wait_set_in_settings_that_runs_out_is_doubled_next_time_as_settings_says(store, settings, two_files, status, monkeypatch):
    """Settings says a summary waits at least the wait set there, "and longer when the model needs it". One that ran
    out of the user's 10 minutes was never tried again: the next run went on without it, under a green toast."""
    model_fit.set_wait(store, MODEL, 10)
    first, other = _first_and_other(store, two_files)
    calls = _model(monkeypatch, slow={first})
    notes: list[str] = []
    file_summaries.summarize_files(store, settings, limit=10, model=MODEL, notes=notes)
    assert notes == [
        f"Stopped summarizing files: {MODEL} took longer than 10 minutes on {first}. CloseDesk waits up to 20 minutes "
        "next time, or Settings → Local model → Wait up to … minutes can give it longer now."
    ]
    calls = _model(monkeypatch)
    assert file_summaries.summarize_files(store, settings, limit=10, model=MODEL) == 2
    assert [(call["file"], call["limit"]) for call in calls] == [(other, 1200.0), (first, 1200.0)]


def test_a_wait_set_in_settings_longer_than_closedesk_gives_that_runs_out_leaves_the_file(store, settings, two_files, status, monkeypatch):
    model_fit.set_wait(store, MODEL, 60)
    first, other = _first_and_other(store, two_files)
    _model(monkeypatch, slow={first})
    notes: list[str] = []
    file_summaries.summarize_files(store, settings, limit=10, model=MODEL, notes=notes)
    assert notes == [
        f"Stopped summarizing files: {MODEL} took longer than 60 minutes on {first}, the wait you set. The next run "
        "goes on with the rest and leaves it until a longer wait is set in Settings → Local model → Wait up to … minutes."
    ], "CloseDesk gives no more than 30 minutes by itself: nothing longer is promised"
    calls = _model(monkeypatch)
    file_summaries.summarize_files(store, settings, limit=10, model=MODEL)
    assert [(call["file"], call["limit"]) for call in calls] == [(other, 3600.0)], "the user's own wait, as they set it"


def test_a_wait_typed_after_a_summary_ran_out_never_shortens_the_summaries_wait(store, settings, two_files, status, monkeypatch):
    first, _other = _first_and_other(store, two_files)
    _model(monkeypatch, slow={first})
    file_summaries.summarize_files(store, settings, limit=10, model=MODEL)  # ran out at 5 minutes: 10 next time
    # Settings shows the chat's wait, 5 minutes; the note said 10 for the summaries. 8 is more than the 5 shown.
    line = model_fit.describe(store, settings, status)["line"]
    assert "waits up to 5 minutes (set automatically), and up to 10 minutes for a file's summary or a related-mail check." in line
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


# --- What a run shows while it waits, and after -----------------------------------------------------------------


def test_each_summary_says_how_long_the_model_may_take_on_it(store, settings, two_files, status, monkeypatch):
    """The top bar sat on "Summarizing attachments · 1 of 2 · Q3 pack.pdf" for the whole wait, up to 12 minutes, and
    nothing said how long it might be: the run looked stuck."""
    model_fit.set_wait(store, MODEL, 12)
    first, other = _first_and_other(store, two_files)
    _model(monkeypatch)
    said: list[tuple] = []
    file_summaries.summarize_files(store, settings, limit=10, model=MODEL, on_progress=lambda *step: said.append(step))
    assert said == [
        (1, 2, first), (1, 2, f"{first} (up to 12 minutes)"), (2, 2, other), (2, 2, f"{other} (up to 12 minutes)"),
    ]


def test_settings_names_a_file_left_until_a_longer_wait_is_set(store, settings, two_files, status, monkeypatch):
    """A file that ran out of the longest wait was skipped by every later run under a green toast, and nothing anywhere
    said it was waiting for a longer wait."""
    store.set_state(model_fit.BACKGROUND_RAN_OUT_KEY, json.dumps({MODEL: model_fit.WAIT_CEILING}))
    first, _other = _first_and_other(store, two_files)
    _model(monkeypatch, slow={first})
    file_summaries.summarize_files(store, settings, limit=10, model=MODEL)
    assert model_fit.describe(store, settings, status)["line"].endswith(f" Left until a longer wait is set: {first}.")
    model_fit.set_wait(store, MODEL, 60)
    assert "Left until" not in model_fit.describe(store, settings, status)["line"], "60 minutes is longer: it is tried"


def test_settings_names_mail_left_until_a_longer_wait_is_set(store, settings, harbor, status, monkeypatch):
    settings.llm_model = MODEL
    store.set_state(model_fit.BACKGROUND_RAN_OUT_KEY, json.dumps({MODEL: model_fit.WAIT_CEILING}))
    _judge(monkeypatch, slow="Question on the Harbor quote 3")
    link_judge.judge_recent(store, settings, limit=10, now=NOW)
    line = model_fit.describe(store, settings, status)["line"]
    assert line.endswith(" Left until a longer wait is set: related mail for “Question on the Harbor quote 3”."), line


def _overnight(store, settings, monkeypatch, *, progress=None):
    from controller_inbox.overnight import run_overnight
    from test_bionic import AgreeingReader

    reader = AgreeingReader()
    reader.model = MODEL
    asked: list[str] = []

    def judge_recent(*_args, **_kwargs):
        asked.append("related mail")
        return 0

    monkeypatch.setattr(link_judge, "judge_recent", judge_recent)
    result = run_overnight(store, settings, limit=1, sync_graph=False, reader=reader, vision_minutes=0, on_progress=progress)
    return result, asked


def test_after_a_summary_ran_out_of_time_the_run_asks_no_related_mail_questions(store, settings, audit_report, status, monkeypatch):
    """LM Studio serving one request at a time goes on reading the prompt CloseDesk gave up on: the first related-mail
    question waited behind it, ran out of time too, was blamed for it ("took longer than 4 minutes on one question"),
    and made every later question and summary wait at least 8 minutes."""
    settings.overnight_ai_links = 10
    _model(monkeypatch, fail=_timeout(360.0))
    result, asked = _overnight(store, settings, monkeypatch)
    assert asked == [], "the model may still be busy with the file it was given up on"
    assert result["model_note"].startswith("Stopped summarizing files") and "related mail" not in result["model_note"]


def test_related_mail_is_still_looked_for_when_the_summaries_finish(store, settings, audit_report, status, monkeypatch):
    settings.overnight_ai_links = 10
    _model(monkeypatch)
    steps: list[tuple] = []
    _result, asked = _overnight(store, settings, monkeypatch, progress=lambda *step: steps.append(step))
    assert asked == ["related mail"]
    # The stage's label says what it is: a note saying it again showed "Looking for related mail · Looking for ...".
    assert ("linking", 1, 1, "") in steps


# --- Stop -------------------------------------------------------------------------------------------------------


class _WritesForever(BaseHTTPRequestHandler):
    """A model server that lists one model and never finishes a chat request (it is still writing a summary), noting
    when CloseDesk hung up on it. Anything else it doesn't have."""

    asked = threading.Event()
    hung_up: list[float] = []

    def log_message(self, *_args) -> None:
        pass

    def _reply(self, code: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/v1/models":
            return self._reply(200, {"data": [{"id": MODEL}]})
        self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path != "/v1/chat/completions":
            return self._reply(404, {"error": "not found"})
        type(self).asked.set()
        until = time.monotonic() + 20
        while time.monotonic() < until:
            ready, _, _ = select.select([self.connection], [], [], 0.05)
            if ready and not self.connection.recv(1):
                type(self).hung_up.append(time.monotonic())
                return
        self._reply(500, {"error": "gave up"})


def test_stop_hangs_up_on_the_summary_being_written_and_the_run_goes_on(store, settings, audit_report, monkeypatch):
    """Only reading scans had a Stop: a summary could only be waited out, for as long as 30 minutes."""
    from controller_inbox import web
    from controller_inbox.overnight import run_overnight
    from test_bionic import AgreeingReader

    _WritesForever.asked.clear()
    _WritesForever.hung_up.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _WritesForever)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    settings.llm = True
    settings.llm_base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    settings.overnight_ai_links = 10
    asked: list[str] = []
    monkeypatch.setattr(link_judge, "judge_recent", lambda *_a, **_k: asked.append("related mail") or 0)
    reader = AgreeingReader()
    reader.model = MODEL
    job = web.ProcessJob()
    try:
        assert job.start(lambda progress: run_overnight(
            store, settings, limit=1, sync_graph=False, reader=reader, vision_minutes=0, on_progress=progress,
        ))
        assert _WritesForever.asked.wait(20), "the summary was never asked for"
        state = job.snapshot()
        assert state["stage"] == "summarizing" and state["can_stop"] is True
        stopped = time.monotonic()
        assert job.request_stop()
        deadline = time.monotonic() + 15
        while job.snapshot()["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        server.shutdown()
    state = job.snapshot()
    assert state["state"] == "done", state["error"]
    assert _WritesForever.hung_up and _WritesForever.hung_up[0] - stopped < 3, "CloseDesk kept waiting for the summary"
    assert state["result"]["files_summarized"] == 0 and asked == [], "stopped: no related-mail questions either"
    assert not state["result"]["model_note"], "the user stopped it: no note that the model was too slow"
    assert model_fit.set_aside(store, MODEL) == {}, "not set aside as too slow: tried again, in turn, next run"
    assert len(store.files_to_summarize(min_chars=agent.SUMMARY_MIN_CHARS, limit=10, model=MODEL)) == 1


def test_the_job_says_how_long_its_step_has_taken():
    from controller_inbox import web

    now = [100.0]
    job = web.ProcessJob(clock=lambda: now[0])
    moved, gate = threading.Event(), threading.Event()

    def run(progress):
        progress("summarizing", 1, 2, "Q3 pack.pdf")
        moved.set()
        gate.wait(5)
        return {}

    assert job.start(run)
    assert moved.wait(5)
    now[0] += 200
    assert job.snapshot()["step_seconds"] == 200
    job.progress("summarizing", 1, 2, "Q3 pack.pdf (up to 12 minutes)")  # the same file, said with its wait
    assert job.snapshot()["step_seconds"] == 200
    job.progress("summarizing", 2, 2, "Vendor review.pdf")
    assert job.snapshot()["step_seconds"] == 0
    assert job.snapshot()["can_stop"] is False, "no model call it can hang up on is going"
    gate.set()


# --- The pages ----------------------------------------------------------------------------------------------------


def _summarizing(client, now: list[float]):
    """The app's job, writing a file's summary for 200 seconds so far, inside a step Stop can hang up on. Returns the
    event that lets it finish."""
    from controller_inbox import local_llm

    job = client.app.state.job
    job.clock = lambda: now[0]
    inside, gate = threading.Event(), threading.Event()

    def run(progress):
        with local_llm.stoppable() as stop:
            progress("summarizing", 1, 2, "Q3 pack.pdf (up to 12 minutes)")
            inside.set()
            while not (gate.is_set() or stop.done):
                time.sleep(0.02)
        return {"ingested": 0, "files_summarized": 0}

    assert job.start(run)
    assert inside.wait(5)
    now[0] += 200
    return gate


def test_the_classic_page_says_how_long_a_summary_has_taken_and_offers_stop(client):
    now = [1000.0]
    gate = _summarizing(client, now)
    try:
        page = client.get("/").text
        assert "Summarizing attachments · 1 of 2" in page and "Q3 pack.pdf (up to 12 minutes)" in page
        assert "3 minutes so far" in page
        form = re.search(r'<form method="post" action="/process/stop" class="job-stop"([^>]*)>(.*?)</form>', page, re.S)
        assert form and "hidden" not in form.group(1), "Stop is offered while the summary is written"
        assert ">Stop</button>" in form.group(2)
        reply = client.post("/process/stop", headers={"Origin": "http://testserver"}, follow_redirects=False)
        assert reply.headers["location"] == "/?notice=stopping-model"
    finally:
        gate.set()
    notice = client.get("/?notice=stopping-model").text.replace("&#39;", "'")
    assert web_common.NOTICES["stopping-model"] in notice


def test_the_workspace_state_says_how_long_a_summary_has_taken_and_stop_says_what_it_did(client):
    now = [1000.0]
    gate = _summarizing(client, now)
    try:
        job = client.get("/api/state", headers=PAGE).json()["job"]
        assert job["step_seconds"] == 200 and job["can_stop"] is True
        reply = client.post("/api/process/stop", headers={**PAGE, "Origin": "http://testserver"}).json()
        assert reply == {"ok": True, "stopping": True, "message": web_common.NOTICES["stopping-model"]}
    finally:
        gate.set()
    assert web_common.NOTICES["stopping-model"] == (
        "Stopped the local model. The rest of the run carries on; what it didn't get to is done next run."
    )


def test_the_classic_page_says_how_many_files_a_run_summarized(client):
    result = {"ingested": 0, "model": MODEL, "read_by_bionic": 0, "files_summarized": 2, "model_note": ""}
    job = client.app.state.job
    assert job.start(lambda progress: result)
    deadline = time.monotonic() + 5
    while job.snapshot()["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
    page = client.get("/").text
    assert "Summarized 2 files." in page, "a run that only summarized files said only “Read 0 new files.”"


def test_saving_a_wait_says_what_it_is_for(client):
    """The wait now also covers reading new mail and the summaries' least wait; saving it still said only Ask CloseDesk."""
    reply = client.post(
        "/api/settings/model-wait", headers={**PAGE, "Origin": "http://testserver"}, json={"model": MODEL, "minutes": 9}
    ).json()
    assert reply["message"] == "Saved. CloseDesk waits at least this long for this model from now on."


def test_the_launcher_window_names_each_step_in_words():
    from controller_inbox import cli

    assert cli._progress_line("summarizing", 1, 2, "Q3 pack.pdf (up to 12 minutes)") == (
        "  Summarizing attachments 1/2: Q3 pack.pdf (up to 12 minutes)"
    )
    assert cli._progress_line("linking", 1, 1, "") == "  Looking for related mail"
    assert cli._progress_line("reading", 3, 9, "Invoice 4410") == "  Model reading 3/9: Invoice 4410"
