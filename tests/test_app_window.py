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


class FakeForm:
    """Windows: pywebview's form (``window.native``), with the WebView2 in it, whose settings pywebview 6.2 left as it
    leaves them without ``debug``: no right-click menu and no browser keys. WebView2 takes its settings on the
    window's own thread only: ``Invoke`` runs what it is given there (a thread of its own here)."""

    def __init__(self) -> None:
        self.settings = SimpleNamespace(
            AreDefaultContextMenusEnabled=False, AreBrowserAcceleratorKeysEnabled=False, AreDevToolsEnabled=False,
            IsStatusBarEnabled=False,
        )
        self.webview = SimpleNamespace(CoreWebView2=SimpleNamespace(Settings=self.settings))
        self.invoked: list[str] = []  # the threads what was given to Invoke ran on
        self.fails: Exception | None = None

    def Invoke(self, delegate):  # noqa: N802 (.NET's name)
        if self.fails is not None:
            raise self.fails
        done: dict = {}

        def on_gui() -> None:
            self.invoked.append(threading.current_thread().name)
            done["result"] = delegate()

        gui = threading.Thread(target=on_gui, name="the window's own thread")
        gui.start()
        gui.join(5)
        return done.get("result")


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
        # macOS (pywebview's Cocoa should_close) reads the question before the closing handlers run; Windows after.
        self.cocoa = False
        self.native = FakeForm()  # Windows: pywebview's form, with the WebView2 in it
        self.webview = None  # the FakeWebview that made it
        # The address the page went to by itself since it was loaded (the workspace moves from page to page without
        # loading one): what get_current_url says then. An exception: the window can't say.
        self.current: str | Exception | None = None

    def get_current_url(self):
        if isinstance(self.current, Exception):
            raise self.current
        if self.current is not None:
            return self.current
        kind, shown = self.shown[-1]
        return shown if kind == "url" else None

    def load_url(self, url) -> None:
        self.events.loaded.clear()
        self.current = None
        self.shown.append(("url", url))
        if self.loads:
            self.events.loaded.set()

    def load_html(self, html) -> None:
        self.events.loaded.clear()
        self.current = None
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

    def person_quits(self, answer: bool = True) -> bool:
        """macOS: Cmd+Q, the Dock's Quit, or logging out (NSApplication's terminate:). pywebview's AppDelegate asks
        should_close, which runs the closing handlers and its "close anyway?"; once that says yes, macOS tells those
        who listen that the app is quitting, then ends the program: the window never closes as a window does, and
        webview.start() never returns. True when it quit."""
        read_first = self.localization["global.quitConfirmation"]
        if self.events.closing.set():
            return False
        if self.confirm_close:
            self.asked.append(read_first if self.cocoa else self.localization["global.quitConfirmation"])
            if not answer:
                return False
        self.closed_by = "quit"
        for told in list(self.webview.quitting):
            told()
        self.webview.quit.set()
        return True

    def _close(self, by: str, *, answer: bool) -> bool:
        if self.gone.is_set():
            return True
        read_first = self.localization["global.quitConfirmation"]
        if self.events.closing.set():
            return False
        if self.confirm_close:
            self.asked.append(read_first if self.cocoa else self.localization["global.quitConfirmation"])
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
        self.quitting: list = []  # told as the app quits (macOS: NSApplicationWillTerminateNotification)
        self.quit = threading.Event()  # macOS ended the program from inside terminate:

    def create_window(self, title, url=None, html=None, js_api=None, **options) -> FakeWindow:
        window = FakeWindow(title, url=url, html=html, js_api=js_api, **options)
        window.webview = self
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
        deadline = time.monotonic() + 30
        while not window.gone.wait(0.01):
            if self.quit.is_set():
                threading.Event().wait()  # the program has ended: nothing after start() runs
            assert time.monotonic() < deadline, "the window was never closed"


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
        self.other = False  # a CloseDesk the window didn't start answers on the port
        self.theirs = False  # another folder's CloseDesk, or another person's, answers on the port
        self.updating = False  # an update runs in CloseDesk's folder (launch.py --update, started elsewhere)
        self.free_from: dict[int, int] = {}  # a port taken: the next one free, as cli._port_to_use finds it
        self.picked: list[int] = []  # the ports the window chose again
        self.popen_server = FakeProcess  # what a CloseDesk the window starts is (SlowToStop: still finishing as it stops)

    def pick_port(self, port: int) -> tuple[int, bool]:
        self.picked.append(port)
        return self.free_from.get(port, port), False

    def popen(self, cmd, **options) -> FakeProcess:
        kind = self.popen_server if "run" in cmd else FakeProcess
        process = kind(cmd, **options)
        self.processes.append(process)
        if "--update" in cmd and self.update_code is not None:
            process.end(self.update_code)
        return process

    def servers(self) -> list[FakeProcess]:
        return [p for p in self.processes if "run" in p.cmd]

    def updates(self) -> list[FakeProcess]:
        return [p for p in self.processes if "--update" in p.cmd]

    def answers(self, url: str) -> bool:
        return self.other or any(p.poll() is None for p in self.servers())


@pytest.fixture
def computer(monkeypatch):
    monkeypatch.setattr(app_window, "NO_WINDOW", NO_WINDOW)
    for name in ("CLOSEDESK_REOPEN", app_window.SMOKE_ENV, app_window.SMOKE_HOLD_ENV, app_window.MAC_APP_PATH):
        monkeypatch.delenv(name, raising=False)
    return Computer()


def make_app(webview, computer, tmp_path, *, owned=True) -> app_window.App:
    app = app_window.App(webview, url=URL, port=8765, owned=owned, log=tmp_path / "CloseDesk-app.log")
    app.popen = computer.popen
    app.answers = computer.answers
    app.working = lambda: computer.busy
    app.open_url = computer.opened.append
    app.open_launcher = lambda why="": computer.launcher.append(why)
    app.update_running = lambda: computer.updating
    app.port_taken = lambda: computer.answers(URL) or computer.theirs
    app.theirs = lambda: computer.theirs
    app.pick_port = computer.pick_port
    app.poll = 0.01
    app.watch = 0.01
    app.gone_for_good = 0.3
    app.other_wait = 0.2
    app.storage = tmp_path / "window"
    app.delegate = lambda fn: fn  # .NET's Func[Type], which Invoke takes on Windows
    app.asked_mark = tmp_path / "update-asked"
    app.when_quitting = webview.quitting.append  # macOS: told as the app quits
    # macOS, CI's check: quit as Cmd+Q does, on the window's own thread (the supervisor goes on meanwhile).
    app.terminate = lambda: threading.Thread(target=lambda: webview.windows[0].person_quits(), daemon=True).start()
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
    assert window.shown[-1][0] == "html" and "Updating CloseDesk" in shown and "opens again by itself" in shown

    update.end(0)
    until(lambda: len(window.urls()) == 2, what="the new version in the window")
    first, second = computer.servers()
    assert first.returncode == updates.UPDATE_AND_RESTART and second.poll() is None
    assert second.options["env"].get("CLOSEDESK_REOPEN") == "1", "on the port the page was on"
    assert "--port" in second.cmd and second.cmd[second.cmd.index("--port") + 1] == "8765"
    window.person_closes()
    thread.join(10)
    assert second.input_closed


def test_only_the_first_closedesk_the_window_starts_is_told_its_environment_was_just_made(computer, tmp_path, monkeypatch):
    # launch.py says so for this start (the shortcut puts back what Windows offered to delete meanwhile); the CloseDesk
    # the window starts again after an update isn't a new environment's.
    from controller_inbox import shortcut

    monkeypatch.setenv(shortcut.NEW_ENVIRONMENT_ENV, "1")
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    computer.servers()[0].end(updates.UPDATE_AND_RESTART)
    until(lambda: len(window.urls()) == 2, what="the new version in the window")
    first, second = computer.servers()
    assert first.options["env"].get(shortcut.NEW_ENVIRONMENT_ENV) == "1"
    assert shortcut.NEW_ENVIRONMENT_ENV not in second.options["env"]
    assert shortcut.NEW_ENVIRONMENT_ENV not in computer.updates()[0].options["env"]
    window.person_closes()
    thread.join(10)


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
    # Windows (and Linux): the window waits for the update. (A Mac's leaves at once, and the update finishes by itself:
    # test_on_a_mac_a_window_closed_during_an_update_leaves_at_once_and_the_update_finishes_by_itself.) Pinned, so the
    # test is the same on CI's macOS leg.
    computer.update_code = None
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    app.platform = "win32"
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


