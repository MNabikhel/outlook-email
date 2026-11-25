"""The CloseDesk window: what the CloseDesk shortcut opens (``launch.py --app``, then ``controller_inbox app``), a
window of its own instead of a browser tab, on Windows and macOS.

The window starts CloseDesk (``controller_inbox run``, as the double-click launchers do) and shows it. Closing the
window stops that CloseDesk; while mail is being processed or an update runs, it asks first. CloseDesk started by the
window holds its input open, and stops when that ends, so it never outlives the window however the window's process
ends. Update now (Settings', or the question as CloseDesk opens) keeps the window up: it says CloseDesk is updating
while ``launch.py --update`` runs, then shows the new version. When a CloseDesk is already open (another window, or
CloseDesk.bat / CloseDesk.command), the window shows that one, and closing it leaves that one open: only this folder's,
run by this person (/health says whose it is), never another person's on a shared computer or another folder's.

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
import socket
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

from controller_inbox import app_log, mac_window, updates, version
from controller_inbox.config import load_settings

ROOT = version.ROOT
TITLE = "CloseDesk"
# The taskbar groups the window under the shortcut that carries the same id (Windows: Company.Product, no version).
APP_ID = "CloseDesk.CloseDesk"
# CloseDesk.app's id (shortcut.py makes the app), kept for good: macOS keeps what it remembers about an app (its place
# in the Dock, what it was allowed) under it.
BUNDLE_ID = "io.github.mnabikhel.closedesk"
UPDATE = updates.UPDATE_AND_RESTART
REBUILD = 76  # launch.py: the environment has to be made again
UPDATE_UNDER_WAY = 77  # launch.py --update: another update runs in the folder (CloseDesk-Update, say), nothing was done
# A console program started without this flashes a console window up on Windows (0 elsewhere).
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# CloseDesk started by the window stops when its input ends (cli._run_server).
STOP_WITH_PARENT = "CLOSEDESK_STOP_WITH_PARENT"
# Set by launch.py for the start whose environment it has just made: the CloseDesk shortcut puts back what Windows
# offered to delete meanwhile (shortcut.py). For the first CloseDesk the window starts only.
NEW_ENVIRONMENT_ENV = "CLOSEDESK_NEW_ENVIRONMENT"
# Why the window couldn't open, for the launcher's window to say too (cli._run).
WHY_ENV = "CLOSEDESK_WINDOW_WHY"
# CloseDesk started by the window opens on the port the window chose, or not at all (``PORT_TAKEN``): on another, the
# window would never find it.
EXACT_PORT = "CLOSEDESK_EXACT_PORT"
PORT_TAKEN = 3
# "0": the browser, not the window.
WINDOW_ENV = "CLOSEDESK_WINDOW"
# CI's check of the real window: seconds to wait for CloseDesk's page to say it loaded, then the window closes itself;
# and seconds it stays open after that, for the check to look at it from outside (what macOS calls it, a screenshot).
# In the check, a window that can't open fails it (exit code 2) instead of opening the browser, and nothing waits for a
# click: what would be a message box is in the log only.
SMOKE_ENV = "CLOSEDESK_APP_SMOKE"
SMOKE_HOLD_ENV = "CLOSEDESK_APP_SMOKE_HOLD"
# CI's check on a Mac: once Update now is answered, the window quits as Cmd+Q quits it (macOS's terminate:), nothing
# asked; the update has to run all the same.
SMOKE_QUIT_ENV = "CLOSEDESK_APP_SMOKE_QUIT"
DIDNT_OPEN = 2
# How the window's process ends when it has said itself, in a message box, why CloseDesk can't open: launch.py, which
# otherwise says so for a window that ends with an error, then says nothing more.
SAID = app_log.SAID
# The window's last line in its log, with how its program ends (CI's check of the double-clicked window reads it: there,
# nothing else sees the exit code).
CLOSED = "The CloseDesk window closed (exit code {code})."
# macOS: where the window's Python says it was started from (launch.py sets it to the link in CloseDesk.app), read for
# the app it belongs to: the Dock and the menu bar say CloseDesk. Not for what the window starts.
MAC_APP_PATH = "CFProcessPath"

SIZE = (1280, 860)
MIN_SIZE = (720, 520)
BACKGROUND = "#F4F5F7"
# pywebview's "close anyway?": its question is there from the start, since macOS reads it before the closing handler
# runs (Windows after; the handler sets it for both). "global.cancel" is pywebview's own ("Cancel"): macOS uses it for
# the pages' own questions too (window.confirm), where "Keep open" would be the wrong word.
LABELS = {"global.quit": "Close", "global.quitConfirmation": (
    "CloseDesk is still processing mail. Close it anyway? What it hasn't finished is picked up the next time "
    "CloseDesk opens."
)}
# The engines CloseDesk's pages run in: WebView2 (Windows) and WebKit (macOS).
ENGINES = ("edgechromium", "wkwebview")
# Windows, in CI's check of the real window: what it says once it has read back from WebView2 that its right-click menu
# and the browser's own keys are on (tests/launcher_smoke.py looks for it).
BROWSER_KEYS_ON = "The window's right-click menu and browser keys are on."
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
BUSY_CLOSE = LABELS["global.quitConfirmation"]
UPDATING_CLOSE = (
    "CloseDesk is updating. Close the window anyway? The update finishes by itself; open CloseDesk again in a minute "
    "or two."
)
UPDATING = "CloseDesk opens again by itself in a minute or two, as the new version. Your mail and settings are kept."
STOPPED = "CloseDesk stopped unexpectedly. What went wrong is written in {log}. Close this window and open CloseDesk again."
NOT_OPEN = "CloseDesk didn't open in time. What went wrong is written in {log}. Close this window and open CloseDesk again."
NO_PORT = "CloseDesk can't open: port {port} and the twenty after it are all in use. Set CONTROLLER_INBOX_PORT in the .env file in CloseDesk's folder to a free port."
CANT_START = "CloseDesk can't start yet:\n\n{problems}\n\nThis is also written in {log}."
NO_LAUNCHER = "CloseDesk couldn't open {launcher}. Double-click it in the CloseDesk folder ({folder})."

# How long the window waits: for its first page to show (WebView2 that fails to start leaves it empty), for CloseDesk
# to answer, for another CloseDesk that took the port to answer, and for CloseDesk to stop once asked.
LOAD_WAIT = 60
UP_WAIT = 180
OTHER_WAIT = 30
STOP_WAIT = 20
# Showing a CloseDesk it didn't start: how often the window looks whether that one is still there, and how long it
# may be away (its port free, no update under way) before the window starts CloseDesk itself.
WATCH_EVERY = 3
GONE_FOR_GOOD = 30
# The longest an update may run before the window gives up on it (its download gives up long before).
UPDATE_LONGEST = 30 * 60
# An update says it is under way in this file in CloseDesk's folder (launch.py's UPDATING_MARK).
UPDATING_MARK = ".closedesk-updating"
# The pages a form sent back, which can't be loaded again as they are: shown again as the page they belong to.
SENT_BACK = {"/settings/update": "/settings#update"}
# What isn't a page to show again: CloseDesk's answers to the pages' scripts.
NOT_PAGES = ("/api/", "/health")
# Said as macOS quits CloseDesk (Cmd+Q, the Dock's Quit, logging out).
QUIT = "CloseDesk was quit (Cmd+Q, the Dock's Quit, or logging out)."
# How CloseDesk ends when another took its port first: "already open in another window" (0), or the port couldn't be
# opened (3).
TAKEN = (0, PORT_TAKEN)

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
    return app_log.path()


def macos_version() -> tuple[int, ...]:
    """This Mac's macOS, (26, 1) say; (0,) when it can't be told (or this isn't a Mac)."""
    try:
        return tuple(int(part) for part in _platform.mac_ver()[0].split(".") if part.isdigit()) or (0,)
    except (ValueError, AttributeError):
        return (0,)


def mac_icon_name(macos: tuple[int, ...] | None = None) -> str:
    """CloseDesk's icon for this macOS: macOS 26 rounds an app's icon itself, and puts one with clear edges on a grey
    tile, so there the square filled to its edges; before 26, the rounded one with clear margins. (CloseDesk.app's own
    icon, and the window's, are the same one.)"""
    macos = tuple(macos) if macos else macos_version()
    return "closedesk-full.icns" if macos[0] >= 26 else "closedesk.icns"


def icon_path(*, platform: str | None = None, macos: tuple[int, ...] | None = None) -> str | None:
    """The window's icon, where this copy has one (static/closedesk.ico on Windows; on macOS the .icns for its
    version)."""
    platform = platform or sys.platform
    name = "closedesk.ico" if platform == "win32" else mac_icon_name(macos) if platform == "darwin" else "closedesk.icns"
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


def settle_mac_name(*, platform: str | None = None) -> None:
    """macOS: which app this program is, read now while ``MAC_APP_PATH`` names CloseDesk.app (macOS reads it once, the
    first time it is asked); then it goes, so nothing the window runs (an alert, the browser, CloseDesk itself) takes
    CloseDesk.app's name and icon."""
    if (platform or sys.platform) != "darwin" or MAC_APP_PATH not in os.environ:
        return
    try:
        from Foundation import NSBundle

        NSBundle.mainBundle()
    except Exception:  # pywebview's part for macOS is missing: there is no window to name
        pass
    os.environ.pop(MAC_APP_PATH, None)


# macOS: what runs as CloseDesk is quit, and the one that hears it (kept, so that it isn't let go).
_QUITTING: list = []
_HEARS_QUIT: list = []


def when_mac_quits(action) -> None:
    """macOS: run ``action`` as CloseDesk is quit (Cmd+Q, the Dock's Quit, logging out), once the window's closing
    handler said yes, on the window's own thread. macOS ends the program as soon as those told have done (pywebview
    has no applicationWillTerminate_), and webview.start() never returns: what follows the window's close is done in
    ``action``. Said in the log when it can't be listened for."""
    _QUITTING.append(action)
    if _HEARS_QUIT:
        return
    try:
        from AppKit import NSApplication, NSApplicationWillTerminateNotification
        from Foundation import NSNotificationCenter, NSObject

        class CloseDeskQuits(NSObject):
            def willQuit_(self, notification):  # noqa: N802 (Objective-C's name: willQuit:)
                _mac_quits(NSApplication.sharedApplication())

        hears = CloseDeskQuits.alloc().init()
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            hears, "willQuit:", NSApplicationWillTerminateNotification, None
        )
        _HEARS_QUIT.append(hears)
    except Exception as exc:  # the window still works; quitting it drops an update asked for meanwhile
        print(f"Couldn't listen for CloseDesk being quit: {_said(exc)}", flush=True)


