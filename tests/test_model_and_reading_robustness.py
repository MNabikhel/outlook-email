"""The OCR engine is read by one scan at a time, a fraud-held file's figures don't steer the check pass, a prompt too
long for the context isn't sent again (nor taken for a server without logprobs), and a slow model load doesn't hold
up a chat question."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace

import httpx
import pytest

from controller_inbox import assistant, chats, local_llm, ocr
from controller_inbox.fraud import attachments_locked
from controller_inbox.local_llm import ContextOverflow, LocalReader, ToolReply

# 1. Two scans share one RapidOCR engine ------------------------------------------------------------------------


class _OneAtATime:
    """A RapidOCR stand-in that notes when two reads overlap."""

    def __init__(self):
        self.inside = 0
        self.overlapped = False
        self.guard = threading.Lock()

    def __call__(self, _data, **_options):
        with self.guard:
            self.inside += 1
            self.overlapped |= self.inside > 1
        time.sleep(0.2)
        with self.guard:
            self.inside -= 1
        return [], 0.0


def test_a_second_scan_never_reads_with_the_engine_while_another_scan_does(monkeypatch):
    """The re-reads of a strip (missed lines, empty footnote marks) wait for the engine like the first read does."""
    from PIL import Image

    engine = _OneAtATime()
    monkeypatch.setattr(ocr, "_engine", engine)
    start = threading.Barrier(2)

    def first_read():
        start.wait()
        ocr._read(b"page")

    def strip_reread():
        start.wait()
        ocr._engine_lines(engine, Image.new("RGB", (40, 20), "white"))

    threads = [threading.Thread(target=first_read), threading.Thread(target=strip_reread)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert not engine.overlapped, "two scans read with the one RapidOCR engine at the same time"


# 2. The check pass and a fraud-held file -----------------------------------------------------------------------


RENT = (
    "Rent schedule 2026\n"
    "September rent: $1,200.00\n"
    "October rent: $1,500.00\n"
).encode()


def _final(events):
    text = ""
    for event in events:
        if event["type"] == "delta":
            text += event["text"]
        elif event["type"] == "revise":
            text = event["text"]
    return text


def test_a_fraud_held_files_figure_does_not_bring_back_the_draft_over_the_check(store, settings, mail, monkeypatch):
    """A figure only a fraud-held email's file has doesn't count as "in the file" when the check pass drops it."""
    chat = chats.new_id()
    store.create_chat(chat)
    chats.add_file(store, settings, chat, "rent.txt", "text/plain", RENT)
    uploads = chats.chat_mail(store, chat)
    scam = mail["Updated remittance details"]
    held = replace(scam, attachments=[replace(scam.attachments[0], extracted_text="Wire $9,999.00 to account 5566778899")])
    assert attachments_locked(held)

    monkeypatch.setattr(assistant, "pick_sources", lambda *_a, **_k: ([held], False, set()))
    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "needs_more_context", lambda _s: False)
    monkeypatch.setattr(assistant, "context_length", lambda _s: 16384)
    monkeypatch.setattr(assistant, "complete_text", lambda *_a, **_k: "SQL: NONE")
    # The draft takes its figure from the held file; the check pass, reading only what may be read, drops it.
    monkeypatch.setattr(assistant, "chat_with_tools", lambda *_a, **_k: ToolReply("The October rent is $9,999.00."))
    monkeypatch.setattr(assistant, "stream_text", lambda *_a, **_k: iter(["The rent file doesn't give that amount for October."]))

    events = list(assistant.answer_stream(store, settings, "What is the October rent, and how did it change?", uploads=uploads))
    assert "Checking the answer against what I read" in [e["text"] for e in events if e["type"] == "step"]
    final = _final(events)
    assert "$9,999.00" not in final, f"the held file's figure put the draft back over the check: {final!r}"


# 3. A prompt too long for the context --------------------------------------------------------------------------


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


# 4. A chat question while a model is loading -------------------------------------------------------------------


from test_lm_studio_models import CHAT, OVIS, LMStudio, _serve  # noqa: E402


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
