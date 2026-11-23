"""CloseDesk fitting itself to whatever chat model is loaded: the context it asks for and how long it waits.

A 31B model on a 32 GB Mac was loaded with Setup's 16,384-token minimum, which doesn't fit its memory, and the chat
waited its usual 5 minutes for the first word, which a model that size reading a long prompt on a laptop can need
more than. Now the context is the largest of Setup's steps that fits (a small model still gets the whole minimum)
and the wait is worked out per model, from its speed when measured, else its size; the user's own wait wins.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from controller_inbox import local_llm, model_fit, model_roles, web
from controller_inbox.local_llm import LocalReader, check_model, ensure_context, needs_more_context, use_chat_model
from test_lm_studio_models import LMStudio, _serve

GB = 1024**3
EIGHT, BIG = "qwen/qwen3-8b", "qwen/qwen3-32b"  # an 8B (5 GB at Q4) and a 31-32B (18 GB at Q4)
SIZES = {EIGHT: 5 * GB, BIG: 18 * GB}
PAGE = {"X-CloseDesk": "1"}
ORIGIN = {"Origin": "http://testserver"}


def _computer(monkeypatch, memory_gb: int, *, mac: bool) -> None:
    monkeypatch.setattr(model_roles, "computer_memory", lambda: memory_gb * GB)
    monkeypatch.setattr(model_roles, "_apple_silicon", lambda: mac)


def _lm_studio(monkeypatch, loaded: list[str] | None = None) -> LMStudio:
    server = LMStudio(loaded=loaded or [])
    server.extra = [EIGHT, BIG]
    server.sizes = dict(SIZES)
    _serve(monkeypatch, server)
    return server


@pytest.mark.parametrize(
    ("memory_gb", "mac", "small", "big"),
    [(32, True, 16384, 8192), (16, False, 16384, 4096)],
    ids=["32 GB Mac", "16 GB PC"],
)
def test_the_context_asked_for_is_the_most_that_fits_this_computer(settings, monkeypatch, memory_gb, mac, small, big):
    """Load and use: an 8B gets Setup's whole minimum; a 31B the largest step that fits, never under 4,096."""
    settings.llm = None
    _computer(monkeypatch, memory_gb, mac=mac)
    server = _lm_studio(monkeypatch)
    assert settings.min_context_tokens == 16384
    assert use_chat_model(settings, EIGHT) == ""
    assert use_chat_model(settings, BIG) == ""
    assert [call for call in server.calls if call[0] == "load"] == [("load", EIGHT, small), ("load", BIG, big)]


def test_the_minimum_is_at_most_for_a_big_model(settings, monkeypatch):
    settings.llm = None
    _computer(monkeypatch, 32, mac=True)
    _lm_studio(monkeypatch)
    status = check_model(settings)
    assert model_fit.auto_context(settings, status, BIG) == 8192
    assert model_fit.auto_context(settings, status, BIG, want=4096) == 4096, "a smaller minimum is kept"
    assert model_fit.auto_context(settings, status, EIGHT, want=32768) == 32768, "the 8B fits a longer one too"
    assert model_fit.auto_context(settings, status, EIGHT, want=0) == 0, "Off: LM Studio's own"


def test_the_chat_raises_a_big_model_only_as_far_as_fits(settings, monkeypatch):
    """A 31B the user loaded at 4,096 tokens on a 32 GB Mac: raised to 8,192, which fits, not to 16,384 (before, it
    was left at 4,096 because 16,384 wouldn't fit)."""
    settings.llm = None
    _computer(monkeypatch, 32, mac=True)
    server = _lm_studio(monkeypatch, loaded=[BIG])
    server.contexts[BIG] = 4096
    assert needs_more_context(settings)
    said = ensure_context(settings)
    assert said == f"Reloaded {BIG} in LM Studio with a 8,192-token context (the most that fits this computer) so whole attachments fit."
    assert server.calls == [("unload", BIG), ("load", BIG, 8192)]
    local_llm._status_cache.clear()
    assert not needs_more_context(settings)


