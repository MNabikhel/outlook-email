"""What CloseDesk.command and CloseDesk.bat run on every start: check that CloseDesk is set up here, put right
whatever isn't, then start it. Only the standard library is used, so it runs before anything is installed.

On a start it checks, in order:
- that it runs inside the folder's own environment (``.venv``), creating it when it isn't there;
- that every package pyproject.toml asks for is installed and new enough (an update can add or raise one);
- that the environment runs this folder's code, not that of a folder it was copied or moved from;
- the optional add-ons (the OCR reader, the second table reader), tried once per Python and kept where they install.

Then it starts CloseDesk. Two exit codes ask the launcher to do what a running Python can't do to itself:
``REBUILD`` (remove ``.venv`` and run this again), and CloseDesk's own ``UPDATE`` (Settings' Update button: run this
with ``--update``, then start CloseDesk again). CloseDesk is told a launcher started it (``CLOSEDESK_LAUNCHER``), so
it offers that button only then. A third, ``UPDATE_UNDER_WAY``, says an update runs in the folder now, which was
said, and nothing was started: the launcher's window stays open on it, with nothing more. (The launcher's update after
Update now waits for that one instead, and CloseDesk opens again once it is done; when that one left CloseDesk as it
was, the update asked for runs then.)

The Python is whichever the launcher found on the computer; none is ever downloaded unasked. (The launcher's
``--get-python`` downloads one for CloseDesk alone, when the person asks for it.)

The CloseDesk app (``--app``, what the CloseDesk shortcut runs with pythonw, no console window) sets up the same way,
without a word on screen: what it says goes to its log (``app_log_path``), nothing it runs opens a console window
(CREATE_NO_WINDOW), and what goes wrong is said in a message box. Then it opens the CloseDesk window
(``controller_inbox app``, app_window.py), which handles ``UPDATE`` itself; ``REBUILD`` is left to the double-click
launcher, opened in its window, since a Python running from ``.venv`` can't remove it. On a Mac, Python is started
through the link in CloseDesk.app (shortcut.py), so the Dock and the menu bar say CloseDesk, not Python.

On a Mac with Apple's processor, an environment set up as the other kind (x86_64, from a Terminal opened under
Rosetta) is run as it was set up, whatever starts it (CloseDesk.app from the Dock runs as the Mac's own kind): its
compiled parts load only as that kind (``run_as_set_up``).

    launch.py                 set up if needed, then run CloseDesk
    launch.py --app           the same, silently, then open the CloseDesk window
    launch.py --shortcut      set up if needed, then add the CloseDesk shortcut again (Desktop and Start menu; Applications)
    launch.py --check         set up if needed, then say what is and isn't in order (no dashboard)
    launch.py --update        fetch the newest CloseDesk, keeping mail and settings, then check it
    launch.py --command NAME  set up if needed, then run that command (``overnight`` for the scheduler)
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 11):  # before anything newer is imported
    sys.stderr.write("CloseDesk needs Python 3.11 or newer; this is Python %d.%d.\n" % sys.version_info[:2])
    sys.exit(1)

import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_deps  # noqa: E402  (beside this file)

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
STATE = VENV / "closedesk-setup.json"
LOG = VENV / "closedesk-setup.log"

# The exit codes the launchers act on (see the module docstring). UPDATE is controller_inbox.updates.UPDATE_AND_RESTART.
REBUILD = 76
UPDATE = 75
UPDATE_UNDER_WAY = 77

WINDOWS = os.name == "nt"
# The app (``--app``): set for what it runs, so none of it opens a console window. The names are
# controller_inbox.updates' APP_ENV and APP_LOG_ENV.
APP_ENV = "CLOSEDESK_APP"
APP_LOG_ENV = "CLOSEDESK_APP_LOG"
# A console program started without this flashes a console window up from the app on Windows (0 elsewhere).
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# Windows and macOS open the app with no console (the shortcut, the Dock): what it says goes to its log there. On
# Linux, where only a developer runs --app, the terminal shows it.
APP_LOGS = sys.platform in ("win32", "darwin")
APP_LOG_LIMIT = 2_000_000  # a log grown past this starts afresh (the one before is kept as .old)
INSTALLING = "CloseDesk is installing what it needs. This takes a minute or two; it opens by itself when it is done."
UPDATING_NOW = "CloseDesk is being updated just now. Open it again in a minute or two, once the update is done."
# Update now, from CloseDesk a launcher started, while another update runs in the folder (CloseDesk-Update, double-clicked
# meanwhile): that one is waited for, looked at this often, and then the launcher opens CloseDesk again.
OTHER_UPDATE = "Another update is running in CloseDesk's folder (CloseDesk-Update, say). CloseDesk opens again by itself once it is done."
OTHER_DONE = "That update is done: CloseDesk opens again."
# That one ended with CloseDesk as it was (its download failed, say): the update asked for runs now, as asked.
OTHER_FAILED = "That update didn't finish, so the update you asked for runs now."
OTHER_UPDATE_EVERY = 3
# Set by the launchers for the update after Update now (and for the start after it): CloseDesk opens again after it,
# in the page that asked, which reloads itself.
REOPEN_ENV = "CLOSEDESK_REOPEN"
# The window's process ended with an error before it could say anything itself (a part of Python that won't load, say).
CANT_OPEN = (
    "CloseDesk couldn't open. What went wrong is written in {log}. Double-click {launcher} in CloseDesk's folder to "
    "open it in your browser instead: its window shows what goes wrong."
)
# How the window's process ends when it has said why it can't open itself (a message box of its own): not said again.
# (app_window.SAID)
SAID = 4
SETUP_FAILED = (
    "CloseDesk couldn't install what it needs. Most often the internet connection (or a company proxy) stopped a "
    "download: try again when it is back. What went wrong is written in {log}."
)

# CloseDesk.app on a Mac (controller_inbox/shortcut.py): where it is kept for the tests and the launcher check, and the
# link in it to the Python that runs CloseDesk. MAC_APP_PATH (app_window's) says where that Python was started from,
# which macOS reads first for the app a program belongs to.
DIR_ENV = "CLOSEDESK_SHORTCUT_DIR"
LINKED_PYTHON = "CloseDesk-python"
MAC_APP_PATH = "CFProcessPath"
# CloseDesk.app's id (app_window.BUNDLE_ID), and where its script says it was opened from: the person can move it (to
# /Applications, say). That one's link is tried first, and it is recorded (shortcut.json, shortcut.APPS_KEY), so that
# CloseDesk keeps it up to date and points it at a moved folder.
BUNDLE_ID = "io.github.mnabikhel.closedesk"
APP_BUNDLE_ENV = "CLOSEDESK_APP_BUNDLE"
APPS_KEY = "apps"
# CI's check of the real window (app_window's SMOKE_ENV): no message box waits there for a click no one makes.
SMOKE_ENV = "CLOSEDESK_APP_SMOKE"
# macOS: the kind of processor the environment was set up for, recorded in STATE ("arm64", or "x86_64": an Intel Mac, or
# a Terminal opened under Rosetta on Apple's). CloseDesk.app, opened from the Dock, runs as the Mac's own kind; an
# environment set up as the other is run as it was set up (``ARCH`` -x86_64, say), which ARCH_ENV says from then on.
MACHINE_KEY = "machine"
ARCH = "/usr/bin/arch"
ARCH_ENV = "CLOSEDESK_ARCH"
# A Mach-O file's processor (mach/machine.h: CPU_TYPE_X86_64, CPU_TYPE_ARM64).
MACH_O = {0x01000007: "x86_64", 0x0100000C: "arm64"}

REPO = "MNabikhel/outlook-email"
BRANCH = "main"
# What an update never touches: the mail, the settings and the environment. (Anything else the new version
# doesn't have, such as a folder you made, is left alone too.)
KEPT = {"data", "inbox", ".env", ".venv", "AP cost codes", ".previous", ".git"}
# An update says it is under way (its program, since when, its download folder) in this file in CloseDesk's folder, from
# its start to the end of the check after it: the CloseDesk app doesn't open meanwhile. One older than this is stale.
UPDATING_MARK = ".closedesk-updating"
UPDATE_LONGEST = 30 * 60
# Set by the update for the check after it, which the update's own mark doesn't stop. (The name stays: the update a
# copy runs hands over to the newest launch.py's check with it.)
AFTER_UPDATE_ENV = "CLOSEDESK_AFTER_UPDATE"
# Set for the start whose environment this launch.py has just set up (the first start, or the environment made again:
# an update by hand, the folders swapped; its Python removed). Meanwhile Windows offered to delete the CloseDesk shortcut,
# whose pythonw.exe was gone: CloseDesk puts it back (controller_inbox.shortcut.NEW_ENVIRONMENT_ENV).
NEW_ENVIRONMENT_ENV = "CLOSEDESK_NEW_ENVIRONMENT"
# An update that found this copy the newest already says so here (in .venv), for the next start: nothing went wrong.
# (controller_inbox.updates.NEWEST_NOTE)
NEWEST_NOTE = "closedesk-newest.txt"
# The version CloseDesk was when it stopped to update, said in this file in .venv (controller_inbox.updates.STOPPED_NOTE),
# heeded for this long after: CloseDesk-Update can have replaced the folder's files under the CloseDesk that stopped,
# which is then opened as the version there, with nothing downloaded again.
STOPPED_NOTE = "closedesk-stopped-as.txt"
STOPPED_FRESH = 10 * 60
UPDATED_UNDER = (
    "CloseDesk-Update had already updated CloseDesk's folder while CloseDesk was open, so nothing is downloaded again: "
    "CloseDesk opens as the new version now."
)
# The longest a download of the update may take, and how slow it may be for how long: a connection that stays open
# and sends nothing (a proxy holding it) ends instead of keeping the update, and the window, waiting.
DOWNLOAD_LONGEST = 15 * 60
SLOWEST = ("1024", "60")  # bytes a second, for that many seconds


def say(text: str = "") -> None:
    print(text, flush=True)


def _flush() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (AttributeError, OSError, ValueError):  # no console (pythonw): nothing to flush
            pass


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if WINDOWS else "bin/python")


def app_python() -> Path:
    """The Python the CloseDesk window runs on: Windows' pythonw, which has no console window, where the environment
    has it."""
    if WINDOWS:
        windowless = VENV / "Scripts" / "pythonw.exe"
        if windowless.exists():
            return windowless
    return venv_python()


def mac_interpreter(sys_module=None, config_var=None) -> Path | None:
    """The program that really runs this Python (shortcut.mac_interpreter's copy: CloseDesk isn't importable here yet).
    A framework's (python.org's, Homebrew's) is Python.app's program; any other is the file behind the links."""
    import sysconfig

    sys_module = sys_module or sys
    config_var = config_var or sysconfig.get_config_var
    framework = config_var("PYTHONFRAMEWORK") or ""
    if framework:
        path = Path(sys_module.base_prefix) / "Resources" / "Python.app" / "Contents" / "MacOS" / framework
    else:
        path = Path(os.path.realpath(getattr(sys_module, "_base_executable", "") or sys_module.executable))
    return path if path.is_file() else None


def opened_from() -> Path | None:
    """macOS: the CloseDesk.app the app was opened from, as its script says (``APP_BUNDLE_ENV``), when it is
    CloseDesk's own."""
    named = os.environ.get(APP_BUNDLE_ENV, "")
    if not named:
        return None
    app = Path(named)
    try:
        import plistlib

        with (app / "Contents" / "Info.plist").open("rb") as handle:
            info = plistlib.load(handle)
    except Exception:  # not there, or not an app's
        return None
    return app if isinstance(info, dict) and info.get("CFBundleIdentifier") == BUNDLE_ID else None


