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
        self.loaded = True  # False: LM Studio unloaded it while idle, and the question has it load the model again
        self.usage: dict = {}  # the token counts sent with the last piece, if any
        self.context = 8192  # the context it is loaded with
        self.loads: list[int | None] = []  # the context each load CloseDesk asked for gave, in order

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": KEY}]})
        if path == "/api/v1/models":
            instances = [{"id": KEY, "config": {"context_length": self.context}}] if self.loaded else []
            return httpx.Response(200, json={"models": [
                {"type": "llm", "key": KEY, "loaded_instances": instances, "max_context_length": 131072, "size_bytes": 18 * GB},
            ]})
        if path == "/api/v1/models/load":
            self.loads.append(json.loads(request.content).get("context_length"))
            self.loaded, self.context = True, self.loads[-1] or 4096
            return httpx.Response(200, json={"status": "loaded"})
        if path == "/api/v1/models/unload":
            self.loaded = False
            return httpx.Response(200, json={"status": "unloaded"})
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
        chunks.append({"choices": [{"delta": {}, "finish_reason": "stop"}], **({"usage": self.usage} if self.usage else {})})
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


def test_a_timeout_while_lm_studio_loads_the_model_says_so_and_offers_no_longer_wait(settings, loaded, lm):
    """LM Studio unloaded the model while idle and loading it took longer than the chat waits. The chat said the
    model needed more time for a question like this and offered to keep a longer wait for every question after,
    though the model was loaded by then and asking again a minute later answers at the usual speed."""
    server = lm(timeout="start")
    server.loaded = False
    model_fit.record_speed(loaded, KEY, prompt_tokens=2000, read_seconds=20.0)  # 100 tokens a second, measured here
    events = _ask(loaded, settings)
    assert not [event for event in events if event["type"] == "more_time"]
    mode = next(event for event in events if event["type"] == "mode")
    assert mode["mode"] == "lookup" and "may still be loading it" in mode["note"]
    assert "needed more time" not in mode["note"]


def test_the_offer_keeps_the_email_that_was_open_when_the_conversation_has_files(settings, loaded, lm):
    """With files added to the conversation, a question not about the email on screen is answered from the files
    first, under their stand-in id. The offer kept that id, so asked again, the email that was open wasn't read."""
    from controller_inbox import chats
    from controller_inbox.fraud import attachments_locked

    lm(timeout="start")
    chat = chats.new_id()
    loaded.create_chat(chat)
    chats.add_file(loaded, settings, chat, "notes.txt", "text/plain", b"Budget notes for the quarter.")
    uploads = chats.chat_mail(loaded, chat)
    email_id = next(email.id for email in loaded.list_emails(limit=50) if not attachments_locked(email))
    local_llm._status_cache.clear()
    events = list(answer_stream(loaded, settings, QUESTION, email_id=email_id, uploads=uploads))
    offer = next(event for event in events if event["type"] == "more_time")
    assert offer["email_id"] == email_id


def test_with_the_longest_wait_already_no_more_time_is_offered(settings, loaded, lm):
    """At 120 minutes, the most the chat waits, the offer said "needed more time than CloseDesk gives it (120
    minutes) ... usually needs about 120 minutes" and asked again with the same wait, to run out the same way."""
    lm(timeout="start")
    model_fit.set_wait(loaded, KEY, model_fit.MAX_MINUTES)
    events = _ask(loaded, settings)
    assert not [event for event in events if event["type"] == "more_time"]
    mode = next(event for event in events if event["type"] == "mode")
    assert mode["mode"] == "lookup" and "didn't answer within 120 minutes, the longest CloseDesk waits" in mode["note"]
    assert "smaller model" in mode["note"]


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
    assert "ran out of memory" in mode["note"] and "3B to 8B model is plenty" in mode["note"]


