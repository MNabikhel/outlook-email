"""The CloseDesk window (app_window.py, opened by ``launch.py --app``, which the CloseDesk shortcut runs): it starts
CloseDesk and shows it in a window of its own, closing it stops CloseDesk (asking first while mail is processed or an
update runs), an update keeps the window up throughout, a CloseDesk already open is shown rather than a second
started, and where the window can't open (no WebView2, no pywebview, Linux) CloseDesk opens in the browser as before.

pywebview, the processes and the Windows registry are fakes here: the real window opens in CI on Windows and macOS.
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from controller_inbox import app_window, cli, updates

ROOT = Path(__file__).resolve().parent.parent
URL = "http://127.0.0.1:8765/"
NO_WINDOW = 0x08000000  # subprocess.CREATE_NO_WINDOW on Windows


def until(check, timeout: float = 10.0, what: str = "") -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, f"timed out waiting for {what or check}"
        time.sleep(0.01)


# A pywebview of its own: what the real one does that CloseDesk relies on --------------------------------------------


class FakeEvent:
    """pywebview's Event: handlers added with +=, a handler with a ``window`` parameter is given the window, and
    ``set`` says whether any handler returned False (closing: don't close; initialized: don't open)."""

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
            if not params:
                said.append(handler())
            elif "window" in params:
                said.append(handler(self.window, *args))
            else:
                said.append(handler(*args))
        self.flag.set()
        return any(value is False for value in said)

    def wait(self, timeout=None) -> bool:
        return self.flag.wait(timeout)

    def is_set(self) -> bool:
        return self.flag.is_set()

    def clear(self) -> None:
        self.flag.clear()


class FakeWindow:
    def __init__(self, title, *, url=None, html=None, js_api=None, localization=None, **options) -> None:
        self.title, self.js_api, self.options = title, js_api, options
        self.localization = {"global.quitConfirmation": "Do you really want to quit?", "global.quit": "Quit",
                             "global.cancel": "Cancel", **(localization or {})}
        self.confirm_close = False
        self.shown: list[tuple[str, str]] = [("html", html)] if html else [("url", url)]
        self.asked: list[str] = []  # the "close anyway?" questions pywebview put to the person
        self.closed_by = ""
        self.gone = threading.Event()
        self.events = SimpleNamespace(**{name: FakeEvent(self) for name in ("closing", "closed", "loaded", "shown", "initialized")})
        self.loads = True  # whether a page shown in it loads

    def load_url(self, url) -> None:
        self.events.loaded.clear()
        self.shown.append(("url", url))
        if self.loads:
            self.events.loaded.set()

    def load_html(self, html) -> None:
        self.events.loaded.clear()
        self.shown.append(("html", html))
        if self.loads:
            self.events.loaded.set()

    def destroy(self) -> None:
        # On Windows, destroy() closes the form, which raises FormClosing: the closing handlers run, and pywebview
        # asks the person when confirm_close is set, as it does for the close button.
        self._close("CloseDesk", answer=True)

    def person_closes(self, answer: bool = True) -> bool:
        """The close button (or Alt+F4, or Cmd+W): True when the window closed."""
        return self._close("person", answer=answer)

    def _close(self, by: str, *, answer: bool) -> bool:
        if self.gone.is_set():
            return True
        if self.events.closing.set():
            return False
        if self.confirm_close:
            self.asked.append(self.localization["global.quitConfirmation"])
            if not answer:
                return False
        self.closed_by = by
        self.events.closed.set()
        self.gone.set()
        return True

    def htmls(self) -> list[str]:
        return [text for kind, text in self.shown if kind == "html"]

    def urls(self) -> list[str]:
        return [text for kind, text in self.shown if kind == "url"]


class FakeWebview:
    def __init__(self, renderer: str = "edgechromium", *, page_loads: bool = True, fails: Exception | None = None,
                 screen=(1920, 1080)) -> None:
        self.settings = {"ALLOW_DOWNLOADS": False, "OPEN_EXTERNAL_LINKS_IN_BROWSER": True}
        self.windows: list[FakeWindow] = []
        self.renderer, self.page_loads, self.fails = renderer, page_loads, fails
        self.started: dict | None = None
        self.screens = [SimpleNamespace(width=screen[0], height=screen[1])]

    def create_window(self, title, url=None, html=None, js_api=None, **options) -> FakeWindow:
        window = FakeWindow(title, url=url, html=html, js_api=js_api, **options)
        window.loads = self.page_loads
        self.windows.append(window)
        return window

    def start(self, func=None, args=None, **options) -> None:
        self.started = options
        if self.fails is not None:
            raise self.fails
        window = self.windows[0]
        if window.events.initialized.set(self.renderer):
            return  # an initialized handler said no: no window, and func isn't run
        window.events.shown.set()
        if self.page_loads:
            window.events.loaded.set()
        if func is not None:
            threading.Thread(target=func, daemon=True).start()
        assert window.gone.wait(30), "the window was never closed"


# The processes the window starts ---------------------------------------------------------------------------------


class FakeProcess:
    def __init__(self, cmd, **options) -> None:
        self.cmd, self.options = list(cmd), options
        self.returncode = None
        self.ended = threading.Event()
        self.terminated = False
        self.input_closed = False
        self.stdin = SimpleNamespace(close=self._input_ends)

    def _input_ends(self) -> None:
        # CloseDesk started by the window stops when its input ends (cli: CLOSEDESK_STOP_WITH_PARENT).
        self.input_closed = True
        self.end(0)

    def end(self, code: int) -> None:
        if self.returncode is None:
            self.returncode = code
            self.ended.set()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self.ended.wait(timeout):
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.end(-15)

    def kill(self) -> None:
        self.end(-9)


class Computer:
    """What the window starts on this computer, and what it finds."""

    def __init__(self) -> None:
        self.processes: list[FakeProcess] = []
        self.busy = False  # mail being processed
        self.update_code: int | None = 0  # None: the update runs until the test ends it
        self.opened: list[str] = []  # given to the default browser or mail app
        self.launcher: list[str] = []  # the double-click launcher opened, and why

    def popen(self, cmd, **options) -> FakeProcess:
        process = FakeProcess(cmd, **options)
        self.processes.append(process)
        if "--update" in cmd and self.update_code is not None:
            process.end(self.update_code)
        return process

    def servers(self) -> list[FakeProcess]:
        return [p for p in self.processes if "run" in p.cmd]

    def updates(self) -> list[FakeProcess]:
        return [p for p in self.processes if "--update" in p.cmd]

    def answers(self, url: str) -> bool:
        return any(p.poll() is None for p in self.servers())


@pytest.fixture
def computer(monkeypatch):
    monkeypatch.setattr(app_window, "NO_WINDOW", NO_WINDOW)
    for name in ("CLOSEDESK_REOPEN", app_window.SMOKE_ENV):
        monkeypatch.delenv(name, raising=False)
    return Computer()


def make_app(webview, computer, tmp_path, *, owned=True) -> app_window.App:
    app = app_window.App(webview, url=URL, port=8765, owned=owned, log=tmp_path / "CloseDesk-app.log")
    app.popen = computer.popen
    app.answers = computer.answers
    app.working = lambda: computer.busy
    app.open_url = computer.opened.append
    app.open_launcher = lambda why="": computer.launcher.append(why)
    app.poll = 0.01
    app.storage = tmp_path / "window"
    return app


def running(app):
    result: dict = {}
    thread = threading.Thread(target=lambda: result.setdefault("code", app.run()), daemon=True)
    thread.start()
    return thread, result


def window_of(webview) -> FakeWindow:
    until(lambda: webview.windows, what="the window")
    return webview.windows[0]


# The window opens CloseDesk, and closing it stops CloseDesk ---------------------------------------------------------


def test_the_window_starts_closedesk_shows_it_and_closing_it_stops_closedesk(computer, tmp_path):
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    thread, result = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls(), what="CloseDesk in the window")

    assert window.title == "CloseDesk"
    assert "Opening CloseDesk" in window.htmls()[0], "the window says what it is doing until CloseDesk answers"
    assert window.options["text_select"] is True and window.options["zoomable"] is True
    width, height = window.options["min_size"]
    assert width <= 900 and height <= 640
    assert webview.settings["ALLOW_DOWNLOADS"] is True, "Download, .eml and CSV would do nothing"
    assert webview.started["private_mode"] is False, "the pages' own settings (theme, drafts) would go every start"
    assert webview.started["storage_path"] == str(tmp_path / "window")

    (server,) = computer.servers()
    assert server.cmd[1:] == ["-m", "controller_inbox", "run", "--no-browser", "--port", "8765"]
    assert server.options["creationflags"] == NO_WINDOW, "a console window would flash up on Windows"
    assert server.options["stdin"] == subprocess.PIPE, "CloseDesk stops when the window's process ends"
    assert server.options["env"][updates.LAUNCHER_ENV] == "1", "Update now starts CloseDesk again only with a launcher"
    assert server.options["env"][app_window.STOP_WITH_PARENT] == "1"
    assert server.options["cwd"] == str(app_window.ROOT)

    assert window.person_closes() is True
    thread.join(10)
    assert result["code"] == 0
    assert server.input_closed and not server.terminated, "CloseDesk was stopped as Ctrl+C stops it, not killed"
    assert window.asked == [], "nothing was being done: nothing is asked"


def test_closing_while_mail_is_processed_asks_first_and_keep_open_keeps_closedesk(computer, tmp_path):
    computer.busy = True
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())

    assert window.person_closes(answer=False) is False
    assert window.asked == [app_window.BUSY_CLOSE]
    assert "still processing mail" in app_window.BUSY_CLOSE and "Close it anyway?" in app_window.BUSY_CLOSE
    (server,) = computer.servers()
    assert server.poll() is None and thread.is_alive(), "Keep open: CloseDesk carries on"

    computer.busy = False
    assert window.person_closes() is True
    assert window.asked == [app_window.BUSY_CLOSE], "done meanwhile: closing doesn't ask again"
    thread.join(10)
    assert server.input_closed


def test_the_close_question_is_pywebviews_own_set_in_the_handler(computer, tmp_path):
    # macOS hangs when the closing handler opens a dialog itself or runs JavaScript (pywebview #1699): the handler
    # only sets what pywebview asks, and returns nothing (False would cancel the close without asking).
    computer.busy = True
    app = make_app(FakeWebview(), computer, tmp_path)
    window = SimpleNamespace(confirm_close=False, localization={"global.quitConfirmation": "Do you really want to quit?"})
    assert app.on_closing(window) is None
    assert window.confirm_close is True and window.localization["global.quitConfirmation"] == app_window.BUSY_CLOSE
    computer.busy = False
    assert app.on_closing(window) is None and window.confirm_close is False


def test_closedesk_closing_its_window_itself_never_asks(computer, tmp_path):
    # On Windows, window.destroy() raises the same closing as the close button: closing on purpose (for the browser,
    # or the launcher) must not stop on "Close anyway?".
    computer.busy = True
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    app.close_on_purpose()
    thread.join(10)
    assert not thread.is_alive() and window.asked == [] and window.closed_by == "CloseDesk"


# An update keeps the window up throughout ----------------------------------------------------------------------------


def test_update_now_shows_updating_in_the_window_throughout_then_the_new_version(computer, tmp_path):
    computer.update_code = None  # the update runs until it is ended below
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())

    computer.servers()[0].end(updates.UPDATE_AND_RESTART)  # Update now: CloseDesk stops to be updated
    until(lambda: computer.updates(), what="the update")
    (update,) = computer.updates()
    assert update.cmd == [sys.executable, str(app_window.ROOT / "scripts" / "launch.py"), "--update"]
    assert update.options["creationflags"] == NO_WINDOW and update.options["stdin"] == subprocess.DEVNULL
    assert update.options["env"][updates.APP_ENV] == "1", "its set-up runs as the app's too: no console windows"
    shown = window.htmls()[-1]
    assert window.shown[-1][0] == "html" and "Updating CloseDesk" in shown and updates.UPDATING in shown

    update.end(0)
    until(lambda: len(window.urls()) == 2, what="the new version in the window")
    first, second = computer.servers()
    assert first.returncode == updates.UPDATE_AND_RESTART and second.poll() is None
    assert second.options["env"].get("CLOSEDESK_REOPEN") == "1", "on the port the page was on"
    assert "--port" in second.cmd and second.cmd[second.cmd.index("--port") + 1] == "8765"
    window.person_closes()
    thread.join(10)
    assert second.input_closed


def test_an_update_that_fails_opens_the_version_there_is(computer, tmp_path):
    # The update said why in the log; CloseDesk opens as it was and its first page says it wasn't updated.
    computer.update_code = 1
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    computer.servers()[0].end(updates.UPDATE_AND_RESTART)
    until(lambda: len(window.urls()) == 2)
    assert len(computer.servers()) == 2 and computer.servers()[1].poll() is None
    window.person_closes()
    thread.join(10)


def test_closing_during_an_update_asks_and_lets_the_update_finish(computer, tmp_path):
    computer.update_code = None
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    thread, result = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    computer.servers()[0].end(updates.UPDATE_AND_RESTART)
    until(lambda: computer.updates())

    assert window.person_closes(answer=False) is False
    assert window.asked == [app_window.UPDATING_CLOSE]
    assert window.person_closes() is True
    time.sleep(0.2)
    (update,) = computer.updates()
    assert thread.is_alive() and not update.terminated, "an update stopped half way would leave CloseDesk half updated"
    update.end(0)
    thread.join(10)
    assert result["code"] == 0 and len(computer.servers()) == 1, "closed: not opened again after the update"


def test_an_update_that_needs_the_environment_made_again_hands_over_to_the_double_click_launcher(computer, tmp_path):
    # The environment can't be made again by a Python running from it: the launcher does it, in its window.
    computer.update_code = 76
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    computer.servers()[0].end(updates.UPDATE_AND_RESTART)
    thread.join(10)
    assert not thread.is_alive() and computer.launcher and window.closed_by == "CloseDesk" and window.asked == []
    assert len(computer.servers()) == 1


def test_closedesk_stopping_by_itself_says_so_in_the_window_and_where_to_look(computer, tmp_path):
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    computer.servers()[0].end(1)
    until(lambda: "stopped" in window.htmls()[-1], what="the window saying CloseDesk stopped")
    assert str(tmp_path / "CloseDesk-app.log") in window.htmls()[-1]
    assert len(computer.servers()) == 1, "not started again and again"
    window.person_closes()
    thread.join(10)


def test_closedesk_that_never_answers_says_so_in_the_window(computer, tmp_path):
    computer.answers = lambda url: False
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    app.answers = computer.answers
    app.up_wait = 0.3
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: len(window.htmls()) == 2, what="the window saying CloseDesk didn't open")
    assert "didn't open" in window.htmls()[-1] and str(tmp_path / "CloseDesk-app.log") in window.htmls()[-1]
    window.person_closes()
    thread.join(10)
    assert computer.servers()[0].input_closed


# A CloseDesk already open is shown, not a second started -------------------------------------------------------------


def test_a_closedesk_already_open_is_shown_and_closing_the_window_leaves_it_open(computer, tmp_path):
    computer.busy = True
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path, owned=False)
    thread, result = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    assert computer.processes == []
    assert window.person_closes() is True and window.asked == [], "it isn't this window's to stop: nothing to ask"
    thread.join(10)
    assert result["code"] == 0 and computer.processes == []


def test_the_shortcut_shows_a_closedesk_already_open_instead_of_starting_a_second(monkeypatch, settings):
    monkeypatch.setattr(cli, "_port_to_use", lambda host, port: (port, True))
    monkeypatch.setattr(app_window, "load_settings", lambda: settings)
    app = app_window.prepare(SimpleNamespace(host=None, port=None), FakeWebview())
    assert app.owned is False and app.url == f"http://127.0.0.1:{settings.port}/"
    monkeypatch.setattr(cli, "_port_to_use", lambda host, port: (port + 1, False))
    app = app_window.prepare(SimpleNamespace(host=None, port=None), FakeWebview())
    assert app.owned is True and app.port == settings.port + 1


def test_closedesk_listening_on_every_address_is_shown_on_this_computers_own(monkeypatch, settings):
    settings.host = "0.0.0.0"
    monkeypatch.setattr(cli, "_port_to_use", lambda host, port: (port, False))
    monkeypatch.setattr(app_window, "load_settings", lambda: settings)
    app = app_window.prepare(SimpleNamespace(host=None, port=8800), FakeWebview())
    assert app.url == "http://127.0.0.1:8800/"


def test_settings_that_stop_closedesk_starting_are_said_in_a_message_box(monkeypatch, tmp_path):
    told: list = []
    monkeypatch.setattr(app_window, "tell", told.append)

    def broken():
        raise ValueError("CONTROLLER_INBOX_POLL_SECONDS: not a number")

    monkeypatch.setattr(app_window, "load_settings", broken)
    monkeypatch.setattr("controller_inbox.doctor.checks", lambda **_: [
        SimpleNamespace(state="problem", text="Settings: CONTROLLER_INBOX_POLL_SECONDS isn't valid", todo="Fix it in .env")])
    assert app_window.prepare(SimpleNamespace(host=None, port=None), FakeWebview()) is None
    assert told and "CloseDesk can't start yet" in told[0] and "CONTROLLER_INBOX_POLL_SECONDS" in told[0]


# Where the window can't show CloseDesk, the browser does -------------------------------------------------------------


def test_an_engine_that_cant_run_closedesk_opens_it_in_the_browser_instead(computer, tmp_path):
    # pywebview 6 falls back to Internet Explorer when WebView2 is missing, with only a warning in its log.
    webview = FakeWebview(renderer="mshtml")
    app = make_app(webview, computer, tmp_path)
    assert app.run() == 0
    assert app.fallback and app.fallback[0] == "webview2" and computer.processes == []


def test_a_window_that_cant_start_opens_closedesk_in_the_browser_instead(computer, tmp_path):
    webview = FakeWebview(fails=RuntimeError("pythonnet couldn't load .NET"))
    app = make_app(webview, computer, tmp_path)
    app.run()
    assert app.fallback and app.fallback[0] == "broken" and "pythonnet" in app.fallback[1]
    assert computer.processes == []


def test_a_window_that_never_shows_a_page_opens_closedesk_in_the_browser_instead(computer, tmp_path):
    # WebView2 that fails to start only logs it and leaves an empty window.
    webview = FakeWebview(page_loads=False)
    app = make_app(webview, computer, tmp_path)
    app.load_wait = 0.2
    app.run()
    assert app.fallback and app.fallback[0] == "blank"
    assert webview.windows[0].closed_by == "CloseDesk" and computer.processes == []


@pytest.mark.parametrize(
    "platform, environ, registry, webview_found, key",
    [
        ("linux", {}, None, True, "platform"),
        ("win32", {}, "none", True, "webview2"),
        ("win32", {}, "zero", True, "webview2"),
        ("win32", {}, "old-net", True, "webview2"),
        ("win32", {}, "runtime", True, None),
        ("win32", {}, "per-user", True, None),
        ("win32", {}, "runtime", False, "missing"),
        ("darwin", {}, None, True, None),
        ("darwin", {}, None, False, "missing"),
        ("darwin", {"CLOSEDESK_WINDOW": "0"}, None, True, "off"),
    ],
)
def test_whether_the_window_can_open_here(monkeypatch, platform, environ, registry, webview_found, key):
    monkeypatch.setattr(app_window, "_webview_found", lambda: "" if webview_found else "No module named 'webview'")
    found = app_window.why_no_window(platform=platform, environ=environ, registry=fake_registry(registry) if registry else None)
    assert (found[0] if found else None) == key
    if found:
        assert found[1] and "browser" in found[1]


def test_without_webview2_the_reason_is_said_in_plain_words():
    words = app_window.why_no_window(platform="win32", environ={}, registry=fake_registry("none"))[1]
    assert "Microsoft Edge WebView2 Runtime" in words and "opens in your browser" in words


def fake_registry(kind: str):
    """winreg with what a computer has: the WebView2 Runtime for everyone or for this user, an empty one (0.0.0.0, what
    an uninstall leaves), none, or only a .NET too old for it."""
    runtime = r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    user = r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    net = r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full"
    values = {("HKLM", net): {"Release": 533320 if kind != "old-net" else 379893}}
    if kind in ("runtime", "old-net"):
        values[("HKLM", runtime)] = {"pv": "131.0.2903.70"}
    if kind == "zero":
        values[("HKLM", runtime)] = {"pv": "0.0.0.0"}
    if kind == "per-user":
        values[("HKCU", user)] = {"pv": "120.0.2210.91"}

    class Key:
        def __init__(self, found):
            self.found = found

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def open_key(hive, path):
        if (hive, path) not in values:
            raise FileNotFoundError(path)
        return Key(values[(hive, path)])

    def query(key, name):
        if name not in key.found:
            raise FileNotFoundError(name)
        return key.found[name], 1

    return SimpleNamespace(HKEY_LOCAL_MACHINE="HKLM", HKEY_CURRENT_USER="HKCU", OpenKey=open_key, QueryValueEx=query)


def test_the_browser_instead_is_the_double_click_launcher_said_once(monkeypatch, tmp_path):
    # With no console (the shortcut), CloseDesk opens as CloseDesk.bat / CloseDesk.command open it, in a window of its
    # own that closing stops; why is said in a message box the first time, and in the log every time.
    told, opened = [], []
    monkeypatch.setattr(app_window, "tell", lambda text, **_: told.append(text))
    monkeypatch.setattr(app_window, "open_launcher", lambda why="", **_: opened.append(why))
    monkeypatch.setattr(app_window, "user_dir", lambda: tmp_path)
    monkeypatch.setattr(app_window, "_console", lambda: False)
    reason = ("webview2", app_window.NO_WEBVIEW2)
    assert app_window.browser_instead(reason, SimpleNamespace(host=None, port=None), platform="win32") == 0
    assert app_window.browser_instead(reason, SimpleNamespace(host=None, port=None), platform="win32") == 0
    assert told == [app_window.NO_WEBVIEW2] and opened == [app_window.NO_WEBVIEW2] * 2
    assert app_window.browser_instead(("off", "x"), SimpleNamespace(host=None, port=None), platform="win32") == 0
    assert told == [app_window.NO_WEBVIEW2], "turned off on purpose: nothing to explain"


def test_the_browser_instead_runs_here_on_linux_or_from_a_terminal(monkeypatch):
    ran: list = []
    monkeypatch.setattr(cli, "main", lambda argv: ran.append(argv) or 0)
    monkeypatch.setattr(app_window, "open_launcher", lambda why="": pytest.fail("no launcher on Linux"))
    monkeypatch.setenv(updates.LAUNCHER_ENV, "1")
    assert app_window.browser_instead(("platform", app_window.NOT_HERE), SimpleNamespace(host=None, port=8801), platform="linux") == 0
    assert ran == [["run", "--port", "8801"]]
    assert os.environ.get(updates.LAUNCHER_ENV) is None, "nothing here starts CloseDesk again after an update"


def test_the_double_click_launcher_is_opened_for_the_browser(monkeypatch, tmp_path):
    started, ran = [], []
    monkeypatch.setenv(updates.APP_ENV, "1")
    monkeypatch.setenv(updates.LAUNCHER_ENV, "1")
    app_window.open_launcher("why", platform="win32", startfile=lambda path: started.append((path, dict(os.environ))))
    path, env = started[0]
    assert path == str(app_window.ROOT / "CloseDesk.bat")
    assert updates.APP_ENV not in env and updates.LAUNCHER_ENV not in env, "it is the ordinary start, with its window"
    assert env[app_window.WHY_ENV] == "why", "its window says why too"
    app_window.open_launcher("why", platform="darwin", run=lambda cmd: ran.append(cmd))
    assert ran == [["/usr/bin/open", "-a", "Terminal", str(app_window.ROOT / "CloseDesk.command")]]


def test_the_launchers_window_says_why_closedesk_opened_in_the_browser(settings, store, monkeypatch, capsys):
    monkeypatch.setenv(app_window.WHY_ENV, app_window.NO_WEBVIEW2)
    monkeypatch.setattr(cli, "_serve", lambda *a, **k: 0)
    args = SimpleNamespace(no_serve=False, no_browser=True, limit=0, host="127.0.0.1", port=_free_port())
    cli._run(settings, store, args)
    assert app_window.NO_WEBVIEW2 in capsys.readouterr().out


# Links, downloads and printing from the window -----------------------------------------------------------------------


def test_links_that_leave_closedesk_open_in_the_default_browser_or_mail_app(computer, tmp_path):
    app = make_app(FakeWebview(), computer, tmp_path)
    bridge = app_window.Bridge(app)
    for url in ("mailto:ap@harbor.example?subject=INV-2201", "https://lmstudio.ai/", f"{URL}digest/2026-09-22.html"):
        assert bridge.open_outside(url) is True
    for url in ("javascript:alert(1)", "file:///C:/Windows/System32/calc.exe", "ms-msdt:/id PCWDiagnostic", "",
                "https://example.com/\nfile:///etc/passwd"):
        assert bridge.open_outside(url) is False, url
    assert computer.opened == ["mailto:ap@harbor.example?subject=INV-2201", "https://lmstudio.ai/", f"{URL}digest/2026-09-22.html"]


def test_the_page_can_reach_only_what_the_bridge_offers(computer, tmp_path):
    # pywebview offers the page every public method of the bridge, and of every object one of its attributes holds.
    bridge = app_window.Bridge(make_app(FakeWebview(), computer, tmp_path))
    public = {name for name in dir(bridge) if not name.startswith("_")}
    assert public == {"open_outside", "page_ready"}


# The smoke check CI runs: the real window, with CloseDesk's page loaded in it ------------------------------------------


def test_in_the_smoke_check_the_window_closes_itself_once_the_page_says_it_loaded(computer, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(app_window.SMOKE_ENV, "5")
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    thread, result = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    window.js_api.page_ready("/app")
    thread.join(10)
    assert result["code"] == 0 and window.closed_by == "CloseDesk"
    assert computer.servers()[0].input_closed
    assert "The CloseDesk window showed /app" in capsys.readouterr().out


def test_in_the_smoke_check_a_page_that_never_says_it_loaded_fails(computer, tmp_path, monkeypatch):
    monkeypatch.setenv(app_window.SMOKE_ENV, "0.2")
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    thread, result = running(app)
    thread.join(10)
    assert result["code"] == 1


# The window's size, the log, the taskbar ------------------------------------------------------------------------------


def test_the_window_fits_a_small_screen(computer, tmp_path):
    webview = FakeWebview(screen=(1366, 768))
    app = make_app(webview, computer, tmp_path)
    thread, _ = running(app)
    window = window_of(webview)
    assert window.options["width"] <= 1366 * 0.9 and window.options["height"] <= 768 * 0.9
    until(lambda: URL in window.urls())
    window.person_closes()
    thread.join(10)


def test_without_a_console_what_closedesk_says_goes_to_its_log(monkeypatch, tmp_path):
    log = tmp_path / "logs" / "CloseDesk-app.log"
    monkeypatch.setenv(updates.APP_LOG_ENV, str(log))
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    app_window.use_log_if_no_console()
    try:
        print("hello from the window")
        sys.stderr.write("and its errors\n")
        sys.stdout.flush()
        assert "hello from the window" in log.read_text(encoding="utf-8") and "and its errors" in log.read_text(encoding="utf-8")
    finally:
        sys.stdout.close()


def test_the_taskbar_groups_the_window_under_the_shortcuts_icon(monkeypatch):
    # Windows: the window's process says it is CloseDesk (the shortcut says the same), before any window opens.
    said: list = []
    shell32 = SimpleNamespace(SetCurrentProcessExplicitAppUserModelID=SimpleNamespace(__call__=None))

    class Call:
        argtypes = restype = None

        def __call__(self, value):
            said.append(value)
            return 0

    shell32.SetCurrentProcessExplicitAppUserModelID = Call()
    app_window.set_app_id(platform="win32", shell32=shell32)
    assert said == ["CloseDesk.CloseDesk"] == [app_window.APP_ID]
    app_window.set_app_id(platform="darwin", shell32=shell32)
    assert said == ["CloseDesk.CloseDesk"]


def test_the_app_command_starts_the_window_before_reading_the_settings(monkeypatch):
    # A setting that isn't valid is said in a message box by the window (no console), not printed.
    seen: list = []
    monkeypatch.setattr(app_window, "main", lambda args: seen.append((args.host, args.port)) or 0)
    monkeypatch.setattr(cli, "load_settings", lambda: pytest.fail("the app reads its settings itself"))
    assert cli.main(["app", "--port", "8811"]) == 0
    assert seen == [(None, 8811)]


def test_an_update_that_didnt_finish_says_where_the_app_wrote_why(monkeypatch, tmp_path):
    question = updates.Question()
    assert "CloseDesk's window says what went wrong" in updates.unfinished_message()
    monkeypatch.setenv(updates.APP_ENV, "1")
    monkeypatch.setenv(updates.APP_LOG_ENV, str(tmp_path / "CloseDesk-app.log"))
    said = updates.unfinished_message()
    assert str(tmp_path / "CloseDesk-app.log") in said and "window says" not in said
    assert question is not None


# CloseDesk started by the window stops when the window's process ends --------------------------------------------------


def test_closedesk_stops_when_its_input_ends():
    read, write = os.pipe()
    stopped = threading.Event()
    with os.fdopen(read, "rb") as stream:
        thread = threading.Thread(target=cli._stop_when_input_ends, args=(stream, stopped.set), daemon=True)
        thread.start()
        time.sleep(0.1)
        assert not stopped.is_set()
        os.close(write)
        assert stopped.wait(5)


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_a_real_closedesk_started_as_the_window_starts_it_stops_when_the_window_goes(tmp_path):
    # However the window's process ends (closed, Cmd+Q, ended in Task Manager), its end of CloseDesk's input closes,
    # and CloseDesk stops as Ctrl+C stops it: nothing is left running without a window.
    import json
    import urllib.request

    port = _free_port()
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "CONTROLLER_INBOX_DATA_DIR": str(tmp_path / "data"),
           "CONTROLLER_INBOX_INBOX_DIR": str(tmp_path / "inbox"), "CONTROLLER_INBOX_LLM": "false",
           "CONTROLLER_INBOX_UPDATE_CHECK": "false", app_window.STOP_WITH_PARENT: "1"}
    server = subprocess.Popen(
        [sys.executable, "-m", "controller_inbox", "run", "--no-browser", "--port", str(port)], cwd=tmp_path, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        answer = None
        deadline = time.monotonic() + 60
        while answer is None and time.monotonic() < deadline and server.poll() is None:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as reply:
                    answer = json.load(reply)
            except OSError:
                time.sleep(0.2)
        assert answer and answer["app"] == "closedesk", server.stdout.read().decode(errors="replace") if server.poll() is not None else ""
        server.stdin.close()
        assert server.wait(30) == 0
    finally:
        if server.poll() is None:
            server.kill()
            server.wait()


def test_every_download_link_says_download_so_the_window_saves_it():
    # WebKit (the macOS window) shows a CSV, or another file it can show, in the window itself unless its link says
    # "download", and the window has no back button to return to CloseDesk. WebView2 saves either way.
    import re

    package = app_window.ROOT / "src" / "controller_inbox"
    missing, navigated = [], []
    for path in [*sorted((package / "templates").glob("*.html")), *sorted((package / "static").rglob("*.js"))]:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            route = re.search(r"(/download\b|/original\b|\.csv\b)", line)
            if not route:
                continue
            if re.search(r"<a\b", line) and not re.search(r"\sdownload[\s>=]", line):
                missing.append(f"{path.name}:{n}")
            if 'h("a"' in line and "download: true" not in line:
                missing.append(f"{path.name}:{n}")
            if re.search(r"location\.href\s*=", line):
                navigated.append(f"{path.name}:{n}")
    assert missing == [], "a download link without download"
    assert navigated == [], "a download by going to it: the window would show it instead (use a download link)"