def test_update_now_while_another_update_runs_waits_for_that_one_before_closedesk_opens(computer, tmp_path, capsys):
    # CloseDesk-Update was double-clicked while the window was open, and is downloading. Update now in the window:
    # launch.py --update says another update runs (77) and does nothing. CloseDesk opened at once would run from a
    # folder that update is about to replace: the window says it is updating until that update is done.
    computer.update_code = app_window.UPDATE_UNDER_WAY
    computer.updating = True  # the other update's mark, with its program running
    asked: list[float] = []
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    app.update_running = lambda: asked.append(time.monotonic()) or computer.updating
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    computer.servers()[0].end(updates.UPDATE_AND_RESTART)
    until(lambda: computer.updates(), what="the update")
    until(lambda: len(asked) >= 3, what="the window looking whether the other update is done")
    assert len(computer.servers()) == 1, "CloseDesk started under an update that replaces its folder"
    assert "Updating CloseDesk" in window.htmls()[-1] and window.shown[-1][0] == "html"
    assert window.person_closes(answer=False) is False and window.asked == [app_window.UPDATING_CLOSE]

    computer.updating = False  # the other update is done
    until(lambda: len(computer.servers()) == 2, what="CloseDesk opened again")
    until(lambda: len(window.urls()) == 2, what="the new version in the window")
    assert computer.servers()[1].options["env"].get("CLOSEDESK_REOPEN") == "1"
    assert "Another update" in capsys.readouterr().out
    window.person_closes()
    thread.join(10)


def test_a_window_closed_while_it_waits_for_another_update_doesnt_open_closedesk(computer, tmp_path):
    computer.update_code = app_window.UPDATE_UNDER_WAY
    computer.updating = True
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    app.platform = "win32"
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    computer.servers()[0].end(updates.UPDATE_AND_RESTART)
    until(lambda: computer.updates())
    time.sleep(0.1)
    assert window.person_closes() is True
    thread.join(10)
    assert not thread.is_alive() and len(computer.servers()) == 1, "the other update finishes by itself"
    assert len(computer.updates()) == 1, "CloseDesk had stopped for the update started already: not a second"


def test_waiting_for_another_update_gives_up_in_time(computer, tmp_path, capsys):
    # Something holds that update for good: past the longest an update takes, CloseDesk opens as it is.
    computer.update_code = app_window.UPDATE_UNDER_WAY
    computer.updating = True
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    app.update_longest = 0.4
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    computer.servers()[0].end(updates.UPDATE_AND_RESTART)
    until(lambda: len(computer.servers()) == 2, what="CloseDesk opened as it is")
    assert "still running" in capsys.readouterr().out
    window.person_closes()
    thread.join(10)


def _closed_as_closedesk_stops_to_update(computer, tmp_path, monkeypatch, platform, *, answer=True):
    """Update now answered (CloseDesk writes the window's mark), then the window closed while CloseDesk was still
    stopping: it ends with UPDATE a moment later."""
    computer.update_code = None
    computer.popen_server = SlowToStop
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    app.platform = platform
    thread, result = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    (server,) = computer.servers()
    assert server.options["env"][updates.WINDOW_ASKED_ENV] == str(app.asked_mark)
    app.asked_mark.write_text("1", encoding="utf-8")  # stop_to_update: CloseDesk stops once the answer is sent
    return app, window, server, thread, result


def test_closing_just_after_update_now_asks_first(computer, tmp_path, monkeypatch):
    app, window, server, thread, _ = _closed_as_closedesk_stops_to_update(computer, tmp_path, monkeypatch, "win32")
    assert window.person_closes(answer=False) is False
    assert window.asked == [app_window.UPDATING_CLOSE], "closing dropped the update without asking"
    server.end(updates.UPDATE_AND_RESTART)
    until(lambda: computer.updates(), what="the update")
    window.person_closes()
    computer.updates()[0].end(0)
    thread.join(10)
    assert not thread.is_alive()


def test_on_a_mac_the_question_says_closedesk_updates_before_the_first_close(computer, tmp_path, monkeypatch):
    # macOS reads pywebview's question before the closing handler runs: the window has put it there already.
    app, window, server, thread, _ = _closed_as_closedesk_stops_to_update(computer, tmp_path, monkeypatch, "darwin")
    window.cocoa = True
    until(lambda: window.localization["global.quitConfirmation"] == app_window.UPDATING_CLOSE, what="the question")
    assert window.person_closes(answer=False) is False and window.asked == [app_window.UPDATING_CLOSE]
    server.end(updates.UPDATE_AND_RESTART)
    until(lambda: computer.updates())
    window.person_closes()
    thread.join(10)
    assert not thread.is_alive()


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_a_window_closed_before_closedesk_has_stopped_to_update_still_runs_the_update(computer, tmp_path, monkeypatch, platform):
    # Closed (Close, or Cmd+Q) between Update now's answer and CloseDesk's exit code: the update the person asked for
    # runs all the same, a program of its own, and the next CloseDesk opens as the new version.
    monkeypatch.setattr(app_window, "STOP_WAIT", 5)
    app, window, server, thread, result = _closed_as_closedesk_stops_to_update(computer, tmp_path, monkeypatch, platform)
    assert window.person_closes() is True
    assert computer.updates() == [] and thread.is_alive(), "the window waits for CloseDesk to stop"
    time.sleep(0.1)
    server.end(updates.UPDATE_AND_RESTART)
    until(lambda: computer.updates(), what="the update the person asked for")
    (update,) = computer.updates()
    assert update.cmd == [sys.executable, str(app_window.ROOT / "scripts" / "launch.py"), "--update"]
    assert update.options["creationflags"] == NO_WINDOW and update.options["start_new_session"] is (os.name != "nt")
    assert updates.WINDOW_ASKED_ENV not in update.options["env"]
    if platform == "darwin":
        thread.join(10)
        assert not thread.is_alive() and update.poll() is None, "a Mac's window leaves once the update has started"
    else:
        time.sleep(0.1)
        assert thread.is_alive(), "elsewhere the window waits for the update, as it does for one closed while it runs"
        update.end(0)
        thread.join(10)
    assert result["code"] == 0 and len(computer.servers()) == 1, "closed: CloseDesk isn't opened again"
    assert not app.asked_mark.exists()


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_a_second_update_now_closed_before_closedesk_stopped_still_runs_the_update(computer, tmp_path, monkeypatch, platform):
    # The first update in this window didn't finish (offline), and CloseDesk opened again as it was. Later Update now
    # again, and the window closed while CloseDesk stopped for it: the finished first update, still the window's,
    # made it look as if this one had been started already, and it was dropped.
    monkeypatch.setattr(app_window, "STOP_WAIT", 5)
    computer.update_code = 1
    computer.popen_server = SlowToStop
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    app.platform = platform
    thread, result = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    first = computer.servers()[0]
    app.asked_mark.write_text("1", encoding="utf-8")
    first.end(updates.UPDATE_AND_RESTART)
    until(lambda: len(computer.servers()) == 2, what="CloseDesk opened again, as it was")
    second = computer.servers()[1]
    until(lambda: len(window.urls()) == 2, what="CloseDesk shown again")
    computer.update_code = None
    app.asked_mark.write_text("1", encoding="utf-8")  # Update now, from Settings this time
    assert window.person_closes() is True
    time.sleep(0.1)
    second.end(updates.UPDATE_AND_RESTART)
    until(lambda: len(computer.updates()) == 2, what="the update asked for the second time")
    if platform == "win32":
        computer.updates()[1].end(0)
    thread.join(10)
    assert not thread.is_alive() and result["code"] == 0 and len(computer.servers()) == 2


def test_closing_after_an_update_and_before_closedesk_opens_again_starts_no_second_update(computer, tmp_path):
    # The update is done, and CloseDesk not started again yet: closing then isn't a second Update now.
    computer.update_code = 0
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    app.platform = "win32"
    started = threading.Event()
    go_on = threading.Event()
    popen = app.popen

    def slow_to_start(cmd, **options):
        if "run" in cmd and computer.servers():
            started.set()
            go_on.wait(10)
        return popen(cmd, **options)

    app.popen = slow_to_start
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    computer.servers()[0].end(updates.UPDATE_AND_RESTART)
    assert started.wait(10)
    stopping = threading.Thread(target=app.stop, daemon=True)
    stopping.start()
    stopping.join(5)
    go_on.set()
    window.person_closes()
    thread.join(10)
    assert len(computer.updates()) == 1


