"""Large models: how they fail, and what the person is told.

A 32B or 70B model on a laptop runs out of memory in more ways than LM Studio's "Compute error" (llama.cpp's
"llama_decode returned -3", Vulkan's "ErrorOutOfDeviceMemory", MLX's buffer limit, a crash with a huge exit code),
and a model that can't stop thinking (DeepSeek-R1 distills, QwQ) needs more room, and more time, than one that can.
"""

from __future__ import annotations

import json

import httpx
import pytest
from test_lm_studio_models import CHAT, LMStudio
from test_lm_studio_models import _serve as serve_lm_studio
from test_reasoning_models import FakeLMStudio
from test_reasoning_models import _serve as serve_reasoning

from controller_inbox import local_llm
from controller_inbox.local_llm import EmptyReply, LocalReader, complete_text, failure_note, stream_text

# What LM Studio, llama.cpp, MLX and Vulkan say when a model is too big for the computer, as people have reported them.
MEMORY_FAILURES = [
    "llama_decode returned -3",
    "Failed to process prompt: llama_decode returned -3",
    "ErrorOutOfDeviceMemory",
    "vk::Device::allocateMemory: ErrorOutOfDeviceMemory",
    "Insufficient Memory (00000008:kIOGPUCommandBufferCallbackErrorOutOfMemory)",
    "[METAL] Command buffer execution failed: Insufficient Memory",
    "ggml_metal_graph_compute: command buffer 0 failed with status 5\nerror: Insufficient Memory "
    "(00000008:kIOGPUCommandBufferCallbackErrorOutOfMemory)",
    "[metal::malloc] Attempting to allocate 21474836480 bytes which is greater than the maximum allowed buffer size "
    "of 17179869184 bytes.",
    "std::bad_alloc",
    "Error loading model.\n\n(Exit code: 18446744072635812000). Unknown error. Try a different model and/or config.",
    "(Exit code: 18446744072635811000). Unknown error. Try a different model and/or config.",
    "The model has crashed without additional information. (Exit code: 6)",
]


@pytest.mark.parametrize("said", MEMORY_FAILURES)
def test_the_ways_a_large_model_runs_out_of_memory(said):
    assert local_llm.out_of_memory(said)


@pytest.mark.parametrize("said", ["Model not found", "Model is reloading", "Exit code: 1", "Unknown error", "llama_decode returned 1"])
def test_other_failures_are_still_not_taken_for_memory(said):
    assert not local_llm.out_of_memory(said)


@pytest.mark.parametrize("said", MEMORY_FAILURES)
@pytest.mark.parametrize("where", ["status", "stream"])
def test_a_large_models_memory_failure_gets_the_advice_however_it_comes(settings, monkeypatch, said, where):
    settings.llm = None
    server = LMStudio(loaded=[CHAT])
    server.answer_error = where
    server.error_body = {"error": {"message": said}}
    serve_lm_studio(monkeypatch, server)
    with pytest.raises(httpx.HTTPError) as caught:
        list(stream_text(settings, [{"role": "user", "content": "How much is due?"}]))
    note = failure_note(caught.value)
    assert "ran out of memory" in note and "3B to 8B" in note


@pytest.mark.parametrize("where", ["status", "stream"])
def test_memory_words_past_the_first_200_characters_are_still_seen(settings, monkeypatch, where):
    settings.llm = None
    server = LMStudio(loaded=[CHAT])
    server.answer_error = where
    server.error_body = {"error": {"message": "ggml_backend_metal_graph_compute: " + "x " * 150 + "kIOGPUCommandBufferCallbackErrorOutOfMemory"}}
    serve_lm_studio(monkeypatch, server)
    with pytest.raises(httpx.HTTPError) as caught:
        list(stream_text(settings, [{"role": "user", "content": "How much is due?"}]))
    assert "ran out of memory" in failure_note(caught.value)


def _status_error(status: int, **body) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://127.0.0.1:1234/v1/chat/completions")
    response = httpx.Response(status, request=request, **body)
    return httpx.HTTPStatusError(f"Server error '{status}' for url '{request.url}'", request=request, response=response)


