"""Which model CloseDesk asks LM Studio for, and how long it waits.

LM Studio with just-in-time loading lists every downloaded model on /v1/models and loads whichever a request names:
with nothing loaded, the first on that list (a coding model) was loaded though nobody chose it. A busy LM Studio
(loading a model, reading a long prompt on a laptop) was taken for one that had stopped.
"""

from __future__ import annotations

import json
import time

import httpx

from controller_inbox import local_llm
from controller_inbox.local_llm import check_model, complete_text, failure_note, llm_active, stream_text, use_chat_model

CODER, CHAT = "qwen2.5-coder-7b-instruct", "qwen/qwen3.5-9b"
OVIS, EMBED = "ath-maas_ovisocr2", "text-embedding-nomic-embed-text-v1.5"


class LMStudio:
    """LM Studio 0.4 with just-in-time loading on: /v1/models lists every downloaded model."""

    def __init__(self, loaded: list[str] | None = None):
        self.loaded = list(loaded or [])
        self.contexts = {key: 16384 for key in self.loaded}
        self.slow = False  # the model routes take longer than a status check waits
        self.down = False
        self.asked: list[str] = []
        self.timeouts: list[float] = []
        self.fails_to_load: set[str] = set()  # not enough memory for these
        self.calls: list[tuple] = []  # loads and unloads, in order

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refused", request=request)
        path = request.url.path
        if path.endswith("/models") and self.slow:
            raise httpx.ReadTimeout("timed out", request=request)
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": key} for key in (CODER, CHAT, OVIS, EMBED)]})
        if path == "/api/v1/models":

            def item(kind, key, **extra):
                instances = [{"id": key, "config": {"context_length": self.contexts.get(key, 4096)}}] if key in self.loaded else []
                return {"type": kind, "key": key, "loaded_instances": instances, **extra}

            models = [
                item("llm", CODER, max_context_length=32768),
                item("llm", CHAT, max_context_length=262144),
                item("llm", OVIS, capabilities={"vision": True}),
                item("embedding", EMBED),
            ]
            return httpx.Response(200, json={"models": models})
        if path == "/api/v1/models/load":
            payload = json.loads(request.content)
            self.calls.append(("load", payload["model"], payload.get("context_length")))
            if payload["model"] in self.fails_to_load:
                return httpx.Response(500, json={"error": "not enough memory to load the model"})
            self.loaded.append(payload["model"])
            self.contexts[payload["model"]] = payload.get("context_length") or 4096
            return httpx.Response(200, json={"status": "loaded"})
        if path == "/api/v1/models/unload":
            instance = json.loads(request.content)["instance_id"]
            self.calls.append(("unload", instance))
            self.loaded.remove(instance)
            return httpx.Response(200, json={"status": "unloaded"})
        if path == "/v1/chat/completions":
            payload = json.loads(request.content)
            self.asked.append(payload["model"])
            self.timeouts.append(request.extensions["timeout"]["read"])
            if payload.get("stream"):
                body = 'data: {"choices": [{"delta": {"content": "It is $12,480."}}]}\n\ndata: [DONE]\n\n'
                return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json={"choices": [{"message": {"content": "It is $12,480."}, "finish_reason": "stop"}]})
        return httpx.Response(404)


def _serve(monkeypatch, server: LMStudio) -> None:
    client = httpx.Client(transport=httpx.MockTransport(server))

    def get(url, **kw):
        return client.get(url, headers=kw.get("headers"), timeout=kw.get("timeout"))

    def post(url, **kw):
        return client.post(url, json=kw.get("json"), headers=kw.get("headers"), timeout=kw.get("timeout"))

    def stream(method, url, **kw):
        return client.stream(method, url, json=kw.get("json"), headers=kw.get("headers"), timeout=kw.get("timeout"))

    monkeypatch.setattr(local_llm.httpx, "get", get)
    monkeypatch.setattr(local_llm.httpx, "post", post)
    monkeypatch.setattr(local_llm.httpx, "stream", stream)


def test_with_nothing_loaded_lm_studio_isnt_asked_for_the_first_model_on_its_list(settings, monkeypatch):
    settings.llm = None
    server = LMStudio(loaded=[])
    _serve(monkeypatch, server)
    status = check_model(settings)
    assert status.reachable and status.model == "" and not llm_active(settings)
    assert "no model is loaded. Load one in LM Studio" in status.describe()
    assert server.asked == [], "nothing was asked of a model nobody chose"


