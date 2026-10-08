"""Reasoning models behind LM Studio (Qwen3.x, DeepSeek-R1, gpt-oss).

LM Studio can put a reasoning model's JSON in ``reasoning_content`` and leave
``content`` empty (lmstudio-bug-tracker #1698, #1773), and a thinking model can
spend a small token budget before it writes any answer.
"""

from __future__ import annotations

import json

import httpx
import pytest

from controller_inbox import local_llm
from controller_inbox.assistant import answer_stream, draft_reply
from controller_inbox.config import Settings
from controller_inbox.local_llm import EmptyReply, LocalReader, check_model, complete_text, reasoning_effort, stream_text
from controller_inbox.overnight import read_queue

READING = {
    "category": "ap_invoice",
    "folder": "important",
    "importance": "high",
    "summary": "Northwind invoice to enter.",
    "actions": [],
    "why": "Vendor bill.",
}
THOUGHT = "Let me think about this email carefully. " * 80


class FakeLMStudio:
    """LM Studio 0.4 with a Qwen3.5-style model loaded, as the bug reports describe it."""

    def __init__(self, *, native: bool = True, rejects_effort: bool = False, think_tokens: int = 600, options=("off", "on")):
        self.native = native
        self.options = list(options)
        self.rejects_effort = rejects_effort
        self.think_tokens = think_tokens
        self.calls: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/models":
            ids = ["qwen/qwen3.5-35b-a3b", "text-embedding-nomic-embed-text-v1.5", "qwen/qwen3.5-4b"]
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        if path == "/api/v1/models":
            if not self.native:
                return httpx.Response(404, json={"error": "not found"})
            reasoning = {"reasoning": {"allowed_options": self.options, "default": "on"}}
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"type": "llm", "key": "qwen/qwen3.5-35b-a3b", "loaded_instances": [], "capabilities": reasoning},
                        {"type": "embedding", "key": "text-embedding-nomic-embed-text-v1.5", "loaded_instances": []},
                        {"type": "llm", "key": "qwen/qwen3.5-4b", "loaded_instances": [{"id": "qwen/qwen3.5-4b"}], "capabilities": reasoning},
                    ]
                },
            )
        if path == "/api/v0/models":
            return httpx.Response(404)
        payload = json.loads(request.content)
        self.calls.append(payload)
        if self.rejects_effort and "reasoning_effort" in payload:
            return httpx.Response(400, json={"error": "Unrecognized key 'reasoning_effort'"})
        answer = json.dumps(READING) if "JSON object" in payload["messages"][0]["content"] else "Start with [1]; it needs you today."
        thinking = "off" not in self.options or payload.get("reasoning_effort") not in {"none", "minimal"}
        thought = THOUGHT if thinking else ""
        if (payload.get("response_format") or {}).get("type") == "json_schema":
            reasoning, content, finish = (thought + answer), "", "stop"
        elif thinking and self.think_tokens >= int(payload.get("max_tokens") or 0):
            reasoning, content, finish = thought, "", "length"
        else:
            reasoning, content, finish = thought, answer, "stop"
        if payload.get("stream"):
            events = [{"choices": [{"delta": {"reasoning_content": reasoning}}]}] if reasoning else []
            events += [{"choices": [{"delta": {"content": content[i : i + 10]}}]} for i in range(0, len(content), 10)]
            events.append({"choices": [{"delta": {}, "finish_reason": finish}]})
            body = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)
        message = {"role": "assistant", "content": content, "reasoning_content": reasoning}
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": finish}]})


def _serve(monkeypatch, server: FakeLMStudio) -> httpx.Client:
    client = httpx.Client(transport=httpx.MockTransport(server))

    def get(url, **kw):
        return client.get(url, headers=kw.get("headers"))

    def post(url, **kw):
        return client.post(url, json=kw.get("json"), headers=kw.get("headers"))

    def stream(method, url, **kw):
        return client.stream(method, url, json=kw.get("json"), headers=kw.get("headers"))

    monkeypatch.setattr(local_llm.httpx, "get", get)
    monkeypatch.setattr(local_llm.httpx, "post", post)
    monkeypatch.setattr(local_llm.httpx, "stream", stream)
    return client


