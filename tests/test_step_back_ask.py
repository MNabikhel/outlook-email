"""CloseDesk asks before it loads a model again with a smaller context.

An answer that ran out of memory on a model whose context CloseDesk gave it had LM Studio reload the model smaller at
once, mid-question, without a word first: a minutes-long reload the user never chose, and a context that reads long
attachments a section at a time from then on. Now the chat shows the lookup answer and offers the reload ("Reload and
ask again" or "Keep as is"), Settings offers the same, and the mail reading stops using the model for the run and says
where to reload it.

The fake LM Studio is test_context_memory's 32 GB Mac: a 27B model runs out of memory with 16,384 tokens and answers
with 8,192.
"""

from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient
from test_context_memory import BIG, GB, SMALL, MacLMStudio, _ask, _serve

from controller_inbox import local_llm, model_fit, web

PAGE = {"X-CloseDesk": "1"}
QUESTION = "What is the total in this budget?"
OFFER_TEXT = (
    f"{BIG} ran out of memory with a 16,384-token context. Reload it with 8,192? "
    "Long attachments are then read a section at a time."
)


def _closedesk_loaded(settings, monkeypatch, memory_gb: int = 32) -> MacLMStudio:
    """BIG loaded by CloseDesk (Load and use) with the 16,384 tokens its estimate says fit."""
    settings.llm = True
    server = MacLMStudio({})
    _serve(monkeypatch, server, memory_gb=memory_gb)
    assert local_llm.use_chat_model(settings, BIG) == ""
    assert server.calls == [("load", BIG, 16384)]
    return server


def _events(store, settings, question: str, email_id):
    from controller_inbox.assistant import answer_stream

    local_llm._status_cache.clear()
    return list(answer_stream(store, settings, question, email_id=email_id))


def test_an_answer_out_of_memory_offers_the_reload_and_doesnt_do_it(store, settings, mail, monkeypatch):
    server = _closedesk_loaded(settings, monkeypatch)
    budget = mail["Q4 budget draft"].id
    events = _events(store, settings, QUESTION, budget)
    assert server.calls == [("load", BIG, 16384)], "nothing reloaded without the user's yes"
    assert server.loaded[BIG] == 16384
    modes = [e for e in events if e["type"] == "mode"]
    assert modes and modes[0]["mode"] == "lookup" and "ran out of memory" in modes[0]["note"], modes
    assert not any(e["type"] == "step" and e["text"].startswith("Loaded") for e in events)
    offers = [e for e in events if e["type"] == "step_back"]
    assert len(offers) == 1, events
    offer = offers[0]
    assert offer["model"] == BIG and offer["loaded"] == 16384 and offer["size"] == 8192
    assert offer["text"] == OFFER_TEXT
    assert offer["question"] == QUESTION and offer["email_id"] == budget
    assert not any(e["type"] == "more_time" for e in events), "out of memory never offers more time"
    assert events[-1]["type"] == "done"


def test_an_answer_cut_off_by_memory_offers_the_reload_too(store, settings, mail, monkeypatch):
    server = _closedesk_loaded(settings, monkeypatch)
    server.partway = True
    settings.table_queries = False
    events = _events(store, settings, "Who sent this?", mail["Q4 budget draft"].id)
    text = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "stopped answering partway" in text and "Loaded" not in text and "Ask again." not in text, text
    assert [e for e in events if e["type"] == "step_back"][0]["size"] == 8192
    assert server.calls == [("load", BIG, 16384)]


def test_the_offer_is_saved_with_the_conversation():
    log = web._AnswerLog()
    log.take({"type": "step_back", "model": BIG, "loaded": 16384, "size": 8192, "text": OFFER_TEXT, "question": "q", "email_id": "7"})
    assert log.data["step_back"] == {"model": BIG, "loaded": 16384, "size": 8192, "text": OFFER_TEXT, "question": "q", "email_id": "7"}