def test_the_chat_model_last_seen_loaded_is_the_one_asked_for_when_nothing_is(settings, monkeypatch):
    settings.llm = None
    server = LMStudio(loaded=[CHAT])
    _serve(monkeypatch, server)
    assert check_model(settings).model == CHAT
    # LM Studio restarted, nothing loaded: Qwen3.5 is asked for (LM Studio loads it), not the coding model.
    server.loaded = []
    local_llm._status_cache.clear()
    assert check_model(settings).model == CHAT
    assert complete_text(settings, [{"role": "user", "content": "How much is due?"}]) == "It is $12,480."
    assert server.asked == [CHAT]
    # A model set in the settings is asked for whatever is loaded.
    settings.llm_model = CODER
    local_llm._status_cache.clear()
    assert check_model(settings).model == CODER


def test_a_busy_lm_studio_is_still_answering_and_one_that_is_gone_is_not(settings, monkeypatch):
    settings.llm = None
    server = LMStudio(loaded=[CHAT])
    _serve(monkeypatch, server)
    assert llm_active(settings)
    # Loading a model or reading a long prompt, LM Studio takes longer than a status check waits.
    server.slow = True
    key, (at, status) = next(iter(local_llm._status_cache.items()))
    local_llm._status_cache[key] = (at - local_llm._STATUS_TTL_SECONDS - 1, status)
    assert llm_active(settings) and check_model(settings).model == CHAT
    # Gone for good (the server refuses), or silent for longer than the grace: not answering.
    server.slow, server.down = False, True
    local_llm._status_cache[key] = (at - local_llm._STATUS_TTL_SECONDS - 1, status)
    assert not llm_active(settings)
    server.down, server.slow = False, True
    local_llm._status_cache[key] = (time.monotonic() - local_llm.BUSY_GRACE_SECONDS - 1, status)
    assert not llm_active(settings)