@pytest.fixture
def auto(settings: Settings) -> Settings:
    settings.llm = None
    return settings


def test_setup_picks_the_loaded_model_not_the_first_downloaded_one(auto, monkeypatch):
    _serve(monkeypatch, FakeLMStudio())
    status = check_model(auto)
    assert status.active
    assert status.model == "qwen/qwen3.5-4b", "JIT lists every model; only the loaded one should be used"
    assert status.reasoning == ["off", "on"]
    assert reasoning_effort(auto, status.model) == "none"


def test_older_lm_studio_reports_loaded_models_on_api_v0(auto, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "big-model"}, {"id": "small-model"}]})
        if request.url.path == "/api/v0/models":
            return httpx.Response(
                200,
                json={"data": [{"id": "big-model", "type": "llm", "state": "not-loaded"}, {"id": "small-model", "type": "llm", "state": "loaded"}]},
            )
        return httpx.Response(404)

    monkeypatch.setattr(local_llm.httpx, "get", lambda url, **kw: httpx.Client(transport=httpx.MockTransport(handler)).get(url))
    assert check_model(auto).model == "small-model"


def test_a_configured_model_still_wins(auto, monkeypatch):
    _serve(monkeypatch, FakeLMStudio())
    auto.llm_model = "qwen/qwen3.5-35b-a3b"
    assert check_model(auto).model == "qwen/qwen3.5-35b-a3b"


def test_reader_takes_the_json_lm_studio_put_in_reasoning_content(auto, monkeypatch):
    server = FakeLMStudio(native=False)
    client = _serve(monkeypatch, server)
    reader = LocalReader(auto, model="qwen/qwen3.5-4b", client=client)
    parsed = reader.read({"subject": "Invoice 10482", "body": "Please pay."})
    assert parsed and parsed["category"] == "ap_invoice"
    assert len(server.calls) == 1 and server.calls[0]["response_format"]["type"] == "json_schema"


def test_reader_recovers_when_the_model_thinks_past_its_budget(auto, monkeypatch):
    server = FakeLMStudio(native=False, rejects_effort=True)
    client = _serve(monkeypatch, server)
    reader = LocalReader(auto, model="qwen/qwen3.5-4b", client=client)
    reader._structured = False  # a server that ignores response_format
    first = reader.read({"subject": "one", "body": "x"})
    assert first and first["folder"] == "important"
    assert [call["max_tokens"] for call in server.calls if "reasoning_effort" not in call] == [450, 2048]
    server.calls.clear()
    assert reader.read({"subject": "two", "body": "y"})
    assert len(server.calls) == 1 and server.calls[0]["max_tokens"] == 2048, "later messages start with room to think"
    assert reader.stats.read == 2 and not reader.stopped


def test_a_whole_run_reads_every_email_with_a_reasoning_model(loaded, auto, monkeypatch):
    server = FakeLMStudio()
    client = _serve(monkeypatch, server)
    waiting = loaded.counts()["waiting_on_bionic"]
    result = read_queue(loaded, auto, limit=6, reader=LocalReader(auto, client=client))
    assert result["model"] == "qwen/qwen3.5-4b"
    assert len(result["read_ids"]) == 6 and "Stopped" not in result["note"]
    assert loaded.counts()["waiting_on_bionic"] == waiting - 6
    assert all(call["reasoning_effort"] == "none" for call in server.calls), "thinking is turned off for filing"


