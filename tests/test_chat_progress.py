"""What the chat shows while a big model works: what it is reading and about how long that takes, then that it is
writing; and a model's reasoning as it comes, which never becomes part of the answer or of what the model is given
of the conversation next time (tests/test_chat_thinking.py). The fake LM Studio is tests/test_chat_time.py's."""

from __future__ import annotations

import re

import pytest
from test_chat_time import KEY, _ask, lm  # noqa: F401 - lm is the fixture

from controller_inbox import local_llm, model_fit


def _progress(events: list[dict]) -> list[dict]:
    return [event for event in events if event["type"] == "progress"]


# The estimate while it works ----------------------------------------------------------------------------------------


def test_the_chat_says_what_it_reads_and_about_how_long_then_that_it_writes(settings, loaded, lm):  # noqa: F811
    lm()
    model_fit.record_speed(loaded, KEY, prompt_tokens=5000, read_seconds=100.0)  # 50 tokens a second here
    events = _ask(loaded, settings)
    reading = _progress(events)[0]
    assert reading["state"] == "reading"
    assert re.fullmatch(r"Reading \d+ emails?( and \d+ files?)?", reading["text"]), reading["text"]
    assert reading["tokens"] > 500 and reading["seconds"] == pytest.approx(reading["tokens"] / 50, rel=0.01)
    assert reading["about"] == model_fit.about(reading["seconds"]) and reading["about"].startswith("about ")
    kinds = [(event["type"], event.get("state")) for event in events]
    writing = kinds.index(("progress", "writing"))
    first_word = next(i for i, event in enumerate(events) if event["type"] == "delta")
    assert kinds.index(("progress", "reading")) < writing < first_word, "reading, then writing, then the words"


def test_with_no_speed_measured_the_chat_says_what_it_reads_without_a_time(settings, loaded, lm):  # noqa: F811
    lm()
    reading = _progress(_ask(loaded, settings))[0]
    assert reading["state"] == "reading" and reading["seconds"] is None and reading["about"] == ""


def test_the_speeds_are_measured_from_the_chat_s_own_answers(settings, loaded, lm):  # noqa: F811
    lm(first_delay=0.4, gap=0.02)
    reading = _progress(_ask(loaded, settings))[0]
    rates = model_fit.speeds(loaded, KEY)
    # About the prompt's tokens over the time to its first word (0.4 s and a little), and some tokens a second.
    assert reading["tokens"] / 0.8 < rates["read"] < reading["tokens"] / 0.4
    assert 10 < rates["write"] < 200
    # The next question's estimate comes from it.
    assert _progress(_ask(loaded, settings))[0]["seconds"] > 0


def test_a_prompt_the_model_had_read_before_isnt_taken_for_a_fast_model(settings, loaded, lm):  # noqa: F811
    """LM Studio keeps the start of a prompt it has read: one asked again comes back almost at once. Counted as read,
    one such question made a 31B that reads 50 tokens a second look like it read thousands, and cut its wait to 5
    minutes. Only the tokens it read are counted; when the server doesn't say, a reading far faster than measured
    before isn't kept."""
    server = lm(first_delay=0.4)
    model_fit.record_speed(loaded, KEY, prompt_tokens=8000, read_seconds=160.0)  # 50 tokens a second
    server.usage = {"prompt_tokens": 9000, "completion_tokens": 40, "prompt_tokens_details": {"cached_tokens": 8990}}
    _ask(loaded, settings)
    assert model_fit.speeds(loaded, KEY)["read"] == 50, "10 tokens read: too few to tell"
    model_fit.record_speed(loaded, KEY, prompt_tokens=8500, read_seconds=0.5)  # not said: cached, most likely
    assert model_fit.speeds(loaded, KEY)["read"] == 50
    model_fit.record_speed(loaded, KEY, prompt_tokens=8000, read_seconds=100.0)  # faster, but not out of reach
    assert model_fit.speeds(loaded, KEY)["read"] == 65


def test_fast_readings_replace_a_slow_first_one_only_when_the_server_says_what_it_read_anew(settings, loaded, lm):  # noqa: F811
    """Three readings in a row far faster than the one kept are the model's own speed (the one kept waited behind
    another request) only when the server said how much of each prompt it had read before; when it didn't, they may
    be prompts it had read, and the speed kept stays."""
    server = lm(first_delay=0.2)
    model_fit.record_speed(loaded, KEY, prompt_tokens=8000, read_seconds=800.0)  # 10 tokens a second
    for _ in range(3):
        _ask(loaded, settings)
    assert model_fit.speeds(loaded, KEY)["read"] == 10, "not said: kept"
    server.usage = {"prompt_tokens": 9000, "completion_tokens": 40, "prompt_tokens_details": {"cached_tokens": 0}}
    for _ in range(3):
        _ask(loaded, settings)
    assert model_fit.speeds(loaded, KEY)["read"] > 1000


