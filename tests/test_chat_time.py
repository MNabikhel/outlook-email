"""The chat with a big, slow model: the time it needs, how long it is taking, and what it is thinking.

A 31B model on a laptop ran out of the chat's time and the chat said to set CONTROLLER_INBOX_LLM_TIMEOUT, which an
accountant can't. While it read, the chat said only "Thinking…" for minutes, and a model that reasons first showed
nothing of it. Now a timeout offers more time, with a suggestion, and asks again; the chat says what it is reading
and about how long that takes; and the reasoning shows as it comes, never in the answer.
"""

from __future__ import annotations

import json
import math
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from controller_inbox import local_llm, model_fit, model_roles, web
from controller_inbox.assistant import answer_stream

GB = 1024**3
KEY = "qwen/qwen3-32b"
PAGE = {"X-CloseDesk": "1"}
QUESTION = "What needs a reply from me?"
ANSWER = "Reply to the auditor about the PBC list [1]: they asked for the bank reconciliations and the fixed asset roll-forward by Friday, and nothing has gone back yet."
REASONING = ["The user asks what needs a reply. ", "<b>Look</b> at the auditor's email first. ", "It asks for the PBC list."]


class ChatLMStudio:
    """LM Studio with one 18 GB model loaded. ``timeout``: "start" (nothing comes in time) or "partway" (the answer
    goes quiet after a few words); ``oom``: the answer runs out of memory; ``think``: the model reasons first, in
    ``reasoning_content`` ("field") or between think tags ("tags")."""

    def __init__(self, *, timeout: str = "", oom: bool = False, think: str = "", first_delay: float = 0.0, gap: float = 0.0):
        self.timeout, self.oom, self.think = timeout, oom, think
        self.first_delay, self.gap = first_delay, gap  # seconds before the first piece (reading the prompt), between pieces
        self.waits: list[float] = []
        self.prompts: list[int] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": KEY}]})
        if path == "/api/v1/models":
            instances = [{"id": KEY, "config": {"context_length": 8192}}]
            return httpx.Response(200, json={"models": [
                {"type": "llm", "key": KEY, "loaded_instances": instances, "max_context_length": 131072, "size_bytes": 18 * GB},
            ]})
        if path != "/v1/chat/completions":
            return httpx.Response(404)
        payload = json.loads(request.content)
        self.waits.append(request.extensions["timeout"]["read"])
        self.prompts.append(sum(len(str(m.get("content") or "")) for m in payload["messages"]))
        if self.oom:
            return httpx.Response(500, json={"error": {"code": 500, "message": "Compute error."}})
        if self.timeout == "start":
            raise httpx.ReadTimeout("timed out", request=request)
        if not payload.get("stream"):
            return httpx.Response(200, json={"choices": [{"message": {"content": ANSWER}, "finish_reason": "stop"}]})
        chunks = []
        if self.think == "field":
            chunks += [{"choices": [{"delta": {"reasoning_content": part}}]} for part in REASONING]
        elif self.think == "tags":
            text = "<think>" + "".join(REASONING) + "</think>"
            chunks += [{"choices": [{"delta": {"content": text[i:i + 9]}}]} for i in range(0, len(text), 9)]
        chunks += [{"choices": [{"delta": {"content": ANSWER[i:i + 8]}}]} for i in range(0, len(ANSWER), 8)]
        chunks.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        lines = [f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks]

        def body():
            time.sleep(self.first_delay)
            for index, line in enumerate(lines):
                if index:
                    time.sleep(self.gap)
                if self.timeout == "partway" and index == len(lines) - 3:
                    raise httpx.ReadTimeout("timed out", request=request)
                yield line
            yield b"data: [DONE]\n\n"

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body())


@pytest.fixture
def lm(settings, monkeypatch):
    settings.llm = True

    def start(**options) -> ChatLMStudio:
        server = ChatLMStudio(**options)
        client = httpx.Client(transport=httpx.MockTransport(server))
        monkeypatch.setattr(local_llm.httpx, "get", lambda url, **kw: client.get(url, headers=kw.get("headers"), timeout=kw.get("timeout")))
        monkeypatch.setattr(local_llm.httpx, "post", lambda url, **kw: client.post(url, json=kw.get("json"), headers=kw.get("headers"), timeout=kw.get("timeout")))
        monkeypatch.setattr(local_llm.httpx, "stream", lambda method, url, **kw: client.stream(method, url, json=kw.get("json"), headers=kw.get("headers"), timeout=kw.get("timeout")))
        monkeypatch.setattr(model_roles, "computer_memory", lambda: 32 * GB)
        monkeypatch.setattr(model_roles, "_apple_silicon", lambda: True)
        return server

    return start


def _ask(store, settings, question: str = QUESTION) -> list[dict]:
    local_llm._status_cache.clear()
    return list(answer_stream(store, settings, question))


# The time prompt -------------------------------------------------------------------------------------------------