def test_the_choice_at_the_start_loads_with_the_context_that_fits(settings, monkeypatch):
    from controller_inbox import model_choice

    settings.llm = None
    _computer(monkeypatch, 16, mac=False)
    server = _lm_studio(monkeypatch)
    local_llm.remember_model(settings, BIG)  # used last time
    assert model_choice.load_at_start(settings)["model"] == BIG
    assert server.calls[-1] == ("load", BIG, 4096)


@pytest.mark.parametrize(
    ("key", "thinks", "automatic", "chat"),
    [(EIGHT, False, 120, 300), (BIG, False, 432, 432), (BIG, True, 648, 648)],
    ids=["8B", "31B", "31B that thinks"],
)
def test_the_wait_is_worked_out_from_the_model_s_size(settings, store, monkeypatch, key, thinks, automatic, chat):
    """About 2 minutes for an 8B (the chat still waits its usual 5), 7 for a 31B, 11 for one that thinks first."""
    settings.llm = None
    _computer(monkeypatch, 32, mac=True)
    _lm_studio(monkeypatch, loaded=[key])
    if thinks:
        local_llm._reasoning_seen.add(key)
    status = check_model(settings)
    assert model_fit.automatic_wait(settings, status, key, store) == pytest.approx(automatic)
    wait = model_fit.chat_wait(store, settings)
    assert wait.model == key and not wait.yours and wait.seconds == pytest.approx(chat)


def test_a_measured_speed_wins_over_the_size(settings, store, monkeypatch):
    settings.llm = None
    _computer(monkeypatch, 32, mac=True)
    _lm_studio(monkeypatch, loaded=[BIG])
    server_context = 16384  # as the fake loaded it
    model_fit.record_speed(store, BIG, prompt_tokens=8000, read_seconds=400.0, written=300, write_seconds=60.0)
    assert model_fit.speeds(store, BIG) == {"read": 20.0, "write": 5.0}
    expected = server_context / 20.0 * model_fit.MARGIN + model_fit.SPARE_SECONDS
    assert model_fit.automatic_wait(settings, check_model(settings), BIG, store) == pytest.approx(expected)
    # Two measures: the second moves it halfway.
    model_fit.record_speed(store, BIG, prompt_tokens=8000, read_seconds=200.0)
    assert model_fit.speeds(store, BIG)["read"] == 30.0


def test_the_chat_waits_the_model_s_own_wait_and_the_user_s_wins(settings, loaded, monkeypatch):
    """The chat's calls wait the 31B's 7 minutes (more than the usual 5); a wait the user set wins, for that model."""
    from controller_inbox.assistant import answer_stream

    settings.llm = True
    _computer(monkeypatch, 32, mac=True)
    server = _lm_studio(monkeypatch, loaded=[BIG])
    list(answer_stream(loaded, settings, "What is the total of the Northwind invoice?"))
    assert server.timeouts and set(server.timeouts) == {18 * model_fit.SECONDS_PER_GB}, server.timeouts
    model_fit.set_wait(loaded, BIG, 12)
    server.timeouts.clear()
    list(answer_stream(loaded, settings, "What is the total of the Northwind invoice?"))
    assert set(server.timeouts) == {720.0}
    model_fit.set_wait(loaded, BIG, None)  # back to automatic, which has measured this (fast) fake by now
    assert not model_fit.chat_wait(loaded, settings).yours and model_fit.speeds(loaded, BIG)["read"] > 0


