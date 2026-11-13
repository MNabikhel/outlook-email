"""A model too big for the computer's memory.

A colleague loaded a 31B model on a Mac. LM Studio ran out of memory partway through each answer and the chat said
only `"code":500,"message":"Compute error."`: nothing about why, or what to do. CloseDesk only needs a small model.
"""

from __future__ import annotations

import httpx
import pytest
from test_lm_studio_models import CHAT, CODER, LMStudio, _serve

from controller_inbox import local_llm, model_roles
from controller_inbox.local_llm import check_model, failure_note, stream_text, use_chat_model

GB = 1024**3


@pytest.mark.parametrize("where", ["status", "stream"])
def test_lm_studios_compute_error_is_said_in_plain_words(settings, monkeypatch, where):
    settings.llm = None
    server = LMStudio(loaded=[CHAT])
    server.answer_error = where
    _serve(monkeypatch, server)
    with pytest.raises(httpx.HTTPError) as caught:
        list(stream_text(settings, [{"role": "user", "content": "How much is due?"}]))
    note = failure_note(caught.value)
    assert "ran out of memory" in note and "3B to 8B" in note and "minimum context" in note
    assert "Compute error" not in note and "500" not in note


@pytest.mark.parametrize("said", [
    "Compute error.", "Compute) error.", "ggml_metal: kIOGPUCommandBufferCallbackErrorOutOfMemory",
    "CUDA error: out of memory", "failed to allocate buffer", "Insufficient system memory to load the model",
])
def test_the_ways_a_model_server_says_it_ran_short_of_memory(said):
    assert local_llm.out_of_memory(said)


@pytest.mark.parametrize("said", ["Model not found", "the model server stopped answering", "context length exceeded", ""])
def test_other_errors_are_not_taken_for_memory(said):
    assert not local_llm.out_of_memory(said)


def test_a_model_lm_studio_couldnt_load_for_memory_gets_the_advice(settings, monkeypatch):
    settings.llm = None
    server = LMStudio(loaded=[CODER])
    server.fails_to_load = {CHAT}  # "not enough memory to load the model"
    _serve(monkeypatch, server)
    problem = use_chat_model(settings, CHAT)
    assert "couldn't load" in problem and "too big for this computer's memory" in problem


def test_a_model_too_big_for_this_computer_is_flagged_before_it_is_loaded(settings, monkeypatch):
    settings.llm = None
    server = LMStudio(loaded=[CODER])
    server.sizes = {CHAT: 19 * GB, CODER: 4 * GB}
    _serve(monkeypatch, server)
    monkeypatch.setattr(model_roles, "computer_memory", lambda: 16 * GB)
    status = check_model(settings, use_cache=False)
    assert status.chat_sizes == {CHAT: 19 * GB, CODER: 4 * GB}
    choice = model_roles.chat_choices(settings, status)
    warning = choice["too_big"][CHAT]
    assert "about 21 GB" in warning and "16 GB" in warning and "3B to 8B" in warning
    assert CODER not in choice["too_big"], "a 4 GB model fits in 16 GB"


def test_no_warning_without_the_sizes_the_memory_or_a_model_on_this_computer(settings, monkeypatch):
    settings.llm = None
    server = LMStudio(loaded=[CODER])
    _serve(monkeypatch, server)
    monkeypatch.setattr(model_roles, "computer_memory", lambda: 16 * GB)
    assert model_roles.chat_choices(settings, check_model(settings, use_cache=False))["too_big"] == {}, "no sizes"
    server.sizes = {CHAT: 19 * GB}
    monkeypatch.setattr(model_roles, "computer_memory", lambda: 0)
    assert model_roles.chat_choices(settings, check_model(settings, use_cache=False))["too_big"] == {}, "memory unknown"
    monkeypatch.setattr(model_roles, "computer_memory", lambda: 16 * GB)
    settings.llm_base_url = "http://192.168.1.20:1234/v1"  # LM Studio on another computer: its memory isn't this one's
    assert model_roles.chat_choices(settings, check_model(settings, use_cache=False))["too_big"] == {}


def test_a_bigger_context_needs_more_memory(settings):
    small = model_roles.memory_needed(8 * GB, 8192)
    assert small < model_roles.memory_needed(8 * GB, 32768)
    assert model_roles.memory_needed(8 * GB, 0) == pytest.approx(8 * GB, rel=0.2)


def test_this_computers_memory_is_read():
    assert model_roles.computer_memory() > 512 * 1024**2


def test_both_settings_pages_mark_a_model_too_big_and_still_offer_it(settings, store, monkeypatch):
    from fastapi.testclient import TestClient

    from controller_inbox import web

    settings.llm = None
    server = LMStudio(loaded=[CHAT])
    server.sizes = {CODER: 19 * GB, CHAT: 5 * GB}
    _serve(monkeypatch, server)
    monkeypatch.setattr(model_roles, "computer_memory", lambda: 16 * GB)
    client = TestClient(web.create_app(settings, store))
    page = client.get("/settings").text
    form = page.split('id="chat-model"')[1].split("</form>")[0]
    assert f"{CODER} (likely too big for this computer)</option>" in form and f"{CHAT} (loaded)</option>" in form
    assert f'data-too-big="{CODER} needs about 21 GB' in form
    assert '<p class="warn-box small" data-too-big-note hidden></p>' in form, "the one loaded fits: no warning yet"
    chat = client.get("/api/settings", headers={"X-CloseDesk": "1"}).json()["chat"]
    flagged = {item["key"]: item["too_big"] for item in chat["models"]}
    assert flagged[CHAT] == "" and flagged[CODER].startswith(f"{CODER} needs about 21 GB")
    # Still loads when asked: the warning is advice, not a refusal.
    reply = client.post("/settings/chat-model", data={"model": CODER}, headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert reply.headers["location"] == "/settings?notice=chat-model#chat-model" and CODER in server.loaded
    page = client.get("/settings").text
    assert f'<p class="warn-box small" data-too-big-note>{CODER} needs about 21 GB' in page, "the loaded one is too big: said"


@pytest.mark.parametrize("said", [
    # LM Studio's own guardrail, refusing to load a model too big for the computer.
    "Model loading was stopped due to insufficient system resources. Continuing to load the model will likely overload your system and cause it to freeze.",
    # Ollama's refusal.
    "model requires more system memory (24.5 GiB) than is available (12.1 GiB)",
])
def test_lm_studios_and_ollamas_refusals_to_load_too_big_a_model(said):
    assert local_llm.out_of_memory(said)


def test_an_answer_cut_short_by_memory_says_so(loaded, settings, monkeypatch):
    from controller_inbox import assistant

    def model_answer(_ws, _question, state, **_kw):
        # Part of the answer is on the screen when LM Studio runs out of memory.
        state["wrote"] = True
        state["text"] += "The invoice from Northwind is "
        yield {"type": "delta", "text": "The invoice from Northwind is "}
        raise httpx.HTTPError("the model server stopped with an error (Compute error.)")

    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "needs_more_context", lambda _s: False)
    monkeypatch.setattr(assistant, "_model_answer", model_answer)
    events = list(assistant.answer_stream(loaded, settings, "Who sent the Northwind invoice?"))
    text = "".join(e["text"] for e in events if e["type"] == "delta")
    assert text.startswith("The invoice from Northwind is ")
    assert "(The local model stopped answering partway. The model is likely too big for this computer's memory." in text
