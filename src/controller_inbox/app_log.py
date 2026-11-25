"""The CloseDesk app's log, and how it says it can't open when there is no console (the shortcut runs it with no
console window): only the standard library here, so it works even when a part CloseDesk needs won't load.

What the app's programs say goes to the log launch.py names (``CLOSEDESK_APP_LOG``): the window's, the CloseDesk it
starts, the update. Each opens it again for itself, for adding at its end: Windows passes on the open file, not the way
it was opened, and a program writing to it as passed on writes where the one that passed it last wrote, which is over
the lines of a second CloseDesk window when there are two.

When the window's program fails before it can say anything itself (Python stops while loading a part a company's
policy blocks, say), ``cant_open`` says so in a message box (an alert on a Mac), naming the log, and the program ends
with ``SAID``: launch.py, which says the same for a window that ends with an error, then says nothing more.
"""

from __future__ import annotations

import os
import subprocess
import sys
import traceback
from pathlib import Path

TITLE = "CloseDesk"
# Set by launch.py for the app's programs (controller_inbox.updates has the same names).
APP_ENV = "CLOSEDESK_APP"
APP_LOG_ENV = "CLOSEDESK_APP_LOG"
# CI's check of the real window: no message box waits for a click no one makes (app_window.SMOKE_ENV).
SMOKE_ENV = "CLOSEDESK_APP_SMOKE"
# How the window's program ends when it has said why it couldn't open (launch.py's SAID).
SAID = 4
CANT_OPEN = (
    "CloseDesk couldn't open. What went wrong is written in {log}. Double-click {launcher} in CloseDesk's folder to "
    "open it in your browser instead: its window shows what goes wrong."
)


def user_dir() -> Path:
    """This person's own folder for CloseDesk (no admin rights needed): the one the launchers keep uv in."""
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "CloseDesk"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "CloseDesk"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "CloseDesk"


def store_python(sys_module=None) -> bool:
    """Whether CloseDesk runs on the Microsoft Store's Python (a packaged app: its folder is in WindowsApps, and an
    environment made with it keeps that as its base). Windows keeps a file that Python writes in AppData to that Python
    alone, in a folder of its own (``shown``): Explorer, a shortcut, Notepad don't find it where it was written.
    (launch.py has a copy.)"""
    sys_module = sys_module or sys
    if getattr(sys_module, "platform", "") != "win32":
        return False
    places = [getattr(sys_module, name, "") for name in ("_base_executable", "base_prefix", "executable")]
    return any("\\windowsapps\\" in str(place or "").lower() for place in places)


def shown(file, *, store: bool | None = None, realpath=os.path.realpath) -> Path:
    """Where a person finds ``file``, which CloseDesk wrote: with the Microsoft Store's Python, a file written in
    AppData is in that Python's own folder (%LOCALAPPDATA%\\Packages\\PythonSoftwareFoundation.Python...\\LocalCache),
    which ``realpath`` says; elsewhere where it was written."""
    if not (store_python() if store is None else store):
        return Path(file)
    try:
        return Path(realpath(str(file)))
    except (OSError, ValueError):
        return Path(file)


def path() -> Path:
    """The app's log, as launch.py named it (its app_log_path)."""
    named = os.environ.get(APP_LOG_ENV, "")
    if named:
        return Path(named)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "CloseDesk" / "CloseDesk-app.log"
    return user_dir() / "CloseDesk-app.log"


def write_at_end(*, platform: str | None = None) -> None:
    """In the app (``CLOSEDESK_APP=1``, with its log named): what this program says is added at the end of the log,
    opened again here. On Windows the log this program was given as its output is passed on as an open file whose
    place is shared with the program that passed it (see the module's note); elsewhere it already adds at its end, and
    is left as it is. With no output at all (pythonw), the log too."""
    if os.environ.get(APP_ENV) != "1" or not os.environ.get(APP_LOG_ENV):
        return
    if (platform or sys.platform) != "win32" and sys.stdout is not None and sys.stderr is not None:
        return
    log = path()
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        stream = open(log, "a", encoding="utf-8", errors="replace", buffering=1)  # noqa: SIM115 (open for good)
    except OSError:
        return
    for stream_of_old in (sys.stdout, sys.stderr):
        try:
            stream_of_old.flush()
        except (AttributeError, OSError, ValueError):
            pass
    for fd in (1, 2):
        # What writes beneath Python (a crash's report, a library's own messages) goes there too.
        try:
            os.dup2(stream.fileno(), fd)
        except OSError:
            pass
    sys.stdout = sys.stderr = stream


def tell(text: str, *, platform: str | None = None) -> None:
    """Say ``text`` where a person sees it with no console: a message box (Windows), an alert (macOS). Also logged."""
    platform = platform or sys.platform
    try:
        print(text, flush=True)
    except (AttributeError, OSError, ValueError):
        pass
    if os.environ.get(SMOKE_ENV):
        return  # no one is there to close it
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


def cant_open(*, platform: str | None = None) -> int:
    """The window's program failed before it could say anything itself: what went wrong goes to the log, and the
    person is told in a message box where it is. The exit code to end with (``SAID``)."""
    platform = platform or sys.platform
    try:
        traceback.print_exc()
    except (AttributeError, OSError, ValueError):
        pass
    launcher = "CloseDesk.bat" if platform == "win32" else "CloseDesk.command"
    tell(CANT_OPEN.format(log=shown(path()), launcher=launcher), platform=platform)
    return SAID