def test_a_closedesk_that_ended_with_update_as_the_window_closed_is_updated(computer, tmp_path):
    # No mark (Update now pressed in a browser tab onto the window's CloseDesk), and the window closed: CloseDesk,
    # asked to stop, ends with UPDATE all the same; the update runs.
    computer.update_code = None
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    app.platform = "win32"
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    (server,) = computer.servers()
    server.stdin = SimpleNamespace(close=lambda: server.end(updates.UPDATE_AND_RESTART))
    window.person_closes()
    until(lambda: computer.updates(), what="the update")
    computer.updates()[0].end(0)
    thread.join(10)
    assert not thread.is_alive() and len(computer.servers()) == 1


def test_a_mark_left_from_before_isnt_taken_for_update_now(computer, tmp_path):
    # Each CloseDesk the window starts begins without one.
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    app.asked_mark.write_text("1", encoding="utf-8")
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    assert not app.asked_mark.exists()
    assert window.person_closes() is True and window.asked == []
    thread.join(10)


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


def test_a_second_double_click_that_races_the_first_shows_the_first(computer, tmp_path):
    # Both windows found the port free; the second's CloseDesk then found the first's there and ended. The second window
    # shows the first's CloseDesk, as if it had been open already, and closing it leaves that one open.
    others_up = {"now": False}

    def popen(cmd, **options):
        process = computer.popen(cmd, **options)
        others_up["now"] = True  # the first double-click's CloseDesk answers on the port
        process.end(0)  # "CloseDesk is already open in another window"
        return process

    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    app.popen = popen
    app.answers = lambda url: others_up["now"]
    thread, result = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls(), what="the first CloseDesk in the second window")
    assert app.owned is False and len(computer.servers()) == 1
    computer.busy = True
    assert window.person_closes() is True and window.asked == []
    thread.join(10)
    assert result["code"] == 0


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
    # open_launcher sets the reason itself: set, then removed, so that it is undone after the test.
    monkeypatch.setenv(app_window.WHY_ENV, "")
    monkeypatch.delenv(app_window.WHY_ENV)
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


# On Windows the window works as a browser does: its right-click menu and the browser's own keys ---------------------


def test_on_windows_the_right_click_menu_and_the_browsers_keys_are_on_once_the_window_shows(computer, tmp_path):
    # pywebview 6.2 turns off WebView2's right-click menu (Copy an IBAN, Paste in the chat, spelling) and its browser
    # keys (Ctrl+F find, Ctrl+P, F5, Ctrl+Plus/Minus/0 zoom, Alt+Left back) unless it runs for debugging, which would
    # open the developer tools too. The window turns the two back on, on the window's own thread (WebView2 takes nothing
    # from another), and leaves the developer tools off.
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    app.platform = "win32"
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    settings = window.native.settings
    assert settings.AreDefaultContextMenusEnabled is True and settings.AreBrowserAcceleratorKeysEnabled is True
    assert settings.AreDevToolsEnabled is False and settings.IsStatusBarEnabled is False
    assert window.native.invoked and set(window.native.invoked) == {"the window's own thread"}
    window.person_closes()
    thread.join(10)


def test_the_right_click_menu_is_turned_on_before_closedesk_is_shown(computer, tmp_path):
    # Its first page is the window's own "Opening CloseDesk": on by the time CloseDesk's page loads (WebView2 reads some
    # settings at the next page).
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    app.platform = "win32"
    on_when_shown: list[bool] = []
    window_load = FakeWindow.load_url

    def load_url(self, url) -> None:
        on_when_shown.append(self.native.settings.AreDefaultContextMenusEnabled)
        window_load(self, url)

    FakeWindow.load_url = load_url
    try:
        thread, _ = running(app)
        window = window_of(webview)
        until(lambda: URL in window.urls())
    finally:
        FakeWindow.load_url = window_load
    assert on_when_shown == [True]
    window.person_closes()
    thread.join(10)


def test_a_right_click_menu_that_cant_be_turned_on_leaves_the_window_open(computer, tmp_path, capsys):
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    app.platform = "win32"
    webview_create = webview.create_window

    def create_window(*args, **options):
        window = webview_create(*args, **options)
        window.native.fails = RuntimeError("InvalidOperationException")
        return window

    webview.create_window = create_window
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    assert thread.is_alive() and "right-click menu" in capsys.readouterr().out
    window.person_closes()
    thread.join(10)


def test_elsewhere_the_window_leaves_its_engines_menu_and_keys_as_they_are(computer, tmp_path):
    # macOS's WebKit window has its right-click menu and keys already; Linux has no window.
    for platform in ("darwin", "linux"):
        webview = FakeWebview(renderer="wkwebview")
        app = make_app(webview, computer, tmp_path)
        app.platform = platform
        thread, _ = running(app)
        window = window_of(webview)
        until(lambda: URL in window.urls())
        window.person_closes()
        thread.join(10)
        assert window.native.invoked == [] and window.native.settings.AreDefaultContextMenusEnabled is False


def test_in_the_smoke_check_on_windows_the_window_says_its_menu_and_keys_are_on(computer, tmp_path, monkeypatch, capsys):
    # CI's check of the real window reads both back from WebView2 once CloseDesk's page shows, and looks for this line.
    monkeypatch.setenv(app_window.SMOKE_ENV, "5")
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    app.platform = "win32"
    thread, result = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    window.js_api.page_ready("/app")
    thread.join(10)
    out = capsys.readouterr().out
    assert result["code"] == 0 and app_window.BROWSER_KEYS_ON in out
    smoke = (ROOT / "tests" / "launcher_smoke.py").read_text(encoding="utf-8")
    assert app_window.BROWSER_KEYS_ON in smoke