@pytest.mark.parametrize("body", [
    {"message": "llama_decode returned -3"},  # llama.cpp's server
    {"detail": "llama_decode returned -3"},  # servers built on FastAPI
    {"error": {"message": "llama_decode returned -3"}},
    {"error": "llama_decode returned -3"},
])
def test_lm_studios_words_are_read_from_each_shape_of_error(body):
    exc = _status_error(500, json=body)
    assert local_llm._lm_studio_said(exc) == "llama_decode returned -3"
    assert local_llm.ran_out_of_memory(exc)


def test_a_failure_not_recognised_shows_the_servers_own_words(settings, monkeypatch):
    settings.llm = None
    server = LMStudio(loaded=[CHAT])
    server.answer_error = "status"
    server.error_body = {"error": {"message": "Model is reloading,\n  try again shortly"}}
    serve_lm_studio(monkeypatch, server)
    with pytest.raises(httpx.HTTPError) as caught:
        complete_text(settings, [{"role": "user", "content": "How much is due?"}])
    note = failure_note(caught.value)
    assert note == "The local model didn't answer (Model is reloading, try again shortly)."
    # A reply that isn't JSON: its text, plainly, not httpx's "Server error '500 Internal Server Error' for url ...".
    assert failure_note(_status_error(500, text="Model unloaded.")) == "The local model didn't answer (Model unloaded.)."


@pytest.mark.parametrize("call", ["complete", "stream"])
def test_a_model_out_of_memory_isnt_asked_again_with_thinking_on(settings, monkeypatch, call):
    """A 500 was asked again without ``reasoning_effort``: for a model out of memory, that turned thinking back on
    with the same budget, and the person waited for a second failure."""
    settings.llm = None
    local_llm._reasoning_seen.add(CHAT)  # a thinker: thinking is turned off with reasoning_effort "none"
    server = LMStudio(loaded=[CHAT])
    server.answer_error = "status"
    server.error_body = {"error": {"message": "llama_decode returned -3"}}
    sent: list[dict] = []
    real = server.__call__

    def recording(request):
        if request.url.path.endswith("/chat/completions"):
            sent.append(json.loads(request.content))
        return real(request)

    serve_lm_studio(monkeypatch, recording)
    with pytest.raises(httpx.HTTPError) as caught:
        if call == "complete":
            complete_text(settings, [{"role": "user", "content": "How much is due?"}])
        else:
            list(stream_text(settings, [{"role": "user", "content": "How much is due?"}]))
    assert [payload.get("reasoning_effort") for payload in sent] == ["none"], "asked once, with thinking off"
    assert "ran out of memory" in failure_note(caught.value)


def test_a_busy_stream_is_asked_again_with_thinking_still_off(settings, monkeypatch):
    status = local_llm.ModelStatus(mode="auto", reachable=True, model="qwen3-4b", reasoning=["off", "on"])
    monkeypatch.setattr(local_llm, "check_model", lambda *_a, **_k: status)
    sent: list = []

    def server(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content).get("reasoning_effort"))
        if len(sent) == 1:
            return httpx.Response(500, json={"error": "Model is busy, try again"})
        body = 'data: {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    client = httpx.Client(transport=httpx.MockTransport(server))
    monkeypatch.setattr(local_llm.httpx, "stream", lambda method, url, **kw: client.stream(method, url, json=kw.get("json")))
    assert "".join(stream_text(settings, [{"role": "user", "content": "hi"}])) == "ok"
    assert sent == ["none", "none"]


# A model that can't stop thinking (DeepSeek-R1 distills, QwQ) -----------------------------------------------------
R1 = dict(options=["on"])  # LM Studio lists only "on": reasoning_effort can't turn its thinking off


def test_waiting_grows_with_the_room_to_think(settings):
    assert local_llm._timeout(settings, local_llm.THINKING_ROOM) == local_llm.THINKING_TIMEOUT
    assert local_llm._timeout(settings, 2 * local_llm.THINKING_ROOM) >= 60 + 2 * local_llm.THINKING_ROOM / 8


