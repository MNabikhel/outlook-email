"""The CloseDesk window: what the CloseDesk shortcut opens (``launch.py --app``, then ``controller_inbox app``), a
window of its own instead of a browser tab, on Windows and macOS.

The window starts CloseDesk (``controller_inbox run``, as the double-click launchers do) and shows it. Closing the
window stops that CloseDesk; while mail is being processed or an update runs, it asks first. CloseDesk started by the
window holds its input open, and stops when that ends, so it never outlives the window however the window's process
ends. Update now (Settings', or the question as CloseDesk opens) keeps the window up: it says CloseDesk is updating
while ``launch.py --update`` runs, then shows the new version. When a CloseDesk is already open (another window, or
CloseDesk.bat / CloseDesk.command), the window shows that one, and closing it leaves that one open.

Where the window can't open (Linux; Windows without Microsoft Edge WebView2 Runtime, where pywebview would use
Internet Explorer, which can't run CloseDesk; pywebview missing or broken), CloseDesk opens in the browser as before:
the double-click launcher is opened, in its own window, and why is said in a message box the first time and in the log
every time. ``CLOSEDESK_WINDOW=0`` asks for the browser.

There is no console, so what CloseDesk says goes to its log (``CLOSEDESK_APP_LOG``, set by launch.py), and nothing it
starts opens a console window (CREATE_NO_WINDOW on Windows). The pages ask the window for one thing: to open what
leaves CloseDesk (a mail address, another website, a page meant for a new tab) where a browser would
(``Bridge``, used by static/ui/in_window.js); downloads are saved where the person chooses, Downloads first.

pywebview's own rules this keeps to (6.2): ``webview.start`` runs on the main thread; the closing handler runs on the
window's thread and on macOS may neither open a dialog nor run JavaScript, or the app hangs (pywebview #1699), so it
only sets what pywebview's own "close anyway?" asks; on Windows ``destroy()`` raises the same closing as the close
button, so closing on purpose says so first.
"""

from __future__ import annotations

import io
import json
import os
import platform as _platform
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from controller_inbox import updates, version
from controller_inbox.config import load_settings

ROOT = version.ROOT
TITLE = "CloseDesk"
# The taskbar groups the window under the shortcut that carries the same id (Windows: Company.Product, no version).
APP_ID = "CloseDesk.CloseDesk"
UPDATE = updates.UPDATE_AND_RESTART
REBUILD = 76  # launch.py: the environment has to be made again
# A console program started without this flashes a console window up on Windows (0 elsewhere).
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# CloseDesk started by the window stops when its input ends (cli._run_server).
STOP_WITH_PARENT = "CLOSEDESK_STOP_WITH_PARENT"
# Why the window couldn't open, for the launcher's window to say too (cli._run).
WHY_ENV = "CLOSEDESK_WINDOW_WHY"
# "0": the browser, not the window.
WINDOW_ENV = "CLOSEDESK_WINDOW"
# CI's check of the real window: seconds to wait for CloseDesk's page to say it loaded, then the window closes itself.
SMOKE_ENV = "CLOSEDESK_APP_SMOKE"

SIZE = (1280, 860)
MIN_SIZE = (720, 520)
BACKGROUND = "#F4F5F7"
LABELS = {"global.quit": "Close", "global.cancel": "Keep open"}
# The engines CloseDesk's pages run in: WebView2 (Windows) and WebKit (macOS).
ENGINES = ("edgechromium", "wkwebview")
# Where what leaves CloseDesk may be opened: another website, a mail address.
OUTSIDE = ("http", "https", "mailto")

