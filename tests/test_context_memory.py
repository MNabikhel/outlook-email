"""The context CloseDesk asks LM Studio for, and memory.

A big model the user loaded with LM Studio's default 4,096 tokens was reloaded with 16,384 on the first question. On a
Mac the load goes through (the graphics side over-commits the shared memory) but every answer then fails with LM
Studio's "Compute error.", and CloseDesk never stepped back. Lowering the minimum in Settings didn't reload anything
either, though the out-of-memory advice says to do just that.

The fake LM Studio below is a 32 GB Mac (about 24 GB for the graphics side): loads always go through, and an answer
fails with "Compute error." when the model and its context take more than that.
"""

from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient

from controller_inbox import agent, local_llm, model_roles, web
from controller_inbox.assistant import answer_stream

GB = 1024**3
BIG = "google/gemma-3-27b"
SMALL = "qwen/qwen3-8b"
SIZES = {BIG: int(17.0 * GB), SMALL: int(5.0 * GB)}
# What the context really takes per token (layers x KV heads x head size x K and V x 2 bytes): more than CloseDesk's
# estimate for a model this big, so the estimate says 16,384 fits on 32 GB and the answer runs out of memory.
KV = {BIG: 62 * 16 * 128 * 2 * 2, SMALL: 36 * 8 * 128 * 2 * 2}
GRAPHICS_MEMORY = 24 * GB


class MacLMStudio:
    def __init__(self, loaded: dict[str, int]):
        self.loaded = dict(loaded)  # key -> the context it is loaded with
        self.calls: list[tuple] = []  # loads and unloads, in order
        self.chats: list[tuple[str, int, str]] = []  # (model, its context, "ok" or "Compute error")
        self.partway = False  # the answer runs out of memory after its first words, not before them

    def need(self, key: str) -> int:
        return SIZES[key] + self.loaded[key] * KV[key]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": key} for key in SIZES]})
        if path == "/api/v1/models":
            models = []
            for key in SIZES:
                instances = [{"id": key, "config": {"context_length": self.loaded[key]}}] if key in self.loaded else []
                models.append({"type": "llm", "key": key, "loaded_instances": instances, "size_bytes": SIZES[key],
                               "max_context_length": 131072})
            return httpx.Response(200, json={"models": models})
        if path == "/api/v1/models/load":
            body = json.loads(request.content)
            self.calls.append(("load", body["model"], body.get("context_length")))
            self.loaded[body["model"]] = body.get("context_length") or 4096
            return httpx.Response(200, json={"status": "loaded"})
        if path == "/api/v1/models/unload":
            instance = json.loads(request.content)["instance_id"]
            self.calls.append(("unload", instance))
            self.loaded.pop(instance, None)
            return httpx.Response(200, json={"status": "unloaded"})
        if path == "/v1/chat/completions":
            body = json.loads(request.content)
            key = body["model"]
            ok = key in self.loaded and self.need(key) <= GRAPHICS_MEMORY
            self.chats.append((key, self.loaded.get(key, 0), "ok" if ok else "Compute error"))
            if not ok and self.partway and body.get("stream"):
                words = "The total is in the summary tab, where each department's line is added up. " * 8
                text = ('data: {"choices": [{"delta": {"content": "' + words + '"}}]}\n\n'
                        'data: {"error": {"code": 500, "message": "Compute error."}}\n\n')
                return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})
            if not ok:
                return httpx.Response(500, json={"error": {"code": 500, "message": "Compute error."}})
            if body.get("stream"):
                text = 'data: {"choices": [{"delta": {"content": "The total is $12,480."}}]}\n\ndata: [DONE]\n\n'
                return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json={"choices": [{"message": {"content": "The total is $12,480."}, "finish_reason": "stop"}]})
        return httpx.Response(404)


def _serve(monkeypatch, server, memory_gb: int = 32) -> None:
    client = httpx.Client(transport=httpx.MockTransport(server))
    monkeypatch.setattr(local_llm.httpx, "get", lambda url, **kw: client.get(url, headers=kw.get("headers"), timeout=kw.get("timeout")))
    monkeypatch.setattr(local_llm.httpx, "post", lambda url, **kw: client.post(url, json=kw.get("json"), headers=kw.get("headers"), timeout=kw.get("timeout")))
    monkeypatch.setattr(local_llm.httpx, "stream", lambda method, url, **kw: client.stream(method, url, json=kw.get("json"), headers=kw.get("headers"), timeout=kw.get("timeout")))
    monkeypatch.setattr(model_roles, "computer_memory", lambda: memory_gb * GB)


def _ask(store, settings, question: str, email_id: int):
    local_llm._status_cache.clear()
    events = list(answer_stream(store, settings, question, email_id=email_id))
    steps = [e["text"] for e in events if e["type"] == "step"]
    modes = [e for e in events if e["type"] == "mode"]
    text = "".join(e["text"] for e in events if e["type"] == "delta")
    return steps, modes, text