def test_a_timeout_offers_more_time_under_the_lookup_answer(settings, loaded, lm):
    lm(timeout="start")
    model_fit.record_speed(loaded, KEY, prompt_tokens=2000, read_seconds=200.0)  # 10 tokens a second, measured here
    events = _ask(loaded, settings)
    kinds = [event["type"] for event in events]
    offer = next(event for event in events if event["type"] == "more_time")
    assert kinds.index("more_time") > kinds.index("delta"), "under the lookup answer"
    mode = next(event for event in events if event["type"] == "mode")
    assert mode["mode"] == "lookup" and "CONTROLLER_INBOX" not in json.dumps(events)
    waited = model_fit.chat_wait(loaded, settings)
    assert offer["model"] == KEY and offer["minutes"] == waited.minutes and offer["question"] == QUESTION
    # From the measured speed and this question's prompt: its tokens at 10 a second, with room to spare.
    assert offer["tokens"] > 500
    expected = math.ceil((offer["tokens"] / 10 * model_fit.MARGIN + model_fit.SPARE_SECONDS) / 60)
    assert offer["suggest"] == max(expected, offer["minutes"] + 1)
    assert offer["text"] == (
        f"The local model needed more time than CloseDesk gives it ({offer['minutes']} minutes). qwen3-32b usually needs "
        f"about {offer['suggest']} minutes for a question like this on this computer."
    )


def test_the_suggestion_without_a_measured_speed_comes_from_the_size_then_the_time_taken(settings, store, lm):
    lm()
    status = local_llm.check_model(settings)
    # The size: 18 GB is about 7 minutes; more than the 5 it was given.
    assert model_fit.suggested_minutes(store, settings, status, prompt_tokens=3000, waited=300, elapsed=300) == 8
    # Nothing known of it (no size): a bit over the time it had taken when it was cut off, and more than it had.
    status.chat_sizes = {}
    assert model_fit.suggested_minutes(store, settings, status, prompt_tokens=0, waited=300, elapsed=420) == 11
    assert model_fit.suggested_minutes(store, settings, status, prompt_tokens=0, waited=300, elapsed=10) == 6
    assert model_fit.suggested_minutes(store, settings, status, prompt_tokens=0, waited=7200, elapsed=7200) == 120


def test_a_model_that_went_quiet_partway_is_offered_more_time_too(settings, loaded, lm):
    lm(timeout="partway")
    events = _ask(loaded, settings)
    text = "".join(event["text"] for event in events if event["type"] == "delta")
    assert text.startswith(ANSWER[:8]) and "stopped answering partway" in text
    assert any(event["type"] == "more_time" for event in events)


def test_out_of_memory_keeps_the_memory_advice_and_offers_no_more_time(settings, loaded, lm):
    lm(oom=True)
    events = _ask(loaded, settings)
    assert not any(event["type"] == "more_time" for event in events)
    mode = next(event for event in events if event["type"] == "mode")
    assert local_llm.MEMORY_ADVICE in mode["note"]


def test_saving_the_wait_keeps_it_for_the_model_and_the_next_question_waits_that_long(settings, loaded, lm):
    server = lm(timeout="start")
    client = TestClient(web.create_app(settings, loaded))
    lines = [json.loads(line) for line in client.post("/chat", json={"message": QUESTION}, headers=PAGE).text.splitlines()]
    chat_id = lines[0]["id"]
    offer = next(line for line in lines if line["type"] == "more_time")
    # Saved with the conversation, so it shows again when the conversation is opened.
    turn = loaded.chat_turns(chat_id)[-1]
    assert turn["more_time"]["suggest"] == offer["suggest"]

    for bad, said in (("abc", "Type a number of minutes, from 1 to 120."), (0, "Type a number of minutes from 1 to 120."),
                      (500, "Type a number of minutes from 1 to 120."), (2.5, "Type a whole number of minutes, from 1 to 120.")):
        refused = client.post("/chat/wait", json={"model": KEY, "minutes": bad}, headers=PAGE)
        assert refused.status_code == 400 and refused.json()["detail"] == said
    assert client.post("/chat/wait", json={"minutes": 12}, headers=PAGE).status_code == 400, "no model named"
    saved = client.post("/chat/wait", json={"model": KEY, "minutes": 12}, headers=PAGE)
    assert saved.status_code == 200 and saved.json()["seconds"] == 720
    assert model_fit.chosen_wait(loaded, KEY) == 720

    # Asked again in the same conversation (the page does this after saving): the model is waited for 12 minutes.
    server.timeout = ""
    server.waits.clear()
    lines = [json.loads(line) for line in client.post("/chat", json={"message": QUESTION, "chat_id": chat_id}, headers=PAGE).text.splitlines()]
    assert lines[0]["id"] == chat_id and server.waits and set(server.waits) == {720.0}
    assert not any(line["type"] == "more_time" for line in lines)


def test_the_timeout_notes_no_longer_send_people_to_a_settings_file():
    for exc in (httpx.ReadTimeout("timed out"),):
        assert "CONTROLLER_INBOX" not in local_llm.failure_note(exc)
    started = httpx.ReadTimeout("timed out")
    started.reply_started = "writing"
    assert "CONTROLLER_INBOX" not in local_llm.failure_note(started)