NOT_HERE = "The CloseDesk window is for Windows and macOS: here CloseDesk opens in your browser."
NO_WEBVIEW2 = (
    "The CloseDesk window needs Microsoft Edge WebView2 Runtime, which this computer doesn't have, so CloseDesk opens "
    "in your browser instead. Your IT team can install it (it is free from Microsoft); after that CloseDesk opens in "
    "its window."
)
NO_PART = "The CloseDesk window couldn't start on this computer ({detail}), so CloseDesk opens in your browser instead."
BLANK = "The CloseDesk window stayed empty, so CloseDesk opens in your browser instead."
TURNED_OFF = "CloseDesk opens in your browser (CLOSEDESK_WINDOW=0 is set)."
BUSY_CLOSE = (
    "CloseDesk is still processing mail. Close it anyway? What it hasn't finished is picked up the next time "
    "CloseDesk opens."
)
UPDATING_CLOSE = (
    "CloseDesk is updating. Close the window anyway? The update finishes by itself; open CloseDesk again in a minute "
    "or two."
)
STOPPED = "CloseDesk stopped unexpectedly. What went wrong is written in {log}. Close this window and open CloseDesk again."
NOT_OPEN = "CloseDesk didn't open in time. What went wrong is written in {log}. Close this window and open CloseDesk again."
NO_PORT = "CloseDesk can't open: port {port} and the twenty after it are all in use. Set CONTROLLER_INBOX_PORT in the .env file in CloseDesk's folder to a free port."
CANT_START = "CloseDesk can't start yet:\n\n{problems}\n\nThis is also written in {log}."
NO_LAUNCHER = "CloseDesk couldn't open {launcher}. Double-click it in the CloseDesk folder ({folder})."

# How long the window waits: for its first page to show (WebView2 that fails to start leaves it empty), for CloseDesk
# to answer, and for CloseDesk to stop once asked.
LOAD_WAIT = 60
UP_WAIT = 180
STOP_WAIT = 20

# 127.0.0.1 is asked directly, never through a proxy the computer may be set to use.
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# Where things are --------------------------------------------------------------------------------------------------


def user_dir() -> Path:
    """This person's own folder for CloseDesk (no admin rights needed): the one the launchers keep uv in."""
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "CloseDesk"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "CloseDesk"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "CloseDesk"


def log_path() -> Path:
    """The app's log, as launch.py named it (its app_log_path)."""
    named = os.environ.get(updates.APP_LOG_ENV, "")
    if named:
        return Path(named)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "CloseDesk" / "CloseDesk-app.log"
    return user_dir() / "CloseDesk-app.log"


def icon_path() -> str | None:
    """The window's icon, where this copy has one (static/closedesk.ico on Windows, .icns on macOS)."""
    name = "closedesk.ico" if sys.platform == "win32" else "closedesk.icns"
    path = ROOT / "src" / "controller_inbox" / "static" / name
    return str(path) if path.is_file() else None