def test_the_reading_waits_between_words_as_long_as_the_model_needs(settings, store, monkeypatch):
    settings.llm = None
    _computer(monkeypatch, 32, mac=True)
    server = _lm_studio(monkeypatch, loaded=[BIG])
    waits: list[float] = []

    def reply(request: httpx.Request) -> httpx.Response:
        waits.append(request.extensions["timeout"]["read"])
        return server(request)

    client = httpx.Client(transport=httpx.MockTransport(reply))
    with LocalReader(settings, client=client, store=store) as reader:
        reader.read({"subject": "Invoice", "sender_email": "ap@vendor.example", "body": "Attached."})
    assert waits and min(waits) >= 18 * model_fit.SECONDS_PER_GB
    model_fit.set_wait(store, BIG, 20)
    waits.clear()
    with LocalReader(settings, client=client, store=store) as reader:
        reader.read({"subject": "Invoice", "sender_email": "ap@vendor.example", "body": "Attached."})
    assert min(waits) >= 1200


def test_minutes_typed_are_checked(store):
    assert model_fit.parse_minutes("12") == 12 and model_fit.parse_minutes(" 3 ") == 3
    for bad in ("", "abc", "0", "1", "-5", "121", "2.5", "nan", "1e9", None):
        with pytest.raises(ValueError):
            model_fit.parse_minutes(bad)
    with pytest.raises(ValueError):
        model_fit.set_wait(store, BIG, "lots")
    assert model_fit.chosen_wait(store, BIG) == 0


def test_a_wait_shorter_than_every_call_s_own_is_said_as_the_wait_that_runs(store, settings):
    """A 1-minute wait was taken, and Settings said "waits up to 1 minute", while every call waits at least
    settings.llm_timeout (90 seconds), and the offer of more time then said CloseDesk gave it 2 minutes. The least
    is now 2 minutes, and a wait kept from before is said as the one that runs."""
    from controller_inbox.local_llm import ModelStatus

    with pytest.raises(ValueError, match="from 2 to 120"):
        model_fit.set_wait(store, BIG, 1)
    store.set_state(model_fit.WAIT_KEY, json.dumps({BIG: 60.0}))  # kept from before
    status = ModelStatus(mode="on", reachable=True, model=BIG, context_length=16384, server="ollama")
    wait = model_fit.chat_wait(store, settings, status)
    assert wait.seconds == settings.llm_timeout == 90 and wait.yours
    assert "waits up to 2 minutes (set by you)" in model_fit.describe(store, settings, status)["line"]


def test_settings_say_what_was_chosen_and_take_a_wait_on_both_pages(settings, store, monkeypatch):
    settings.llm = None
    _computer(monkeypatch, 32, mac=True)
    _lm_studio(monkeypatch, loaded=[BIG])
    client = TestClient(web.create_app(settings, store))
    line = "qwen3-32b: 16,384-token context, waits up to 7 minutes (set automatically)."  # as LM Studio loaded it
    fit = client.get("/api/settings", headers=PAGE).json()["fit"]
    assert fit["line"] == line and fit["minutes"] == 7 and not fit["yours"]
    assert line in client.get("/settings").text

    reply = client.post("/api/settings/model-wait", json={"model": BIG, "minutes": 15}, headers=PAGE)
    assert reply.status_code == 200, reply.text
    fit = client.get("/api/settings", headers=PAGE).json()["fit"]
    assert fit["line"] == "qwen3-32b: 16,384-token context, waits up to 15 minutes (set by you)."
    bad = client.post("/api/settings/model-wait", json={"model": BIG, "minutes": "soon"}, headers=PAGE)
    assert bad.status_code == 400 and bad.json()["detail"] == "Type a number of minutes, from 2 to 120."
    assert client.post("/api/settings/model-wait", json={"model": BIG, "minutes": None}, headers=PAGE).status_code == 200
    assert not client.get("/api/settings", headers=PAGE).json()["fit"]["yours"]

    # The classic page: a form with minutes, and Automatic.
    reply = client.post("/settings/model-wait", data={"model": BIG, "minutes": "9"}, headers=ORIGIN, follow_redirects=False)
    assert reply.status_code == 303
    page = client.get(reply.headers["location"]).text
    assert "waits up to 9 minutes (set by you)" in page
    reply = client.post("/settings/model-wait", data={"model": BIG, "minutes": "500"}, headers=ORIGIN, follow_redirects=False)
    assert "Type a number of minutes from 2 to 120." in client.get(reply.headers["location"]).text
    reply = client.post("/settings/model-wait", data={"model": BIG, "automatic": "1"}, headers=ORIGIN, follow_redirects=False)
    assert line in client.get(reply.headers["location"]).text
    assert json.loads(store.get_state(model_fit.WAIT_KEY)) == {}