@pytest.mark.parametrize("call", ["complete", "tools"])
def test_a_thinker_given_twice_the_room_is_given_the_time_for_it(settings, monkeypatch, call):
    """complete_text and chat_with_tools waited 300 s whatever the budget: a slow laptop thinking 4,096 tokens took
    longer, and the failure was blamed on LM Studio loading the model."""
    settings.llm = None
    server = FakeLMStudio(think_tokens=3000, **R1)
    serve_reasoning(monkeypatch, server)
    messages = [{"role": "system", "content": "chat"}, {"role": "user", "content": "hi"}]
    if call == "complete":
        assert complete_text(settings, messages, max_tokens=320)
    else:
        assert local_llm.chat_with_tools(settings, messages, [{"type": "function", "function": {"name": "f", "parameters": {}}}]).content
    assert [c["max_tokens"] for c in server.calls] == [2048, 4096]
    assert server.timeouts[1] >= 60 + 4096 / 8 > server.timeouts[0]


def test_a_reader_with_a_model_that_cant_stop_thinking_gets_twice_the_room_once(settings, monkeypatch):
    settings.llm = None
    server = FakeLMStudio(think_tokens=3000, schema_thinks_too=True, **R1)
    reader = LocalReader(settings, client=serve_reasoning(monkeypatch, server))
    for subject in ("one", "two", "three"):
        assert reader.read({"subject": subject, "body": "Northwind, $4,200 due Friday."}), reader.stats.last_error
    assert [c["max_tokens"] for c in server.calls] == [2048, 4096, 4096, 4096], "doubled once, then kept"
    assert server.timeouts[-1] >= 60 + 4096 / 8
    assert reader.stats.read == 3 and not reader.stopped


def test_a_reader_whose_model_thinks_past_twice_the_room_says_so_plainly(settings, monkeypatch):
    settings.llm = None
    server = FakeLMStudio(think_tokens=6000, schema_thinks_too=True, **R1)
    reader = LocalReader(settings, client=serve_reasoning(monkeypatch, server))
    for subject in ("one", "two", "three"):
        reader.read({"subject": subject, "body": "Northwind, $4,200 due Friday."})
    assert [c["max_tokens"] for c in server.calls] == [2048, 4096], "4,096 once, then the run stops"
    reason = reader.stats.stopped_reason
    assert reason.startswith(f"{reader.model} thinks before every answer and can't be told not to"), reason
    assert "like Gemma 3, or a smaller one in Settings" in reason and "three messages" not in reason


@pytest.mark.parametrize("call", ["complete", "stream"])
def test_a_chat_whose_model_thinks_past_twice_the_room_says_so_plainly(settings, monkeypatch, call):
    settings.llm = None
    server = FakeLMStudio(think_tokens=6000, **R1)
    serve_reasoning(monkeypatch, server)
    messages = [{"role": "system", "content": "chat"}, {"role": "user", "content": "hi"}]
    with pytest.raises(EmptyReply) as caught:
        if call == "complete":
            complete_text(settings, messages, max_tokens=320)
        else:
            list(stream_text(settings, messages, max_tokens=500))
    note = failure_note(caught.value)
    assert "thinks before every answer and can't be told not to" in note and "like Gemma 3" in note, note
    assert note.endswith("in Settings.")


def test_a_model_that_can_turn_thinking_off_isnt_told_it_cant(settings, monkeypatch):
    settings.llm = None
    server = FakeLMStudio(think_tokens=6000, native=False, options=())  # no options known: nothing is said about it
    serve_reasoning(monkeypatch, server)
    with pytest.raises(EmptyReply, match="whole reply budget thinking"):
        complete_text(settings, [{"role": "user", "content": "hi"}], max_tokens=320)


def test_the_overnight_reading_says_once_why_it_stopped(loaded, settings, monkeypatch):
    from controller_inbox.overnight import read_queue

    settings.llm = None
    server = FakeLMStudio(think_tokens=6000, schema_thinks_too=True, **R1)
    reader = LocalReader(settings, client=serve_reasoning(monkeypatch, server))
    result = read_queue(loaded, settings, limit=6, reader=reader)
    assert result["note"] == (
        f"Stopped reading: {reader.model} thinks before every answer and can't be told not to, and it ran out of room "
        "even with 4,096 tokens. Choose one that doesn't have to think first, like Gemma 3, or a smaller one in Settings."
    )
    assert len(server.calls) == 2, "one email, at 2,048 tokens and then 4,096, before the run stopped"