def use_log_if_no_console() -> None:
    """Started with no console (pythonw, the shortcut): what CloseDesk says goes to its log, not nowhere (uvicorn and
    print would fail on a missing stdout)."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(path, "a", encoding="utf-8", errors="replace", buffering=1)  # noqa: SIM115 (open for good)
    except OSError:
        stream = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def _console() -> bool:
    """Whether a person is at a terminal that shows what CloseDesk says."""
    try:
        return bool(sys.stdout is not None and sys.stdout.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _output() -> dict:
    """Where a program the window starts writes: the window's own output (its log)."""
    try:
        sys.stdout.flush()
        sys.stdout.fileno()
    except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
        return {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    return {"stdout": sys.stdout, "stderr": subprocess.STDOUT}


def set_app_id(*, platform: str | None = None, shell32=None) -> None:
    """Windows: this process is CloseDesk, so its window groups under the CloseDesk shortcut's icon on the taskbar
    (not Python's), and pinning it pins the shortcut. Must come before any window."""
    if (platform or sys.platform) != "win32":
        return
    try:
        if shell32 is None:
            import ctypes

            shell32 = ctypes.windll.shell32
            shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [ctypes.c_wchar_p]
            shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.HRESULT
        shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception as exc:  # the window still opens, grouped under Python
        print(f"Couldn't give the window CloseDesk's taskbar id: {exc}", flush=True)


# Whether the window can open here -----------------------------------------------------------------------------------

# The WebView2 Runtime, then its Beta, Dev and Canary builds: the keys Microsoft documents, which pywebview reads too.
WEBVIEW2 = (
    "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
    "{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}",
    "{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}",
    "{65C35B14-6C1D-4122-AC46-7148CC9D6497}",
)
NET_462 = 394802  # .NET Framework 4.6.2's "Release": the oldest pywebview's WebView2 window runs on
WEBVIEW2_OLDEST = (86, 0, 622, 0)


def _version(text: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in str(text).strip().split("."))
    except ValueError:
        return (0,)


def webview2_installed(registry=None, *, machine: str | None = None) -> bool:
    """Whether Microsoft Edge WebView2 Runtime is on this computer (for everyone, or for this person), as Microsoft
    says to check: its ``pv`` value, which an uninstall leaves as 0.0.0.0. Without it pywebview would quietly use
    Internet Explorer."""
    if registry is None:
        import winreg as registry
    machine = (machine or _platform.machine()).lower()
    try:
        with registry.OpenKey(registry.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full") as key:
            release = int(registry.QueryValueEx(key, "Release")[0])
    except (OSError, ValueError, TypeError):
        return False
    if release < NET_462:
        return False
    for client in WEBVIEW2:
        for hive in ("HKEY_CURRENT_USER", "HKEY_LOCAL_MACHINE"):
            node = "" if hive == "HKEY_CURRENT_USER" or machine == "x86" else "WOW6432Node\\"
            try:
                with registry.OpenKey(getattr(registry, hive), rf"SOFTWARE\{node}Microsoft\EdgeUpdate\Clients\{client}") as key:
                    found = str(registry.QueryValueEx(key, "pv")[0])
            except (OSError, ValueError, TypeError):
                continue
            if _version(found) >= WEBVIEW2_OLDEST:
                return True
    return False


def _webview_found() -> str:
    """"" when pywebview imports; else what went wrong."""
    try:
        import webview  # noqa: F401
    except Exception as exc:  # not installed, or a part of it (pythonnet, pyobjc) that won't load
        return f"{type(exc).__name__}: {exc}"
    return ""


def why_no_window(*, platform: str | None = None, environ=None, registry=None) -> tuple[str, str] | None:
    """(what, why in plain words) when the window can't open here, so CloseDesk opens in the browser; else None."""
    platform = platform or sys.platform
    environ = os.environ if environ is None else environ
    if environ.get(WINDOW_ENV) == "0":
        return ("off", TURNED_OFF)
    if platform not in ("win32", "darwin"):
        return ("platform", NOT_HERE)
    if platform == "win32":
        try:
            found = webview2_installed(registry)
        except Exception:
            found = False
        if not found:
            return ("webview2", NO_WEBVIEW2)
    missing = _webview_found()
    if missing:
        return ("missing", NO_PART.format(detail=missing))
    return None


# The browser instead ------------------------------------------------------------------------------------------------


def tell(text: str, *, platform: str | None = None) -> None:
    """Say ``text`` where a person sees it with no console: a message box (Windows), an alert (macOS). Also logged."""
    platform = platform or sys.platform
    print(text, flush=True)
    try:
        if platform == "win32":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, text, TITLE, 0x40 | 0x10000)  # an "i", brought to the front
        elif platform == "darwin":
            # The words are passed as they are, never written into the script.
            subprocess.run(
                ["/usr/bin/osascript", "-e", "on run argv", "-e", "display alert (item 1 of argv) message (item 2 of argv)",
                 "-e", "end run", TITLE, text],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=3600,
            )
    except (OSError, AttributeError, ValueError, subprocess.SubprocessError):
        pass


def _said_file() -> Path:
    return user_dir() / "window-said.json"


def _said_before(key: str) -> bool:
    try:
        return key in json.loads(_said_file().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False


def _remember_said(key: str) -> None:
    try:
        said = json.loads(_said_file().read_text(encoding="utf-8"))
        said = said if isinstance(said, list) else []
    except (OSError, ValueError):
        said = []
    try:
        _said_file().parent.mkdir(parents=True, exist_ok=True)
        _said_file().write_text(json.dumps(sorted({*said, key})), encoding="utf-8")
    except OSError:
        pass


def _start(cmd: list[str]) -> None:
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def open_launcher(why: str = "", *, platform: str | None = None, startfile=None, run=None) -> None:
    """Open the double-click launcher (CloseDesk.bat; CloseDesk.command in Terminal), as if it were double-clicked:
    CloseDesk in the browser, with its window that closing stops, or the set-up that has to be done again shown as it
    goes. ``why``: said in its window too (Windows passes it on)."""
    platform = platform or sys.platform
    for name in (updates.APP_ENV, updates.APP_LOG_ENV, updates.LAUNCHER_ENV, STOP_WITH_PARENT, "CLOSEDESK_REOPEN"):
        os.environ.pop(name, None)
    if why:
        os.environ[WHY_ENV] = why
    launcher = ROOT / ("CloseDesk.bat" if platform == "win32" else "CloseDesk.command")
    try:
        if platform == "win32":
            (startfile or os.startfile)(str(launcher))
        elif platform == "darwin":
            (run or _start)(["/usr/bin/open", "-a", "Terminal", str(launcher)])
    except (OSError, AttributeError) as exc:
        print(f"Couldn't open {launcher}: {exc}", flush=True)
        tell(NO_LAUNCHER.format(launcher=launcher.name, folder=ROOT), platform=platform)


def run_here(args, *, browser: bool = True) -> int:
    """CloseDesk runs in this process, as the launcher runs it. Nothing here starts it again after an update, so it
    doesn't offer one."""
    os.environ.pop(updates.LAUNCHER_ENV, None)
    from controller_inbox import cli

    argv = ["run"] if browser else ["run", "--no-browser"]
    if getattr(args, "host", None):
        argv += ["--host", args.host]
    if getattr(args, "port", None):
        argv += ["--port", str(args.port)]
    return cli.main(argv)


def browser_instead(fallback: tuple[str, str], args, *, platform: str | None = None) -> int:
    """The window can't show CloseDesk here: open it in the browser as before, and say why."""
    key, words = fallback
    platform = platform or sys.platform
    print(words, flush=True)
    if platform not in ("win32", "darwin") or _console():
        return run_here(args)  # Linux (a developer's), or a terminal
    if key != "off" and not _said_before(key):
        tell(words, platform=platform)
        _remember_said(key)
    open_launcher(words, platform=platform)
    return 0


# What the window shows while CloseDesk isn't there -------------------------------------------------------------------

_STYLE = """
:root { color-scheme: light dark; --bg: #f4f5f7; --text: #17202c; --muted: #5f6b7b; --brand: #1b365d; --on-brand: #f3e3bd;
  --track: #e1e5eb; --bar: #2557a7; }
@media (prefers-color-scheme: dark) { :root { --bg: #121820; --text: #e7ebf1; --muted: #a3adbb; --track: #2a3341; --bar: #7aa7ec; } }
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body { background: var(--bg); color: var(--text); display: grid; place-items: center; padding: 16px;
  font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
main { max-width: 34rem; text-align: center; }
.mark { width: 64px; height: 64px; margin: 0 auto 20px; border-radius: 14px; background: var(--brand); color: var(--on-brand);
  display: grid; place-items: center; font: 700 26px/1 Arial, sans-serif; letter-spacing: 0.5px; }
h1 { font-size: 1.35rem; margin: 0 0 8px; }
p { margin: 0 0 20px; color: var(--muted); overflow-wrap: anywhere; }
.bar { height: 4px; border-radius: 2px; background: var(--track); overflow: hidden; width: 16rem; max-width: 100%; margin: 0 auto; }
.bar span { display: block; height: 100%; width: 40%; border-radius: 2px; background: var(--bar); animation: go 1.4s ease-in-out infinite; }
@keyframes go { from { transform: translateX(-100%); } to { transform: translateX(250%); } }
@media (prefers-reduced-motion: reduce) { .bar span { animation: none; width: 100%; opacity: 0.5; } }
"""


def _page(heading: str, text: str, *, busy: bool = False) -> str:
    bar = f'<div class="bar" role="progressbar" aria-label="{escape(heading)}"><span></span></div>' if busy else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        f"<title>{TITLE}</title><style>{_STYLE}</style></head><body><main>"
        f'<div class="mark" aria-hidden="true">CD</div><h1>{escape(heading, quote=False)}</h1>'
        f'<p role="status">{escape(text, quote=False)}</p>{bar}</main></body></html>'
    )


def opening_page() -> str:
    return _page("Opening CloseDesk", "Your mail shows here in a moment.", busy=True)


def updating_page() -> str:
    return _page("Updating CloseDesk", updates.UPDATING, busy=True)


def stopped_page(log: Path) -> str:
    return _page("CloseDesk stopped", STOPPED.format(log=log))


def not_open_page(log: Path) -> str:
    return _page("CloseDesk didn't open", NOT_OPEN.format(log=log))


# What the pages may ask of the window ---------------------------------------------------------------------------------


def _outside_ok(url: str) -> bool:
    if not url or len(url) > 8000 or any(ord(ch) < 32 or ord(ch) == 127 for ch in url):
        return False
    return urlsplit(url).scheme.lower() in OUTSIDE


class Bridge:
    """What CloseDesk's pages may ask of the window, as ``window.pywebview.api`` (static/ui/in_window.js). pywebview
    offers the page every public method here (and of every object a public attribute holds): only these two."""

    def __init__(self, app: App) -> None:
        self._app = app

    def open_outside(self, url: str) -> bool:
        """Open a mail address (mailto:), another website, or a CloseDesk page meant for a new tab (the printable
        digest, a file's original) in the default mail app or browser, as a click in a browser would. Nothing else:
        no other kind of address."""
        url = str(url or "")
        if not _outside_ok(url):
            return False
        self._app.open_url(url)
        return True

    def page_ready(self, path: str = "") -> None:
        """CloseDesk's page loaded in the window (CI's check of the real window waits for this)."""
        self._app.page_ready(str(path or "")[:200])


# The window, and the CloseDesk in it ---------------------------------------------------------------------------------


def _answers(url: str) -> bool:
    """Whether CloseDesk answers at ``url``."""
    try:
        with _LOCAL.open(url + "health", timeout=2) as reply:
            return json.load(reply).get("app") == "closedesk"
    except (OSError, ValueError, AttributeError):
        return False


def _working(url: str) -> bool:
    """Whether the CloseDesk at ``url`` is processing mail (or reading scans): what closing it would cut off."""
    try:
        with _LOCAL.open(url + "process/status", timeout=1) as reply:
            return json.load(reply).get("state") == "running"
    except (OSError, ValueError, AttributeError):
        return False


def _said(exc: BaseException) -> str:
    return " ".join(f"{type(exc).__name__}: {exc}".split())[:200]


class App:
    """One CloseDesk window, with the CloseDesk it started (``owned``) or found already open at ``url``."""

    def __init__(self, webview, *, url: str, port: int, owned: bool, log: Path, host: str | None = None) -> None:
        self.webview = webview
        self.url, self.port, self.owned, self.log, self.host = url, port, owned, log, host
        self.window = None
        self.server = None  # the CloseDesk this window started, while it runs
        self.update = None  # the update under way
        self.updating = False
        self.closing = False  # the window is going: nothing starts again
        self.on_purpose = False  # CloseDesk closes the window itself: nothing is asked
        self.fallback: tuple[str, str] | None = None  # the window can't show CloseDesk: the browser does
        self.code = 0
        self.ready = threading.Event()  # CloseDesk's page said it loaded
        self.ready_path = ""
        self.smoke = float(os.environ.get(SMOKE_ENV) or 0)
        self._lock = threading.Lock()
        # What runs things on the computer (the tests give their own).
        self.popen = subprocess.Popen
        self.answers = _answers
        self.working = lambda: _working(self.url)
        self.open_url = webbrowser.open
        self.open_launcher = open_launcher
        self.storage = user_dir() / "window"
        self.load_wait, self.up_wait, self.poll = LOAD_WAIT, UP_WAIT, 0.25

    # On the main thread -----------------------------------------------------------------------------------------

    def run(self) -> int:
        """Open the window and keep it until it is closed; then CloseDesk it started stops. The exit code."""
        webview = self.webview
        # Download, .eml and CSV are saved where the person chooses (Downloads first); without this they do nothing.
        webview.settings["ALLOW_DOWNLOADS"] = True
        # A link meant for a new tab (and window.open) opens in the default browser.
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        width, height = self._size()
        self.window = webview.create_window(
            TITLE, html=opening_page(), js_api=Bridge(self), width=width, height=height, min_size=MIN_SIZE,
            text_select=True, zoomable=True, background_color=BACKGROUND, localization=LABELS,
        )
        self.window.events.initialized += self.check_engine
        self.window.events.closing += self.on_closing
        try:
            # Not private: the pages keep their own settings (theme, drafts, what was said) from one start to the next.
            webview.start(self.supervise, private_mode=False, storage_path=str(self.storage), icon=icon_path())
        except Exception as exc:
            traceback.print_exc()
            if self.fallback is None:
                self.fallback = ("broken", NO_PART.format(detail=_said(exc)))
        self.stop()
        return self.code

    def _size(self) -> tuple[int, int]:
        """A good size for CloseDesk, never bigger than the screen."""
        try:
            screen = self.webview.screens[0]
            width, height = int(screen.width), int(screen.height)
        except Exception:
            return SIZE
        return min(SIZE[0], int(width * 0.9)), min(SIZE[1], int(height * 0.88))

    def stop(self) -> None:
        """The window has closed: the CloseDesk it started stops; an update under way finishes first (one stopped half
        way would leave CloseDesk half updated)."""
        with self._lock:
            self.closing = True
            server, update = self.server, self.update
        if update is not None and update.poll() is None:
            print("The window was closed during the update: the update finishes first.", flush=True)
            update.wait()
        if server is not None and server.poll() is None:
            self._end(server)

    def _end(self, server) -> None:
        """Stop CloseDesk the window started, as Ctrl+C would: its input ends. Ended outright if it doesn't."""
        try:
            server.stdin.close()
        except (OSError, AttributeError, ValueError):
            pass
        try:
            server.wait(STOP_WAIT)
        except subprocess.TimeoutExpired:
            print(f"CloseDesk didn't stop within {STOP_WAIT} seconds: ending it.", flush=True)
            server.terminate()
            try:
                server.wait(5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(5)

    # On the window's own thread (pywebview's handlers) ----------------------------------------------------------

    def check_engine(self, renderer):
        """Before the window opens: only an engine that runs CloseDesk's pages. pywebview 6 uses Internet Explorer
        where WebView2 is missing; False keeps the window from opening, and the browser shows CloseDesk."""
        if renderer in ENGINES:
            return None
        print(f"The window's web engine here is {renderer}, which can't run CloseDesk.", flush=True)
        self.fallback = ("webview2", NO_WEBVIEW2) if renderer == "mshtml" else ("engine", NO_PART.format(detail=f"its web engine is {renderer}"))
        return False

    def on_closing(self, window):
        """The window is about to close. While mail is being processed or an update runs, pywebview asks "close
        anyway?" with these words; nothing else is done here (see the module's note). Returns None: False would keep
        the window open without asking."""
        if self.on_purpose or self.closing:
            window.confirm_close = False
            return None
        if self.updating:
            words = UPDATING_CLOSE
        elif self.owned and self.working():
            words = BUSY_CLOSE
        else:
            words = ""
        if words:
            window.localization["global.quitConfirmation"] = words
        window.confirm_close = bool(words)
        return None

    def close_on_purpose(self) -> None:
        """CloseDesk closes its window itself (for the browser, the launcher, or CI's check): nothing is asked."""
        self.on_purpose = True
        with self._lock:
            self.closing = True
        try:
            self.window.destroy()
        except Exception as exc:
            print(f"Couldn't close the window: {_said(exc)}", flush=True)

    def page_ready(self, path: str) -> None:
        self.ready_path = path
        self.ready.set()

    # Beside the window (pywebview runs this in a thread of its own once the window is there) ---------------------

    def supervise(self) -> None:
        try:
            self._supervise()
        except Exception:
            traceback.print_exc()
            if not self.closing:
                self._show(stopped_page(self.log))

    def _supervise(self) -> None:
        if not self.window.events.loaded.wait(self.load_wait):
            # WebView2 that fails to start only says so in its own log, and leaves the window empty.
            self.fallback = ("blank", BLANK)
            self.close_on_purpose()
            return
        if not self.owned:
            self._show_closedesk()
            return
        reopen = False
        while True:
            server = self._start_server(reopen=reopen)
            if server is None:
                return
            if not self._wait_up(server):
                if self.closing:
                    return
                if server.poll() is None:  # started, and never answered
                    self._end(server)
                    self._show(not_open_page(self.log))
                else:
                    print(f"CloseDesk stopped as it started (exit code {server.poll()}).", flush=True)
                    self._show(stopped_page(self.log))
                return
            self._show_closedesk()
            code = server.wait()
            if self.closing:
                return
            if code != UPDATE:
                print(f"CloseDesk stopped by itself (exit code {code}).", flush=True)
                self._show(stopped_page(self.log))
                return
            if not self._update():
                return
            reopen = True

    def _start_server(self, *, reopen: bool):
        cmd = [sys.executable, "-m", "controller_inbox", "run", "--no-browser"]
        if self.host:
            cmd += ["--host", self.host]
        cmd += ["--port", str(self.port)]
        env = {
            **os.environ,
            # Update now stops CloseDesk for the window to update and start again: offered, as with a launcher.
            updates.LAUNCHER_ENV: "1",
            updates.APP_ENV: "1",
            updates.APP_LOG_ENV: str(self.log),
            STOP_WITH_PARENT: "1",
        }
        env.pop("CLOSEDESK_REOPEN", None)
        if reopen:
            env["CLOSEDESK_REOPEN"] = "1"  # on the port it was on, where the window looks for it
        with self._lock:
            if self.closing:
                return None
            self.server = self.popen(cmd, cwd=str(ROOT), env=env, stdin=subprocess.PIPE, creationflags=NO_WINDOW, **_output())
            return self.server

    def _wait_up(self, server) -> bool:
        deadline = time.monotonic() + self.up_wait
        while time.monotonic() < deadline:
            if self.closing or server.poll() is not None:
                return False
            if self.answers(self.url):
                return True
            time.sleep(self.poll)
        return False

    def _show(self, html: str) -> None:
        try:
            self.window.load_html(html)
        except Exception as exc:
            print(f"Couldn't show it in the window: {_said(exc)}", flush=True)

    def _show_closedesk(self) -> None:
        self.window.load_url(self.url)
        if not self.smoke:
            return
        if self.ready.wait(self.smoke):
            print(f"The CloseDesk window showed {self.ready_path}", flush=True)
            self.code = 0
        else:
            print(f"The CloseDesk window didn't show CloseDesk's page within {self.smoke:g} seconds.", flush=True)
            self.code = 1
        self.close_on_purpose()

    def _update(self) -> bool:
        """Update now: CloseDesk stopped to be updated. The window says so throughout; True when CloseDesk is to be
        started again (updated, or as it was when the update didn't finish: its first page says so)."""
        self.updating = True
        self._show(updating_page())
        print("Updating CloseDesk...", flush=True)
        env = {**os.environ, updates.APP_ENV: "1", updates.APP_LOG_ENV: str(self.log)}
        for name in (STOP_WITH_PARENT, "CLOSEDESK_REOPEN"):
            env.pop(name, None)
        try:
            with self._lock:
                if self.closing:
                    return False
                self.update = self.popen(
                    [sys.executable, str(ROOT / "scripts" / "launch.py"), "--update"], cwd=str(ROOT), env=env,
                    stdin=subprocess.DEVNULL, creationflags=NO_WINDOW, **_output(),
                )
            code = self.update.wait()
        finally:
            self.updating = False
        if code == REBUILD:
            # The environment has to be made again, which a Python running from it can't do: the launcher does, in its
            # window, then starts CloseDesk.
            print("After the update, CloseDesk's environment has to be made again: the double-click launcher does that.", flush=True)
            self.open_launcher()
            self.close_on_purpose()
            return False
        if code:
            print(f"The update ended with exit code {code}: CloseDesk opens as it is.", flush=True)
        return not self.closing


def prepare(args, webview) -> App | None:
    """The window for CloseDesk: the one already open on its port, or one it starts. None (said why) when it can't."""
    try:
        settings = load_settings()
    except Exception as exc:  # a line in .env that isn't valid, a folder that can't be made
        tell(_cant_start(exc))
        return None
    from controller_inbox import cli

    host = args.host or settings.host
    wanted = args.port or settings.port
    port, running = cli._port_to_use(host, wanted)
    if not port:
        tell(NO_PORT.format(port=wanted))
        return None
    shown = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
    if ":" in shown:
        shown = f"[{shown}]"
    return App(webview, url=f"http://{shown}:{port}/", port=port, owned=not running, log=log_path(), host=args.host)


def _cant_start(exc: BaseException) -> str:
    from controller_inbox import doctor

    problems: list[str] = []
    try:
        for check in doctor.checks(port=False):
            if check.state == doctor.PROBLEM:
                problems.append(check.text + (f" {check.todo}" if check.todo else ""))
    except Exception:
        pass
    return CANT_START.format(problems="\n".join(problems) or _said(exc), log=log_path())


def _leave(code: int) -> None:
    """End the window's process now: a GUI library can leave a thread behind that would keep it running unseen."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (AttributeError, OSError, ValueError):
            pass
    os._exit(code)


def main(args) -> int:
    """``controller_inbox app``: open the CloseDesk window, or the browser where it can't open."""
    use_log_if_no_console()
    set_app_id()
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} The CloseDesk window opens ({version.label()}).", flush=True)
    if getattr(args, "no_browser", False):
        # The launcher check: the app's whole start, with no window and no browser.
        return run_here(args, browser=False)
    fallback = why_no_window()
    if fallback:
        return browser_instead(fallback, args)
    import webview

    app = prepare(args, webview)
    if app is None:
        return 1
    code = app.run()
    if app.fallback:
        code = browser_instead(app.fallback, args)
    _leave(code)
    return code  # pragma: no cover (not reached)