def _mac_quits(application) -> None:
    try:  # out of sight at once: CloseDesk can take a few seconds to stop
        for window in application.windows():
            window.orderOut_(None)
    except Exception:
        pass
    for action in list(_QUITTING):
        try:
            action()
        except Exception:
            traceback.print_exc()


def quit_mac_app() -> None:
    """macOS: quit CloseDesk as Cmd+Q does (NSApplication's terminate:), on the window's own thread (CI's check)."""
    from AppKit import NSApplication
    from PyObjCTools import AppHelper

    AppHelper.callAfter(NSApplication.sharedApplication().terminate_, None)


def mac_bundle_id() -> str:
    """macOS: the id of the app this program is to macOS (BUNDLE_ID when it runs as CloseDesk.app). "" when macOS
    can't be asked."""
    try:
        from Foundation import NSBundle

        return str(NSBundle.mainBundle().bundleIdentifier() or "")
    except Exception:
        return ""


def mac_app_name() -> str:
    """macOS: the name the Dock and the menu bar give this program, with its app's id ("CloseDesk
    (io.github.mnabikhel.closedesk)" when it runs as CloseDesk.app; "Python (org.python.python)" when not). "" when
    macOS can't be asked."""
    try:
        from AppKit import NSRunningApplication

        current = NSRunningApplication.currentApplication()
        return f"{current.localizedName()} ({current.bundleIdentifier()})"
    except Exception:
        return ""


# The browser instead ------------------------------------------------------------------------------------------------


def _in_check() -> bool:
    """Whether this is CI's check of the real window (``CLOSEDESK_APP_SMOKE``)."""
    return bool(os.environ.get(SMOKE_ENV))