def test_the_time_lm_studio_takes_to_load_the_model_isnt_taken_for_reading(settings, loaded, lm):  # noqa: F811
    """After LM Studio unloaded the model while idle, the question has it load again before the model reads: that
    load isn't counted as reading (a prompt of seconds would be said to take minutes)."""
    server = lm(first_delay=0.4, gap=0.02)
    server.loaded = False
    local_llm.remember_model(settings, KEY)  # the model used last: LM Studio loads it when the question names it
    events = _ask(loaded, settings)
    assert any(event["type"] == "delta" for event in events) and server.prompts, "the model answered"
    rates = model_fit.speeds(loaded, KEY)
    assert "read" not in rates and rates["write"] > 0


def test_a_model_lm_studio_unloaded_is_said_to_load_rather_than_to_take_longer_than_usual(settings, loaded, lm):  # noqa: F811
    """The estimate was the reading alone: for a 31B that LM Studio unloaded while idle, "about 20 seconds", then
    "Taking longer than usual…" for the minute or two LM Studio took to load it, though nothing was wrong."""
    server = lm(first_delay=0.1)
    model_fit.record_speed(loaded, KEY, prompt_tokens=5000, read_seconds=100.0)  # measured before
    server.loaded = False
    local_llm.remember_model(settings, KEY)
    first = _progress(_ask(loaded, settings))[0]
    assert first["state"] == "reading" and first["seconds"] is None and first["about"] == ""
    assert re.fullmatch(r"Loading the model in LM Studio, then reading \d+ emails?", first["text"]), first["text"]
    # Loaded now: the next question has its estimate.
    server.loaded = True
    local_llm._status_cache.clear()
    again = _progress(_ask(loaded, settings))[0]
    assert again["seconds"] and again["text"].startswith("Reading ")


def test_a_call_that_isnt_streamed_is_timed_for_its_reading_and_its_writing(settings, loaded, lm):  # noqa: F811
    """A call that asks for tools sends nothing until the model has read the prompt and written its whole reply, so
    an estimate of the reading alone said "Taking longer than usual…" on every normal question about files."""
    from controller_inbox import agent, assistant

    lm()
    model_fit.record_speed(loaded, KEY, prompt_tokens=5000, read_seconds=100.0, written=100, write_seconds=20.0)
    ws = agent.Workspace(loaded, settings, [], question="What is the October rent?")
    ws.call["model"] = KEY
    messages = [{"role": "user", "content": "x" * 4000}]
    reading = next(assistant._tool_loop(ws, messages, 4000))
    tokens = reading["tokens"]
    writes = local_llm.reply_budget(settings, settings.chat_max_tokens)
    assert reading["seconds"] == pytest.approx(tokens / 50 + writes / 5, rel=0.01)
    # With no writing speed measured yet, no time is said: a bar that keeps moving, never "longer than usual".
    loaded.set_state(model_fit.RATES_KEY, '{"%s": {"read": 50}}' % KEY)
    reading = next(assistant._tool_loop(ws, messages, 4000))
    assert reading["seconds"] is None and reading["about"] == ""


def test_the_line_says_what_is_read_without_a_count_of_none():
    """A question about meetings, with only the calendar to read, said "Reading 0 emails"."""
    from types import SimpleNamespace as N

    from controller_inbox.assistant import _reading_text

    calendar = N(source="calendar", id="calendar", attachments=[], flags=[], category=None)
    email = N(source="outlook", id="m1", attachments=[], flags=[], category=None)
    upload = N(source="chat", id="chat-1", attachments=[N(extracted_text="Rent 1,500")], flags=[], category=None)
    assert _reading_text(N(sources=[calendar]), False) == "Reading your calendar"
    assert _reading_text(N(sources=[email, calendar]), False) == "Reading 1 email and your calendar"
    assert _reading_text(N(sources=[]), False) == "Reading"
    assert _reading_text(N(sources=[upload]), False) == "Reading"
    assert _reading_text(N(sources=[upload, email]), True) == "Reading 1 email and 1 file"


def test_about_says_seconds_or_minutes():
    assert model_fit.about(None) == "" and model_fit.about(0) == ""
    assert model_fit.about(3) == "about 5 seconds" and model_fit.about(42) == "about 40 seconds"
    assert model_fit.about(80) == "about 1 minute" and model_fit.about(200) == "about 3 minutes"


def test_a_table_query_that_had_lm_studio_load_the_model_is_the_load(settings, store, mail, lm):  # noqa: F811
    """The table query for a question about a workbook was the call that had LM Studio load the model it had
    unloaded while idle. The answer's call after it still said "Loading the model in LM Studio" with no estimate, and
    its reading wasn't timed, as if it were loading the model."""
    from controller_inbox.assistant import answer_stream

    server = lm(first_delay=0.1)
    model_fit.record_speed(store, KEY, prompt_tokens=5000, read_seconds=0.5)  # measured before
    server.loaded = False
    local_llm.remember_model(settings, KEY)
    local_llm._status_cache.clear()
    events = list(answer_stream(store, settings, "What is the total in this budget?", email_id=mail["Q4 budget draft"].id))
    assert "Writing a query over the tables for the question" in [event["text"] for event in events if event["type"] == "step"]
    assert len(server.waits) >= 2, "the table query and the answer"
    assert not [event for event in _progress(events) if event["text"].startswith("Loading the model")]
    assert model_fit.speeds(store, KEY)["read"] != 10000, "the answer's reading was timed"