def test_reload_and_ask_again_reloads_with_the_size_offered(store, settings, mail, monkeypatch):
    server = _closedesk_loaded(settings, monkeypatch)
    client = TestClient(web.create_app(settings, store))
    lines = [json.loads(line) for line in client.post("/chat", json={"message": QUESTION, "email_id": mail["Q4 budget draft"].id}, headers=PAGE).text.splitlines()]
    offer = next(line for line in lines if line["type"] == "step_back")
    chat_id = lines[0]["id"]
    assert store.chat_turns(chat_id)[-1]["step_back"]["size"] == 8192, "shown again when the conversation is opened"

    # Only from the page, and only the model and a size the page could send.
    assert client.post("/chat/step-back", json={"model": BIG, "size": 8192}).status_code == 403
    assert client.post("/chat/step-back", json={"model": BIG, "size": 5000}, headers=PAGE).status_code == 400
    assert client.post("/chat/step-back", json={"model": BIG, "size": "8192"}, headers=PAGE).status_code == 400
    assert client.post("/chat/step-back", json={"size": 8192}, headers=PAGE).status_code == 400
    refused = client.post("/chat/step-back", json={"model": SMALL, "size": 8192}, headers=PAGE)
    assert refused.status_code == 409 and "nothing was reloaded" in refused.json()["detail"], refused.json()
    refused = client.post("/chat/step-back", json={"model": BIG, "size": 4096}, headers=PAGE)
    assert refused.status_code == 409, "not the size offered"
    assert server.calls == [("load", BIG, 16384)]

    # Busy with another answer: not unloaded under it.
    monkeypatch.setattr(local_llm, "model_busy", lambda *_names: True)
    busy = client.post("/chat/step-back", json={"model": BIG, "size": 8192}, headers=PAGE)
    assert busy.status_code == 409 and "answering another request" in busy.json()["detail"]
    assert server.calls == [("load", BIG, 16384)]
    monkeypatch.setattr(local_llm, "model_busy", lambda *_names: False)

    done = client.post("/chat/step-back", json={"model": offer["model"], "size": offer["size"]}, headers=PAGE)
    assert done.status_code == 200, done.json()
    assert done.json()["message"] == f"Loaded {BIG} again with an 8,192-token context: 16,384 didn't fit in this computer's memory."
    assert server.calls[1:] == [("unload", BIG), ("load", BIG, 8192)]

    # The page asks the same question again: answered by the model, and not raised again.
    again = [json.loads(line) for line in client.post("/chat", json={"message": QUESTION, "chat_id": chat_id, "email_id": offer["email_id"]}, headers=PAGE).text.splitlines()]
    assert "$12,480" in "".join(line.get("text", "") for line in again if line["type"] == "delta")
    assert not any(line["type"] in ("step_back", "mode") for line in again), again
    assert len(server.calls) == 3


def test_the_next_size_is_offered_when_the_one_accepted_runs_out_too(store, settings, mail, monkeypatch):
    import test_context_memory as memory

    monkeypatch.setattr(memory, "GRAPHICS_MEMORY", 20 * GB)  # 8,192 doesn't fit either; 4,096 does
    _closedesk_loaded(settings, monkeypatch)
    budget = mail["Q4 budget draft"].id
    offer = next(e for e in _events(store, settings, QUESTION, budget) if e["type"] == "step_back")
    assert offer["size"] == 8192
    local_llm.step_back_context(settings, BIG, 8192)
    offer = next(e for e in _events(store, settings, QUESTION, budget) if e["type"] == "step_back")
    assert (offer["loaded"], offer["size"]) == (8192, 4096)
    assert offer["text"] == (
        f"{BIG} ran out of memory with an 8,192-token context. Reload it with 4,096? "
        "Long attachments are then read a section at a time."
    )
    local_llm.step_back_context(settings, BIG, 4096)
    _steps, modes, text = _ask(store, settings, QUESTION, budget)
    assert not modes and "$12,480" in text


