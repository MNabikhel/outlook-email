"""Stop, or closing the page, hangs up on the model at once, even while it is still reading the prompt.

The chat's lines were read in a worker thread that couldn't be stopped: Stop only took effect once LM Studio sent its
first word, after reading the whole prompt (91 seconds on a slow laptop), and the next question waited behind that
reading, which was then kept as the model's speed. A real local server here, since the hang-up is on the connection.
"""

from __future__ import annotations

import json
import select
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anyio

from controller_inbox import local_llm, web


class _ReadsForever(BaseHTTPRequestHandler):
    """A model server that lists one model and never answers a chat request: it is still reading the prompt. It notes
    when CloseDesk hung up on it."""

    asked = threading.Event()
    hung_up: list[float] = []

    def log_message(self, *_args) -> None:
        pass

    def do_GET(self) -> None:
        body = json.dumps({"data": [{"id": "slow-model"}]} if self.path == "/v1/models" else {"error": "not found"}).encode()
        self.send_response(200 if self.path == "/v1/models" else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).asked.set()
        until = time.monotonic() + 15
        while time.monotonic() < until:
            ready, _, _ = select.select([self.connection], [], [], 0.05)
            if ready and not self.connection.recv(1):
                type(self).hung_up.append(time.monotonic())
                return
        self.send_response(500)
        self.end_headers()


def test_stopping_an_answer_hangs_up_on_the_model_while_it_reads(settings):
    _ReadsForever.asked.clear()
    _ReadsForever.hung_up.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ReadsForever)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    settings.llm = True
    settings.llm_base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    cleaned_up: list[float] = []

    def lines():
        try:
            yield "started\n"
            yield from local_llm.stream_text(settings, [{"role": "user", "content": "When is it due?"}], max_tokens=50)
        finally:
            cleaned_up.append(time.monotonic())

    async def ask_then_stop() -> float:
        async def read() -> None:
            async for _line in web._closing(lines(), hang_up=local_llm.HangUp()):
                pass

        async with anyio.create_task_group() as group:
            group.start_soon(read)
            await anyio.to_thread.run_sync(_ReadsForever.asked.wait, 10)
            group.cancel_scope.cancel()  # the user clicked Stop, or closed the page, while the model read
        return time.monotonic()

    try:
        stopped = anyio.run(ask_then_stop)
    finally:
        server.shutdown()
    assert _ReadsForever.hung_up, "CloseDesk kept the connection open while the model read"
    assert _ReadsForever.hung_up[0] - stopped < 3
    assert cleaned_up and cleaned_up[0] - stopped < 3, "the answer's clean-up (saving what was said) didn't run"
