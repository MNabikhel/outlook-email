"""A real CloseDesk server on a free local port, for tests that need a browser or a dropped connection.

The test client runs the app in-process and reads each response to the end, so it can't show what a
browser sees mid-answer or what happens when the browser goes away. These tests use uvicorn instead.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import uvicorn

# Where this machine keeps Playwright's Chromium; CLOSEDESK_CHROMIUM points elsewhere.
CHROMIUM_PATHS = (os.environ.get("CLOSEDESK_CHROMIUM", ""), "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")


@contextmanager
def serving(app) -> Iterator[str]:
    """Runs the app until the block ends and gives its address, such as http://127.0.0.1:50123."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off", timeout_graceful_shutdown=2))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("the test server didn't start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def chromium_path() -> str | None:
    """A Chromium that Playwright can drive, or None when this machine has none."""
    for path in CHROMIUM_PATHS:
        if path and Path(path).is_file():
            return path
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            path = p.chromium.executable_path
    except Exception:
        return None
    return path if path and Path(path).is_file() else None