def test_no_offer_at_the_smallest_size(store, settings, mail, monkeypatch):
    import test_context_memory as memory

    monkeypatch.setattr(memory, "GRAPHICS_MEMORY", 18 * GB)
    settings.llm = True
    server = MacLMStudio({BIG: 4096})
    local_llm._context_given[BIG] = 4096  # loaded by CloseDesk
    _serve(monkeypatch, server, memory_gb=24)
    events = _events(store, settings, QUESTION, mail["Q4 budget draft"].id)
    assert not any(e["type"] == "step_back" for e in events)
    local_llm._status_cache.clear()
    line = model_fit.context_line(settings, local_llm.check_model(settings))
    assert line["step_back"] is None and f"{BIG} ran out of memory even with 4,096 tokens" in line["advice"]


def test_a_model_the_user_loaded_is_never_offered_a_reload(store, settings, mail, monkeypatch):
    settings.llm = True
    server = MacLMStudio({BIG: 32768})  # loaded by the user with more than fits
    _serve(monkeypatch, server)
    events = _events(store, settings, QUESTION, mail["Q4 budget draft"].id)
    assert not any(e["type"] == "step_back" for e in events)
    note = next(e for e in events if e["type"] == "mode")["note"]
    assert f"reload {BIG} in LM Studio with Context Length 16,384" in note, note
    assert server.calls == []
    refused = TestClient(web.create_app(settings, store)).post("/chat/step-back", json={"model": BIG, "size": 16384}, headers=PAGE)
    assert refused.status_code == 409 and server.calls == []


def test_settings_offers_the_reload_after_an_answer_ran_out_of_memory(store, settings, mail, monkeypatch):
    server = _closedesk_loaded(settings, monkeypatch)
    local_llm._status_cache.clear()
    assert model_fit.context_line(settings, local_llm.check_model(settings))["step_back"] is None, "nothing ran out yet"
    _events(store, settings, QUESTION, mail["Q4 budget draft"].id)
    local_llm._status_cache.clear()
    line = model_fit.context_line(settings, local_llm.check_model(settings))
    assert line["step_back"] == {"model": BIG, "loaded": 16384, "size": 8192, "text": OFFER_TEXT}
    assert line["advice"].startswith(OFFER_TEXT) and "Reload with 8,192" in line["advice"], line["advice"]

    client = TestClient(web.create_app(settings, store))
    state = client.get("/api/settings", headers=PAGE).json()
    assert state["context"]["window"]["step_back"]["size"] == 8192
    page = client.get("/settings").text
    assert "Reload with 8,192" in page and 'action="/settings/step-back"' in page

    assert client.post("/api/settings/step-back", json={"model": BIG, "size": 5000}, headers=PAGE).status_code == 400
    done = client.post("/api/settings/step-back", json={"model": BIG, "size": 8192}, headers=PAGE)
    assert done.status_code == 200 and done.json()["message"].startswith(f"Loaded {BIG} again with an 8,192-token context")
    assert server.calls[1:] == [("unload", BIG), ("load", BIG, 8192)]
    local_llm._status_cache.clear()
    assert model_fit.context_line(settings, local_llm.check_model(settings))["step_back"] is None


def test_the_setup_page_reloads_with_the_size_offered(store, settings, mail, monkeypatch):
    server = _closedesk_loaded(settings, monkeypatch)
    _events(store, settings, QUESTION, mail["Q4 budget draft"].id)
    client = TestClient(web.create_app(settings, store))
    sent = client.post("/settings/step-back", data={"model": BIG, "size": "8192"}, follow_redirects=False)
    assert sent.status_code == 303 and "notice=step-back" in sent.headers["location"]
    assert server.calls[1:] == [("unload", BIG), ("load", BIG, 8192)]
    page = client.get(sent.headers["location"]).text
    assert f"Loaded {BIG} again with an 8,192-token context" in page
    again = client.post("/settings/step-back", data={"model": BIG, "size": "8192"}, follow_redirects=False)
    assert "notice=step-back-refused" in again.headers["location"] and len(server.calls) == 3


