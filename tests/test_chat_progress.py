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


def test_about_says_seconds_or_minutes():
    assert model_fit.about(None) == "" and model_fit.about(0) == ""
    assert model_fit.about(3) == "about 5 seconds" and model_fit.about(42) == "about 40 seconds"
    assert model_fit.about(80) == "about 1 minute" and model_fit.about(200) == "about 3 minutes"