def test_a_model_raised_past_the_memory_is_loaded_back_and_the_answer_asked_again(store, settings, mail, monkeypatch):
    settings.llm = True
    server = MacLMStudio({BIG: 4096})
    _serve(monkeypatch, server)
    budget = mail["Q4 budget draft"].id
    assert settings.min_context_tokens == 16384

    steps, modes, text = _ask(store, settings, "What is the total in this budget?", budget)
    assert f"Reloaded {BIG} in LM Studio with a 16,384-token context so whole attachments fit." in steps
    assert f"Loaded {BIG} back with a 4,096-token context: 16,384 didn't fit in this computer's memory." in steps
    assert server.calls == [("unload", BIG), ("load", BIG, 16384), ("unload", BIG), ("load", BIG, 4096)]
    assert not modes, "answered by the model, not the lookup"
    assert "$12,480" in text
    assert server.chats[0][1:] == (16384, "Compute error") and server.chats[-1][1:] == (4096, "ok")

    # The next question doesn't raise it again.
    steps, modes, text = _ask(store, settings, "And the total again?", budget)
    assert not modes and "$12,480" in text and len(server.calls) == 4
    assert not any("Reload" in step or "Loaded" in step for step in steps)
    assert not local_llm.needs_more_context(settings)
    # Until the minimum is changed in Settings.
    local_llm.set_min_context(settings, 16384)
    local_llm._status_cache.clear()
    assert local_llm.needs_more_context(settings)


def test_an_answer_cut_off_by_memory_isnt_asked_again_but_the_next_question_has_the_room(store, settings, mail, monkeypatch):
    settings.llm = True
    server = MacLMStudio({BIG: 4096})
    server.partway = True
    # The answer itself runs out of memory partway: no table query before it (that one would run out first, and the
    # model be loaded back before the answer is asked).
    settings.table_queries = False
    _serve(monkeypatch, server)
    budget = mail["Q4 budget draft"].id
    _steps, _modes, text = _ask(store, settings, "Who sent this?", budget)
    assert text.startswith("The total") and "stopped answering partway" in text
    assert f"Loaded {BIG} back with a 4,096-token context: 16,384 didn't fit in this computer's memory. Ask again." in text
    assert server.calls[-2:] == [("unload", BIG), ("load", BIG, 4096)]
    calls = len(server.calls)
    _steps, modes, text = _ask(store, settings, "Who sent this?", budget)
    assert not modes and "The total is $12,480." in text and len(server.calls) == calls


def test_a_model_the_user_loaded_that_runs_out_of_memory_is_left_as_it_is(store, settings, mail, monkeypatch):
    settings.llm = True
    server = MacLMStudio({BIG: 32768})  # loaded with 32,768 in LM Studio: CloseDesk didn't raise it
    _serve(monkeypatch, server)
    steps, modes, _text = _ask(store, settings, "What is the total in this budget?", mail["Q4 budget draft"].id)
    assert server.calls == [] and modes and modes[0]["mode"] == "lookup"
    assert "ran out of memory" in modes[0]["note"]


def test_a_model_that_wouldnt_fit_with_more_context_keeps_its_own(store, settings, mail, monkeypatch):
    """A 17 GB model on a 16 GB computer fits with no more context than the smallest CloseDesk asks for (4,096
    tokens), so the minimum of 16,384 is "at most" for it: it isn't reloaded, and nothing in the chat says so; Settings
    says why it has no more (model_fit)."""
    settings.llm = True
    server = MacLMStudio({BIG: 4096})
    _serve(monkeypatch, server, memory_gb=16)  # 17 GB of model and 2 GB of context: more than a 16 GB computer has
    budget = mail["Q4 budget draft"].id
    steps, modes, text = _ask(store, settings, "What is the total in this budget?", budget)
    assert server.calls == [], "not reloaded"
    assert not any(BIG in step or step.startswith("Reloading the model") for step in steps)
    assert not modes and "$12,480" in text
    steps, _modes, _text = _ask(store, settings, "And the total again?", budget)
    assert not any(BIG in step for step in steps) and server.calls == []
    # Settings doesn't say the next question reloads it, and says why it has no more.
    client = TestClient(web.create_app(settings, store))
    page = client.get("/settings").text
    assert "The next time you ask something, CloseDesk reloads" not in page
    said = f"{BIG} is too big for this computer's memory with a longer context, so CloseDesk asks LM Studio for 4,096 tokens."
    assert said in page
    window = client.get("/api/settings", headers={"X-CloseDesk": "1"}).json()["context"]["window"]
    assert said in window["advice"] and "next time you ask" not in str(window)