def test_in_the_smoke_check_a_menu_turned_off_again_is_said(computer, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(app_window.SMOKE_ENV, "5")
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    app.platform = "win32"
    thread, result = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    window.native.settings.AreBrowserAcceleratorKeysEnabled = False  # as if a page load had put it back
    window.js_api.page_ready("/app")
    thread.join(10)
    out = capsys.readouterr().out
    assert app_window.BROWSER_KEYS_ON not in out and "browser keys off" in out


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


def test_in_the_smoke_check_the_window_says_which_port_its_page_came_from(computer, tmp_path, monkeypatch, capsys):
    # CI's check of a window onto a CloseDesk already open has to tell that one from a second the window started.
    monkeypatch.setenv(app_window.SMOKE_ENV, "5")
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    thread, result = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    window.js_api.page_ready("/app", "8765")
    thread.join(10)
    assert result["code"] == 0
    assert "The CloseDesk window showed /app (port 8765)\n" in capsys.readouterr().out
    script = (ROOT / "src" / "controller_inbox" / "static" / "ui" / "in_window.js").read_text(encoding="utf-8")
    assert "page_ready(window.location.pathname, window.location.port)" in script


def test_in_the_smoke_check_a_page_that_never_says_it_loaded_fails(computer, tmp_path, monkeypatch):
    monkeypatch.setenv(app_window.SMOKE_ENV, "0.2")
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    thread, result = running(app)
    thread.join(10)
    assert result["code"] == 1


def test_in_the_smoke_check_the_window_stays_open_as_long_as_asked_once_the_page_loaded(computer, tmp_path, monkeypatch):
    # CI looks at the open window from outside before it closes: what macOS calls it (lsappinfo), a screenshot.
    monkeypatch.setenv(app_window.SMOKE_ENV, "5")
    monkeypatch.setenv(app_window.SMOKE_HOLD_ENV, "0.5")
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    thread, result = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    ready = time.monotonic()
    window.js_api.page_ready("/app")
    until(window.gone.is_set, what="the window closed")
    assert time.monotonic() - ready >= 0.45
    thread.join(10)
    assert result["code"] == 0 and window.closed_by == "CloseDesk" and computer.servers()[0].input_closed


def test_in_the_smoke_check_a_window_closed_while_it_is_held_open_is_not_closed_again(computer, tmp_path, monkeypatch):
    monkeypatch.setenv(app_window.SMOKE_ENV, "5")
    monkeypatch.setenv(app_window.SMOKE_HOLD_ENV, "30")
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    window.js_api.page_ready("/app")
    time.sleep(0.1)
    window.person_closes()
    thread.join(10)
    assert not thread.is_alive() and window.closed_by == "person"


def test_in_the_smoke_check_update_now_is_followed_through_to_the_new_version(computer, tmp_path, monkeypatch, capsys):
    # CI presses Update now while the window is held open: the window updates, shows the new version, and closes
    # itself once that page loaded (held open the first time only).
    monkeypatch.setenv(app_window.SMOKE_ENV, "5")
    monkeypatch.setenv(app_window.SMOKE_HOLD_ENV, "30")
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    thread, result = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    window.js_api.page_ready("/")
    until(lambda: URL + "app" in window.urls(), what="the workspace")
    window.js_api.page_ready("/app")
    time.sleep(0.1)
    computer.servers()[0].end(updates.UPDATE_AND_RESTART)  # Update now
    until(lambda: len(window.urls()) == 3, what="the new version in the window")
    assert window.urls()[-1] == URL + "app", "the page it showed: the workspace"
    assert len(computer.updates()) == 1 and not window.gone.is_set(), "not closed before the new version's page loaded"
    started = time.monotonic()
    window.js_api.page_ready("/app")
    # The other UI too, so that both run in the real window's engine as the new version.
    until(lambda: len(window.urls()) == 4, what="the classic page too")
    assert window.urls()[-1] == URL
    window.js_api.page_ready("/")
    thread.join(10)
    assert time.monotonic() - started < 5, "the new version's page isn't held open"
    assert result["code"] == 0 and window.closed_by == "CloseDesk"
    said = capsys.readouterr().out
    assert said.count("The CloseDesk window showed /app") == 2 and said.count("The CloseDesk window showed /\n") == 2


def test_in_the_smoke_check_closedesk_stopping_by_itself_fails_and_closes_the_window(computer, tmp_path, monkeypatch):
    # The check must end: a window left saying "CloseDesk stopped" would keep it waiting until its time ran out.
    monkeypatch.setenv(app_window.SMOKE_ENV, "5")
    monkeypatch.setenv(app_window.SMOKE_HOLD_ENV, "30")
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    thread, result = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    window.js_api.page_ready("/app")
    time.sleep(0.1)
    computer.servers()[0].end(1)
    thread.join(10)
    assert result["code"] == 1 and window.closed_by == "CloseDesk" and "CloseDesk stopped" in window.htmls()[-1]


def test_in_the_smoke_check_closedesk_that_never_opens_fails_and_closes_the_window(computer, tmp_path, monkeypatch):
    monkeypatch.setenv(app_window.SMOKE_ENV, "5")
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    app.answers = lambda url: False
    app.up_wait = 0.2
    thread, result = running(app)
    thread.join(10)
    assert result["code"] == 1 and window_of(webview).closed_by == "CloseDesk"


def test_in_the_smoke_check_a_window_that_cant_open_fails_and_says_why(monkeypatch, tmp_path, capsys):
    # CI checks the window itself: the browser instead (and a message box no one is there to close) would hide that it
    # didn't open, and keep the check waiting.
    monkeypatch.setenv(app_window.SMOKE_ENV, "60")
    monkeypatch.setattr(app_window, "tell", lambda text, **_: pytest.fail("no message box in the check"))
    monkeypatch.setattr(app_window, "open_launcher", lambda why="", **_: pytest.fail("no browser in the check"))
    monkeypatch.setattr(app_window, "run_here", lambda *a, **k: pytest.fail("no browser in the check"))
    monkeypatch.setattr(app_window, "user_dir", lambda: tmp_path)
    monkeypatch.setattr(app_window, "_console", lambda: False)
    for platform in ("win32", "darwin", "linux"):
        reason = ("webview2", app_window.NO_WEBVIEW2)
        assert app_window.browser_instead(reason, SimpleNamespace(host=None, port=None), platform=platform) == 2
    assert app_window.NO_WEBVIEW2 in capsys.readouterr().out


def test_in_the_smoke_check_nothing_waits_for_a_click(monkeypatch, capsys):
    monkeypatch.setenv(app_window.SMOKE_ENV, "60")
    monkeypatch.setattr(app_window.subprocess, "run", lambda *a, **k: pytest.fail("no alert in the check"))
    app_window.tell("CloseDesk can't start yet", platform="darwin")
    assert "CloseDesk can't start yet" in capsys.readouterr().out, "still in the log"


# What macOS calls the window (the Dock, the menu bar) -------------------------------------------------------------------


def fake_appkit(name="CloseDesk", bundle="io.github.mnabikhel.closedesk"):
    current = SimpleNamespace(localizedName=lambda: name, bundleIdentifier=lambda: bundle)
    return SimpleNamespace(NSRunningApplication=SimpleNamespace(currentApplication=lambda: current))


def test_on_a_mac_the_log_says_what_the_dock_calls_the_window(computer, tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "AppKit", fake_appkit())
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    app.platform = "darwin"
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    window.person_closes()
    thread.join(10)
    assert "macOS shows the window as CloseDesk (io.github.mnabikhel.closedesk)." in capsys.readouterr().out


def test_the_name_macos_gives_the_window_is_said_as_it_is_or_not_at_all(monkeypatch):
    monkeypatch.setitem(sys.modules, "AppKit", fake_appkit("Python", "org.python.python"))
    assert app_window.mac_app_name() == "Python (org.python.python)"
    monkeypatch.setitem(sys.modules, "AppKit", None)  # import fails
    assert app_window.mac_app_name() == ""


def test_elsewhere_nothing_is_asked_of_macos(computer, tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "AppKit", fake_appkit())
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    app.platform = "win32"
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    window.person_closes()
    thread.join(10)
    assert "macOS shows" not in capsys.readouterr().out


def test_what_the_window_starts_isnt_given_the_windows_name_on_a_mac(computer, tmp_path, monkeypatch):
    # launch.py starts the window's Python as CloseDesk.app (CFProcessPath, which macOS reads for the app's name and
    # icon). CloseDesk itself and the update are plain Python programs, and the launcher a Terminal window.
    monkeypatch.setenv(app_window.MAC_APP_PATH, "/Users/x/Applications/CloseDesk.app/Contents/MacOS/CloseDesk-python")
    computer.update_code = 0
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    computer.servers()[0].end(updates.UPDATE_AND_RESTART)
    until(lambda: len(window.urls()) == 2, what="the new version in the window")
    window.person_closes()
    thread.join(10)
    for process in computer.processes:
        assert app_window.MAC_APP_PATH not in process.options["env"], process.cmd
    started: list = []
    app_window.open_launcher("", platform="darwin", run=lambda cmd: started.append(dict(os.environ)))
    assert app_window.MAC_APP_PATH not in started[0]


def _mac_bundle(monkeypatch, bundle: str) -> None:
    monkeypatch.setitem(sys.modules, "Foundation", SimpleNamespace(
        NSBundle=SimpleNamespace(mainBundle=lambda: SimpleNamespace(bundleIdentifier=lambda: bundle))))


def _icon_given(computer, tmp_path, *, platform: str):
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    app.platform = platform
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    window.person_closes()
    thread.join(10)
    return webview.started["icon"]


def test_on_a_mac_running_as_closedesk_app_the_dock_keeps_the_apps_own_icon(computer, tmp_path, monkeypatch):
    # pywebview puts the icon it is given on the Dock for as long as CloseDesk is open. CloseDesk.app's own icon is the
    # one made for this macOS (filled to its edges on 26, where clear edges get a grey tile): nothing takes its place.
    _mac_bundle(monkeypatch, app_window.BUNDLE_ID)
    assert _icon_given(computer, tmp_path, platform="darwin") is None


def test_on_a_mac_running_as_python_the_window_gives_the_icon_for_this_macos(computer, tmp_path, monkeypatch):
    # Started without CloseDesk.app's link (the Dock says Python): the icon pywebview shows is the one for this macOS.
    _mac_bundle(monkeypatch, "org.python.python")
    monkeypatch.setattr(app_window, "macos_version", lambda: (26, 1))
    assert Path(_icon_given(computer, tmp_path, platform="darwin")).name == "closedesk-full.icns"
    monkeypatch.setattr(app_window, "macos_version", lambda: (15, 6))
    assert Path(_icon_given(computer, tmp_path, platform="darwin")).name == "closedesk.icns"
    assert Path(_icon_given(computer, tmp_path, platform="win32")).name == "closedesk.ico"


def test_the_window_and_closedesk_app_pick_the_same_icon_for_each_macos():
    from controller_inbox import shortcut

    for macos, name in (((26, 0), "closedesk-full.icns"), ((27, 2), "closedesk-full.icns"), ((15, 7), "closedesk.icns"), ((12,), "closedesk.icns")):
        assert Path(app_window.icon_path(platform="darwin", macos=macos)).name == name
        app = shortcut.Mac(ROOT, apps=ROOT / "nowhere", home=ROOT, macos=macos, interpreter=None, register=lambda app: None)
        assert app.icon() == Path(app_window.icon_path(platform="darwin", macos=macos)).read_bytes(), macos


def test_on_a_mac_the_window_takes_its_name_then_passes_it_on_to_nothing(monkeypatch):
    # macOS reads where the program was started from once, the first time it is asked which app this is: asked here,
    # then the variable goes, so nothing the window runs (an alert, the browser, CloseDesk itself) takes CloseDesk.app's
    # name.
    asked: list = []
    monkeypatch.setitem(sys.modules, "Foundation", SimpleNamespace(
        NSBundle=SimpleNamespace(mainBundle=lambda: asked.append(os.environ.get(app_window.MAC_APP_PATH)))))
    monkeypatch.setenv(app_window.MAC_APP_PATH, "/Applications/CloseDesk.app/Contents/MacOS/CloseDesk-python")
    app_window.settle_mac_name(platform="darwin")
    assert asked == ["/Applications/CloseDesk.app/Contents/MacOS/CloseDesk-python"]
    assert app_window.MAC_APP_PATH not in os.environ
    monkeypatch.setenv(app_window.MAC_APP_PATH, "x")
    monkeypatch.setitem(sys.modules, "Foundation", None)  # can't be asked: the variable goes all the same
    app_window.settle_mac_name(platform="darwin")
    assert app_window.MAC_APP_PATH not in os.environ
    monkeypatch.setenv(app_window.MAC_APP_PATH, "x")
    app_window.settle_mac_name(platform="win32")
    assert os.environ[app_window.MAC_APP_PATH] == "x", "only a Mac's"


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
    # Started by CloseDesk.bat or CloseDesk.command: the launcher's window that showed the download may be closed (it
    # was closed during the update, say), and the one open now says nothing of it: no window to point to, but how to
    # try again, where what stops it is shown.
    said = updates.unfinished_message()
    assert "window says what went wrong" not in said
    assert "CloseDesk-Update" in said and "Update in Settings" in said
    monkeypatch.setenv(updates.APP_ENV, "1")
    monkeypatch.setenv(updates.APP_LOG_ENV, str(tmp_path / "CloseDesk-app.log"))
    said = updates.unfinished_message()
    assert str(tmp_path / "CloseDesk-app.log") in said and "window says" not in said
    assert question is not None


# CloseDesk started by the window stops when the window's process ends --------------------------------------------------


def test_closedesk_stops_when_its_input_ends():
    read, write = os.pipe()
    stopped = threading.Event()
    try:
        thread = threading.Thread(target=cli._stop_when_input_ends, args=(read, stopped.set), daemon=True)
        thread.start()
        time.sleep(0.1)
        assert not stopped.is_set()
        os.close(write)
        assert stopped.wait(5)
    finally:
        os.close(read)


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


def test_closedesk_started_by_the_window_that_stops_by_itself_ends_with_its_own_exit_code(tmp_path):
    # Update now stops CloseDesk with UPDATE (75) for the window to update it, while the window still holds its input
    # open. A thread left waiting inside Python's own reader of that input made Python end with "could not acquire lock
    # for <stdin> at interpreter shutdown" (an abort, -6), not 75: the window said "CloseDesk stopped" instead of
    # updating.
    import textwrap

    script = tmp_path / "serve.py"
    script.write_text(textwrap.dedent("""
        import socket, sys, threading, time
        from fastapi import FastAPI
        from controller_inbox import cli

        app = FastAPI()
        port = int(sys.argv[1])

        def update_now():  # once CloseDesk is open, as Update now stops it
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=1):
                        break
                except OSError:
                    time.sleep(0.1)
            app.state.stop_server()

        threading.Thread(target=update_now, daemon=True).start()
        cli._run_server(app, host="127.0.0.1", port=port)
        sys.exit(75)
    """), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), app_window.STOP_WITH_PARENT: "1"}
    server = subprocess.Popen(
        [sys.executable, str(script), str(_free_port())], cwd=tmp_path, env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        code = server.wait(60)  # its input still open, as the window holds it
        said = server.stdout.read().decode(errors="replace")
    finally:
        if server.poll() is None:
            server.kill()
            server.wait()
        server.stdin.close()
        server.stdout.close()
    assert "Fatal Python error" not in said, said
    assert code == 75, said


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


@pytest.mark.parametrize("code", [0, 1])
def test_the_window_says_how_it_closed_as_its_last_line(monkeypatch, capsys, code):
    # Opened by a double-click, nothing else sees how the window's program ended: CI's check read only that both pages
    # had shown and the port was free, and passed a window that ended with an error.
    left: list = []
    monkeypatch.setattr(app_window, "why_no_window", lambda: None)
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace())
    monkeypatch.setattr(app_window, "prepare", lambda args, webview: SimpleNamespace(run=lambda: code, fallback=None))
    monkeypatch.setattr(app_window, "_leave", left.append)
    monkeypatch.setattr(app_window, "set_app_id", lambda: None)
    app_window._open(SimpleNamespace(no_browser=False, host=None, port=None))
    assert left == [code]
    assert capsys.readouterr().out.rstrip().splitlines()[-1] == app_window.CLOSED.format(code=code) == (
        f"The CloseDesk window closed (exit code {code}).")