def tell(text: str, *, platform: str | None = None) -> None:
    """Say ``text`` where a person sees it with no console: a message box (Windows), an alert (macOS). Also logged. (In
    CI's check of the window, the log only.)"""
    app_log.tell(text, platform=platform)


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
    for name in (updates.APP_ENV, updates.APP_LOG_ENV, updates.LAUNCHER_ENV, STOP_WITH_PARENT, "CLOSEDESK_REOPEN", MAC_APP_PATH):
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
    if _in_check():
        print("This is the check of the CloseDesk window, and the window didn't open.", flush=True)
        return DIDNT_OPEN
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


# The window's own pages are in its history too: Back (Alt+Left, the mouse's Back button) from CloseDesk's first page
# would show "Opening CloseDesk" again, waiting for good. Come to by Back, such a page goes forward again.
_FORWARD = (
    "<script>var n=performance.getEntriesByType&&performance.getEntriesByType('navigation')[0];"
    "if(n&&n.type==='back_forward')history.forward();"
    "addEventListener('pageshow',function(e){if(e.persisted)history.forward();});</script>"
)


def _page(heading: str, text: str, *, busy: bool = False) -> str:
    bar = f'<div class="bar" role="progressbar" aria-label="{escape(heading)}"><span></span></div>' if busy else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        f"<title>{TITLE}</title><style>{_STYLE}</style>{_FORWARD if busy else ''}</head><body><main>"
        f'<div class="mark" aria-hidden="true">CD</div><h1>{escape(heading, quote=False)}</h1>'
        f'<p role="status">{escape(text, quote=False)}</p>{bar}</main></body></html>'
    )


def opening_page() -> str:
    return _page("Opening CloseDesk", "Your mail shows here in a moment.", busy=True)


def updating_page() -> str:
    return _page("Updating CloseDesk", UPDATING, busy=True)


def stopped_page(log: Path) -> str:
    return _page("CloseDesk stopped", STOPPED.format(log=app_log.shown(log)))


def not_open_page(log: Path) -> str:
    return _page("CloseDesk didn't open", NOT_OPEN.format(log=app_log.shown(log)))


# The window as a browser is (Windows) -------------------------------------------------------------------------------


def browser_keys(window, *, delegate=None, turn_on: bool = False) -> dict[str, bool]:
    """Windows: whether the window's right-click menu (Copy, Paste, Select all, spelling suggestions) and the browser's
    own keys (Ctrl+F find, Ctrl+P print, F5, Ctrl+Plus/Minus/0 zoom, Alt+Left back) are on, as WebView2 says;
    ``turn_on`` turns both on first. pywebview 6.2 turns both off unless it runs for debugging, which would also open
    the developer tools; here they are turned on by themselves, and the developer tools stay off. WebView2 takes its
    settings on the window's own thread only: given to it there (``Invoke``, with ``delegate``, .NET's Func[Type])."""
    if delegate is None:
        from System import Func, Type  # pythonnet, which pywebview's Windows part has loaded

        delegate = Func[Type]
    form = window.native
    found: dict[str, bool] = {}

    def on_the_windows_thread():
        settings = form.webview.CoreWebView2.Settings
        if turn_on:
            settings.AreDefaultContextMenusEnabled = True
            settings.AreBrowserAcceleratorKeysEnabled = True
        found["menu"] = bool(settings.AreDefaultContextMenusEnabled)
        found["keys"] = bool(settings.AreBrowserAcceleratorKeysEnabled)

    form.Invoke(delegate(on_the_windows_thread))
    return found


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

    def page_ready(self, path: str = "", port: str = "") -> None:
        """CloseDesk's page loaded in the window (CI's check of the real window waits for this), from ``port``."""
        self._app.page_ready(str(path or "")[:200], "".join(ch for ch in str(port or "")[:10] if ch.isdigit()))


# The window, and the CloseDesk in it ---------------------------------------------------------------------------------


def _health(url: str) -> dict | None:
    """What CloseDesk's /health at ``url`` says; None when nothing answers there as CloseDesk."""
    try:
        with _LOCAL.open(url + "health", timeout=2) as reply:
            body = json.load(reply)
    except (OSError, ValueError, AttributeError):
        return None
    return body if isinstance(body, dict) and body.get("app") == "closedesk" else None


def _answers(url: str) -> bool:
    """Whether this CloseDesk (this folder's, run by this person) answers at ``url``."""
    body = _health(url)
    return body is not None and version.is_mine(body)


def _theirs(url: str) -> bool:
    """Whether another CloseDesk answers at ``url``: another folder's, or another person's on a shared computer (every
    person on it has the same 127.0.0.1). The window never shows it."""
    body = _health(url)
    return body is not None and not version.is_mine(body)


def url_for(host: str | None, port: int) -> str:
    """The address the window shows CloseDesk at: this computer's own when CloseDesk listens on every address."""
    shown = "127.0.0.1" if host in (None, "0.0.0.0", "", "::") else host
    if ":" in shown:
        shown = f"[{shown}]"
    return f"http://{shown}:{port}/"


def _working(url: str) -> bool:
    """Whether the CloseDesk at ``url`` is processing mail (or reading scans): what closing it would cut off."""
    try:
        with _LOCAL.open(url + "process/status", timeout=1) as reply:
            return json.load(reply).get("state") == "running"
    except (OSError, ValueError, AttributeError):
        return False


def _taken(url: str) -> bool:
    """Whether something holds CloseDesk's port at ``url`` (a CloseDesk too busy to answer in time is still there)."""
    parts = urlsplit(url)
    try:
        with socket.create_connection((parts.hostname or "127.0.0.1", parts.port or 80), timeout=1):
            return True
    except OSError:
        return False


def process_runs(pid: int, started: float = 0.0, *, mine: bool = False) -> bool:
    """Whether the program ``pid``, which said at ``started`` that it was updating, still runs. ``mine``: this
    person's update, whose program they can always ask about: one they may not is another program, given the number
    since. (launch.py has a copy, for before CloseDesk is installed.)"""
    if pid == os.getpid():
        return True
    if os.name == "nt":
        return _windows_process_runs(pid, started, mine=mine)
    try:
        os.kill(pid, 0)  # asks, sends nothing
    except ProcessLookupError:
        return False
    except PermissionError:  # there, another user's: when it started says whether it is the update
        if mine:
            return False  # this person's update is theirs to ask about: another program has its number
    except OSError:
        return False
    began = _started_at(pid) if started else None
    # One started after the update said so isn't it: the number was given to another program since (on a Mac or Linux
    # the update becomes the check after it, which leaves the mark behind when it ends).
    return began is None or began <= started + 5