def test_a_busy_lm_studio_never_seen_before_isnt_asked_for_the_first_model_either(settings, monkeypatch):
    settings.llm = None
    server = LMStudio(loaded=[CHAT])
    _serve(monkeypatch, server)
    original = server.__call__

    def lists_but_slow_on_its_own_routes(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/"):
            raise httpx.ReadTimeout("timed out", request=request)
        return original(request)

    _serve(monkeypatch, lists_but_slow_on_its_own_routes)
    assert check_model(settings).model == ""


def test_the_chat_waits_minutes_for_a_long_prompt_to_be_read(settings, monkeypatch):
    settings.llm = None
    server = LMStudio(loaded=[CHAT])
    _serve(monkeypatch, server)
    messages = [{"role": "user", "content": "How much is due?"}]
    assert "".join(stream_text(settings, messages)) == "It is $12,480."
    assert complete_text(settings, messages) == "It is $12,480."
    assert server.timeouts == [local_llm.CHAT_TIMEOUT, local_llm.CHAT_TIMEOUT]
    settings.llm_timeout = 900  # set longer for a slow computer: taken as set
    list(stream_text(settings, messages))
    assert server.timeouts[-1] == 900


def test_a_model_that_took_too_long_is_said_to_be_busy_not_broken():
    note = failure_note(httpx.ReadTimeout("timed out"))
    assert "took too long to start answering" in note and "CONTROLLER_INBOX_LLM_TIMEOUT" in note
    assert failure_note(httpx.ConnectError("refused")) == "The local model didn't answer (refused)."


def test_the_chat_model_chosen_in_setup_is_loaded_in_place_of_the_other(settings, monkeypatch):
    settings.llm = None
    server = LMStudio(loaded=[CODER, OVIS])
    _serve(monkeypatch, server)
    status = check_model(settings)
    assert status.model == CODER and list(status.chat_models) == [CODER, CHAT], "the page reader isn't offered to answer"
    assert use_chat_model(settings, CHAT) == ""
    # The coding model goes first, so the two don't share the memory; the page reader stays loaded.
    assert server.calls == [("unload", CODER), ("load", CHAT, settings.min_context_tokens)]
    assert server.loaded == [OVIS, CHAT]
    assert check_model(settings).model == CHAT and local_llm.remembered_model(settings) == CHAT
    # Chosen again while it is loaded: nothing to do.
    assert use_chat_model(settings, CHAT) == "" and len(server.calls) == 2
    # No more context than the model supports.
    settings.min_context_tokens = 65536
    assert use_chat_model(settings, CODER) == ""
    assert server.calls[-2:] == [("unload", CHAT), ("load", CODER, 32768)]


def test_a_model_lm_studio_cant_load_leaves_the_one_before_answering(settings, monkeypatch):
    settings.llm = None
    server = LMStudio(loaded=[CHAT])
    server.fails_to_load = {CODER}
    _serve(monkeypatch, server)
    problem = use_chat_model(settings, CODER)
    assert problem == f"LM Studio couldn't load {CODER} (not enough memory to load the model). {CHAT} was loaded back."
    assert server.loaded == [CHAT] and server.contexts[CHAT] == 16384, "loaded back as it was"
    assert check_model(settings).model == CHAT
    # Not a chat model LM Studio has (the page reader isn't one), or LM Studio stopped: nothing is touched.
    assert use_chat_model(settings, OVIS) == f"LM Studio doesn't have {OVIS} downloaded."
    assert use_chat_model(settings, "gpt-oss-20b") == "LM Studio doesn't have gpt-oss-20b downloaded."
    server.down = True
    assert use_chat_model(settings, CODER) == "LM Studio's server isn't answering. Start it, then try again."
    assert len(server.calls) == 3


def test_setup_offers_the_chat_models_and_loads_the_one_chosen(settings, store, monkeypatch):
    from fastapi.testclient import TestClient

    from controller_inbox import web

    settings.llm = None
    server = LMStudio(loaded=[CODER])
    _serve(monkeypatch, server)
    client = TestClient(web.create_app(settings, store))
    page = client.get("/settings").text
    assert f'<option value="{CODER}" selected>{CODER} (loaded)</option>' in page
    assert f'<option value="{CHAT}" >{CHAT}</option>' in page and f'<option value="{OVIS}"' not in page.split('id="chat-model"')[1].split("</form>")[0]
    origin = {"Origin": "http://testserver"}
    reply = client.post("/settings/chat-model", data={"model": CHAT}, headers=origin, follow_redirects=False)
    assert reply.headers["location"] == "/settings?notice=chat-model#chat-model"
    page = client.get(reply.headers["location"]).text
    assert "Loaded in LM Studio. It answers your questions now." in page
    assert f'<option value="{CHAT}" selected>{CHAT} (loaded)</option>' in page
    server.fails_to_load = {CODER}
    reply = client.post("/settings/chat-model", data={"model": CODER}, headers=origin, follow_redirects=False)
    page = client.get(reply.headers["location"]).text
    assert "The model that answers wasn&#39;t changed." in page and f"{CHAT} was loaded back." in page
    # A model named in .env answers whatever is loaded, so Setup says where it is set instead of offering a list.
    settings.llm_model = CODER
    local_llm._status_cache.clear()
    page = client.get("/settings").text
    assert f"CONTROLLER_INBOX_LLM_MODEL={CODER}" in page and 'name="model" required' not in page


class _WithPageReader(LMStudio):
    """LM Studio with a general vision model chosen in Setup as the page reader, listed before the chat model."""

    VL = "google/gemma-3-12b"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": key} for key in (self.VL, CHAT)]})
        if path == "/api/v1/models":

            def item(kind, key, **extra):
                instances = [{"id": key, "config": {"context_length": self.contexts.get(key, 4096)}}] if key in self.loaded else []
                return {"type": kind, "key": key, "loaded_instances": instances, "max_context_length": 131072, **extra}

            return httpx.Response(200, json={"models": [item("vlm", self.VL, capabilities={"vision": True}), item("llm", CHAT)]})
        return super().__call__(request)


def test_the_chat_model_keeps_answering_after_the_page_reader_chosen_in_setup_loads(settings, monkeypatch):
    """The page reader loaded beside the chat model to read a scan came first on LM Studio's list, so it became the
    chat model (and the remembered one)."""
    settings.llm = None
    settings.ensure_data_dir()
    settings.vision_model = _WithPageReader.VL
    server = _WithPageReader(loaded=[])
    _serve(monkeypatch, server)
    assert use_chat_model(settings, CHAT) == ""
    assert local_llm.load_for_reading(settings, _WithPageReader.VL, 12288) == ""
    assert sorted(server.loaded) == sorted([CHAT, _WithPageReader.VL])
    assert check_model(settings, use_cache=False).model == CHAT
    assert local_llm.remembered_model(settings) == CHAT


def test_switching_the_chat_model_leaves_the_page_reader_chosen_in_setup_loaded(settings, monkeypatch):
    """Only an OvisOCR page reader was spared: a general vision model chosen in Setup was unloaded."""
    settings.llm = None
    settings.ensure_data_dir()
    settings.vision_model = _WithPageReader.VL
    server = _WithPageReader(loaded=[_WithPageReader.VL])
    _serve(monkeypatch, server)
    assert use_chat_model(settings, CHAT) == ""
    assert _WithPageReader.VL in server.loaded


def test_a_search_model_only_downloaded_is_not_said_to_be_loaded(settings, monkeypatch):
    """LM Studio lists every downloaded model on /v1/models, so Setup said the search model was "Loaded."."""
    from controller_inbox import model_roles

    settings.llm = None
    settings.embedding_model = EMBED
    server = LMStudio(loaded=[CHAT])
    _serve(monkeypatch, server)
    status = check_model(settings)
    assert model_roles.search_row(settings, status)["status"] == "Downloaded: LM Studio loads it when mail is searched."
    server.loaded.append(EMBED)
    status = check_model(settings, use_cache=False)
    assert model_roles.search_row(settings, status)["status"] == "Loaded."
    assert EMBED not in status.instances and status.model == CHAT
