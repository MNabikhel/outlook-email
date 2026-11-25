"""A stand-in for pywebview, for the CloseDesk window's check on Linux (``tests/launcher_smoke.py --window``), where no
window opens: everything else of the window runs for real (launch.py --app, the window's process, the CloseDesk it
starts and stops, Update now). Its "window" loads CloseDesk's page with urllib and then says the page loaded, as
static/ui/in_window.js does in the real window; closing it runs the window's closing handlers, as pywebview does.
Used only through PYTHONPATH (see sitecustomize.py beside it)."""

from __future__ import annotations

import inspect
import threading
import time
import urllib.request
from urllib.parse import urlsplit

settings: dict = {}
screens: list = []
windows: list = []
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Event:
    """pywebview's event: handlers added with +=; one with a ``window`` parameter is given the window; ``set`` says
    whether a handler returned False."""

    def __init__(self, window) -> None:
        self.items: list = []
        self.window = window
        self.flag = threading.Event()

    def __iadd__(self, handler):
        self.items.append(handler)
        return self

    def set(self, *args) -> bool:
        said = []
        for handler in self.items:
            params = inspect.signature(handler).parameters
            said.append(handler(self.window, *args) if "window" in params else handler(*args))
        self.flag.set()
        return any(value is False for value in said)

    def wait(self, timeout=None) -> bool:
        return self.flag.wait(timeout)

    def clear(self) -> None:
        self.flag.clear()


class Window:
    def __init__(self, title, html=None, js_api=None, **options) -> None:
        self.title, self.js_api = title, js_api
        self.localization = dict(options.get("localization") or {})
        self.confirm_close = False
        self.gone = threading.Event()
        self.events = type("Events", (), {})()
        for name in ("closing", "closed", "loaded", "shown", "initialized"):
            setattr(self.events, name, Event(self))

    def load_html(self, html: str) -> None:
        heading = html[html.find("<h1>") + 4 : html.find("</h1>")]
        print(f"[stand-in window] shows: {heading}", flush=True)
        self.events.loaded.clear()
        self.events.loaded.set()

    def load_url(self, url: str) -> None:
        self.events.loaded.clear()

        def load() -> None:
            try:
                with _LOCAL.open(url, timeout=30) as reply:
                    path, port = urlsplit(reply.geturl()).path, str(urlsplit(reply.geturl()).port or "")
                    reply.read()
            except OSError as exc:
                print(f"[stand-in window] couldn't load {url}: {exc}", flush=True)
                return
            print(f"[stand-in window] loaded {path}", flush=True)
            self.events.loaded.set()
            time.sleep(0.3)
            self.js_api.page_ready(path, port)

        threading.Thread(target=load, daemon=True).start()

    def destroy(self) -> None:
        if self.gone.is_set():
            return
        self.events.closing.set()
        print(f"[stand-in window] closed (asked first: {self.confirm_close})", flush=True)
        self.events.closed.set()
        self.gone.set()


def create_window(title, url=None, html=None, js_api=None, **options) -> Window:
    window = Window(title, html=html, js_api=js_api, **options)
    windows.append(window)
    return window


def start(func=None, **options) -> None:
    window = windows[0]
    if window.events.initialized.set("edgechromium"):
        return
    window.events.shown.set()
    window.events.loaded.set()
    if func is not None:
        threading.Thread(target=func, daemon=True).start()
    window.gone.wait()