def test_saving_the_wait_keeps_it_for_the_model_and_the_next_question_waits_that_long(settings, loaded, lm):
    server = lm(timeout="start")
    client = TestClient(web.create_app(settings, loaded))
    lines = [json.loads(line) for line in client.post("/chat", json={"message": QUESTION}, headers=PAGE).text.splitlines()]
    chat_id = lines[0]["id"]
    offer = next(line for line in lines if line["type"] == "more_time")
    # Saved with the conversation, so it shows again when the conversation is opened.
    turn = loaded.chat_turns(chat_id)[-1]
    assert turn["more_time"]["suggest"] == offer["suggest"]

    for bad, said in (("abc", "Type a number of minutes, from 2 to 120."), (0, "Type a number of minutes from 2 to 120."),
                      (500, "Type a number of minutes from 2 to 120."), (2.5, "Type a whole number of minutes, from 2 to 120.")):
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


class ThinkingLMStudio(ChatLMStudio):
    """A model that can't turn thinking off (LM Studio lists only "on"): a call that isn't streamed uses up its 2,048
    tokens thinking, and the one with twice the room runs out of time."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/models":
            return httpx.Response(200, json={"models": [
                {"type": "llm", "key": KEY, "loaded_instances": [{"id": KEY, "config": {"context_length": 8192}}],
                 "max_context_length": 131072, "size_bytes": 18 * GB,
                 "capabilities": {"reasoning": {"allowed_options": ["on"], "default": "on"}}},
            ]})
        if path != "/v1/chat/completions":
            return super().__call__(request)
        payload = json.loads(request.content)
        self.waits.append(request.extensions["timeout"]["read"])
        if payload.get("stream") or payload["max_tokens"] > local_llm.THINKING_ROOM:
            raise httpx.ReadTimeout("timed out", request=request)
        message = {"content": "", "reasoning_content": "Let me think about the auditor's email. " * 40}
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "length"}]})


def test_the_offer_says_the_wait_that_ran_out_and_suggests_one_that_changes_it(settings, loaded, monkeypatch):
    """A thinking model's second try, with twice the room, waits 10 minutes, more than the wait worked out for it
    (about 7 here). The offer said it was given those 7 minutes and suggested 8, from reading the prompt alone:
    saved, the second try still waited 10 minutes and ran out the same way. Now it says the 10 minutes and suggests
    enough for reading the prompt and writing all the call may write, and more than the 10."""
    from controller_inbox import assistant

    server = ThinkingLMStudio()
    client = httpx.Client(transport=httpx.MockTransport(server))
    monkeypatch.setattr(local_llm.httpx, "get", lambda url, **kw: client.get(url, headers=kw.get("headers"), timeout=kw.get("timeout")))
    monkeypatch.setattr(local_llm.httpx, "post", lambda url, **kw: client.post(url, json=kw.get("json"), headers=kw.get("headers"), timeout=kw.get("timeout")))
    settings.llm = True
    model_fit.record_speed(loaded, KEY, prompt_tokens=6000, read_seconds=40.0, written=700, write_seconds=100.0)
    wait = model_fit.chat_wait(loaded, settings)
    assert 300 < wait.seconds < 600
    messages = [{"role": "user", "content": QUESTION}]
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object", "properties": {}}}}]
    calls = (
        lambda: local_llm.chat_with_tools(settings, messages, tools, limit=wait.seconds),
        lambda: local_llm.complete_text(settings, messages, limit=wait.seconds),
    )
    for call in calls:
        server.waits.clear()
        with pytest.raises(httpx.TimeoutException) as ran_out:
            call()
        assert server.waits == [wait.seconds, 600]
        assert ran_out.value.waited == 600 and ran_out.value.written == 2 * local_llm.THINKING_ROOM

        state = {"wait": wait, "store": loaded, "call": {"tokens": 8000, "started": time.monotonic() - 600}}
        offer = assistant._more_time(state, settings, ran_out.value, QUESTION, None)
        assert offer["minutes"] == 10 and "(10 minutes)" in offer["text"]
        assert offer["suggest"] * 60 > 600
        # Reading the prompt at 150 tokens a second, and writing the 4,096 tokens it may write at 7 a second.
        assert offer["suggest"] * 60 >= 8000 / 150 * model_fit.MARGIN + 4096 / 7


def test_a_table_query_that_runs_out_of_time_is_the_wait_the_offer_speaks_of(settings, store, mail, lm):
    """A question about a workbook has the model write a query over its tables first. That call ran out of the whole
    wait and was let go without a word, then the answer's own call waited the whole wait again: the user waited twice
    what the offer of more time said CloseDesk gave the model."""
    server = lm(timeout="start")
    local_llm._status_cache.clear()
    events = list(answer_stream(store, settings, "What is the total in this budget?", email_id=mail["Q4 budget draft"].id))
    steps = [event["text"] for event in events if event["type"] == "step"]
    assert "Writing a query over the tables for the question" in steps
    assert len(server.waits) == 1, "the model was asked again after the table query ran out of time"
    offer = next(event for event in events if event["type"] == "more_time")
    assert offer["minutes"] == model_fit.chat_wait(store, settings).minutes


def test_a_model_slower_than_its_measured_speed_is_offered_much_more_time(settings, store, lm):
    """A model measured reading 533 tokens a second slowed to 10 (more of it on the processor, a laptop on battery).
    The offer went on from the old speed: "usually needs about 2 minutes" after it had sent nothing for a minute, then
    3, 4, 5 minutes, one more each time, so a question needing 20 minutes ran out about 18 times."""
    lm()
    status = local_llm.check_model(settings)
    model_fit.record_speed(store, KEY, prompt_tokens=12000, read_seconds=12000 / 533)
    waited, tries = 60.0, 0
    while waited < 1200:
        suggest = model_fit.suggested_minutes(store, settings, status, prompt_tokens=12000, waited=waited, elapsed=waited)
        assert suggest * 60 >= 2 * waited, "it took longer than its speed allows: twice as long at least"
        assert model_fit.slower_than_measured(store, status, prompt_tokens=12000, elapsed=waited)
        waited, tries = suggest * 60.0, tries + 1
    assert tries <= 3
    text = model_fit.more_time_text(KEY, 60, 3, slower=True)
    assert "usually needs" not in text and "slower than it was earlier on this computer" in text
    # Cut off well within its measured time, the speed still stands.
    assert not model_fit.slower_than_measured(store, status, prompt_tokens=12000, elapsed=20)


def test_asked_again_after_its_wait_ran_out_the_reading_estimate_is_no_shorter_than_that_wait(settings, loaded, lm):
    """The model was slower than its measured speed and ran out of its wait. Asked again with more time, the chat
    still said it reads in about 20 seconds from the old speed, then "Taking longer than usual…" for the whole wait."""
    server = lm(timeout="start")
    model_fit.record_speed(loaded, KEY, prompt_tokens=2000, read_seconds=20.0)  # 100 tokens a second, measured earlier
    client = TestClient(web.create_app(settings, loaded))
    lines = [json.loads(line) for line in client.post("/chat", json={"message": QUESTION}, headers=PAGE).text.splitlines()]
    offer = next(line for line in lines if line["type"] == "more_time")
    ran_out = offer["minutes"] * 60
    client.post("/chat/wait", json={"model": KEY, "minutes": offer["suggest"]}, headers=PAGE)
    server.timeout = ""
    again = [json.loads(line) for line in client.post("/chat", json={"message": QUESTION, "chat_id": lines[0]["id"]}, headers=PAGE).text.splitlines()]
    reading = [line for line in again if line["type"] == "progress" and line["state"] == "reading"]
    assert reading and all(line["seconds"] is None or line["seconds"] >= ran_out * 0.9 for line in reading), reading
    # Once it answered, its speed is what it was measured at again: the next question's estimate comes from it.
    later = [json.loads(line) for line in client.post("/chat", json={"message": QUESTION}, headers=PAGE).text.splitlines()]
    estimate = next(line for line in later if line["type"] == "progress" and line["state"] == "reading")["seconds"]
    assert estimate is not None and estimate < ran_out / 2
