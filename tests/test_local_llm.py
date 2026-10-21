"""The local model client (local_llm.py): loading and switching models, the context, logprobs, effort and
prompts too long for the context."""

from __future__ import annotations

import gc
import json
import threading
import time

import httpx
import pytest

from controller_inbox import local_llm
from controller_inbox.local_llm import ContextOverflow, LocalReader, _post_chat
from test_lm_studio_models import CHAT, CODER, OVIS, LMStudio, _serve


class _SlowLoad(LMStudio):
    """LM Studio loading a big model takes longer than CloseDesk's wait: the request times out, the load goes on."""

    def __init__(self, loaded, slow: set[str]):
        super().__init__(loaded=loaded)
        self.slow_loads = slow

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/models/load":
            payload = json.loads(request.content)
            if payload["model"] in self.slow_loads:
                self.calls.append(("load", payload["model"], payload.get("context_length")))
                self.loaded.append(payload["model"])  # still loading in LM Studio, using the memory
                self.slow_loads.discard(payload["model"])
                raise httpx.ReadTimeout("timed out", request=request)
        return super().__call__(request)


def test_switching_the_chat_model_never_has_two_big_models_loading_at_once(settings, monkeypatch):
    settings.llm = None
    server = _SlowLoad(loaded=[CODER], slow={CHAT})
    _serve(monkeypatch, server)
    local_llm.use_chat_model(settings, CHAT)
    loads = [call for call in server.calls if call[0] == "load"]
    # The slow load of CHAT was taken for a failure and CODER was loaded back beside it.
    assert len(loads) == 1, f"a second model was loaded while the first was still loading: {server.calls}"


def test_raising_the_context_never_loads_the_model_twice(settings, monkeypatch):
    settings.llm = None
    settings.min_context_tokens = 65536
    server = _SlowLoad(loaded=[CHAT], slow={CHAT})
    _serve(monkeypatch, server)
    assert local_llm.needs_more_context(settings)
    local_llm.ensure_context(settings)
    loads = [call for call in server.calls if call[0] == "load"]
    assert len(loads) == 1, f"the model was loaded again while the longer load was still going: {server.calls}"


# A request being answered is not disturbed by raising the context or switching the model ---------------------
def test_the_context_isnt_raised_under_a_request_the_model_is_answering(settings, monkeypatch):
    settings.llm = None
    settings.min_context_tokens = 65536
    server = LMStudio(loaded=[CHAT])
    _serve(monkeypatch, server)
    with local_llm._using(CHAT):  # the overnight reading, a page read or another chat is using it
        said = local_llm.ensure_context(settings)
    assert server.calls == [] and "answering another request" in said
    assert local_llm.needs_more_context(settings), "tried again once the model is free"


def test_the_chat_model_isnt_switched_while_an_answer_is_being_written(settings, monkeypatch):
    settings.llm = None
    server = LMStudio(loaded=[CODER])
    _serve(monkeypatch, server)
    with local_llm._using(CODER):
        said = local_llm.use_chat_model(settings, CHAT)
    assert server.calls == [] and "Try again once it is done" in said


class _NeverShows(LMStudio):
    """A load that times out and doesn't show up in LM Studio's list within CloseDesk's wait."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/models/load":
            self.calls.append(("load", json.loads(request.content)["model"]))
            raise httpx.ReadTimeout("timed out", request=request)
        return super().__call__(request)


def test_a_load_still_going_past_the_wait_is_said_plainly_and_no_other_load_starts(settings, monkeypatch):
    settings.llm = None
    monkeypatch.setattr(local_llm, "LOAD_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(local_llm, "LOAD_POLL_SECONDS", 0.0)
    server = _NeverShows(loaded=[CODER])
    _serve(monkeypatch, server)
    said = local_llm.use_chat_model(settings, CHAT)
    assert "still loading" in said
    assert [call for call in server.calls if call[0] == "load"] == [("load", CHAT)], "the unloaded model wasn't loaded back beside it"
    # While it may still be loading, nothing else is loaded.
    monkeypatch.setattr(local_llm, "LOAD_WAIT_SECONDS", 3600.0)
    assert "still loading" in local_llm.use_chat_model(settings, CODER)
    assert [call for call in server.calls if call[0] == "load"] == [("load", CHAT)]


# A prompt too long for the context --------------------------------------------------------------------------
OVERFLOW = {"error": "the request exceeds the available context size, try increasing it"}


def _stream_server(monkeypatch, handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(local_llm.httpx, "get", lambda url, **kw: httpx.Response(503, request=httpx.Request("GET", url)))
    monkeypatch.setattr(local_llm.httpx, "post", lambda url, **kw: client.post(url, json=kw.get("json")))
    monkeypatch.setattr(local_llm.httpx, "stream", lambda method, url, **kw: client.stream(method, url, json=kw.get("json")))


def _streamed(text: str) -> httpx.Response:
    body = f"data: {json.dumps({'choices': [{'delta': {'content': text}, 'finish_reason': 'stop'}]})}\n\ndata: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)


def test_a_prompt_too_long_is_not_taken_for_a_server_without_logprobs(settings, monkeypatch):
    """A context overflow while asking for token probabilities raises ContextOverflow at once and keeps logprobs on."""
    settings.llm = None
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(400, json=OVERFLOW)

    _stream_server(monkeypatch, handler)
    local_llm._logprobs_rejected.clear()
    with pytest.raises(ContextOverflow):
        list(local_llm.stream_text(settings, [{"role": "user", "content": "a long page"}], token_probs=[]))
    assert len(calls) == 1, "the prompt too long for the context was sent again"
    assert settings.llm_model not in local_llm._logprobs_rejected, "confidence was turned off for good over a long prompt"


def test_logprobs_are_only_given_up_when_the_request_without_them_goes_through(settings, monkeypatch):
    """A 400 to logprobs followed by a failure without them is not remembered as a server that refuses logprobs."""
    settings.llm = None
    replies = iter([httpx.Response(400, json={"error": "unknown parameter: logprobs"}), httpx.Response(500, json={"error": "busy"})])
    _stream_server(monkeypatch, lambda _r: next(replies))
    local_llm._logprobs_rejected.clear()
    with pytest.raises(httpx.HTTPError):
        list(local_llm.stream_text(settings, [{"role": "user", "content": "hi"}], token_probs=[]))
    assert settings.llm_model not in local_llm._logprobs_rejected

    replies = iter([httpx.Response(400, json={"error": "unknown parameter: logprobs"}), _streamed("ok")])
    assert "".join(local_llm.stream_text(settings, [{"role": "user", "content": "hi"}], token_probs=[])) == "ok"
    assert settings.llm_model in local_llm._logprobs_rejected
    local_llm._logprobs_rejected.clear()


def test_a_prompt_too_long_is_sent_once_by_the_reader_and_by_complete_text(settings, monkeypatch):
    """Overflow isn't retried without reasoning_effort or response_format: one request, then a failure noted."""
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(400, json=OVERFLOW)

    reader = LocalReader(settings, model="tiny-3b", client=httpx.Client(transport=httpx.MockTransport(handler)))
    reader._effort = "none"
    assert reader.read({"subject": "Invoice", "body": "x" * 200}) is None
    assert len(calls) == 1, f"the prompt too long for the context was sent {len(calls)} times"
    assert "context" in reader.stats.last_error

    calls.clear()
    settings.llm = None
    _stream_server(monkeypatch, handler)
    monkeypatch.setattr(local_llm, "reasoning_effort", lambda *_a, **_k: "none")
    with pytest.raises(ContextOverflow):
        local_llm.complete_text(settings, [{"role": "user", "content": "a long page"}])
    assert len(calls) == 1