def test_unusable_replies_say_what_came_back(auto, monkeypatch):
    monkeypatch.setattr(local_llm.httpx, "get", lambda url, **kw: httpx.Response(503, request=httpx.Request("GET", url)))
    reply = {"choices": [{"message": {"content": "I think this is an invoice."}}]}
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=reply)))
    reader = LocalReader(auto, model="tiny", client=client)
    for subject in ("a", "b", "c"):
        assert reader.read({"subject": subject}) is None
    assert reader.stats.stopped_reason == "three messages in a row failed (reply was not the JSON shape: “I think this is an invoice.”)"

    assert local_llm.Reply("", "hmm", "length").why_unusable() == "the model used its whole reply budget thinking"
    assert local_llm.Reply("", "hmm", "stop").why_unusable() == "the model sent only its reasoning, no answer"
    assert local_llm.Reply("").why_unusable() == "the model sent an empty reply"


def test_chat_gives_a_thinking_model_room_and_turns_thinking_down(auto, monkeypatch):
    server = FakeLMStudio(native=False)
    _serve(monkeypatch, server)
    text = "".join(stream_text(auto, [{"role": "system", "content": "chat"}, {"role": "user", "content": "hi"}], max_tokens=500))
    assert text == "Start with [1]; it needs you today."
    assert [(call["max_tokens"], call.get("reasoning_effort")) for call in server.calls] == [(500, None), (2048, "none")]
    server.calls.clear()
    assert "".join(stream_text(auto, [{"role": "system", "content": "chat"}], max_tokens=500))
    assert len(server.calls) == 1, "the next question goes straight to the settings that worked"


def test_a_model_that_always_thinks_gets_room_from_the_first_request(auto, monkeypatch):
    server = FakeLMStudio(options=["on"], think_tokens=1500)
    _serve(monkeypatch, server)
    assert reasoning_effort(auto, "qwen/qwen3.5-4b") is None, "a model that can't turn thinking off isn't asked to"
    text = "".join(stream_text(auto, [{"role": "system", "content": "chat"}, {"role": "user", "content": "hi"}], max_tokens=500))
    assert text == "Start with [1]; it needs you today."
    assert [(call["max_tokens"], call.get("reasoning_effort")) for call in server.calls] == [(2048, None)]

    server.calls.clear()
    reader = LocalReader(auto, client=_serve(monkeypatch, server))
    assert reader.model == "qwen/qwen3.5-4b"
    reader._structured = False
    assert reader.read({"subject": "Invoice INV-7", "body": "Northwind, $4,200 due Friday."})
    assert [call["max_tokens"] for call in server.calls] == [2048]


def test_chat_raises_instead_of_a_blank_answer(auto, monkeypatch):
    def handler(_request):
        body = f"data: {json.dumps({'choices': [{'delta': {'reasoning_content': 'thinking'}, 'finish_reason': 'length'}]})}\n\ndata: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    monkeypatch.setattr(local_llm.httpx, "get", lambda url, **kw: httpx.Response(503, request=httpx.Request("GET", url)))
    monkeypatch.setattr(
        local_llm.httpx, "stream", lambda method, url, **kw: httpx.Client(transport=httpx.MockTransport(handler)).stream(method, url, json=kw.get("json"))
    )
    with pytest.raises(EmptyReply, match="whole reply budget thinking"):
        list(stream_text(auto, [{"role": "user", "content": "hi"}]))