def remember_opened_from(app: Path) -> None:
    """Record the CloseDesk.app the app was opened from (once), with what the shortcut keeps (shortcut.py)."""
    record = Path(os.environ.get(DIR_ENV) or tools_dir()) / "shortcut.json"
    try:
        saved = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    saved = saved if isinstance(saved, dict) else {}
    apps = [str(item) for item in saved.get(APPS_KEY) or [] if isinstance(item, str)] if isinstance(saved.get(APPS_KEY), list) else []
    if str(app) in apps:
        return
    saved[APPS_KEY] = [*apps, str(app)][-5:]
    try:
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps(saved), encoding="utf-8")
    except OSError:
        pass


def linked_python(*, platform: str = "") -> Path | None:
    """macOS: CloseDesk.app's link to the Python that runs CloseDesk, when it leads to this environment's. Python started
    through it is CloseDesk.app to macOS: the Dock and the menu bar say CloseDesk, with CloseDesk's icon. The app it was
    opened from first, wherever the person keeps it."""
    if (platform or sys.platform) != "darwin":
        return None
    wanted = mac_interpreter()
    if wanted is None:
        return None
    named = os.environ.get(DIR_ENV, "")
    apps = [Path(named) / "CloseDesk.app"] if named else [Path.home() / "Applications" / "CloseDesk.app", ROOT / "CloseDesk.app"]
    opened = opened_from()
    if opened is not None:
        apps.insert(0, opened)
    for app in apps:
        link = app / "Contents" / "MacOS" / LINKED_PYTHON
        try:
            if link.is_symlink() and os.path.realpath(link) == os.path.realpath(wanted):
                return link
        except OSError:
            continue
    return None