def test_a_reading_that_runs_out_of_memory_says_so_with_the_advice(loaded, settings, monkeypatch):
    """A 31B on a 32 GB Mac: LM Studio's "Compute error." on every email. The note on the page and in the overnight
    file read "Stopped reading: three messages in a row failed (Server error '500 Internal Server Error' for url
    'http://127.0.0.1:1234/v1/chat/completions')": no word that the memory ran out, or what to do."""
    from controller_inbox.overnight import read_queue

    settings.llm = None
    server = LMStudio(loaded=[CHAT])
    server.answer_error = "status"  # {"error": {"message": "Compute error."}}
    serve_lm_studio(monkeypatch, server)
    reader = LocalReader(settings, client=httpx.Client(transport=httpx.MockTransport(server)))
    result = read_queue(loaded, settings, limit=6, reader=reader)
    note = result["note"]
    assert note.startswith("Stopped reading: the local model ran out of memory on three messages in a row. "), note
    assert "3B to 8B" in note and "Server error" not in note and "http" not in note and ".." not in note, note
    assert reader.stats.failed == 3, "three emails, then the run stopped"


@pytest.mark.parametrize(
    ("given", "said"),
    [
        (2, "The local model couldn't read either of the 2 emails waiting for it: it ran out of memory on each. "),
        (1, "The local model couldn't read the one email waiting for it: it ran out of memory. "),
    ],
)
def test_a_reading_too_short_to_stop_that_read_none_still_says_why(loaded, settings, monkeypatch, given, said):
    """Two new messages (or one) and a model that runs out of memory on each: fewer than the three in a row that stop
    a run, so the run said nothing of it, and the job box was the green of all done: "Processed. The local model read
    0." with no reason. Said in one sentence with no label of its own (the job box puts "Needs a look:" before it), and
    naming the mail it means: a run with no new files still gives the model the emails earlier runs left waiting."""
    from controller_inbox.overnight import read_queue

    settings.llm = None
    server = LMStudio(loaded=[CHAT])
    server.answer_error = "status"  # {"error": {"message": "Compute error."}}
    serve_lm_studio(monkeypatch, server)
    reader = LocalReader(settings, client=httpx.Client(transport=httpx.MockTransport(server)))
    result = read_queue(loaded, settings, limit=given, reader=reader)
    assert reader.stats.failed == given and not reader.stats.read and not reader.stopped
    note = result["note"]
    assert note.startswith(said), note
    assert "3B to 8B" in note and "Server error" not in note and "http" not in note and ".." not in note, note
    assert "Read none" not in note and "message" not in note and result["read_none"], note


def test_a_reading_that_read_none_for_another_reason_says_it(loaded, settings, monkeypatch, caplog):
    """A model whose replies CloseDesk can't use: the note said "Read none: both messages it was given failed (reply
    was not the JSON shape: “I think this is an invoice.”)", the model's own words in front of an accountant. They go
    to the log; the page says what happens to the mail."""
    from controller_inbox.overnight import read_queue

    settings.llm = None
    server = LMStudio(loaded=[CHAT])
    serve_lm_studio(monkeypatch, server)

    def unusable(_request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "I think this is an invoice."}, "finish_reason": "stop"}]})

    reader = LocalReader(settings, client=httpx.Client(transport=httpx.MockTransport(unusable)))
    with caplog.at_level("WARNING", logger="controller_inbox.overnight"):
        result = read_queue(loaded, settings, limit=2, reader=reader)
    assert reader.stats.failed == 2 and not reader.stats.read and not reader.stopped
    assert result["note"] == (
        "The local model couldn't read either of the 2 emails waiting for it. They are tried again on the next run."
    ), result["note"]
    assert "JSON" in caplog.text and "I think this is an invoice." in caplog.text, caplog.text
    # One read of the two: the run went on, and the note is for a model that read none.
    calls = []

    def first_fails(request):
        calls.append(request)
        if len(calls) == 1:
            return unusable(request)
        reading = {"category": "ap_invoice", "folder": "important", "importance": "high", "summary": "Invoice to enter.", "actions": [], "why": "Vendor bill."}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(reading)}, "finish_reason": "stop"}]})

    reader = LocalReader(settings, client=httpx.Client(transport=httpx.MockTransport(first_fails)))
    result = read_queue(loaded, settings, limit=2, reader=reader)
    assert reader.stats.read >= 1 and result["note"] == "" and not result.get("read_none"), result["note"]