def test_an_error_the_server_writes_into_the_stream_is_not_taken_for_the_answer(loaded, auto, monkeypatch):
    sent = {}

    def handler(request):
        if request.url.path != "/v1/chat/completions":
            return httpx.Response(404)
        start = f"data: {json.dumps({'choices': [{'delta': {'content': 'INV-10482 is for $4,200 and is due'}}]})}\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=start + sent["error"])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(local_llm.httpx, "get", lambda url, **kw: client.get(url))
    monkeypatch.setattr(local_llm.httpx, "stream", lambda method, url, **kw: client.stream(method, url, json=kw.get("json")))
    # llama.cpp writes a failure partway as an "error:" line; LM Studio and OpenAI as a chunk with "error".
    sent["error"] = f"error: {json.dumps({'code': 500, 'message': 'slot crashed', 'type': 'server_error'})}\n\n"
    with pytest.raises(httpx.HTTPError, match="slot crashed"):
        list(stream_text(auto, [{"role": "user", "content": "hi"}]))
    sent["error"] = f"data: {json.dumps({'error': {'message': 'the request exceeds the available context size'}})}\n\n"
    with pytest.raises(local_llm.ContextOverflow):
        list(stream_text(auto, [{"role": "user", "content": "hi"}]))

    sent["error"] = f"data: {json.dumps({'error': 'Model crashed (exit code 1)'})}\n\n"
    monkeypatch.setattr("controller_inbox.assistant.llm_active", lambda _s: True)
    events = list(answer_stream(loaded, auto, "Who sent this?", email_id="demo-inv-10482"))
    text = "".join(event["text"] for event in events if event["type"] == "delta")
    note = next(event["note"] for event in events if event["type"] == "mode")
    assert "Model crashed (exit code 1)" in note and "is due" not in text and "straight lookup instead" in text


def test_chat_box_says_the_model_failed_not_that_it_is_off(loaded, auto, monkeypatch):
    monkeypatch.setattr("controller_inbox.assistant.llm_active", lambda _s: True)

    def empty(*_a, **_k):
        raise EmptyReply("the model sent only its reasoning, no answer")
        yield  # pragma: no cover

    def thinks_only(*_a, **_k):
        raise EmptyReply("the model sent only its reasoning, no answer")

    monkeypatch.setattr("controller_inbox.assistant.stream_text", empty)
    monkeypatch.setattr("controller_inbox.assistant.chat_with_tools", thinks_only)
    events = list(answer_stream(loaded, auto, "What does this email need from me?", email_id="demo-inv-10482"))
    note = next(event["note"] for event in events if event["type"] == "mode")
    text = "".join(event["text"] for event in events if event["type"] == "delta")
    assert "didn't answer (the model sent only its reasoning" in note
    assert "isn't running" not in text and "straight lookup instead" in text


def test_drafts_from_a_thinking_model(loaded, auto, monkeypatch):
    server = FakeLMStudio()
    _serve(monkeypatch, server)
    email = loaded.get_email("demo-inv-10482")
    email.body_text = "Could you confirm Friday works for the review?"
    result = draft_reply(auto, email)
    assert result["mode"] == "model" and result["text"].startswith("Start with")
    assert server.calls[-1]["reasoning_effort"] == "none"


def test_a_draft_that_comes_back_empty_says_so(loaded, auto, monkeypatch):
    monkeypatch.setattr("controller_inbox.assistant.llm_active", lambda _s: True)
    monkeypatch.setattr(local_llm.httpx, "get", lambda url, **kw: httpx.Response(503, request=httpx.Request("GET", url)))
    monkeypatch.setattr(
        local_llm.httpx,
        "post",
        lambda url, **kw: httpx.Response(200, json={"choices": [{"message": {"content": ""}}]}, request=httpx.Request("POST", url)),
    )
    email = loaded.get_email("demo-inv-10482")
    email.body_text = "Could you confirm Friday works?"
    result = draft_reply(auto, email)
    assert result["mode"] == "template"
    assert "didn't answer (the model sent an empty reply)" in result["note"]


def test_complete_text_retries_a_thinking_model(auto, monkeypatch):
    server = FakeLMStudio(native=False)
    _serve(monkeypatch, server)
    assert complete_text(auto, [{"role": "system", "content": "draft"}], max_tokens=320) == "Start with [1]; it needs you today."
    assert [call["max_tokens"] for call in server.calls] == [320, 2048]