@pytest.mark.parametrize(
    ("memory_gb", "card_gb", "key"),
    [(64, 8, BIG), (16, 6, EIGHT)],
    ids=["31B on a 64 GB PC with an 8 GB card", "8B on a 16 GB PC with a 6 GB card"],
)
def test_a_model_that_never_fits_the_graphics_card_is_raised_as_far_as_load_and_use_loads_it(
    settings, monkeypatch, memory_gb, card_gb, key
):
    """It runs partly from the computer's memory at any context, so it is fitted to that, as Load and use fits it.
    Before, the chat compared the raise against the card alone: it kept the model at 4,096 tokens and said 16,384
    wouldn't fit in this computer's memory, though Load and use loaded it with 16,384."""
    settings.llm = None
    _computer(monkeypatch, memory_gb, mac=False)
    monkeypatch.setattr(model_roles, "_gpu_found", card_gb * GB, raising=False)
    server = _lm_studio(monkeypatch, loaded=[key])
    server.contexts[key] = 4096
    status = check_model(settings)
    assert local_llm.context_target(settings, status) == model_fit.auto_context(settings, status, key) == 16384
    assert local_llm.context_fits(settings, status)
    assert needs_more_context(settings)
    said = ensure_context(settings)
    assert "wouldn't fit" not in said
    assert server.calls == [("unload", key), ("load", key, 16384)]


def test_the_wait_is_worked_out_again_once_the_question_raises_the_context(settings, store, monkeypatch):
    """A 31B loaded at 4,096 tokens and measured reading 15 tokens a second: the wait was worked out from 4,096
    before the same question reloaded the model with 8,192, so that question's longer prompt ran out of time."""
    from controller_inbox import assistant

    settings.llm = None
    _computer(monkeypatch, 32, mac=True)
    server = _lm_studio(monkeypatch, loaded=[BIG])
    server.contexts[BIG] = 4096
    model_fit.record_speed(store, BIG, prompt_tokens=6000, read_seconds=400.0)  # 15 tokens a second
    waits = []

    def answer(ws, *_a, **_k):
        waits.append(ws.wait)
        yield {"type": "delta", "text": "Due Friday."}

    monkeypatch.setattr(assistant, "_model_answer", answer)
    events = list(assistant.answer_stream(store, settings, "What is due?"))
    assert ("load", BIG, 8192) in server.calls, events
    assert waits == [pytest.approx(8192 / 15 * model_fit.MARGIN + model_fit.SPARE_SECONDS)]


def test_settings_say_the_context_the_model_is_loaded_with(settings, store, monkeypatch):
    """Settings said the context CloseDesk would ask for (8,192 tokens for a 31B on a 32 GB Mac) though the model
    ran with another one: one the user loaded with more (left as it is), or one loaded back after running out of
    memory."""
    settings.llm = None
    _computer(monkeypatch, 32, mac=True)
    server = _lm_studio(monkeypatch, loaded=[BIG])
    server.contexts[BIG] = 32768
    fit = model_fit.describe(store, settings)
    assert fit["context"] == 32768 and fit["line"].startswith("qwen3-32b: 32,768-token context, waits up to")

    # Loaded with less: the next question reloads it with what fits, and the line says so.
    server.contexts[BIG] = 4096
    local_llm._status_cache.clear()
    fit = model_fit.describe(store, settings)
    assert fit["context"] == 4096
    assert fit["line"].startswith("qwen3-32b: 4,096-token context (8,192 from the next question), waits up to")

    # Loaded back after running out of memory: not raised again, so only what it has.
    local_llm._context_raised.add(BIG)
    assert model_fit.describe(store, settings)["line"].startswith("qwen3-32b: 4,096-token context, waits up to")