def _started_at(pid: int) -> float | None:
    """When the program ``pid`` started (seconds since 1970), as ps says (Mac, Linux); None when it can't be told."""
    try:
        done = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
        text = " ".join(done.stdout.split())
        return time.mktime(time.strptime(text, "%a %b %d %H:%M:%S %Y")) if done.returncode == 0 and text else None
    except (OSError, ValueError, subprocess.SubprocessError, OverflowError):
        return None


def _windows_process_runs(pid: int, started: float, *, mine: bool = False) -> bool:
    """Asked of Windows (os.kill would end the program). One started after the update said it was running isn't it:
    Windows gave the number to another program since. Refused (a SYSTEM program, another person's), it isn't this
    person's update (``mine``): only another person's may be it."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5 and not mine  # access denied: there, a SYSTEM program or another user's
    try:
        code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != 259:  # STILL_ACTIVE
            return False
        created, ended, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if started and kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in (created, ended, kernel, user))):
            when = ((created.dwHighDateTime << 32) | created.dwLowDateTime) / 1e7 - 11_644_473_600
            if when > started + 5:
                return False
        return True
    finally:
        kernel32.CloseHandle(handle)


def update_running(root: Path | None = None) -> bool:
    """Whether an update runs in CloseDesk's folder now (launch.py --update, from its start to the end of the check after
    it, says so in ``UPDATING_MARK``); one whose program is gone doesn't count."""
    try:
        said = json.loads(((root or ROOT) / UPDATING_MARK).read_text(encoding="utf-8"))
        pid, started = int(said["pid"]), float(said["started"])
        me = version.whose()["user"]
        # Whose update it is: this person's unless another person's mark says so (an older CloseDesk's doesn't say).
        mine = str(said.get("user") or me) == me
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
    return 0 <= time.time() - started < UPDATE_LONGEST and process_runs(pid, started, mine=mine)


def _said(exc: BaseException) -> str:
    return " ".join(f"{type(exc).__name__}: {exc}".split())[:200]


def _seconds(name: str) -> float:
    """A number of seconds from the environment; 0 when it isn't one."""
    try:
        return max(0.0, float(os.environ.get(name) or 0))
    except ValueError:
        return 0.0


def _environment(**more: str) -> dict:
    """The environment for a program the window starts: its own, without what makes the window's Python CloseDesk.app
    to macOS."""
    env = {**os.environ, **more}
    env.pop(MAC_APP_PATH, None)
    return env