class _StillLoading(LMStudio):
    """LM Studio loading a model past CloseDesk's wait: the request times out and the model isn't in yet."""

    def __init__(self, loaded, slow: str):
        super().__init__(loaded=loaded)
        self.slow_load = slow
        self.asked_to_load = threading.Event()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/models/load":
            payload = json.loads(request.content)
            self.calls.append(("load", payload["model"], payload.get("context_length")))
            if payload["model"] == self.slow_load:
                self.asked_to_load.set()
                raise httpx.ReadTimeout("timed out", request=request)
        return super().__call__(request)


def test_a_chat_question_isnt_held_up_while_another_model_loads(settings, monkeypatch):
    """ensure_context answers at once, with a note, while a page reader's slow load is being watched."""
    settings.llm = None
    settings.min_context_tokens = 65536
    server = _StillLoading(loaded=[CHAT], slow=OVIS)
    _serve(monkeypatch, server)
    monkeypatch.setattr(local_llm, "LOAD_WAIT_SECONDS", 1.5)
    monkeypatch.setattr(local_llm, "LOAD_POLL_SECONDS", 0.05)
    monkeypatch.setattr(local_llm, "CONTEXT_WAIT_SECONDS", 0.05)
    local_llm._pending_loads.clear()
    assert local_llm.needs_more_context(settings)

    reading = threading.Thread(target=local_llm.load_for_reading, args=(settings, OVIS, 8192))
    reading.start()
    try:
        assert server.asked_to_load.wait(5)
        started = time.monotonic()
        said = local_llm.ensure_context(settings)
        waited = time.monotonic() - started
    finally:
        reading.join(5)
        local_llm._pending_loads.clear()
    assert waited < 0.75, f"the chat question waited {waited:.1f}s for another model's load"
    assert "later question" in said
    assert [call for call in server.calls if call[0] in {"load", "unload"}] == [("load", OVIS, 8192)], "two loads ran at once"
    assert local_llm.needs_more_context(settings), "the longer context is still tried on a later question"


# reasoning_effort is only blamed when the request without it works.
def test_effort_not_blamed_for_a_response_format_rejection(settings):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(("reasoning_effort" in body, "response_format" in body))
        if "response_format" in body:
            return httpx.Response(400, json={"error": "response_format not supported"})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"category": "ap_invoice"})}}]})

    reader = LocalReader(settings, model="tiny", client=httpx.Client(transport=httpx.MockTransport(handler)))
    reader._effort = "none"
    assert reader.read({"subject": "x"}) == {"category": "ap_invoice"}
    assert "tiny" not in local_llm._effort_rejected
    assert calls[-1] == (True, False), "the plain retry still asks for no thinking"


def test_effort_blamed_when_dropping_it_works(settings):
    def handler(request):
        if "reasoning_effort" in json.loads(request.content):
            return httpx.Response(400, json={"error": "Unrecognized key"})
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    payload = {"model": "m1", "reasoning_effort": "none"}
    assert _post_chat(client.post, "http://x/chat/completions", payload, settings).status_code == 200
    assert "m1" in local_llm._effort_rejected


# The reader closes the client it made.
def test_reader_closes_its_own_client(settings):
    with LocalReader(settings, model="tiny") as reader:
        client = reader.client
    assert client.is_closed
    leaked = LocalReader(settings, model="tiny").client
    gc.collect()
    assert leaked.is_closed
    mine = httpx.Client()
    LocalReader(settings, model="tiny", client=mine).close()
    assert not mine.is_closed
    mine.close()