def test_settings_say_the_context_another_model_server_reports(store, settings):
    """With Ollama or llama.cpp, Settings named LM Studio and left out the context the server reported."""
    from controller_inbox.local_llm import ModelStatus

    ollama = ModelStatus(mode="on", reachable=True, model="llama3.1:8b", context_length=8192, server="ollama")
    fit = model_fit.describe(store, settings, ollama)
    assert fit["context"] == 8192 and fit["line"].startswith("llama3.1:8b: 8,192-token context, waits up to")
    unknown = ModelStatus(mode="on", reachable=True, model="llama3.1:8b", context_length=0, server="ollama")
    line = model_fit.describe(store, settings, unknown)["line"]
    assert "LM Studio" not in line and line.startswith("llama3.1:8b: the context the model server loaded it with,")


def test_one_slow_first_reading_doesn_t_keep_a_model_slow_for_good(store):
    """The first question waited behind another request (the reading of mail) before the model read it: 3,000 tokens
    in 60 seconds were kept as 50 tokens a second, and every true reading after it (about 1,000 a second) was more
    than FASTEST_JUMP times faster, so it was taken for a prompt read before and dropped, for good. A few such
    readings in a row are the model's own speed, and the slowest of them is kept."""
    model_fit.record_speed(store, BIG, prompt_tokens=3000, read_seconds=60.0)
    assert model_fit.speeds(store, BIG)["read"] == 50
    model_fit.record_speed(store, BIG, prompt_tokens=3000, read_seconds=3.0, counted=True)
    model_fit.record_speed(store, BIG, prompt_tokens=3000, read_seconds=2.5, counted=True)
    assert model_fit.speeds(store, BIG) == {"read": 50}, "one or two may be prompts read before"
    model_fit.record_speed(store, BIG, prompt_tokens=3000, read_seconds=2.8, counted=True)
    assert model_fit.speeds(store, BIG) == {"read": 1000}
    assert model_fit.estimate(store, BIG, tokens=3000) == 3

    # A reading in reach between fast ones starts the count again.
    model_fit.record_speed(store, EIGHT, prompt_tokens=3000, read_seconds=60.0)
    model_fit.record_speed(store, EIGHT, prompt_tokens=3000, read_seconds=3.0, counted=True)
    model_fit.record_speed(store, EIGHT, prompt_tokens=3000, read_seconds=40.0)
    model_fit.record_speed(store, EIGHT, prompt_tokens=3000, read_seconds=3.0, counted=True)
    model_fit.record_speed(store, EIGHT, prompt_tokens=3000, read_seconds=3.0, counted=True)
    assert model_fit.speeds(store, EIGHT)["read"] == 62.5


def test_fast_readings_in_a_row_replace_the_speed_only_when_the_server_said_what_it_read_anew(store):
    """When the server doesn't say how much of the prompt it had read before, follow-up questions about the same mail
    look many times faster than the model reads. Three in a row replaced its true speed: 100 tokens a second became
    700, the wait for a fresh 20,000-token prompt fell from 200 seconds to 29, and the next question with new mail
    ran out of time."""
    model_fit.record_speed(store, BIG, prompt_tokens=20000, read_seconds=200.0)
    for tokens in (21000, 22000, 23000, 24000):
        model_fit.record_speed(store, BIG, prompt_tokens=tokens, read_seconds=30.0)
    assert model_fit.speeds(store, BIG) == {"read": 100}
    assert model_fit.estimate(store, BIG, tokens=20000) == 200