def test_the_app_without_a_browser_only_serves(monkeypatch):
    # launch.py --app --no-browser: the launcher check in CI runs the app's whole start (pythonw, the log, no console
    # windows) with no window and no browser, as --no-browser means for the double-click launchers.
    ran: list = []
    monkeypatch.setattr(cli, "main", lambda argv: ran.append(argv) or 0)
    monkeypatch.setattr(app_window, "why_no_window", lambda **_: pytest.fail("no window is asked for"))
    monkeypatch.setenv(updates.LAUNCHER_ENV, "1")
    assert app_window.main(SimpleNamespace(no_browser=True, host=None, port=8813)) == 0
    assert ran == [["run", "--no-browser", "--port", "8813"]]
    assert os.environ.get(updates.LAUNCHER_ENV) is None, "nothing here starts CloseDesk again after an update"


def test_a_smoke_wait_that_isnt_a_number_is_no_smoke_check(computer, tmp_path, monkeypatch):
    monkeypatch.setenv(app_window.SMOKE_ENV, "soon")
    monkeypatch.setenv(app_window.SMOKE_HOLD_ENV, "a while")
    app = make_app(FakeWebview(), computer, tmp_path)
    assert app.smoke == 0 and app.smoke_hold == 0


def test_a_storage_folder_that_cant_be_made_leaves_the_window_its_default(computer, tmp_path):
    # pywebview refuses to start at all with a storage folder it can't write: the pages' settings would be lost, not
    # the window.
    (tmp_path / "a-file").write_text("")
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    app.storage = tmp_path / "a-file" / "window"
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    assert webview.started["storage_path"] is None
    window.person_closes()
    thread.join(10)


# What goes wrong as the window opens is said, once --------------------------------------------------------------------


def test_what_stops_the_window_opening_is_said_once_and_the_launcher_says_nothing_more(monkeypatch, settings):
    # Said in a message box by the window itself: its exit code tells launch.py so (SAID), which otherwise says it.
    told: list = []
    monkeypatch.setattr(app_window, "tell", lambda text, **_: told.append(text))
    monkeypatch.setattr(app_window, "why_no_window", lambda **_: None)
    monkeypatch.setitem(sys.modules, "webview", FakeWebview())
    monkeypatch.setattr(app_window, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "_port_to_use", lambda host, port: (0, False))
    assert app_window.main(SimpleNamespace(no_browser=False, host=None, port=None)) == app_window.SAID
    assert len(told) == 1 and "port" in told[0]


def test_an_error_before_the_window_opens_is_said_in_a_message_box(monkeypatch, tmp_path):
    told: list = []
    monkeypatch.setattr(app_window, "tell", lambda text, **_: told.append(text))
    monkeypatch.setattr(app_window, "why_no_window", lambda **_: None)
    monkeypatch.setitem(sys.modules, "webview", FakeWebview())
    monkeypatch.setenv(updates.APP_LOG_ENV, str(tmp_path / "CloseDesk-app.log"))

    def broken(args, webview):
        raise RuntimeError("create_window failed")

    monkeypatch.setattr(app_window, "prepare", broken)
    assert app_window.main(SimpleNamespace(no_browser=False, host=None, port=None)) == app_window.SAID
    assert len(told) == 1 and "couldn't open" in told[0] and str(tmp_path / "CloseDesk-app.log") in told[0]