def test_thinking_requests_get_a_longer_timeout(auto, monkeypatch):
    server = FakeLMStudio(native=False)
    seen: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            seen.append(request.extensions["timeout"]["read"])
        return server(request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(local_llm.httpx, "get", lambda url, **kw: client.get(url))
    reader = LocalReader(auto, model="qwen/qwen3.5-4b", client=client)
    reader._structured = False
    assert reader.read({"subject": "one", "body": "x"})
    assert seen == [auto.llm_timeout, local_llm.THINKING_TIMEOUT]


def test_a_query_call_may_think_and_the_rest_stay_quick(auto, monkeypatch):
    server = FakeLMStudio()
    _serve(monkeypatch, server)
    assert complete_text(auto, [{"role": "user", "content": "hi"}], max_tokens=300) == "Start with [1]; it needs you today."
    assert server.calls[-1]["reasoning_effort"] == "none", "an ordinary call turns thinking off"
    complete_text(auto, [{"role": "user", "content": "hi"}], max_tokens=300, think=True, temperature=0.7)
    asked = server.calls[-1]
    assert asked["reasoning_effort"] == "low" and asked["max_tokens"] >= local_llm.THINKING_ROOM and asked["temperature"] == 0.7


def test_thinking_asked_of_a_server_that_reports_no_options_is_left_to_the_model(auto, monkeypatch):
    server = FakeLMStudio(options=())
    _serve(monkeypatch, server)
    assert complete_text(auto, [{"role": "user", "content": "hi"}], max_tokens=300, think=True) == "Start with [1]; it needs you today."
    assert "reasoning_effort" not in server.calls[0], "nothing to ask for"
    # This one thinks anyway and ran out of room: the retry turns thinking down, as for any call.
    assert server.calls[-1]["reasoning_effort"] == "none" and server.calls[-1]["max_tokens"] >= local_llm.THINKING_ROOM


def _reply(status, body):
    url = "http://127.0.0.1:9/v1/chat/completions"
    return httpx.Response(status, json=body, request=httpx.Request("POST", url))


def test_one_badly_written_tool_call_does_not_turn_tools_off(settings, monkeypatch):
    from controller_inbox import agent
    from controller_inbox.local_llm import ToolsUnsupported

    monkeypatch.setattr(local_llm, "check_model", lambda *_a, **_k: local_llm.ModelStatus(mode="auto", reachable=True, model="qwen3-4b"))
    replies = iter([
        # llama.cpp and Ollama fail the request when the model writes a tool call that isn't JSON.
        _reply(500, {"error": {"message": "error parsing tool call: raw='{\"name\":\"read_file\",}'", "type": "api_error"}}),
        _reply(200, {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "read_file", "arguments": "{\"email\": \"1\"}"}}]}}]}),
    ])
    monkeypatch.setattr(local_llm.httpx, "post", lambda *_a, **_k: next(replies))
    messages = [{"role": "user", "content": "hi"}]
    with pytest.raises(ToolsUnsupported):
        local_llm.chat_with_tools(settings, messages, agent.TOOLS)
    assert local_llm.chat_with_tools(settings, messages, agent.TOOLS).calls[0]["name"] == "read_file"


def test_a_busy_server_does_not_stop_thinking_being_turned_off(settings, monkeypatch):
    status = local_llm.ModelStatus(mode="auto", reachable=True, model="qwen3-4b", reasoning=["off", "on"])
    monkeypatch.setattr(local_llm, "check_model", lambda *_a, **_k: status)
    sent = []
    replies = iter([
        _reply(500, {"error": "Model is busy, try again"}),
        _reply(200, {"choices": [{"message": {"content": "ok"}}]}),
        _reply(200, {"choices": [{"message": {"content": "ok"}}]}),
    ])
    monkeypatch.setattr(local_llm.httpx, "post", lambda url, json=None, **_k: sent.append(dict(json)) or next(replies))
    local_llm.complete_text(settings, [{"role": "user", "content": "hi"}])
    local_llm.complete_text(settings, [{"role": "user", "content": "hi"}])
    assert [payload.get("reasoning_effort") for payload in sent] == ["none", None, "none"]