def test_the_mail_reading_stops_using_the_model_and_says_where_to_reload_it(store, settings, mail, monkeypatch):
    from controller_inbox.overnight import read_queue

    server = _closedesk_loaded(settings, monkeypatch)
    client = httpx.Client(transport=httpx.MockTransport(server))
    reader = local_llm.LocalReader(settings, client=client)
    result = read_queue(store, settings, limit=6, reader=reader)
    assert server.calls == [("load", BIG, 16384)], "nothing reloaded without the user's yes"
    assert reader.stats.failed == 1 and reader.stats.read == 0, "the run stopped using the model after the first email"
    assert all(chat[1] == 16384 for chat in server.chats)
    assert result["note"] == (
        "Stopped reading: the local model ran out of memory with a 16,384-token context. Settings → Local model can "
        f"reload {BIG} with 8,192."
    ), result["note"]
    local_llm._status_cache.clear()
    assert model_fit.context_line(settings, local_llm.check_model(settings))["step_back"]["size"] == 8192


def _user_sized_then_raised(settings, monkeypatch, own: int) -> MacLMStudio:
    """BIG loaded by the user with ``own`` tokens (not one of Setup's steps), raised by CloseDesk to 16,384 on the
    first question, which runs out of memory."""
    settings.llm = True
    server = MacLMStudio({BIG: own})
    _serve(monkeypatch, server)
    return server


def test_a_users_own_size_that_isnt_a_step_can_be_accepted(store, settings, mail, monkeypatch):
    server = _user_sized_then_raised(settings, monkeypatch, 12000)
    budget = mail["Q4 budget draft"].id
    offer = next(e for e in _events(store, settings, QUESTION, budget) if e["type"] == "step_back")
    assert server.loaded[BIG] == 16384
    assert (offer["loaded"], offer["size"]) == (16384, 12000)
    client = TestClient(web.create_app(settings, store))
    local_llm._status_cache.clear()
    assert client.get("/api/settings", headers=PAGE).json()["context"]["window"]["step_back"]["size"] == 12000
    assert client.post("/chat/step-back", json={"model": BIG, "size": 12001}, headers=PAGE).status_code == 400
    done = client.post("/chat/step-back", json={"model": offer["model"], "size": offer["size"]}, headers=PAGE)
    assert done.status_code == 200, done.json()
    assert server.loaded[BIG] == 12000
    _steps, modes, text = _ask(store, settings, QUESTION, budget)
    assert not modes and "$12,480" in text


def test_a_users_own_size_that_isnt_a_step_can_be_accepted_in_settings(store, settings, mail, monkeypatch):
    server = _user_sized_then_raised(settings, monkeypatch, 12000)
    _events(store, settings, QUESTION, mail["Q4 budget draft"].id)
    client = TestClient(web.create_app(settings, store))
    sent = client.post("/settings/step-back", data={"model": BIG, "size": "12000"}, follow_redirects=False)
    assert sent.status_code == 303 and "notice=step-back" in sent.headers["location"] and "refused" not in sent.headers["location"]
    assert server.loaded[BIG] == 12000


def test_the_offer_is_never_under_4096_tokens(store, settings, mail, monkeypatch):
    server = _user_sized_then_raised(settings, monkeypatch, 2048)
    offer = next(e for e in _events(store, settings, QUESTION, mail["Q4 budget draft"].id) if e["type"] == "step_back")
    assert offer["size"] >= model_fit.SMALLEST_CONTEXT, offer
    done = TestClient(web.create_app(settings, store)).post("/chat/step-back", json={"model": BIG, "size": offer["size"]}, headers=PAGE)
    assert done.status_code == 200, done.json()
    assert server.loaded[BIG] == offer["size"]


def test_an_old_offer_pressed_after_the_reload_says_an_8192_token_context(store, settings, mail, monkeypatch):
    _closedesk_loaded(settings, monkeypatch)
    _events(store, settings, QUESTION, mail["Q4 budget draft"].id)
    local_llm.step_back_context(settings, BIG, 8192)
    refused = TestClient(web.create_app(settings, store)).post("/chat/step-back", json={"model": BIG, "size": 8192}, headers=PAGE)
    assert refused.status_code == 409
    assert f"{BIG} is loaded with an 8,192-token context now" in refused.json()["detail"], refused.json()