def test_a_part_of_python_that_wont_load_is_said_before_any_of_closedesk_runs(tmp_path):
    # A company's policy (Smart App Control, WDAC) blocks a compiled part CloseDesk needs: Python ends while importing
    # it, before the window's own code. Said in a message box, with the log that has the details.
    blocked = tmp_path / "blocked" / "pydantic"
    blocked.mkdir(parents=True)
    (blocked / "__init__.py").write_text(
        'raise ImportError("DLL load failed while importing _pydantic_core: An Application Control policy has blocked this file.")\n',
        encoding="utf-8")
    log = tmp_path / "CloseDesk-app.log"
    # As in CI's check of the window: what would be a message box (an alert on a Mac) is in the log only, since no one
    # is at this computer to close it. (The box itself: the next test.)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(tmp_path / "blocked"), str(ROOT / "src")]),
           updates.APP_ENV: "1", updates.APP_LOG_ENV: str(log), app_window.SMOKE_ENV: "1"}

    def logged(since: int = 0) -> str:
        return log.read_bytes()[since:].decode("utf-8", errors="replace") if log.exists() else ""

    done = subprocess.run([sys.executable, "-m", "controller_inbox", "app"], cwd=tmp_path, env=env, capture_output=True,
                          text=True, timeout=120)
    # On Windows the app's program writes to its log, not its output (app_log.write_at_end).
    said = done.stdout + done.stderr + logged()
    assert done.returncode == app_window.SAID, said
    assert "CloseDesk couldn't open" in said and str(log) in said and "Application Control policy" in said
    # Anything else isn't the window's to say: as before.
    before = len(log.read_bytes()) if log.exists() else 0
    done = subprocess.run([sys.executable, "-m", "controller_inbox", "doctor"], cwd=tmp_path, env=env, capture_output=True,
                          text=True, timeout=120)
    assert done.returncode == 1 and "CloseDesk couldn't open" not in done.stdout + done.stderr + logged(before)


def test_what_stops_the_window_loading_is_said_in_windows_message_box_or_a_macs_alert(monkeypatch, capsys):
    # The box itself, with Windows' and macOS's parts standing in: the words as they are, named CloseDesk.
    import ctypes

    from controller_inbox import app_log

    monkeypatch.delenv(app_window.SMOKE_ENV, raising=False)
    boxes: list = []
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=SimpleNamespace(
        MessageBoxW=lambda owner, text, title, kind: boxes.append((text, title)) or 1)), raising=False)
    assert app_log.cant_open(platform="win32") == app_window.SAID
    (text, title), = boxes
    assert title == "CloseDesk" and "CloseDesk couldn't open" in text and "CloseDesk.bat" in text
    alerts: list = []
    monkeypatch.setattr(app_log.subprocess, "run", lambda cmd, **kwargs: alerts.append(cmd))
    assert app_log.cant_open(platform="darwin") == app_window.SAID
    (cmd,) = alerts
    assert cmd[0] == "/usr/bin/osascript" and "CloseDesk.command" in cmd[-1], "the words passed as they are, never into the script"
    # In CI's check of the window, neither: nobody would close it.
    monkeypatch.setenv(app_window.SMOKE_ENV, "1")
    app_log.cant_open(platform="win32")
    app_log.cant_open(platform="darwin")
    assert len(boxes) == 1 and len(alerts) == 1
    assert capsys.readouterr().out.count("CloseDesk couldn't open") == 4, "said in the log every time"


# The app's log: two CloseDesk windows write at its end, never over each other's lines --------------------------------


def test_a_program_the_window_starts_writes_at_the_end_of_the_log_not_where_it_was_passed_on(tmp_path):
    # Windows passes on the open log, not the way it was opened: a program writing to it as passed on writes where the
    # one that passed it last wrote, over a second CloseDesk window's lines. (A file opened for reading and writing
    # stands in for that here.) Opened again for adding at its end, it can't.
    log = tmp_path / "CloseDesk-app.log"
    log.write_text("the first window's line\nthe second window's line\n", encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), updates.APP_ENV: "1", updates.APP_LOG_ENV: str(log)}
    code = "from controller_inbox import app_log; app_log.write_at_end(platform='win32'); print('CloseDesk started')"
    with log.open("r+", encoding="utf-8") as passed_on:  # at the start, as an inherited file can be
        subprocess.run([sys.executable, "-c", code], stdout=passed_on, stderr=subprocess.STDOUT, env=env, timeout=60, check=True)
    assert log.read_text(encoding="utf-8") == "the first window's line\nthe second window's line\nCloseDesk started\n"


# A window onto a CloseDesk it didn't start ---------------------------------------------------------------------------


def test_a_window_onto_another_closedesk_starts_its_own_when_that_one_is_closed(computer, tmp_path):
    # The shortcut opened a second window onto the first one's CloseDesk, then the first window was closed: the second
    # went on showing a CloseDesk that wasn't there, with nothing to say so.
    computer.other = True
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path, owned=False)
    thread, result = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    assert computer.processes == []
    computer.other = False  # the other window closed, and its CloseDesk with it
    until(lambda: computer.servers(), what="CloseDesk started by this window")
    until(lambda: len(window.urls()) == 2, what="CloseDesk shown again")
    (server,) = computer.servers()
    assert app.owned is True and server.cmd[server.cmd.index("--port") + 1] == "8765", "on the port the page was on"
    assert window.person_closes() is True
    thread.join(10)
    assert result["code"] == 0 and server.input_closed, "it is this window's now: closing it stops it"


def test_a_window_onto_closedesk_never_shows_another_persons_or_folders_that_takes_the_port(computer, tmp_path):
    # The CloseDesk the window showed closed, and another person's (a shared computer) or another folder's opened on
    # the same port: the window went on showing what that one served, as if it were this one.
    computer.other = True
    computer.free_from = {8765: 8766}
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path, owned=False)
    thread, result = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    computer.other, computer.theirs = False, True
    until(lambda: computer.servers(), what="this CloseDesk started on a port of its own")
    (server,) = computer.servers()
    assert server.cmd[server.cmd.index("--port") + 1] == "8766" and app.url == "http://127.0.0.1:8766/"
    until(lambda: "http://127.0.0.1:8766/" in window.urls(), what="this CloseDesk shown")
    assert window.urls() == [URL, "http://127.0.0.1:8766/"], "the other one's page was never shown again"
    assert window.person_closes() is True
    thread.join(10)
    assert result["code"] == 0


def test_a_window_doesnt_wait_on_another_persons_closedesk_that_took_the_port_as_this_one_started(computer, tmp_path):
    # Another person's CloseDesk opened on the port between the shortcut choosing it and CloseDesk starting: CloseDesk
    # ended (the port isn't the window's now), and the window chose another and started it there.
    def popen(cmd, **options):
        process = computer.popen(cmd, **options)
        if len(computer.servers()) == 1:
            computer.theirs = True
            process.end(app_window.PORT_TAKEN)
        return process

    computer.free_from = {8765: 8766}
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    app.popen = popen
    app.other_wait = 30
    thread, result = running(app)
    window = window_of(webview)
    until(lambda: len(computer.servers()) == 2, timeout=5, what="CloseDesk started again, on another port")
    first, second = computer.servers()
    assert first.options["env"][app_window.EXACT_PORT] == "1"
    assert second.cmd[second.cmd.index("--port") + 1] == "8766"
    until(lambda: "http://127.0.0.1:8766/" in window.urls())
    assert URL not in window.urls()
    window.person_closes()
    thread.join(10)
    assert result["code"] == 0


def test_closedesk_started_by_the_window_opens_on_its_port_or_not_at_all(settings, store, monkeypatch, capsys):
    monkeypatch.setenv(app_window.EXACT_PORT, "1")
    monkeypatch.setattr(cli, "_port_to_use", lambda host, port: (port + 1, False))
    served = []
    monkeypatch.setattr(cli, "_serve", lambda *a, **k: served.append(1) or 0)
    args = SimpleNamespace(no_serve=False, no_browser=True, limit=0, host="127.0.0.1", port=8765)
    assert cli._run(settings, store, args) == app_window.PORT_TAKEN and served == []
    assert app_window.EXACT_PORT not in os.environ
    monkeypatch.setattr(cli, "_port_to_use", lambda host, port: (port + 1, False))
    assert cli._run(settings, store, args) == 0 and served == [1], "started another way, it moves on as before"