def _runs_as_app(link: Path) -> bool:
    """Whether Python started through CloseDesk.app's link finds this environment: tried before the window depends on
    it (a quick start, with nothing on screen)."""
    try:
        # A part that is compiled (pydantic's), as the window loads first: one built for another kind of processor than
        # this Python runs as is found here, not after the window's start.
        done = subprocess.run(
            _as_set_up([str(link), "-c", "import controller_inbox.config"]), cwd=ROOT,
            env={**os.environ, "__PYVENV_LAUNCHER__": str(venv_python())}, stdin=subprocess.DEVNULL, capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _machine_here() -> str:
    """The kind of processor this Python runs as ("x86_64" under Rosetta)."""
    return os.uname().machine


def _compiled_for() -> str:
    """macOS: the kind of processor the environment's compiled packages are for, as pydantic's compiled part says (it
    has no build for both kinds); "" when it can't be told, or it is built for both."""
    if sys.platform != "darwin":
        return ""
    try:
        spec = importlib.util.find_spec("pydantic_core")
    except (ImportError, ValueError):
        return ""
    for place in list(getattr(spec, "submodule_search_locations", None) or []):
        for path in sorted(Path(place).glob("_pydantic_core*.so")):
            try:
                with path.open("rb") as handle:
                    head = handle.read(8)
            except OSError:
                continue
            if head[:4] == b"\xcf\xfa\xed\xfe":  # one kind (64-bit Mach-O); a universal file starts otherwise
                return MACH_O.get(int.from_bytes(head[4:8], "little"), "")
            return ""
    return ""


def set_up_for() -> str:
    """macOS: the kind of processor the environment was set up for, as recorded when it was set up, or else as its
    compiled parts say (an environment set up before that was recorded). "" when it can't be told."""
    return str(read_state().get(MACHINE_KEY) or "") or _compiled_for()


def _loads_as(machine: str) -> bool:
    """Whether the environment's CloseDesk loads, compiled parts and all, run as ``machine`` (Rosetta, for x86_64)."""
    try:
        done = subprocess.run(
            [ARCH, f"-{machine}", sys.executable, "-c", "import controller_inbox.config"], cwd=ROOT,
            stdin=subprocess.DEVNULL, capture_output=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _as_set_up(cmd: list[str]) -> list[str]:
    """``cmd``, run as the kind of processor the environment was set up for, once this was started that way
    (``ARCH_ENV``). arch keeps the program's number and the path it was started by (the Dock's CloseDesk.app)."""
    machine = os.environ.get(ARCH_ENV, "")
    if sys.platform == "darwin" and machine in MACH_O.values() and cmd[:1] != [ARCH]:
        return [ARCH, f"-{machine}", *cmd]
    return cmd


def run_as_set_up(argv: list[str]) -> int | None:
    """macOS: an environment set up for the other kind of processor than this runs as (from a Terminal opened under
    Rosetta, then CloseDesk.app opened from the Dock; or the other way round) won't load its compiled parts here, and
    CloseDesk couldn't open, every time. This launcher starts again as the environment was set up, and so does what it
    runs. None: carry on here (the same kind, not a Mac, or this Mac can't run it as set up: as before, which says what
    goes wrong)."""
    if sys.platform != "darwin":
        return None
    wanted, here = set_up_for(), _machine_here()
    if not wanted or wanted == here:
        return None
    if os.environ.get(ARCH_ENV) == wanted:  # started that way already, and still not: arch couldn't
        say(f"CloseDesk's environment was set up for {wanted}, and couldn't be run that way here (as {here}).")
        return None
    if not _loads_as(wanted):
        say(f"CloseDesk's environment was set up for {wanted} (in a Terminal opened under Rosetta, say), and this runs "
            f"as {here}; it doesn't load as {wanted} either, so it is started as it is.")
        return None
    say(f"CloseDesk's environment was set up for {wanted} (in a Terminal opened under Rosetta, say), and this runs as "
        f"{here}: CloseDesk runs as {wanted}, as it was set up.")
    os.environ[ARCH_ENV] = wanted
    if _after_update:
        os.environ[AFTER_UPDATE_ENV] = "1"  # still the check after the update, which the update's mark doesn't stop
    return _hand_over([sys.executable, str(Path(__file__).resolve()), *argv])


def in_venv() -> bool:
    try:
        return Path(sys.prefix).resolve() == VENV.resolve()
    except OSError:
        return False


def tools_dir() -> Path:
    """Where the launchers keep ``uv`` for this user (no admin rights needed): the same folder they use."""
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "CloseDesk"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "CloseDesk"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "CloseDesk"


def app_mode() -> bool:
    return os.environ.get(APP_ENV) == "1"


def app_log_path() -> Path:
    """The app's log: in this person's own folder, kept through updates and a new environment."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "CloseDesk" / "CloseDesk-app.log"
    return tools_dir() / "CloseDesk-app.log"


_app_log = None


def use_app_log(*, rotate: bool = True) -> None:
    """What the app says goes to its log (there is no console; under pythonw print and flush would fail), added at its
    end. ``rotate``: a log grown long is started afresh, unless an open CloseDesk holds it (Windows can't rename an
    open file): then it is added to, never left for nowhere."""
    global _app_log
    path = Path(os.environ.get(APP_LOG_ENV) or app_log_path())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    if rotate:
        try:
            if path.exists() and path.stat().st_size > APP_LOG_LIMIT:
                path.replace(path.with_name(path.name + ".old"))
        except OSError:
            pass  # in use by an open CloseDesk: started afresh another time
    try:
        _app_log = open(path, "a", encoding="utf-8", errors="replace", buffering=1)  # noqa: SIM115 (kept open)
    except OSError:
        _app_log = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    sys.stdout = sys.stderr = _app_log


def _quiet() -> dict:
    """In the app: no console window for a program it runs, and no console to read from."""
    if not app_mode():
        return {}
    return {"creationflags": NO_WINDOW, "stdin": subprocess.DEVNULL}


def _output() -> dict:
    """In the app, where a program it hands over to writes: its log (a Python file object isn't passed on by itself)."""
    if app_mode() and _app_log is not None:
        return {"stdout": _app_log, "stderr": subprocess.STDOUT}
    return {}


def tell(text: str, *, platform: str = "") -> None:
    """Say ``text`` where a person sees it with no console: a message box (Windows), an alert (macOS). Also logged."""
    say(text)
    platform = platform or sys.platform
    if os.environ.get(SMOKE_ENV):
        return
    try:
        if platform == "win32":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, text, "CloseDesk", 0x30 | 0x10000)  # a warning, brought to the front
        elif platform == "darwin":
            # The words are passed as they are, never written into the script.
            subprocess.run(
                ["/usr/bin/osascript", "-e", "on run argv", "-e", "display alert (item 1 of argv) message (item 2 of argv)",
                 "-e", "end run", "CloseDesk", text],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=3600,
            )
    except (OSError, AttributeError, ValueError, subprocess.SubprocessError):
        pass


def notice(text: str, *, platform: str = "") -> None:
    """Say ``text`` without waiting for an answer, while the app carries on (installing): a notification on macOS, a
    message box on Windows that stays until it is closed."""
    say(text)
    platform = platform or sys.platform
    if os.environ.get(SMOKE_ENV):
        return
    try:
        if platform == "win32":
            import ctypes

            threading.Thread(
                target=ctypes.windll.user32.MessageBoxW, args=(None, text, "CloseDesk", 0x40 | 0x10000), daemon=True
            ).start()
        elif platform == "darwin":
            subprocess.Popen(
                ["/usr/bin/osascript", "-e", "on run argv", "-e", 'display notification (item 1 of argv) with title "CloseDesk"',
                 "-e", "end run", text],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    except (OSError, AttributeError, ValueError):
        pass


def open_launcher() -> None:
    """Open the double-click launcher in its window (CloseDesk.bat; CloseDesk.command in Terminal), as if it were
    double-clicked: what the app can't do (make the environment again) is done there, as it goes, then CloseDesk
    opens."""
    for name in (APP_ENV, APP_LOG_ENV, "CLOSEDESK_LAUNCHER", APP_BUNDLE_ENV):
        os.environ.pop(name, None)
    try:
        if WINDOWS:
            os.startfile(str(ROOT / "CloseDesk.bat"))  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["/usr/bin/open", "-a", "Terminal", str(ROOT / "CloseDesk.command")], stdin=subprocess.DEVNULL)
        else:
            say(f"Run {ROOT / 'CloseDesk.command'} to set CloseDesk up again.")
    except OSError as exc:
        tell(f"CloseDesk has to set itself up again. Double-click {'CloseDesk.bat' if WINDOWS else 'CloseDesk.command'} in {ROOT} ({exc}).")


def find_uv() -> str:
    named = os.environ.get("CLOSEDESK_UV", "")
    if named and Path(named).is_file():
        return named
    kept = tools_dir() / ("uv.exe" if os.name == "nt" else "uv")
    if kept.is_file():
        return str(kept)
    return shutil.which("uv") or ""


def _run(cmd: list[str], *, quiet: bool = True) -> bool:
    """Run a set-up command. Its output goes to the set-up log (shown when a step fails), not the window."""
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8", errors="replace") as log:
            log.write(f"\n$ {' '.join(cmd)}\n")
            log.flush()
            done = subprocess.run(
                cmd, cwd=ROOT, stdout=log if quiet else None, stderr=subprocess.STDOUT if quiet else None, **_quiet()
            )
        return done.returncode == 0
    except OSError as exc:
        say(f"Couldn't run {cmd[0]}: {exc}")
        return False


def _log_tail(lines: int = 15) -> str:
    try:
        return "\n".join(LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def _console_python(executable: str | None = None) -> str:
    """The Python to install with: this one, or under the app (pythonw, no console) the python.exe beside it. uv and pip
    run the Python they are given and read what it prints; started from the app, it opens no console window."""
    executable = executable or sys.executable
    path = Path(executable)
    if path.name.lower() == "pythonw.exe" and path.with_name("python.exe").exists():
        return str(path.with_name("python.exe"))
    return executable


def install(*what: str) -> bool:
    """Install into this environment: with ``uv`` when the launcher has it (much faster, and it keeps what it
    downloaded for the next set-up), else with pip."""
    python = _console_python()
    uv = find_uv()
    if uv and _run([uv, "pip", "install", "--python", python, *what]):
        return True
    if importlib.util.find_spec("pip") is None and not _run([python, "-m", "ensurepip", "--upgrade"]):
        return False
    return _run([python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", *what])


def runs_this_folder() -> bool:
    """Whether the environment's CloseDesk is this folder's. One copied from, or left behind by, another folder
    would run that folder's code (or none, once it is deleted)."""
    try:
        spec = importlib.util.find_spec("controller_inbox")
    except (ImportError, ValueError):
        return False
    if spec is None or not spec.origin:
        return False
    try:
        return Path(spec.origin).resolve().is_relative_to((ROOT / "src").resolve())
    except OSError:
        return False


def read_state() -> dict:
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(state: dict) -> None:
    try:
        STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def _applies(requirements: list[str]) -> list[str]:
    """The requirements that apply to this Python and system (an add-on can be for older Pythons only)."""
    return [req for req in requirements if check_deps.applies_here(req)]


def _tried_key(requirements: list[str]) -> str:
    """Changes when the add-on's packages or the Python do: then it is worth one more try."""
    text = "\n".join(sorted(requirements)) + f"\npython {sys.version_info[0]}.{sys.version_info[1]}"
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# Whether this start's environment had nothing set up in it (``ensure_packages``): it was just made.
_new_environment = False


def ensure_packages() -> int:
    """Install what is missing or too old. Returns 0, or the exit code to leave with."""
    global _new_environment
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    state = read_state()
    first = not state
    _new_environment = first
    wrong = [found for found in map(check_deps.problem, project["project"]["dependencies"]) if found]
    here = runs_this_folder()
    installed = False
    if wrong or not here:
        installed = True
        if app_mode() and not _after_update:
            # Nothing else is on screen until the window opens. (The check after the window's update: the window says
            # CloseDesk is updating already.)
            notice(INSTALLING)
        if first:
            say("Installing what CloseDesk needs. This takes a minute or two.")
        elif not here:
            say("This folder is new to its set-up (moved, renamed or replaced): putting that right...")
        else:
            say("Installing what this update needs...")
        if not install("-e", "."):
            if not os.environ.get("CLOSEDESK_REBUILT"):
                return REBUILD
            say()
            say(_log_tail())
            say()
            say("CloseDesk couldn't install what it needs. The lines above say why; the whole log is in")
            say(f"  {LOG}")
            say("Most often the internet connection (or a company proxy) stopped a download: try again when it is back.")
            say(f"If this Python ({sys.version_info[0]}.{sys.version_info[1]}) is very new, a package may not be built for it yet: Python 3.12 is the safe choice.")
            return 1
    extras = project["project"].get("optional-dependencies", {})
    tried = dict(state.get("extras") or {})
    for extra in check_deps.LAUNCHER_EXTRAS:
        wanted = _applies(extras.get(extra, []))
        if not wanted:
            continue  # not for this Python
        key = _tried_key(wanted)
        if check_deps.extra_installed(extras.get(extra, [])):
            if any(check_deps.problem(req) for req in wanted):
                install(*wanted)
        elif tried.get(extra) != key or (extra == "window" and app_mode()):
            # Optional: installed where it can be, tried once where it can't (an older Mac has no build of it). The
            # window's is tried at each start of the app until it installs: without it the app is the browser.
            install(*wanted)
        tried[extra] = key
    write_state({
        "extras": tried,
        "python": ".".join(map(str, sys.version_info[:3])),
        # As it was set up, kept: a start as the other kind (CloseDesk.app from the Dock) runs as this (run_as_set_up).
        MACHINE_KEY: set_up_for() or platform.machine(),
    })
    if first and installed:
        say("Setup finished.")
    return 0


def create_environment(args: list[str]) -> int:
    """Running on a Python outside ``.venv`` (the computer's): make the environment with it, then carry on inside it."""
    version = sys.version_info[:2]
    if os.environ.get("CLOSEDESK_AGAIN"):
        say("Setting CloseDesk up again: the Python it was set up with is gone or was changed.")
    else:
        say(f"First run: setting up CloseDesk with Python {version[0]}.{version[1]}.")
    shutil.rmtree(VENV, ignore_errors=True)
    made = subprocess.run([sys.executable, "-m", "venv", str(VENV)], cwd=ROOT, capture_output=True, text=True, **_quiet())
    if made.returncode != 0 or not venv_python().exists():
        shutil.rmtree(VENV, ignore_errors=True)
        say((made.stdout + made.stderr).strip()[-800:])
        say(f"Python {version[0]}.{version[1]} couldn't make CloseDesk's environment. The lines above say why.")
        say("(On Debian and Ubuntu, Python comes without that part: install the python3-venv package.)")
        return 1
    return _hand_over([str(venv_python()), str(Path(__file__).resolve()), *args])


def _hand_over(cmd: list[str]) -> int:
    """Run ``cmd`` in this one's place (its exit code is this one's), as the environment was set up (``_as_set_up``)."""
    cmd = _as_set_up(cmd)
    _flush()
    if WINDOWS:  # no exec that waits on Windows
        try:
            return subprocess.call(cmd, cwd=ROOT, **_quiet(), **_output())
        except KeyboardInterrupt:
            return 130
    os.chdir(ROOT)
    if app_mode() and _app_log is not None:
        # What runs next writes where this one did: the log.
        for fd in (1, 2):
            os.dup2(_app_log.fileno(), fd)
    os.execv(cmd[0], cmd)
    return 1  # pragma: no cover


# Updating -------------------------------------------------------------------------------------------------


# What the server answered when it refused the last download (GitHub's "404"), "" when it wasn't reached: then it is
# the internet (or a proxy) that stopped it.
_refused = ""


def _fetch(url: str, dest: Path) -> bool:
    """Download with curl (it trusts what the computer trusts, a company proxy's certificate included), else
    with Python. Both together within ``DOWNLOAD_LONGEST``: one that stalls is given up on. When the server answered
    with an error, ``_refused`` says which."""
    global _refused
    _refused = ""
    until = time.monotonic() + DOWNLOAD_LONGEST
    longest = f"{DOWNLOAD_LONGEST / 60:g} minutes"
    curl = shutil.which("curl")
    if curl:
        cmd = [curl, "-fsSL", "--retry", "2", "--connect-timeout", "30", "--speed-limit", SLOWEST[0], "--speed-time",
               SLOWEST[1], "--max-time", str(max(1, int(DOWNLOAD_LONGEST))), "-o", str(dest), url]
        try:
            done = subprocess.run(
                cmd, capture_output=True, text=True, errors="replace", timeout=DOWNLOAD_LONGEST + 60, **_quiet()
            )
        except subprocess.TimeoutExpired:
            say(f"The download of {url} took longer than {longest}: it was stopped.")
            return False
        if done.returncode == 0:
            return True
        if done.returncode == 22:  # -f: the server answered with an error ("returned error: 404")
            found = re.search(r"error: (\d{3})", str(done.stderr or ""))
            _refused = found.group(1) if found else "an error"
    try:
        import ssl
        import urllib.error
        import urllib.request

        try:
            import certifi

            context = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            context = ssl.create_default_context()
        with urllib.request.urlopen(url, timeout=60, context=context) as reply, dest.open("wb") as out:  # noqa: S310
            while True:
                if time.monotonic() > until:
                    raise TimeoutError(f"it took longer than {longest}")
                chunk = reply.read(1 << 16)
                if not chunk:
                    break
                out.write(chunk)
        return True
    except Exception as exc:
        if isinstance(exc, urllib.error.HTTPError):
            _refused = str(exc.code)
        say(f"Couldn't download {url}: {exc}")
        return False


def installed_commit() -> str:
    """The commit this copy was downloaded at (GitHub writes it into the ZIP), or "" for a developer's checkout."""
    try:
        for line in (ROOT / ".git_archival.txt").read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition(":")
            if name.strip() == "commit" and "$" not in value:
                return value.strip()
    except OSError:
        pass
    return ""


def stopped_as() -> str:
    """The version CloseDesk was when it stopped to update just now (``STOPPED_NOTE``), "" when it didn't say."""
    try:
        said = json.loads((VENV / STOPPED_NOTE).read_text(encoding="utf-8"))
        commit, at = str(said.get("commit") or ""), float(said.get("at") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return ""
    return commit if commit and 0 <= time.time() - at < STOPPED_FRESH else ""


def update(stopped: str = "") -> int:
    """Replace CloseDesk's own files with the newest ones, keeping the mail, the settings and the environment.
    The files replaced are kept in ``.previous`` until the next update. ``stopped``: the version CloseDesk was when
    it stopped for this update; the folder already another one (CloseDesk-Update put it there meanwhile) is opened
    as it is."""
    if (ROOT / ".git").exists() and not os.environ.get("CLOSEDESK_UPDATE_URL"):
        say("This folder is a git checkout: update it with git (git pull), not with this.")
        return 1
    there = installed_commit()
    if stopped and there and there.lower() != stopped.lower():
        say(UPDATED_UNDER)
        return 0
    url = os.environ.get("CLOSEDESK_UPDATE_URL") or f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.zip"
    say("Looking for the newest CloseDesk...")
    try:
        (VENV / NEWEST_NOTE).unlink()
    except OSError:
        pass
    with tempfile.TemporaryDirectory(prefix=".update-", dir=ROOT) as tmp:
        _mark_update(folder=Path(tmp).name)  # removed with what the update leaves, if it is stopped
        archive = Path(tmp) / "closedesk.zip"
        if not _fetch(url, archive):
            if _refused:
                say(f"The update couldn't be downloaded: GitHub answered with an error ({_refused}), so the internet works "
                    "but the download isn't there just now. Nothing was changed; try again later.")
            else:
                say("The update couldn't be downloaded. Nothing was changed; try again when the internet is back.")
            return 1
        try:
            with zipfile.ZipFile(archive) as bundle:
                names = bundle.namelist()
                top = {name.split("/", 1)[0] for name in names if name.strip("/")}
                unsafe = [name for name in names if name.startswith(("/", "\\")) or ".." in Path(name).parts]
                if len(top) != 1 or unsafe:
                    raise ValueError("not a CloseDesk download")
                bundle.extractall(Path(tmp) / "new")
        except (zipfile.BadZipFile, ValueError, OSError) as exc:
            say(f"The download isn't a CloseDesk update ({exc}). Nothing was changed.")
            return 1
        new = Path(tmp) / "new" / top.pop()
        try:
            name = tomllib.loads((new / "pyproject.toml").read_text(encoding="utf-8"))["project"]["name"]
        except (OSError, ValueError, KeyError):
            name = ""
        if name != "controller-inbox" or not (new / "scripts" / "launch.py").is_file():
            say("The download isn't a CloseDesk update. Nothing was changed.")
            return 1
        before = installed_commit()
        after = ""
        try:
            for line in (new / ".git_archival.txt").read_text(encoding="utf-8").splitlines():
                if line.startswith("commit:") and "$" not in line:
                    after = line.partition(":")[2].strip()
        except OSError:
            pass
        if before and before == after:
            say("CloseDesk is already the newest version.")
            try:
                (VENV / NEWEST_NOTE).write_text(before + "\n", encoding="utf-8")  # the next start: nothing went wrong
            except OSError:
                pass
            return 0
        previous = ROOT / ".previous"
        shutil.rmtree(previous, ignore_errors=True)
        previous.mkdir()
        moved: list[str] = []
        try:
            for item in sorted(new.iterdir()):
                if item.name in KEPT:
                    continue
                target = ROOT / item.name
                if target.exists() or target.is_symlink():
                    shutil.move(str(target), str(previous / item.name))
                    moved.append(item.name)
                shutil.move(str(item), str(target))
        except OSError as exc:
            # Put back what was moved, so a half-done update never stays.
            for done in moved:
                target = ROOT / done
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target, ignore_errors=True)
                elif target.exists():
                    target.unlink()
                shutil.move(str(previous / done), str(target))
            say(f"The update couldn't be put in place ({exc}). CloseDesk is as it was.")
            say("Close any other CloseDesk window, then try again.")
            return 1
    if os.name != "nt":
        for script in ("CloseDesk.command", "CloseDesk-Update.command", "scripts/overnight.sh", "scripts/install.sh"):
            try:
                (ROOT / script).chmod(0o755)
            except OSError:
                pass
    say("CloseDesk was updated. Your mail and settings are as they were; the files it replaced are in .previous.")
    return 0


# ----------------------------------------------------------------------------------------------------------


_marked: dict = {}
# This run is the check after an update (``AFTER_UPDATE_ENV``, read once as it starts).
_after_update = False


def _me() -> str:
    """Who runs this, as a fingerprint: controller_inbox.version.whose()'s "user". An update's mark says whose it is."""
    if os.name != "nt":
        text = f"uid {os.getuid()}"
    else:
        import getpass

        try:
            name = getpass.getuser()
        except Exception:  # no name to be found
            name = os.environ.get("USERPROFILE", "")
        text = f"{os.environ.get('USERDOMAIN', '')}\\{name}".lower()
    return hashlib.sha256(f"closedesk\n{text}".encode("utf-8", "replace")).hexdigest()[:16]


def _mark_update(**more) -> None:
    """Say in CloseDesk's folder that an update runs (this program, since when, whose, and ``more``)."""
    _marked.setdefault("pid", os.getpid())
    _marked.setdefault("started", time.time())
    _marked.setdefault("user", _me())
    _marked.update(more)
    try:
        (ROOT / UPDATING_MARK).write_text(json.dumps(_marked), encoding="utf-8")
    except OSError:
        pass


def _end_update() -> None:
    try:
        said = json.loads((ROOT / UPDATING_MARK).read_text(encoding="utf-8"))
        if said.get("pid") == os.getpid():
            (ROOT / UPDATING_MARK).unlink()
    except (OSError, ValueError, AttributeError):
        pass
    _marked.clear()


def _alive(pid: int, started: float = 0.0, *, mine: bool = False) -> bool:
    """Whether the program ``pid`` that said at ``started`` it was updating still runs. One started after that isn't
    it: the computer gave the number to another program since (on a Mac or Linux the update becomes the check after
    it, which leaves the mark behind when it ends). ``mine``: this person's update, whose program they can always ask
    about: one they may not ask about is another program, given the number since."""
    if pid == os.getpid():
        return True
    if WINDOWS:
        return _windows_alive(pid, started, mine=mine)
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


def _windows_alive(pid: int, started: float, *, mine: bool = False) -> bool:
    """Windows: asked of Windows (os.kill would end the program). A program started after the update said it was
    running isn't it: Windows gave the number to another one since. Refused (a SYSTEM program, another person's), it
    isn't this person's update (``mine``), which they can always ask about: only another person's may be it."""
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


def update_under_way() -> bool:
    """Whether an update runs in this folder now, from its start to the end of the check after it (it says so in
    ``UPDATING_MARK``). One whose program is gone (stopped: its window closed, the battery ran out) doesn't count, and
    the download it left goes with what it said."""
    mark = ROOT / UPDATING_MARK
    try:
        said = json.loads(mark.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        said = {}
    said = said if isinstance(said, dict) else {}
    try:
        pid, started = int(said.get("pid") or 0), float(said.get("started") or 0)
    except (TypeError, ValueError):
        pid, started = 0, 0.0
    # Whose update it was: this person's unless another person's mark says so (an older CloseDesk's doesn't say).
    mine = str(said.get("user") or _me()) == _me()
    if pid and 0 <= time.time() - started < UPDATE_LONGEST and _alive(pid, started, mine=mine):
        return True
    left = Path(str(said.get("folder") or "")).name
    if left.startswith(".update-"):
        shutil.rmtree(ROOT / left, ignore_errors=True)
    try:
        mark.unlink()
    except OSError:
        pass
    return False


def wait_for_other_update(stopped: str = "") -> bool:
    """Update now, from CloseDesk a launcher started, while another update runs in the folder (CloseDesk-Update,
    double-clicked meanwhile): that one is waited for, not given up on. True when it did what Update now asked (this
    copy was updated, or it found this copy the newest already): the launcher opens CloseDesk again, as the page that
    asked says it will. False when it left CloseDesk as it was (its download failed, say): the update asked for runs
    then. ``update_under_way`` stops counting one older than ``UPDATE_LONGEST``, so this never waits for good.
    ``stopped``: the version CloseDesk was when it stopped (the folder's may be newer already: that update was
    nearly done), compared with the folder's once that update is done."""
    before = stopped or installed_commit()
    say(OTHER_UPDATE)
    while update_under_way():
        time.sleep(OTHER_UPDATE_EVERY)
    after = installed_commit()
    try:
        newest = (VENV / NEWEST_NOTE).read_text(encoding="utf-8").strip().lower()
    except OSError:
        newest = ""
    if not after or after.lower() != before.lower() or newest == after.lower():
        say(OTHER_DONE)
        return True
    say(OTHER_FAILED)
    return False


def app(args: list[str]) -> int:
    """``--app``: set up as every start does, silently, then open the CloseDesk window."""
    os.environ[APP_ENV] = "1"
    if APP_LOGS or sys.stdout is None:
        os.environ[APP_LOG_ENV] = str(app_log_path())
        use_app_log()
    say(f"\n{time.strftime('%Y-%m-%d %H:%M:%S')} CloseDesk opening (the app), in {ROOT}")
    if update_under_way():
        # The window was closed during an update, which finishes by itself: started now, CloseDesk would run from a
        # folder half replaced.
        tell(UPDATING_NOW)
        return 0
    if not in_venv():
        return create_environment(["--app", *args])
    started = run_as_set_up(["--app", *args])
    if started is not None:
        return started
    code = ensure_packages()
    if code == REBUILD:
        say("CloseDesk's environment has to be made again: the double-click launcher does that, in its window.")
        open_launcher()
        return 0
    if code:
        tell(SETUP_FAILED.format(log=LOG))
        return code
    # The window acts on CloseDesk's UPDATE exit code itself, so CloseDesk offers its Update button.
    os.environ["CLOSEDESK_LAUNCHER"] = "1"
    _say_new_environment()
    return _after_window(_open_window(args))


def _say_new_environment() -> None:
    """Tell the CloseDesk this starts whether its environment was just set up (``NEW_ENVIRONMENT_ENV``)."""
    if _new_environment:
        os.environ[NEW_ENVIRONMENT_ENV] = "1"


def _after_window(code: int) -> int:
    """The window's process has ended (Windows: it ran beside this one). With an error it didn't explain itself (it
    ended before it could: a part of Python that won't load, say), the person is told, or they would see nothing at
    all."""
    if code not in (0, SAID, 130):
        say(f"The CloseDesk window's program ended with exit code {code}.")
        if not os.environ.get(SMOKE_ENV):
            tell(CANT_OPEN.format(log=app_log_path(), launcher="CloseDesk.bat" if WINDOWS else "CloseDesk.command"))
    return code


def _open_window(args: list[str]) -> int:
    window = ["-m", "controller_inbox", "app", *args]
    opened = opened_from() if sys.platform == "darwin" else None
    if opened is not None:
        remember_opened_from(opened)
    linked = linked_python()
    os.environ.pop(APP_BUNDLE_ENV, None)  # for this start only
    if linked is not None and not _runs_as_app(linked):
        say(f"Python started as CloseDesk.app ({linked}) didn't find CloseDesk: the Dock shows the window as Python's.")
        linked = None
    if linked is not None:
        say(f"The window opens as CloseDesk.app ({linked}).")
        # As python3.x tells the program it starts: this environment is the one to run.
        os.environ["__PYVENV_LAUNCHER__"] = str(venv_python())
        # And where it was started from, said outright: macOS reads this before the path itself, so the Dock says
        # CloseDesk however the link is resolved. (The window doesn't pass it on to what it starts.)
        os.environ[MAC_APP_PATH] = str(linked)
        try:
            return _hand_over([str(linked), *window])
        except OSError as exc:
            say(f"Python couldn't be started as CloseDesk.app ({exc}): the Dock shows the window as Python's.")
            os.environ.pop("__PYVENV_LAUNCHER__", None)
            os.environ.pop(MAC_APP_PATH, None)
    return _hand_over([str(app_python()), *window])


def main(argv: list[str]) -> int:
    args = list(argv)
    # Only this launch.py's own set-up says the environment is new, never what started it.
    os.environ.pop(NEW_ENVIRONMENT_ENV, None)
    global _new_environment
    _new_environment = False
    if "--app" in args:
        args.remove("--app")
        return app(args)
    if app_mode() and os.environ.get(APP_LOG_ENV) and (WINDOWS or sys.stdout is None):
        # Started by the CloseDesk window (the update, its check) with the app's log as its output: opened again here,
        # for adding at its end (Windows passes on the file, not the way it was opened: written as passed on, it
        # would write over another CloseDesk window's lines).
        use_app_log(rotate=False)
    if "--update" in args:
        args.remove("--update")
        stopped = stopped_as()  # the version CloseDesk was when it stopped for this update
        while update_under_way():  # another update (the window's, carrying on after it closed): not two at once
            if os.environ.get(REOPEN_ENV) != "1":
                say(UPDATING_NOW)
                return UPDATE_UNDER_WAY
            if wait_for_other_update(stopped):
                return 0
        _mark_update()
        try:
            code = update(stopped)
            if code:
                return code
            # The newest launch.py checks the newest set-up (this one is the old file, already in memory). (Outside
            # Windows it takes this program's place, and what it said about the update with it: stale once it ends.)
            os.environ[AFTER_UPDATE_ENV] = "1"
            return _hand_over([sys.executable, str(Path(__file__).resolve()), "--check", *args])
        finally:
            os.environ.pop(AFTER_UPDATE_ENV, None)
            _end_update()
    global _after_update
    _after_update = os.environ.pop(AFTER_UPDATE_ENV, "") == "1"
    if not _after_update and update_under_way():
        # An update runs (the window's, which carries on after the window is closed): started now, CloseDesk would
        # run from a folder being replaced. Said in the launcher's window, which stays open on it.
        say(UPDATING_NOW)
        return UPDATE_UNDER_WAY
    if not in_venv():
        return create_environment(args)
    started = run_as_set_up(args)
    if started is not None:
        return started
    code = ensure_packages()
    if code:
        return code
    module = [sys.executable, "-m", "controller_inbox"]
    if "--check" in args:
        return _hand_over([*module, "doctor"])
    if "--shortcut" in args:
        return _hand_over([*module, "shortcut"])
    if "--command" in args:
        at = args.index("--command")
        return _hand_over([*module, *args[at + 1 :]])
    # The launcher acts on CloseDesk's UPDATE exit code: CloseDesk offers its Update button only when one started it.
    os.environ["CLOSEDESK_LAUNCHER"] = "1"
    _say_new_environment()
    return _hand_over([*module, "run", *args])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
