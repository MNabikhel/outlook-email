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
    assert "like Qwen3, or a smaller one in Settings" in reason and "three messages" not in reason


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
    assert "thinks before every answer and can't be told not to" in note and "like Qwen3" in note, note
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
        "even with 4,096 tokens. Choose one that can, like Qwen3, or a smaller one in Settings."
    )
    assert len(server.calls) == 2, "one email, at 2,048 tokens and then 4,096, before the run stopped"