def test_the_window_shows_only_this_folders_closedesk_run_by_this_person(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import json

    from controller_inbox import version

    said: dict = {}

    class Health(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps(said).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/"
    try:
        said.update(app="closedesk", **version.whose())
        assert app_window._answers(url) is True and app_window._theirs(url) is False
        said.update(user="0" * 16)
        assert app_window._answers(url) is False and app_window._theirs(url) is True
        said.update(version.whose(), folder="1" * 16)
        assert app_window._answers(url) is False and app_window._theirs(url) is True
        said.clear()
        said.update(app="something else")
        assert app_window._answers(url) is False and app_window._theirs(url) is False
    finally:
        server.shutdown()
        server.server_close()


def test_a_window_onto_another_closedesk_waits_while_that_one_updates(computer, tmp_path):
    # Updated by its own launcher, the other CloseDesk is away a minute and comes back on the same port.
    computer.other = True
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path, owned=False)
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    computer.updating = True
    computer.other = False
    time.sleep(app.gone_for_good * 3)
    assert computer.processes == [], "not started over a folder being updated"
    computer.updating = False
    computer.other = True  # back, as the new version
    until(lambda: len(window.urls()) == 2, what="the new version shown")
    assert computer.processes == [] and app.owned is False
    window.person_closes()
    thread.join(10)


def test_a_window_onto_another_closedesk_that_was_only_slow_to_answer_doesnt_start_a_second(computer, tmp_path):
    computer.other = True
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path, owned=False)
    still_there = {"port": True}
    app.port_taken = lambda: still_there["port"]
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    computer.other = False  # it doesn't answer in time (busy), but still holds its port
    time.sleep(app.gone_for_good * 3)
    assert computer.processes == []
    window.person_closes()
    thread.join(10)


# The close question on a Mac -------------------------------------------------------------------------------------------


def test_on_a_mac_the_first_close_while_mail_is_processed_asks_closedesks_question(computer, tmp_path):
    # macOS reads the question before the closing handler runs: set only there, the first close asked pywebview's
    # "Do you really want to quit?", and every close after it the question of the close before.
    computer.busy = True
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    thread, _ = running(app)
    window = window_of(webview)
    window.cocoa = True
    until(lambda: URL in window.urls())
    assert window.person_closes(answer=False) is False
    assert window.asked == [app_window.BUSY_CLOSE]
    computer.busy = False
    window.person_closes()
    thread.join(10)


def test_on_a_mac_the_first_close_during_an_update_says_it_finishes_by_itself(computer, tmp_path):
    computer.update_code = None
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    thread, _ = running(app)
    window = window_of(webview)
    window.cocoa = True
    until(lambda: URL in window.urls())
    computer.servers()[0].end(updates.UPDATE_AND_RESTART)
    until(lambda: computer.updates())
    assert window.person_closes(answer=False) is False
    assert window.asked == [app_window.UPDATING_CLOSE]
    computer.updates()[0].end(0)
    until(lambda: len(window.urls()) == 2, what="the new version")
    computer.busy = True
    assert window.person_closes(answer=False) is False
    assert window.asked[-1] == app_window.BUSY_CLOSE, "the update is over: its words go with it"
    computer.busy = False
    window.person_closes()
    thread.join(10)


def test_the_windows_labels_leave_the_pages_own_questions_alone():
    # pywebview's Cocoa uses "global.cancel" for the pages' own window.confirm() too ("Delete this conversation?"):
    # "Keep open" there would be the wrong word.
    assert "global.cancel" not in app_window.LABELS
    assert app_window.LABELS["global.quit"] == "Close"
    assert app_window.LABELS["global.quitConfirmation"] == app_window.BUSY_CLOSE


# Closed during an update on a Mac ------------------------------------------------------------------------------------


def test_on_a_mac_a_window_closed_during_an_update_leaves_at_once_and_the_update_finishes_by_itself(computer, tmp_path):
    # Waiting kept CloseDesk in the Dock with no window and nothing answering, and opening CloseDesk meanwhile only
    # brought that one forward: nothing appeared. The update is a program of its own, and finishes either way.
    computer.update_code = None
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    app.platform = "darwin"
    thread, result = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    computer.servers()[0].end(updates.UPDATE_AND_RESTART)
    until(lambda: computer.updates())
    (update,) = computer.updates()
    assert update.options.get("start_new_session") is (os.name != "nt"), "nothing that ends the window ends the update"
    assert window.person_closes() is True
    thread.join(10)
    assert not thread.is_alive() and result["code"] == 0
    assert update.poll() is None and not update.terminated, "left to finish"


# Quit on a Mac (Cmd+Q, the Dock's Quit, logging out) ---------------------------------------------------------------


@pytest.mark.parametrize("marked", [True, False])
def test_on_a_mac_quitting_after_update_now_still_runs_the_update(computer, tmp_path, capsys, marked):
    # Cmd+Q, the Dock's Quit and logging out end the program from inside macOS's terminate:, once the closing handler
    # said yes: webview.start() never returns, and nothing after it ran. The update the person had answered was dropped,
    # just after the question said it finishes by itself, and the next opening said "CloseDesk wasn't updated".
    computer.update_code = None
    computer.popen_server = SlowToStop
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    app.platform = "darwin"
    thread, _ = running(app)
    window = window_of(webview)
    window.cocoa = True
    until(lambda: URL in window.urls())
    (server,) = computer.servers()
    # CloseDesk stopping to update ends with UPDATE once its input ends (the window going).
    server.stdin = SimpleNamespace(close=lambda: server.end(updates.UPDATE_AND_RESTART))
    if marked:
        app.asked_mark.write_text("1", encoding="utf-8")
        until(lambda: window.localization["global.quitConfirmation"] == app_window.UPDATING_CLOSE, what="the question")
    assert webview.quitting, "nothing listens for the app quitting"
    assert window.person_quits() is True
    assert window.asked == ([app_window.UPDATING_CLOSE] if marked else [])
    # All of it before macOS ends the program, as the quitting is told.
    (update,) = computer.updates()
    assert update.cmd == [sys.executable, str(app_window.ROOT / "scripts" / "launch.py"), "--update"]
    assert update.options["start_new_session"] is (os.name != "nt"), "it outlives the window's program"
    assert server.poll() == updates.UPDATE_AND_RESTART and not app.asked_mark.exists()
    assert app_window.CLOSED.format(code=0) in capsys.readouterr().out, "the window's last line, for CI's check"
    assert thread.is_alive(), "webview.start() never returns from a quit"


def test_on_a_mac_quitting_stops_closedesk_as_closing_the_window_does(computer, tmp_path):
    computer.busy = True
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    app.platform = "darwin"
    running(app)
    window = window_of(webview)
    window.cocoa = True
    until(lambda: URL in window.urls())
    assert window.person_quits(answer=False) is False and window.asked == [app_window.BUSY_CLOSE]
    (server,) = computer.servers()
    assert server.poll() is None, "Cancel: CloseDesk carries on"
    computer.busy = False
    assert window.person_quits() is True
    assert server.input_closed and computer.updates() == []
    assert window.asked == [app_window.BUSY_CLOSE]


def test_elsewhere_nothing_listens_for_a_macs_quit(computer, tmp_path):
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    app.platform = "win32"
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    assert webview.quitting == []
    window.person_closes()
    thread.join(10)


def test_in_the_smoke_check_on_a_mac_the_window_quits_as_cmd_q_does_once_update_now_is_answered(computer, tmp_path, monkeypatch, capsys):
    # CI's check of the real window on a Mac quits it through macOS's own terminate: (Cmd+Q's way), with nothing asked,
    # once Update now is answered: the update still runs, by itself.
    monkeypatch.setenv(app_window.SMOKE_ENV, "5")
    monkeypatch.setenv(app_window.SMOKE_HOLD_ENV, "30")
    monkeypatch.setenv(app_window.SMOKE_QUIT_ENV, "1")
    computer.update_code = None
    computer.popen_server = SlowToStop
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    app.platform = "darwin"
    running(app)
    window = window_of(webview)
    window.cocoa = True
    until(lambda: URL in window.urls())
    window.js_api.page_ready("/")
    until(lambda: URL + "app" in window.urls())
    window.js_api.page_ready("/app")
    (server,) = computer.servers()
    server.stdin = SimpleNamespace(close=lambda: server.end(updates.UPDATE_AND_RESTART))
    app.asked_mark.write_text("1", encoding="utf-8")  # Update now, pressed by the check
    until(lambda: webview.quit.is_set(), what="the window quitting")
    until(lambda: computer.updates(), what="the update")
    assert window.closed_by == "quit" and window.asked == []
    assert "CloseDesk was quit" in capsys.readouterr().out