class App:
    """One CloseDesk window, with the CloseDesk it started (``owned``) or found already open at ``url``."""

    def __init__(self, webview, *, url: str, port: int, owned: bool, log: Path, host: str | None = None) -> None:
        self.webview = webview
        self.url, self.port, self.owned, self.log, self.host = url, port, owned, log, host
        self.window = None
        self.server = None  # the CloseDesk this window started, while it runs
        self.update = None  # the update under way, or the last one
        self.update_for = None  # the CloseDesk that update was for (it stopped for it)
        self.updating = False
        self.closing = False  # the window is going: nothing starts again
        self.on_purpose = False  # CloseDesk closes the window itself: nothing is asked
        self.fallback: tuple[str, str] | None = None  # the window can't show CloseDesk: the browser does
        self.code = 0
        self.stopped = False  # what follows the window's close is done (once: its close, or macOS quitting)
        self.closed_said = False  # the window's last line is in the log
        self.ready = threading.Event()  # CloseDesk's page said it loaded
        self.ready_path = ""
        self.ready_port = ""  # the port the page says it came from (CI's check: the CloseDesk it was to show)
        self.smoke = _seconds(SMOKE_ENV)
        self.smoke_hold = _seconds(SMOKE_HOLD_ENV)
        self.smoke_quit = bool(self.smoke and os.environ.get(SMOKE_QUIT_ENV))
        self.platform = sys.platform
        self._lock = threading.Lock()
        # What runs things on the computer (the tests give their own).
        self.popen = subprocess.Popen
        self.answers = _answers
        self.theirs = lambda: _theirs(self.url)
        self.port_taken = lambda: _taken(self.url)
        self.pick_port = self._pick_port
        self.update_running = update_running
        self.working = lambda: _working(self.url)
        self.open_url = webbrowser.open
        self.open_launcher = open_launcher
        self.when_quitting = when_mac_quits
        self.terminate = quit_mac_app
        self.storage = user_dir() / "window"
        # Written by the CloseDesk this window started the moment it is to stop to update (updates.WINDOW_ASKED_ENV).
        self.asked_mark = user_dir() / f"update-asked-{os.getpid()}"
        self.delegate = None  # what Invoke takes on Windows (.NET's Func[Type]; the tests give their own)
        # macOS: WebKit's right-click menu, the menu bar's keys, a find bar and zoom, and downloads saved over a file,
        # put back (mac_window.py; the tests give their own).
        self.mac_prepare = mac_window.prepare
        self.mac_menus = mac_window.menus_on
        self.mac_read_back = mac_window.read_back
        self.load_wait, self.up_wait, self.other_wait, self.poll = LOAD_WAIT, UP_WAIT, OTHER_WAIT, 0.25
        self.watch, self.gone_for_good, self.update_longest = WATCH_EVERY, GONE_FOR_GOOD, UPDATE_LONGEST
        self._update_until = 0.0

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
        self.window.events.closed += self.on_closed
        if self.platform == "darwin":
            # Quit (Cmd+Q, the Dock's Quit, logging out), macOS ends the program before webview.start() returns.
            self.when_quitting(self.quitting)
            for missed in self.mac_prepare():
                print(f"The window couldn't put back {missed}: it works without it.", flush=True)
        try:
            # Not private: the pages keep their own settings (theme, drafts, what was said) from one start to the next.
            webview.start(self.supervise, private_mode=False, storage_path=self._storage(), icon=self._icon())
        except Exception as exc:
            traceback.print_exc()
            if self.fallback is None:
                self.fallback = ("broken", NO_PART.format(detail=_said(exc)))
        self.stop()
        return self.code

    def _icon(self) -> str | None:
        """The icon pywebview gives the window's app. None on a Mac where this runs as CloseDesk.app, whose own icon
        (made for this macOS) the Dock shows already: pywebview's would take its place while CloseDesk is open."""
        if self.platform == "darwin" and mac_bundle_id() == BUNDLE_ID:
            return None
        return icon_path(platform=self.platform)

    def _storage(self) -> str | None:
        """Where the window keeps the pages' own settings; None (pywebview's own folder) when it can't be written,
        which pywebview would refuse to start with."""
        try:
            self.storage.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        return str(self.storage) if os.access(self.storage, os.W_OK) else None

    def _size(self) -> tuple[int, int]:
        """A good size for CloseDesk, never bigger than the screen."""
        try:
            screen = self.webview.screens[0]
            width, height = int(screen.width), int(screen.height)
        except Exception:
            return SIZE
        return min(SIZE[0], int(width * 0.9)), min(SIZE[1], int(height * 0.88))

    def stop(self) -> None:
        """The window has closed: the CloseDesk it started stops. An update under way is a program of its own and
        finishes by itself (one stopped half way would leave CloseDesk half updated). On a Mac the window's program
        leaves at once, waiting for neither: kept with no window, it would stay in the Dock, where opening CloseDesk
        again only brings it forward and nothing appears. CloseDesk stops by itself once its input ends, and ends in
        time whatever still runs in it (``cli.leave_within``). Elsewhere the window waits for the update and for
        CloseDesk to stop, but not for good.

        Closed after Update now but before CloseDesk had stopped for it (it takes a few seconds), the window waits for
        CloseDesk to stop, a Mac's too, and then starts the update the person asked for, as a program of its own: unless
        the window had started that one already (an update the window ran before, done, was for an earlier CloseDesk).
        Done once, whether the window closed or macOS quit CloseDesk (``quitting``)."""
        with self._lock:
            if self.stopped:
                return
            self.stopped = self.closing = True
            server, update, updated = self.server, self.update, self.update_for
        if update is not None and update.poll() is None:
            if self.platform == "darwin":
                print("The window was closed during the update: the update finishes by itself.", flush=True)
            else:
                print("The window was closed during the update: the update finishes first.", flush=True)
                self._wait_for_update(update)
        if server is not None and server.poll() is None:
            if self.platform == "darwin" and not self._update_asked():
                self._let_go(server)
            else:
                self._end(server)
        if server is not None and server is not updated and server.poll() == UPDATE:
            self._update_after_close(server)
        self._forget_update_asked()

    def quitting(self) -> None:
        """macOS: CloseDesk is being quit (Cmd+Q, the Dock's Quit, logging out), and the window's closing handler said
        yes. macOS ends the program once this returns, without webview.start() returning: what follows the window's
        close is done here (CloseDesk it started stops, and an update asked for still runs), with its last line."""
        print(QUIT, flush=True)
        self.stop()
        print(CLOSED.format(code=self.code), flush=True)
        self.closed_said = True
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (AttributeError, OSError, ValueError):
                pass

    def _update_after_close(self, server) -> None:
        """CloseDesk the window started stopped to update once the window had closed: the update runs all the same, as
        a program of its own (waited for as an update the window was closed during is: not on a Mac)."""
        print("CloseDesk stopped to update after its window was closed: the update runs by itself.", flush=True)
        self._update_until = time.monotonic() + self.update_longest
        self.update_for = server
        try:
            self.update = self._start_update()
        except OSError as exc:  # the next opening says CloseDesk wasn't updated, and where to read why
            print(f"The update couldn't be started: {_said(exc)}", flush=True)
            return
        if self.platform != "darwin":
            self._wait_for_update(self.update)

    def _update_asked(self) -> bool:
        """Whether the CloseDesk this window started said it stops to update (Update now was answered)."""
        try:
            return self.asked_mark.is_file()
        except OSError:
            return False

    def _forget_update_asked(self) -> None:
        try:
            self.asked_mark.unlink(missing_ok=True)
        except OSError:
            pass

    def _let_go(self, server) -> None:
        """Ask CloseDesk the window started to stop (its input ends), without waiting for it."""
        try:
            server.stdin.close()
        except (OSError, AttributeError, ValueError):
            pass

    def _wait_for_update(self, update) -> int:
        """The update's exit code once it ends; given up on (and ended) past ``update_longest``: something holds it
        that won't let go."""
        try:
            return update.wait(max(0.0, self._update_until - time.monotonic()))
        except subprocess.TimeoutExpired:
            print(f"The update was still running after {self.update_longest / 60:g} minutes: the window gave up on it.", flush=True)
            try:
                update.terminate()
                update.wait(10)
            except (OSError, subprocess.TimeoutExpired):
                pass
            return 1

    def _end(self, server) -> None:
        """Stop CloseDesk the window started, as Ctrl+C would: its input ends. Ended outright if it doesn't."""
        self._let_go(server)
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
        if self.updating or (self.owned and self._update_asked()):
            # Updating, or about to: CloseDesk stops to update (a few seconds) before the update starts.
            words = UPDATING_CLOSE
        elif self.owned and self.working():
            words = BUSY_CLOSE
        else:
            words = ""
        if words:
            window.localization["global.quitConfirmation"] = words
        window.confirm_close = bool(words)
        return None

    def on_closed(self) -> None:
        """The window is gone: nothing more is shown in it, and nothing starts again."""
        with self._lock:
            self.closing = True

    def _ask_on_close(self, words: str) -> None:
        """What pywebview's "close anyway?" asks from now on (macOS reads it before the closing handler runs)."""
        try:
            self.window.localization["global.quitConfirmation"] = words
        except (AttributeError, TypeError):
            pass

    def close_on_purpose(self) -> None:
        """CloseDesk closes its window itself (for the browser, the launcher, or CI's check): nothing is asked."""
        self.on_purpose = True
        with self._lock:
            self.closing = True
        try:
            self.window.destroy()
        except Exception as exc:
            print(f"Couldn't close the window: {_said(exc)}", flush=True)

    def page_ready(self, path: str, port: str = "") -> None:
        self.ready_path, self.ready_port = path, port
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
        if self.platform == "win32":
            self._browser_keys_on()
        if self.platform == "darwin":
            why = self.mac_menus(self.window)
            if why:
                print(f"Couldn't add all of the menu bar's keys, the find bar and zoom: {why}", flush=True)
            named = mac_app_name()
            if named:
                # "Python" here means the Dock shows the window as Python's (CloseDesk.app's link wasn't used).
                print(f"macOS shows the window as {named}.", flush=True)
        reopen = chosen_again = False
        again = ""  # the page shown again once CloseDesk is back after an update ("": its first page)
        while not self.closing:
            if not self.owned:
                # A CloseDesk this window didn't start: shown, and left open when the window closes. Watched meanwhile:
                # when it stops for good (closed with its own window), this window starts CloseDesk itself.
                self._show_closedesk()
                if not self._watch_other():
                    return
                self.owned, reopen = True, False
                self._show(opening_page())
                if not self._choose_port():
                    return
                continue
            server = self._start_server(reopen=reopen)
            if server is None:
                return
            if not self._wait_up(server):
                if self.closing:
                    return
                if server.poll() is None:  # started, and never answered
                    self._end(server)
                    self._failed(not_open_page(self.log))
                elif server.poll() in TAKEN and self._another_answers():
                    # Another CloseDesk opened on the port meanwhile (a second double-click raced the first, and this
                    # one found it there): shown, as one already open is, and left open when the window closes.
                    print("Another CloseDesk opened on the same port meanwhile: the window shows that one.", flush=True)
                    self.owned = False
                    continue
                elif server.poll() == PORT_TAKEN and not chosen_again and self._choose_port():
                    # Another program took the port the window chose as CloseDesk started: once more, on another.
                    chosen_again = True
                    print(f"Another program took CloseDesk's port as it started: the window opens it on port {self.port}.", flush=True)
                    continue
                else:
                    print(f"CloseDesk stopped as it started (exit code {server.poll()}).", flush=True)
                    self._failed(stopped_page(self.log))
                return
            self._show_closedesk(again)
            code = self._until_stopped(server)
            if self.closing:
                return
            if code != UPDATE:
                print(f"CloseDesk stopped by itself (exit code {code}).", flush=True)
                self._failed(stopped_page(self.log))
                return
            # What the person was looking at, shown again as the new version (a browser tab loads it again too).
            again = self._page_shown()
            if not self._update():
                return
            reopen = True

    def _start_server(self, *, reopen: bool):
        cmd = [sys.executable, "-m", "controller_inbox", "run", "--no-browser"]
        if self.host:
            cmd += ["--host", self.host]
        cmd += ["--port", str(self.port)]
        env = _environment(**{
            # Update now stops CloseDesk for the window to update and start again: offered, as with a launcher.
            updates.LAUNCHER_ENV: "1",
            updates.APP_ENV: "1",
            updates.APP_LOG_ENV: str(self.log),
            STOP_WITH_PARENT: "1",
            EXACT_PORT: "1",
            # Written the moment it is to stop to update: closing the window meanwhile asks first.
            updates.WINDOW_ASKED_ENV: str(self.asked_mark),
        })
        self._forget_update_asked()  # one left from before isn't this CloseDesk's
        env.pop("CLOSEDESK_REOPEN", None)
        if reopen:
            env["CLOSEDESK_REOPEN"] = "1"  # on the port it was on, where the window looks for it
        with self._lock:
            if self.closing:
                return None
            self.server = self.popen(cmd, cwd=str(ROOT), env=env, stdin=subprocess.PIPE, creationflags=NO_WINDOW, **_output())
            # launch.py's word that the environment was just made is for the first CloseDesk only (shortcut.py):
            # not the one after an update, nor the update.
            os.environ.pop(NEW_ENVIRONMENT_ENV, None)
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

    def _until_stopped(self, server) -> int:
        """CloseDesk shown until it stops: its exit code. Once it says it stops to update, closing the window asks so
        from then on (macOS reads the question before the closing handler runs)."""
        told = False
        while True:
            try:
                return server.wait(None if told else self.poll)
            except subprocess.TimeoutExpired:
                if self._update_asked():
                    self._ask_on_close(UPDATING_CLOSE)
                    told = True
                    if self.smoke_quit:
                        self._quit_for_check()

    def _watch_other(self) -> bool:
        """The window shows a CloseDesk it didn't start. True when that one has gone for good: it stopped answering, its
        port is free, and no update runs in its folder (an update brings it back on the same port) for
        ``gone_for_good`` seconds. Back meanwhile (updated), it is shown again. False once the window closes."""
        away = None
        while not self.closing:
            time.sleep(self.watch)
            if self.closing:
                return False
            if self.answers(self.url):
                if away is not None:
                    print("The CloseDesk the window shows is back: shown again.", flush=True)
                    away = None
                    self._show_closedesk(self._page_shown())
                continue
            if self.theirs():
                # Another folder's CloseDesk, or another person's, has the port now: never shown in this window.
                print("Another CloseDesk (another folder's, or another person's) answers on the port now: the window starts CloseDesk itself.", flush=True)
                return True
            if self.port_taken():
                continue  # still there, busy: too slow to answer in time
            if away is None:
                print("The CloseDesk the window shows has stopped: the window waits a moment, since an update brings it back.", flush=True)
            if away is None or self.update_running():
                away = time.monotonic()
                continue
            if time.monotonic() - away >= self.gone_for_good:
                print("It didn't come back (closed with its own window): the window starts CloseDesk itself.", flush=True)
                return True
        return False

    def _another_answers(self) -> bool:
        """Whether this CloseDesk, started by another window (or launcher), answers on the port in time. Another
        folder's, or another person's, doesn't count."""
        deadline = time.monotonic() + self.other_wait
        while not self.closing:
            if self.answers(self.url):
                return True
            if self.theirs() or time.monotonic() >= deadline:
                return False
            time.sleep(self.poll)
        return False

    def _pick_port(self, port: int) -> tuple[int, bool]:
        from controller_inbox import cli

        return cli._port_to_use(urlsplit(self.url).hostname or "127.0.0.1", port)

    def _choose_port(self) -> bool:
        """Before the window starts CloseDesk itself in place of one it showed (or once more): the port chosen again,
        as the shortcut chose it, since the one it had may be another program's now. The CloseDesk found on the way,
        if any, is shown instead. False (said in the window) when none is free."""
        port, running = self.pick_port(self.port)
        if not port:
            self._failed(_page("CloseDesk can't open", NO_PORT.format(port=self.port)))
            return False
        if port != self.port:
            self.port, self.url = port, url_for(urlsplit(self.url).hostname, port)
        self.owned = not running
        return True

    def _show(self, html: str) -> None:
        try:
            self.window.load_html(html)
        except Exception as exc:
            print(f"Couldn't show it in the window: {_said(exc)}", flush=True)

    def _failed(self, html: str) -> None:
        """Say in the window what went wrong. In the window's check, that fails the check, and the window closes."""
        self._show(html)
        if self.smoke:
            self.code = 1
            self.close_on_purpose()

    def _page_shown(self) -> str:
        """The CloseDesk page the window shows now (its path, with what follows it), as the window reads its address,
        which follows the workspace from page to page; the page's own word when the window can't say. "" when it isn't
        one of this CloseDesk's."""
        try:
            # pywebview waits (20 seconds) for a page that hasn't loaded before it says: the page's word then.
            loaded = self.window.events.loaded.is_set()
        except AttributeError:
            loaded = True
        try:
            current = str(self.window.get_current_url() or "") if loaded else ""
        except Exception:
            current = ""
        if not current:
            return self.ready_path
        shown, here = urlsplit(current), urlsplit(self.url)
        if shown.scheme != here.scheme or shown.netloc != here.netloc:
            return ""
        return shown.path + (f"?{shown.query}" if shown.query else "") + (f"#{shown.fragment}" if shown.fragment else "")

    def _address(self, path: str = "") -> str:
        """Where the window shows CloseDesk: at ``path``, one of its pages (the one shown before), or its first page."""
        if not path:
            return self.url
        path = SENT_BACK.get(urlsplit(path).path, path)
        if not path.startswith("/") or path.startswith("//") or len(path) > 2000 or path.startswith(NOT_PAGES):
            return self.url
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
            return self.url
        return self.url.rstrip("/") + path

    def _show_closedesk(self, path: str = "") -> None:
        """CloseDesk in the window: at ``path`` (the page it showed before an update), or its first page."""
        self.ready.clear()
        try:
            self.window.load_url(self._address(path))
        except Exception as exc:
            print(f"Couldn't show CloseDesk in the window: {_said(exc)}", flush=True)
            return
        if not self.smoke:
            return
        shown = self._check_shows()
        if shown and (self.ready_path != "/app" or path):
            # The other UI too, so that its own scripts run in the real window's engine: the workspace after "/" (the
            # check's folder has no mail, so that is a classic page), and a classic page after the page shown again
            # after an update (the workspace, where the check pressed Update now).
            self.ready.clear()
            self.window.load_url(self.url if self.ready_path.startswith("/app") else self.url + "app")
            shown = self._check_shows()
        if shown:
            self.code = 0
            if self.platform == "win32":
                self._say_browser_keys()
            elif self.platform == "darwin":
                self._say_mac_keys()
            # Kept open a while (the first time) for the check to look at it, or press Update now; closed meanwhile, it
            # isn't closed again, and CloseDesk stopping meanwhile (to update) is followed through.
            until, self.smoke_hold = time.monotonic() + self.smoke_hold, 0
            while not self.closing and time.monotonic() < until:
                if self.owned and self.server is not None and (self.server.poll() is not None or self._update_asked()):
                    return
                time.sleep(min(self.poll, 0.25))
            if self.closing:
                return
        else:
            self.code = 1
        self.close_on_purpose()

    def _check_shows(self) -> bool:
        """In the window's check: whether the page loaded in it says it loaded, in time."""
        if self.ready.wait(self.smoke):
            port = f" (port {self.ready_port})" if self.ready_port else ""
            print(f"The CloseDesk window showed {self.ready_path}{port}", flush=True)
            return True
        print(f"The CloseDesk window didn't show CloseDesk's page within {self.smoke:g} seconds.", flush=True)
        return False

    def _browser_keys_on(self) -> None:
        """Windows: the right-click menu and the browser's own keys, as in a browser (``browser_keys``). Turned on once
        the window's first page (its own "Opening CloseDesk") has loaded, before CloseDesk's is shown."""
        try:
            browser_keys(self.window, delegate=self.delegate, turn_on=True)
        except Exception as exc:  # the window still shows CloseDesk, as pywebview left it
            print(f"Couldn't turn on the window's right-click menu and browser keys: {_said(exc)}", flush=True)

    def _say_browser_keys(self) -> None:
        """In the window's check on Windows: whether the right-click menu and the browser's keys are on still, now that
        CloseDesk's page shows, read back from WebView2."""
        try:
            found = browser_keys(self.window, delegate=self.delegate)
        except Exception as exc:
            print(f"Couldn't read the window's right-click menu and browser keys: {_said(exc)}", flush=True)
            return
        if found.get("menu") and found.get("keys"):
            print(BROWSER_KEYS_ON, flush=True)
        else:
            menu = "on" if found.get("menu") else "off"
            keys = "on" if found.get("keys") else "off"
            print(f"The window's right-click menu is {menu}, and its browser keys {keys}.", flush=True)

    def _say_mac_keys(self) -> None:
        """In the window's check on a Mac: whether WebKit's right-click menu, the menu bar's keys, the find bar and
        zooming are on, now that CloseDesk's page shows, read back from the window itself."""
        try:
            found = self.mac_read_back(self.window)
        except Exception as exc:
            print(f"Couldn't read the window's right-click menu, keys, find bar and zoom: {_said(exc)}", flush=True)
            return
        if mac_window.all_on(found):
            print(mac_window.KEYS_ON, flush=True)
        else:
            print(f"The window's right-click menu, keys, find bar and zoom aren't all on: {found}", flush=True)

    def _update(self) -> bool:
        """Update now: CloseDesk stopped to be updated. The window says so throughout; True when CloseDesk is to be
        started again (updated, or as it was when the update didn't finish: its first page says so)."""
        self._ask_on_close(UPDATING_CLOSE)
        self.updating = True
        self._forget_update_asked()  # acted on here: the window says it is updating from now on
        self._show(updating_page())
        print("Updating CloseDesk...", flush=True)
        try:
            with self._lock:
                if self.closing:
                    return False
                self._update_until = time.monotonic() + self.update_longest
                self.update_for = self.server
                self.update = self._start_update()
            if self.smoke_quit:
                self._quit_for_check()  # CloseDesk had stopped before the window saw Update now answered
            code = self._wait_for_update(self.update)
            if code == UPDATE_UNDER_WAY:
                # Another update runs in the folder (CloseDesk-Update, double-clicked meanwhile): CloseDesk opened now
                # would run from a folder that update is about to replace. Whether it did what Update now asked is
                # told from the version CloseDesk was when it stopped: the folder's may be the new one already.
                before = updates.stopped_as() or version.installed()[0]
                if self._wait_for_other_update() and not _update_did_it(before):
                    # It left CloseDesk as it was (its download failed, say): the update asked for runs now, once.
                    print("That update didn't finish, so the window's own update runs now.", flush=True)
                    with self._lock:
                        if self.closing:
                            return False
                        self.update = self._start_update()
                    code = self._wait_for_update(self.update)
                    if code == UPDATE_UNDER_WAY:
                        self._wait_for_other_update()
        finally:
            self.updating = False
            self._ask_on_close(BUSY_CLOSE)
        if code == REBUILD:
            # The environment has to be made again, which a Python running from it can't do: the launcher does, in its
            # window, then starts CloseDesk.
            print("After the update, CloseDesk's environment has to be made again: the double-click launcher does that.", flush=True)
            self.open_launcher()
            self.close_on_purpose()
            return False
        if code and code != UPDATE_UNDER_WAY:
            print(f"The update ended with exit code {code}: CloseDesk opens as it is.", flush=True)
        return not self.closing

    def _quit_for_check(self) -> None:
        """CI's check on a Mac: quit as Cmd+Q does (macOS's terminate:), with nothing asked, once Update now is answered:
        the update has to run all the same, by itself."""
        if self.platform != "darwin" or self.on_purpose:
            return
        print("The check quits CloseDesk as Cmd+Q does.", flush=True)
        self.on_purpose = True
        try:
            self.terminate()
        except Exception as exc:
            print(f"Couldn't quit CloseDesk as Cmd+Q does: {_said(exc)}", flush=True)
            self.code = 1
            self.close_on_purpose()

    def _start_update(self):
        """``launch.py --update``: a program of its own, which nothing that ends the window ends (a Mac's window leaves
        at once when it is closed meanwhile), with no console window."""
        env = _environment(**{updates.APP_ENV: "1", updates.APP_LOG_ENV: str(self.log)})
        for name in (STOP_WITH_PARENT, "CLOSEDESK_REOPEN", updates.WINDOW_ASKED_ENV):
            env.pop(name, None)
        return self.popen(
            [sys.executable, str(ROOT / "scripts" / "launch.py"), "--update"], cwd=str(ROOT), env=env,
            stdin=subprocess.DEVNULL, creationflags=NO_WINDOW, start_new_session=os.name != "nt", **_output(),
        )

    def _wait_for_other_update(self) -> bool:
        """Another update runs in CloseDesk's folder: the window keeps saying CloseDesk is updating until that one is
        done (its mark is gone), as it waits before showing a CloseDesk that is updating; then CloseDesk opens. Not past
        ``update_longest`` (CloseDesk opens as it is), nor once the window is closed (that update finishes by itself).
        True when that update ended."""
        print("Another update runs in CloseDesk's folder (CloseDesk-Update, say): the window waits for it to finish.", flush=True)
        while not self.closing:
            if not self.update_running():
                print("The other update is done.", flush=True)
                return True
            if time.monotonic() >= self._update_until:
                print(f"The other update was still running after {self.update_longest / 60:g} minutes: CloseDesk opens as it is.", flush=True)
                return False
            time.sleep(self.watch)
        return False


