"""A big model reading mail on a laptop: slow, but working.

The reading asked for the whole reply in one piece and waited 90 s for it, so a big model writing at 5 words a
second timed out on a long reply, and two timeouts in a row stopped the whole night's reading. A model LM Studio had
unloaded (idle auto-unload, LM Studio restarted) was loaded by the first email's own request, inside those 90 s, so
that email timed out, and a load over about three minutes stopped the run, blamed on the model's speed.

A fake LM Studio on a real socket here, so httpx's real time limits apply. Times are scaled down: CloseDesk's
90 s is half a second.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from controller_inbox import local_llm, model_fit, model_roles
from controller_inbox.local_llm import LocalReader, failure_note, remember_model, stream_text
from controller_inbox.overnight import read_queue, run_overnight

BIG, SMALL = "google/gemma-3-27b", "qwen/qwen3-4b"
GB = 1024**3
READING = {"category": "ap_invoice", "folder": "important", "importance": "medium", "summary": "Invoice to enter.",
           "actions": [], "why": ""}
TIMEOUT = 0.5  # CloseDesk's 90 s, scaled
LONGER = 2.0  # its five minutes (CHAT_TIMEOUT), scaled
PACKET = {"subject": "Invoice 4410", "sender_email": "ap@vendor.example", "body": "Please see the attached invoice."}


class SlowLMStudio:
    """LM Studio 0.4 with just-in-time loading on, serving one big model.

    ``token_gap``: seconds between the words it writes; ``tokens``: how many it writes; ``first_token``: seconds it
    takes to read the prompt before the first word; ``load_seconds``: how long a load takes, whether asked for
    (/api/v1/models/load) or done just in time for a chat request that names the model (with LM Studio's default
    context then). A load goes on when the request that started it hangs up, as LM Studio's does.
    ``unload_after``: LM Studio unloads the model (idle auto-unload) after this many answers.
    ``thinks``: the words come as reasoning, not as the answer."""

    def __init__(self, *, loaded=True, token_gap=0.0, tokens=60, first_token=0.0, load_seconds=0.0, unload_after=0,
                 stall_after=0, thinks=False):
        self.loaded = loaded
        self.context = 16384 if loaded else 0
        self.token_gap, self.tokens, self.first_token = token_gap, tokens, first_token
        self.load_seconds, self.unload_after, self.stall_after, self.thinks = load_seconds, unload_after, stall_after, thinks
        self.log: list[tuple] = []
        self.answered = 0
        self.lock = threading.Lock()
        self.loading: threading.Event | None = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}/v1"

    def stop(self) -> None:
        """LM Studio quits: connections are refused from now on."""
        self.server.shutdown()
        self.server.server_close()

    def ensure_loaded(self, context: int) -> None:
        with self.lock:
            if self.loaded:
                return
            if self.loading is None:
                done = self.loading = threading.Event()

                def load() -> None:
                    time.sleep(self.load_seconds)
                    with self.lock:
                        self.loaded, self.context, self.loading = True, context, None
                    done.set()

                threading.Thread(target=load, daemon=True).start()
            event = self.loading
        event.wait()

    def reply_text(self) -> str:
        reading = dict(READING, why="w" * max(0, self.tokens * 4 - len(json.dumps(READING))))
        return json.dumps(reading)

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_args) -> None:
                pass

            def send(self, data, code=200) -> None:
                body = json.dumps(data).encode()
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

            def do_GET(self) -> None:
                if self.path == "/v1/models":
                    return self.send({"data": [{"id": BIG}, {"id": SMALL}]})
                if self.path == "/api/v1/models":
                    instances = [{"id": BIG, "config": {"context_length": fake.context}}] if fake.loaded else []
                    return self.send({"models": [
                        {"type": "llm", "key": BIG, "loaded_instances": instances, "max_context_length": 131072,
                         "size_bytes": 17 * GB},
                        {"type": "llm", "key": SMALL, "loaded_instances": [], "max_context_length": 32768,
                         "size_bytes": 3 * GB},
                    ]})
                return self.send({"error": "not found"}, 404)

            def do_POST(self) -> None:
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/api/v1/models/load":
                    fake.log.append(("load", payload.get("model"), payload.get("context_length")))
                    fake.ensure_loaded(payload.get("context_length") or 4096)
                    return self.send({"status": "loaded"})
                if self.path != "/v1/chat/completions":
                    return self.send({"error": "not found"}, 404)
                stream = bool(payload.get("stream"))
                fake.log.append(("chat", payload.get("model"), stream, "response_format" in payload))
                fake.ensure_loaded(4096)  # just in time, with LM Studio's own default context
                time.sleep(fake.first_token)
                text = fake.reply_text()
                pieces = [text[i:i + 4] for i in range(0, len(text), 4)]
                field = "reasoning_content" if fake.thinks else "content"
                try:
                    if not stream:  # nothing is sent until the whole reply is written
                        time.sleep(fake.token_gap * len(pieces))
                        message = {"role": "assistant", field: text, **({"content": ""} if fake.thinks else {})}
                        self.send({"choices": [{"message": message, "finish_reason": "stop"}]})
                    else:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.end_headers()
                        for index, piece in enumerate(pieces, start=1):
                            time.sleep(fake.token_gap)
                            self.wfile.write(f"data: {json.dumps({'choices': [{'delta': {field: piece}}]})}\n\n".encode())
                            if fake.stall_after and index == fake.stall_after:
                                time.sleep(LONGER * 3)  # went quiet partway
                                return
                        end = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
                        self.wfile.write(f"data: {json.dumps(end)}\n\ndata: [DONE]\n\n".encode())
                except OSError:  # CloseDesk stopped waiting and hung up
                    return
                fake.answered += 1
                if fake.unload_after and fake.answered == fake.unload_after:
                    fake.loaded = False  # LM Studio unloaded the idle model

        return Handler


@pytest.fixture
def lm_studio(settings, monkeypatch, request):
    """CloseDesk's waits, scaled down, against a ``SlowLMStudio`` built with the test's own settings."""
    settings.llm = None
    settings.llm_timeout = TIMEOUT
    settings.overnight_file_summaries = 0
    settings.overnight_ai_links = 0
    settings.ensure_data_dir()
    monkeypatch.setattr(local_llm, "CHAT_TIMEOUT", LONGER)
    monkeypatch.setattr(local_llm, "THINKING_TIMEOUT", LONGER)
    monkeypatch.setattr(local_llm, "LOAD_WAIT_SECONDS", 5.0)
    monkeypatch.setattr(local_llm, "LOAD_POLL_SECONDS", 0.05)
    monkeypatch.setattr(model_roles, "computer_memory", lambda: 16 * GB)
    # The wait worked out for a model (model_fit), scaled: none from its size unless a test says so.
    monkeypatch.setattr(model_fit, "SECONDS_PER_GB", 0.0)
    monkeypatch.setattr(model_fit, "WAIT_FLOOR", TIMEOUT)
    monkeypatch.setattr(model_fit, "WAIT_CEILING", 60.0)
    servers: list[SlowLMStudio] = []

    def start(**options) -> SlowLMStudio:
        server = SlowLMStudio(**options)
        settings.llm_base_url = server.base
        servers.append(server)
        return server

    yield start
    for server in servers:
        try:
            server.stop()
        except OSError:
            pass


def _chats(server: SlowLMStudio) -> list[tuple]:
    return [entry for entry in server.log if entry[0] == "chat"]


def test_a_big_model_writing_its_whole_reply_slowly_is_read(settings, lm_studio):
    """5 words a second, 450 words: the whole reply takes longer than the time limit, but words keep coming."""
    server = lm_studio(token_gap=0.003, tokens=settings.llm_max_tokens)
    with LocalReader(settings) as reader:
        assert reader.model == BIG
        for subject in ("Invoice 4410", "Invoice 4411"):
            assert reader.read(dict(PACKET, subject=subject)) is not None, reader.stats.last_error
        assert reader.stats.read == 2 and not reader.stopped
    assert all(stream and structured for _chat, _model, stream, structured in _chats(server)), "streamed, with its JSON shape"


def test_a_thinking_model_still_thinking_is_not_cut_off(settings, lm_studio):
    """A reasoning model that writes its reading into its reasoning (as LM Studio can with a JSON shape) is read
    from there, however long the reasoning takes as a whole."""
    lm_studio(token_gap=0.003, tokens=settings.llm_max_tokens, thinks=True)
    with LocalReader(settings) as reader:
        assert reader.read(PACKET) is not None, reader.stats.last_error


def test_process_new_mail_loads_an_unloaded_model_first_and_reads_every_email(settings, loaded, lm_studio):
    """LM Studio unloaded the model since (idle auto-unload): it is loaded with the context that fits this computer
    (model_fit: Setup's 16,384 at most, here the smallest, 4,096, as 17 GB of model is more than this 16 GB computer
    gives) before the first email, the page says so, and the load (longer than two time limits) isn't counted
    against any email."""
    server = lm_studio(loaded=False, load_seconds=TIMEOUT * 3)
    remember_model(settings, BIG)  # read with it yesterday
    stages: list[tuple] = []
    summary = run_overnight(
        loaded, settings, sync_graph=False, limit=3, vision_minutes=0, on_progress=lambda *step: stages.append(step)
    )
    assert summary["read_by_bionic"] == 3 and summary["model_note"] == ""
    assert server.log[0] == ("load", BIG, 4096), "loaded first, with the context that fits"
    assert server.context == 4096
    assert len(_chats(server)) == 3, "no email was asked twice"
    assert [stage for stage in stages if stage[0] == "model"], "the page shows 'Loading a model in LM Studio'"
    assert summary["model_load"]["message"] == f"Loaded {BIG} in LM Studio: it answers your questions and reads the mail."


def test_a_model_unloaded_partway_and_loaded_again_slowly_doesnt_stop_the_reading(settings, loaded, lm_studio):
    """LM Studio unloads the model between two emails and loads it again for the next one's request, in longer than
    two time limits: the reading waits for the load, then goes on."""
    lm_studio(unload_after=1, load_seconds=TIMEOUT * 3)
    result = read_queue(loaded, settings, limit=4)
    assert len(result["read_ids"]) == 4, result["note"]
    assert result["note"] == "" and result["stats"].failed == 0


def test_a_model_too_slow_even_with_longer_stops_the_reading_and_says_so(settings, loaded, lm_studio):
    """No first word within the longer allowance either: the reading stops, saying how slow the model is here and
    which smaller model would answer faster."""
    server = lm_studio(first_token=LONGER * 2)
    result = read_queue(loaded, settings, limit=4)
    assert result["read_ids"] == []
    assert result["note"] == (
        f"Stopped reading: {BIG} takes over {LONGER:.0f} s an email on this computer. "
        f"{SMALL} would fit and answer faster: choose it in Settings → Local model."
    )
    assert len(_chats(server)) == 2, "asked once with the usual limit, once with the longer one, then stopped"


def test_a_model_whose_wait_is_known_isnt_stopped_for_reading_slowly(settings, loaded, lm_studio, monkeypatch):
    """The same slow start, but the wait worked out from the model's size covers it: every email is read, each asked
    once (before, the first two timed out and the reading stopped)."""
    monkeypatch.setattr(model_fit, "SECONDS_PER_GB", LONGER * 3 / 17)  # 17 GB of model: three of its five minutes
    server = lm_studio(first_token=LONGER * 2)
    result = read_queue(loaded, settings, limit=2)
    assert len(result["read_ids"]) == 2 and result["note"] == "", result["note"]
    assert len(_chats(server)) == 2


def test_a_server_that_really_stopped_still_stops_the_reading(settings, loaded, lm_studio):
    server = lm_studio()

    def progress(stage, done, _total, _note) -> None:
        if stage == "reading" and done == 2:
            server.stop()  # LM Studio quit after the first email

    result = read_queue(loaded, settings, limit=4, on_progress=progress)
    assert len(result["read_ids"]) == 1
    assert result["note"].startswith("Stopped reading: the local model server stopped answering (")


def test_lm_studio_still_loading_after_the_whole_wait_stops_the_reading_and_says_so(settings, loaded, lm_studio, monkeypatch):
    monkeypatch.setattr(local_llm, "LOAD_WAIT_SECONDS", TIMEOUT)
    lm_studio(unload_after=1, load_seconds=30)
    result = read_queue(loaded, settings, limit=3)
    assert len(result["read_ids"]) == 1
    assert result["note"].startswith(f"Stopped reading: LM Studio was still loading {BIG}")


def test_a_reader_falls_back_to_one_piece_when_the_server_refuses_streaming(settings):
    """A server that refuses ``stream`` is asked for the whole reply in one piece, from then on."""
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        asked.append(bool(body.get("stream")))
        if body.get("stream"):
            return httpx.Response(400, json={"error": "'stream' is not supported"})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(READING)}, "finish_reason": "stop"}]})

    reader = LocalReader(settings, model="tiny", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert reader.read(PACKET) == READING and reader.read(PACKET) == READING
    assert asked[0] is True and asked[-1] is False and asked.count(True) <= 3


def test_a_chat_answer_that_went_quiet_partway_says_the_model_was_still_writing(settings, lm_studio):
    lm_studio(token_gap=0.001, stall_after=5)
    with pytest.raises(httpx.TimeoutException) as caught:
        list(stream_text(settings, [{"role": "user", "content": "What is due?"}]))
    note = failure_note(caught.value)
    assert "still writing" in note and "may still be loading" not in note


def test_a_chat_answer_that_went_quiet_while_thinking_says_the_model_was_still_thinking(settings, lm_studio):
    lm_studio(token_gap=0.001, stall_after=5, thinks=True)
    with pytest.raises(httpx.TimeoutException) as caught:
        list(stream_text(settings, [{"role": "user", "content": "What is due?"}]))
    note = failure_note(caught.value)
    assert "still thinking" in note and "may still be loading" not in note


def test_reading_mail_measures_the_models_speeds_for_the_summaries_after_it(settings, loaded, lm_studio):
    """A run's file summaries come after its reading of new mail. A model never asked a chat question had no measured
    speeds, so its first long file was cut off at the usual 5 minutes (Gemma 4 12B writing 1,844 tokens at 6.8 a
    second), only to be given longer next run. The reading measures how fast the model reads and writes, so the
    summaries after it wait as long as this model needs."""
    lm_studio(token_gap=0.01, tokens=80, first_token=0.2)
    status = local_llm.check_model(settings)
    before = model_fit.background_wait(loaded, settings, BIG, status, tokens=4000, written=2048)
    result = read_queue(loaded, settings, limit=2)
    assert len(result["read_ids"]) == 2, result["note"]
    rates = model_fit.speeds(loaded, BIG)
    assert 20 < rates.get("write", 0) < 200, rates  # about 100 words a second, a word every 10 ms
    assert rates.get("read", 0) > 0, rates
    after = model_fit.background_wait(loaded, settings, BIG, status, tokens=4000, written=2048)
    assert after.seconds >= 2048 / rates["write"] > before.seconds
