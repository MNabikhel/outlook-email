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
from controller_inbox.local_llm import check_model, complete_text, failure_note, llm_active, stream_text

CODER, CHAT = "qwen2.5-coder-7b-instruct", "qwen/qwen3.5-9b"


class LMStudio:
    """LM Studio 0.4 with just-in-time loading on: /v1/models lists every downloaded model."""

    def __init__(self, loaded: list[str] | None = None):
        self.loaded = list(loaded or [])
        self.slow = False  # the model routes take longer than a status check waits
        self.down = False
        self.asked: list[str] = []
        self.timeouts: list[float] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refused", request=request)
        path = request.url.path
        if path.endswith("/models") and self.slow:
            raise httpx.ReadTimeout("timed out", request=request)
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": CODER}, {"id": CHAT}, {"id": "text-embedding-nomic-embed-text-v1.5"}]})
        if path == "/api/v1/models":
            models = [
                {"type": "llm", "key": key, "loaded_instances": [{"id": key, "config": {"context_length": 16384}}] if key in self.loaded else []}
                for key in (CODER, CHAT)
            ]
            return httpx.Response(200, json={"models": models})
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