def test_lowering_the_minimum_reloads_a_model_closedesk_raised(store, settings, mail, monkeypatch):
    settings.llm = True
    server = MacLMStudio({SMALL: 4096})
    _serve(monkeypatch, server)
    local_llm.set_min_context(settings, 32768)
    steps, modes, _text = _ask(store, settings, "What is the total in this budget?", mail["Q4 budget draft"].id)
    assert not modes and server.loaded[SMALL] == 32768

    client = TestClient(web.create_app(settings, store))
    eight = agent.CONTEXT_STEPS.index(8192)
    saved = client.post("/settings/context", data={"step": eight}, headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert saved.status_code == 303 and "notice=context-reloaded" in saved.headers["location"]
    assert server.calls[-2:] == [("unload", SMALL), ("load", SMALL, 8192)] and server.loaded[SMALL] == 8192
    assert f"Reloaded {SMALL} in LM Studio with an 8,192-token context." in client.get(saved.headers["location"]).text

    # The workspace's Settings says the same.
    four = agent.CONTEXT_STEPS.index(4096)
    local_llm._context_given[SMALL] = 8192  # as if raised again
    local_llm._context_before[SMALL] = 2048
    reply = client.post("/api/settings/context", json={"step": four}, headers={"X-CloseDesk": "1", "Origin": "http://testserver"})
    assert reply.json()["message"] == f"Saved. Reloaded {SMALL} in LM Studio with a 4,096-token context."
    assert server.loaded[SMALL] == 4096

    # Lowered to its own size or below (or Off): loaded back as the user had it, and then it is theirs again.
    server.loaded[SMALL] = 32768
    local_llm._context_given[SMALL] = 32768
    local_llm._context_before[SMALL] = 4096
    local_llm._status_cache.clear()
    calls = len(server.calls)
    assert local_llm.set_min_context(settings, 0) == f"Reloaded {SMALL} in LM Studio with a 4,096-token context."
    assert server.calls[calls:] == [("unload", SMALL), ("load", SMALL, 4096)]
    assert local_llm.set_min_context(settings, 4096) == "" and len(server.calls) == calls + 2


def test_lowering_the_minimum_reloads_a_model_chosen_in_settings(settings, monkeypatch):
    settings.llm = True
    server = MacLMStudio({})
    _serve(monkeypatch, server)
    assert local_llm.use_chat_model(settings, SMALL) == ""
    assert server.calls == [("load", SMALL, 16384)]
    assert local_llm.set_min_context(settings, 8192) == f"Reloaded {SMALL} in LM Studio with an 8,192-token context."
    assert server.calls[1:] == [("unload", SMALL), ("load", SMALL, 8192)]
    # Raising it isn't done here: the next question does that, as before.
    assert local_llm.set_min_context(settings, 32768) == "" and len(server.calls) == 3
    # Off: a model CloseDesk loaded has no size of the user's to go back to, so it stays as it is.
    assert local_llm.set_min_context(settings, 0) == "" and len(server.calls) == 3


def test_lowering_the_minimum_while_a_model_loads_says_so_rather_than_wait(settings, monkeypatch):
    settings.llm = True
    server = MacLMStudio({})
    _serve(monkeypatch, server)
    assert local_llm.use_chat_model(settings, SMALL) == ""
    monkeypatch.setattr(local_llm, "CONTEXT_WAIT_SECONDS", 0.05)
    with local_llm._context_lock:  # another load, as long as LM Studio takes
        said = local_llm.set_min_context(settings, 8192)
    assert said.startswith("Left the model as it is for now: LM Studio is loading a model.")
    assert server.calls == [("load", SMALL, 16384)]
    assert settings.min_context_tokens == 8192, "the new minimum is kept all the same"


def test_lowering_the_minimum_leaves_a_model_the_user_loaded_alone(store, settings, mail, monkeypatch):
    settings.llm = True
    server = MacLMStudio({SMALL: 32768})
    _serve(monkeypatch, server)
    assert local_llm.set_min_context(settings, 8192) == ""
    assert server.calls == [] and server.loaded[SMALL] == 32768

    # One CloseDesk raised, and then the user loaded again in LM Studio at a size they chose: theirs, left alone.
    server.loaded = {SMALL: 4096}
    local_llm.set_min_context(settings, 16384)
    _ask(store, settings, "What is the total in this budget?", mail["Q4 budget draft"].id)
    assert server.loaded[SMALL] == 16384
    server.loaded[SMALL] = 65536
    local_llm._status_cache.clear()
    calls = len(server.calls)
    assert local_llm.set_min_context(settings, 8192) == "" and len(server.calls) == calls


def test_a_second_raise_that_runs_out_of_memory_goes_back_to_the_size_that_answered(store, settings, mail, monkeypatch):
    """Raised to 8,192 (it answers), then to 16,384 (it runs out): loaded back with 8,192, not the user's 4,096."""
    settings.llm = True
    server = MacLMStudio({BIG: 4096})
    _serve(monkeypatch, server)
    budget = mail["Q4 budget draft"].id
    local_llm.set_min_context(settings, 8192)
    _steps, modes, text = _ask(store, settings, "What is the total in this budget?", budget)
    assert not modes and "$12,480" in text and server.loaded[BIG] == 8192

    local_llm.set_min_context(settings, 16384)
    steps, modes, text = _ask(store, settings, "And the total again?", budget)
    assert f"Loaded {BIG} back with an 8,192-token context: 16,384 didn't fit in this computer's memory." in steps
    assert server.calls[-2:] == [("unload", BIG), ("load", BIG, 8192)] and server.loaded[BIG] == 8192
    assert not modes and "$12,480" in text
    assert not local_llm.needs_more_context(settings)

    # Lowering the minimum to the size the user had it at still loads it back as theirs.
    assert local_llm.set_min_context(settings, 4096) == f"Reloaded {BIG} in LM Studio with a 4,096-token context."
    assert server.loaded[BIG] == 4096 and BIG not in local_llm._context_given


def test_a_model_closedesk_loaded_that_runs_out_of_memory_steps_down(store, settings, mail, monkeypatch):
    """Load and use (or the start) loads a big model with what the estimate says fits; when its answers still run out
    of memory it is loaded again one step smaller, rather than every question failing."""
    settings.llm = True
    server = MacLMStudio({})
    _serve(monkeypatch, server)
    assert local_llm.use_chat_model(settings, BIG) == ""
    assert server.calls == [("load", BIG, 16384)]
    budget = mail["Q4 budget draft"].id
    steps, modes, text = _ask(store, settings, "What is the total in this budget?", budget)
    assert f"Loaded {BIG} again with an 8,192-token context: 16,384 didn't fit in this computer's memory." in steps
    assert server.calls[1:] == [("unload", BIG), ("load", BIG, 8192)]
    assert not modes and "$12,480" in text
    # The next question neither raises it nor reloads it.
    _steps, modes, text = _ask(store, settings, "And the total again?", budget)
    assert not modes and "$12,480" in text and len(server.calls) == 3
    # Still CloseDesk's: lowering the minimum reloads it smaller.
    assert local_llm.set_min_context(settings, 4096) == f"Reloaded {BIG} in LM Studio with a 4,096-token context."


def test_a_model_closedesk_loaded_at_the_smallest_context_isnt_stepped_below_it(settings, monkeypatch):
    settings.llm = True
    server = MacLMStudio({BIG: 4096})
    _serve(monkeypatch, server)
    local_llm._context_given[BIG] = 4096
    assert local_llm.step_back_context(settings) == "" and server.calls == []


def test_a_model_closedesk_loaded_and_then_raised_is_still_closedesks_when_the_minimum_is_lowered(store, settings, mail, monkeypatch):
    """Load and use loads it at Setup's minimum; a larger minimum has the next question raise it. Lowering the minimum
    after that reloads it at the size chosen, not at the size Load and use gave it, and it stays CloseDesk's to size."""
    settings.llm = True
    server = MacLMStudio({})
    _serve(monkeypatch, server)
    assert local_llm.use_chat_model(settings, SMALL) == ""
    assert server.loaded[SMALL] == 16384
    local_llm.set_min_context(settings, 32768)
    _ask(store, settings, "What is the total in this budget?", mail["Q4 budget draft"].id)
    assert server.loaded[SMALL] == 32768
    assert local_llm.set_min_context(settings, 8192) == f"Reloaded {SMALL} in LM Studio with an 8,192-token context."
    assert server.loaded[SMALL] == 8192 and local_llm._context_given.get(SMALL) == 8192
    assert local_llm.set_min_context(settings, 4096) == f"Reloaded {SMALL} in LM Studio with a 4,096-token context."
    assert server.loaded[SMALL] == 4096


def test_a_table_query_that_runs_out_of_memory_has_the_model_loaded_back_before_the_answer(store, settings, mail, monkeypatch):
    """The table query ran out of memory, was let go without a word, and the answer's own call ran out the same way."""
    settings.llm = True
    server = MacLMStudio({BIG: 4096})
    _serve(monkeypatch, server)
    steps, modes, text = _ask(store, settings, "What is the total in this budget?", mail["Q4 budget draft"].id)
    assert f"Loaded {BIG} back with a 4,096-token context: 16,384 didn't fit in this computer's memory." in steps
    assert [chat[2] for chat in server.chats].count("Compute error") == 1, "asked again at the size that fits"
    assert not modes and "$12,480" in text