def _update_did_it(before: str) -> bool:
    """Whether an update that ended did what Update now asked: this copy (``before``, its commit) was updated, or it was
    found the newest already (launch.py says so in updates.NEWEST_NOTE). A developer's copy, which isn't updated this
    way, counts as done."""
    after = version.installed()[0]
    if not after or after != before:
        return True
    try:
        newest = (version.ROOT / ".venv" / updates.NEWEST_NOTE).read_text(encoding="utf-8").strip().lower()
    except OSError:
        return False
    return newest == after.lower()


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
    return App(webview, url=url_for(host, port), port=port, owned=not running, log=log_path(), host=args.host)


def _cant_start(exc: BaseException) -> str:
    from controller_inbox import doctor

    problems: list[str] = []
    try:
        for check in doctor.checks(port=False):
            if check.state == doctor.PROBLEM:
                problems.append(check.text + (f" {check.todo}" if check.todo else ""))
    except Exception:
        pass
    return CANT_START.format(problems="\n".join(problems) or _said(exc), log=app_log.shown(log_path()))


def _leave(code: int) -> None:
    """End the window's process now: a GUI library can leave a thread behind that would keep it running unseen."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (AttributeError, OSError, ValueError):
            pass
    os._exit(code)


def main(args) -> int:
    """``controller_inbox app``: open the CloseDesk window, or the browser where it can't open. What stops it opening
    is said in a message box (it has no console), and it ends with ``SAID``."""
    app_log.write_at_end()
    use_log_if_no_console()
    try:
        return _open(args)
    except Exception:
        traceback.print_exc()
        launcher = "CloseDesk.bat" if sys.platform == "win32" else "CloseDesk.command"
        tell(app_log.CANT_OPEN.format(log=app_log.shown(log_path()), launcher=launcher))
        return SAID


def _open(args) -> int:
    set_app_id()
    settle_mac_name()
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
        return SAID  # said in a message box
    code = app.run()
    if app.fallback:
        code = browser_instead(app.fallback, args)
    if not getattr(app, "closed_said", False):
        print(CLOSED.format(code=code), flush=True)
    _leave(code)
    return code  # pragma: no cover (not reached)