@pytest.mark.parametrize(("failed", "memory", "said"), [
    (1, 0, "The local model couldn't read the one email waiting for it. It is tried again on the next run."),
    (3, 0, "The local model couldn't read any of the 3 emails waiting for it. They are tried again on the next run."),
    (2, 1, "The local model couldn't read either of the 2 emails waiting for it. They are tried again on the next run."),
])
def test_the_note_for_a_reading_that_read_none_fits_how_many(settings, failed, memory, said):
    stats = local_llm.ReaderStats(failed=failed, memory_failed=memory, last_error="reply was not the JSON shape")
    assert stats.none_read(settings) == said
    assert local_llm.ReaderStats(failed=2, read=1).none_read(settings) == ""
    assert local_llm.ReaderStats(failed=2, stopped_reason="the server went away").none_read(settings) == ""


def test_a_reading_that_runs_out_of_memory_with_a_context_closedesk_gave_stops_and_says_where_to_reload_it(loaded, settings, monkeypatch):
    """With a context CloseDesk gave the model, the reading loaded it back smaller by itself. It no longer reloads
    without the user's yes: the run stops using the model (the mail is filed without it) and says that Settings can
    reload it smaller."""
    from controller_inbox.overnight import read_queue

    settings.llm = None
    server = LMStudio(loaded=[CHAT])
    server.contexts[CHAT] = 16384
    local_llm._context_given[CHAT] = 16384  # CloseDesk loaded it with 16,384 tokens
    server.answer_error = "status"
    serve_lm_studio(monkeypatch, server)
    reader = LocalReader(settings, client=httpx.Client(transport=httpx.MockTransport(server)))
    result = read_queue(loaded, settings, limit=3, reader=reader)
    assert not [call for call in server.calls if call[0] in ("load", "unload")], server.calls
    assert reader.stats.failed == 1, "the run stopped using the model after the first email"
    assert result["note"] == (
        "Stopped reading: the local model ran out of memory with a 16,384-token context. Settings → Local model can "
        f"reload {CHAT} with 8,192."
    ), result["note"]


def test_a_chat_model_that_thought_past_twice_the_room_is_asked_once_next_time(settings, monkeypatch):
    """A model that can't stop thinking was asked with 2,048 tokens and then 4,096 on every question, minutes each on
    a laptop, before the lookup answer; the second try also sent reasoning_effort "none", which it doesn't allow."""
    settings.llm = None
    server = FakeLMStudio(think_tokens=6000, **R1)
    serve_reasoning(monkeypatch, server)
    messages = [{"role": "system", "content": "chat"}, {"role": "user", "content": "hi"}]
    for _question in range(2):
        with pytest.raises(local_llm.ThinksTooLong):
            list(stream_text(settings, messages, max_tokens=500))
    assert [call["max_tokens"] for call in server.calls] == [2048, 4096, 4096]
    assert not [call for call in server.calls if call.get("reasoning_effort") == "none"], "it allows only on"


@pytest.mark.parametrize("model", ["qwen/qwen3-30b-a3b-thinking-2507", "deepseek/deepseek-r1-0528-qwen3-8b", "deepseek-r1-distill-llama-8b"])
def test_the_model_suggested_in_its_place_isnt_its_own_family(settings, monkeypatch, model):
    """A Qwen3 Thinking build that thought too long was told to choose "one that can, like Qwen3"."""
    status = local_llm.ModelStatus(mode="auto", reachable=True, model=model, reasoning=["on"])
    monkeypatch.setattr(local_llm, "check_model", lambda *_a, **_k: status)
    reply = local_llm.Reply(content="", reasoning="Let me think.", finish="length")
    note = failure_note(local_llm._empty_reply(settings, model, 2 * local_llm.THINKING_ROOM, reply))
    assert "doesn't have to think first, like" in note, note
    assert ("like Qwen3" in note) == ("qwen" not in model.lower()), note