# The page shown again -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "where, again",
    [
        (URL + "app/settings", URL + "app/settings"),  # the workspace, moved to its Settings
        (URL + "app/mail/123?from=search", URL + "app/mail/123?from=search"),
        (URL + "inbox", URL + "inbox"),  # a classic page, with the question's Update now
        (URL + "settings/update", URL + "settings#update"),  # classic Setup's Update: the page its form sent back
        ("http://127.0.0.1:9999/app", URL),  # not this CloseDesk's
        ("about:blank", URL),
    ],
)
def test_after_update_now_the_window_shows_the_page_it_showed_before(computer, tmp_path, where, again):
    # The window opened "/" again after every update, the classic Today page: someone working in the workspace was put
    # out of it each time (a browser tab loads the same page again).
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    window.current = where
    computer.servers()[0].end(updates.UPDATE_AND_RESTART)
    until(lambda: len(window.urls()) == 2, what="the new version in the window")
    assert window.urls()[-1] == again
    window.person_closes()
    thread.join(10)


def test_when_the_window_cant_say_what_it_shows_the_page_says(computer, tmp_path):
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    window.js_api.page_ready("/app/today")
    window.current = RuntimeError("the window can't be asked now")
    computer.servers()[0].end(updates.UPDATE_AND_RESTART)
    until(lambda: len(window.urls()) == 2)
    assert window.urls()[-1] == URL + "app/today"
    window.person_closes()
    thread.join(10)


def test_a_page_that_hasnt_loaded_isnt_asked_its_address(computer, tmp_path):
    # pywebview waits twenty seconds for it first: the window would say nothing of the update meanwhile.
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    window.js_api.page_ready("/app/today")
    window.events.loaded.clear()
    asked: list = []
    window.get_current_url = lambda: asked.append(1) or URL + "app/settings"
    computer.servers()[0].end(updates.UPDATE_AND_RESTART)
    until(lambda: len(window.urls()) == 2)
    assert asked == [] and window.urls()[-1] == URL + "app/today"
    window.person_closes()
    thread.join(10)


def test_a_window_onto_another_closedesk_shows_the_page_it_showed_when_that_one_is_back(computer, tmp_path):
    computer.other = True
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path, owned=False)
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    window.current = URL + "app/settings"
    computer.updating, computer.other = True, False
    time.sleep(app.gone_for_good)
    computer.updating, computer.other = False, True  # back, as the new version
    until(lambda: len(window.urls()) == 2, what="the new version shown")
    assert window.urls()[-1] == URL + "app/settings"
    window.person_closes()
    thread.join(10)


class SlowToStop(FakeProcess):
    """CloseDesk still finishing something once its input ends (a reply a local model is writing, its page gone)."""

    def _input_ends(self) -> None:
        self.input_closed = True


def _closed_while_closedesk_finishes(computer, tmp_path, monkeypatch, platform):
    monkeypatch.setattr(app_window, "STOP_WAIT", 0.5)
    computer.popen = lambda cmd, **options: computer.processes.append(SlowToStop(cmd, **options)) or computer.processes[-1]
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    app.popen = computer.popen
    app.platform = platform
    thread, result = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    started = time.monotonic()
    assert window.person_closes() is True
    thread.join(10)
    return computer.servers()[0], time.monotonic() - started, result


def test_on_a_mac_a_window_closed_while_closedesk_finishes_something_leaves_at_once(computer, tmp_path, monkeypatch):
    # Waiting for CloseDesk to stop kept the window's program in the Dock with no window, as CloseDesk.app: opening
    # CloseDesk meanwhile only brought that one forward, and nothing appeared. CloseDesk stops by itself once its
    # input ends, and ends itself in time (cli.leave_within).
    server, took, result = _closed_while_closedesk_finishes(computer, tmp_path, monkeypatch, "darwin")
    assert result["code"] == 0 and server.input_closed
    assert took < 0.4 and not server.terminated, "the window waited for CloseDesk to stop"


def test_elsewhere_a_window_closed_while_closedesk_finishes_something_waits_then_ends_it(computer, tmp_path, monkeypatch):
    server, took, result = _closed_while_closedesk_finishes(computer, tmp_path, monkeypatch, "win32")
    assert result["code"] == 0 and server.input_closed and server.terminated and took >= 0.5


def test_closedesk_ends_in_time_whatever_still_runs_beside_it(tmp_path):
    # A reply the local model is still writing runs in a thread Python waits for as it ends: CloseDesk stayed open,
    # unseen, until it was done (and the window, the launcher and an update waited on it). It ends within its few
    # seconds, with its own exit code. Run for real: python -m controller_inbox, with a thread that would hold it.
    hold = tmp_path / "hold"
    hold.mkdir()
    (hold / "sitecustomize.py").write_text(
        "import threading, time\nthreading.Thread(target=time.sleep, args=(120,), name='a-reply-being-written').start()\n",
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(hold), str(ROOT / "src")]),
           "CONTROLLER_INBOX_DATA_DIR": str(tmp_path / "data"), "CONTROLLER_INBOX_INBOX_DIR": str(tmp_path / "inbox")}
    # A command that ends as commands do, and one that ends with an error of its own (argparse's: no such command).
    for args, code in ((["status"], 0), (["no-such-command"], 2)):
        started = time.monotonic()
        done = subprocess.run([sys.executable, "-m", "controller_inbox", *args], env=env, cwd=tmp_path, capture_output=True,
                              text=True, timeout=60)
        assert time.monotonic() - started < 30, f"a thread still running kept CloseDesk open after {args} was done"
        assert done.returncode == code, done.stderr


def test_leaving_in_time_keeps_the_exit_code(tmp_path):
    script = tmp_path / "leave.py"
    script.write_text(
        "import sys, threading, time\nsys.path.insert(0, sys.argv[1])\nfrom controller_inbox import cli\n"
        "threading.Thread(target=time.sleep, args=(120,)).start()\nprint('done', flush=True)\n"
        "cli.leave_within(75, 0.2)\n",
        encoding="utf-8",
    )
    started = time.monotonic()
    done = subprocess.run([sys.executable, str(script), str(ROOT / "src")], capture_output=True, text=True, timeout=60)
    assert done.returncode == 75 and done.stdout.strip() == "done", done
    assert time.monotonic() - started < 30


def test_an_update_that_never_ends_is_given_up_on_and_closedesk_opens_as_it_is(computer, tmp_path, capsys):
    computer.update_code = None
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    app.update_longest = 0.3
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    computer.servers()[0].end(updates.UPDATE_AND_RESTART)
    until(lambda: len(window.urls()) == 2, what="CloseDesk opened as it is")
    (update,) = computer.updates()
    assert update.terminated and len(computer.servers()) == 2
    assert "gave up" in capsys.readouterr().out
    window.person_closes()
    thread.join(10)


def test_a_window_closed_during_an_update_that_never_ends_doesnt_wait_for_good(computer, tmp_path):
    computer.update_code = None
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    app.platform = "win32"
    app.update_longest = 0.5
    thread, _ = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    computer.servers()[0].end(updates.UPDATE_AND_RESTART)
    until(lambda: computer.updates())
    window.person_closes()
    thread.join(10)
    assert not thread.is_alive(), "an invisible program waiting until the computer restarts"


# CI's check opens the workspace too -----------------------------------------------------------------------------------


def test_in_the_smoke_check_the_workspace_is_loaded_too(computer, tmp_path, monkeypatch, capsys):
    # The check's folder has no mail, so "/" is a classic page: the workspace's own scripts would never run in the
    # real window (WebKit, WebView2).
    monkeypatch.setenv(app_window.SMOKE_ENV, "5")
    webview = FakeWebview()
    app = make_app(webview, computer, tmp_path)
    thread, result = running(app)
    window = window_of(webview)
    until(lambda: URL in window.urls())
    window.js_api.page_ready("/")
    until(lambda: URL + "app" in window.urls(), what="the workspace in the window")
    assert not window.gone.is_set()
    window.js_api.page_ready("/app")
    thread.join(10)
    said = capsys.readouterr().out
    assert result["code"] == 0 and "The CloseDesk window showed /\n" in said and "The CloseDesk window showed /app\n" in said
