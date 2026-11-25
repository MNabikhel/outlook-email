"""The CloseDesk shortcut: what opens CloseDesk in its window (``launch.py --app``) with a double-click. CloseDesk makes it
itself, on the person's computer, with nothing to install and no admin rights.

- Windows: CloseDesk.lnk on the Desktop and in the Start menu, the person's own (Windows' known folders, which follow a
  Desktop moved into OneDrive). It runs the environment's pythonw.exe (no console window) on scripts\\launch.py --app,
  in CloseDesk's folder, shows CloseDesk's icon, and carries the taskbar id the window gives itself (app_window.APP_ID):
  the running window groups under it, and pinning the window pins the shortcut. It is written with pywin32, in this
  process: PowerShell's way is blocked on locked-down computers, and can't set the id.
- macOS: CloseDesk.app in Applications in the person's own folder (~/Applications), where Spotlight finds it and from
  where it can be kept in the Dock. It is made here, never downloaded, so macOS has nothing to hold back: a script that
  runs launch.py --app, CloseDesk's icon, and a link to the Python that runs CloseDesk. launch.py starts Python through
  that link, so the Dock and the menu bar say CloseDesk, not Python. Nothing in it is signed, or copied from a signed
  program. A CloseDesk folder where macOS guards what an app opens (Desktop, Documents, Downloads, iCloud Drive) has it
  open CloseDesk.command in Terminal instead, as a double-click on it does.
- Linux: none. CloseDesk.command opens CloseDesk in the browser.

Two more are kept right but never made: on Windows, the copy Windows makes when the person pins the open window to the
taskbar; on macOS, a CloseDesk.app the person moved out of ~/Applications (to /Applications, say), recorded by
launch.py --app once it is opened from there.

It is made once, the first time a launcher starts CloseDesk (``upkeep``, from cli._serve), and that is remembered, so
a shortcut the person deleted isn't put back. At each start after that, one still there is kept right: pointed at this
folder when the folder it opened is gone (CloseDesk was moved or renamed), and given what changed after an update (a
new icon). After a move, a shortcut that is missing is put back too, where this computer had one at the start before:
Windows offers to delete one whose folder is gone. So it does while the folder has no environment (an update by hand,
the folders swapped; the environment made again): the start that makes it puts back what that cost.
One that opens another CloseDesk folder, still there, is left alone, as is a shortcut named CloseDesk that isn't
CloseDesk's own, and one naming a folder this computer never had (another computer's, on a Desktop OneDrive keeps the
same on both). Nothing that is right is written again (OneDrive would copy a Desktop's changes between two computers,
with conflicts). ``make`` (Settings, ``launch.py --shortcut``) makes it again, whatever was there.

``CLOSEDESK_SHORTCUT_DIR`` keeps all of it in that folder instead (the tests and the launcher check), never on the
person's Desktop or in their Applications.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shlex
import stat
import subprocess
import sys
import sysconfig
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from controller_inbox import version
from controller_inbox.app_window import APP_ID, BUNDLE_ID, mac_icon_name
# launch.py's word that this start's environment was just made (``upkeep``'s new_environment; cli._serve reads it).
from controller_inbox.app_window import NEW_ENVIRONMENT_ENV as NEW_ENVIRONMENT_ENV
from controller_inbox.app_window import macos_version as _macos
from controller_inbox.app_window import user_dir as _closedesk_dir

NAME = "CloseDesk"
DIR_ENV = "CLOSEDESK_SHORTCUT_DIR"
# macOS: in the record, the CloseDesk.app bundles the person opened CloseDesk from (launch.py --app writes them).
APPS_KEY = "apps"
# In the record: folders this computer's shortcut opened before a move that a shortcut hasn't followed yet (it couldn't
# be read or written then). A shortcut still opening one, gone, is this computer's, and follows at a later start.
BEFORE_KEY = "before"
# The most kept of them.
BEFORE_MOST = 5
# The link in CloseDesk.app to the Python that runs CloseDesk (launch.py has the same name).
LINKED_PYTHON = "CloseDesk-python"
STATIC = Path(__file__).resolve().parent / "static"

# Windows' own numbers (ShObjIdl.h, WinUser.h): no link tracking or search when the folder a shortcut opens is gone
# (Windows says so instead of opening some other pythonw.exe), the path as it was written, a normal window.
SLDF_FORCE_NO_LINKINFO = 0x100
SLDF_FORCE_NO_LINKTRACK = 0x40000
SLGP_RAWPATH = 0x4
SW_SHOWNORMAL = 1

# Folders macOS guards: an app whose main program is a script, opening CloseDesk from there, runs into its privacy
# checks. (Under the person's home folder; anything on another disk too.)
GUARDED = {
    "Desktop": "your Desktop folder",
    "Documents": "your Documents folder",
    "Downloads": "your Downloads folder",
    "Library/Mobile Documents": "iCloud Drive",
    "Library/CloudStorage": "a cloud drive's folder",
}
LSREGISTER = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"

# What a shortcut that is there turned out to be: someone else's (not CloseDesk's own).
THEIRS = object()

_lock = threading.Lock()


@dataclass
class Said:
    """What was done, in plain words ("" when nothing needed doing); ``ok`` is False when a shortcut that should be
    there couldn't be made (Linux has none, and nothing went wrong there)."""

    ok: bool
    message: str = ""


def user_dir() -> Path:
    """Where the shortcut keeps what it needs (its icon on Windows, that it was made): CloseDesk's own folder in the
    person's, or ``CLOSEDESK_SHORTCUT_DIR``."""
    named = os.environ.get(DIR_ENV, "")
    return Path(named) if named else _closedesk_dir()


def record_path() -> Path:
    return user_dir() / "shortcut.json"


def _why(exc: BaseException) -> str:
    """What went wrong, in Windows' or macOS's own few words."""
    text = getattr(exc, "strerror", None)
    if not text and len(getattr(exc, "args", ())) >= 2 and isinstance(exc.args[1], str):
        text = exc.args[1]  # pywin32's com_error: (code, words, ...)
    return " ".join(str(text or exc or type(exc).__name__).split()).rstrip(".")[:160]


def _same(a, b) -> bool:
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))


def _closedesk_at(folder) -> bool:
    return bool(folder) and Path(folder, "scripts", "launch.py").is_file()


def _join(words: list[str]) -> str:
    return " and ".join(words) if len(words) < 3 else ", ".join(words[:-1]) + " and " + words[-1]


# Windows ------------------------------------------------------------------------------------------------------------


# Where Windows keeps what is pinned to the taskbar (in the person's roaming AppData): a copy of the shortcut the open
# window was pinned from, with its folder.
PINNED = ("Microsoft", "Internet Explorer", "Quick Launch", "User Pinned", "TaskBar")


def known_folders() -> dict[str, Path]:
    """This person's Desktop and Start menu, where Windows says they are (a Desktop moved into OneDrive is followed),
    and the taskbar's folder of what is pinned to it."""
    named = os.environ.get(DIR_ENV, "")
    if named:
        return {"Desktop": Path(named) / "Desktop", "Start menu": Path(named) / "Start menu", "Taskbar": Path(named) / "Taskbar"}
    from win32com.shell import shell

    return {
        "Desktop": Path(shell.SHGetKnownFolderPath(shell.FOLDERID_Desktop)),
        "Start menu": Path(shell.SHGetKnownFolderPath(shell.FOLDERID_Programs)),
        "Taskbar": Path(shell.SHGetKnownFolderPath(shell.FOLDERID_RoamingAppData)).joinpath(*PINNED),
    }


def _in_com(work, *args):
    """Run ``work`` with COM started in this thread (a server's threads don't have it), and ended after."""
    import pythoncom

    try:
        pythoncom.CoInitialize()
        started = True
    except Exception:  # started already, another way
        started = False
    try:
        return work(*args)
    finally:
        if started:
            pythoncom.CoUninitialize()


def _new_link():
    import pythoncom
    from win32com.shell import shell

    return pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)


def _read_link(path: Path) -> dict:
    import pythoncom
    from win32com.propsys import propsys, pscon
    from win32com.shell import shell

    link = _new_link()
    link.QueryInterface(pythoncom.IID_IPersistFile).Load(str(path), 0)
    icon, index = link.GetIconLocation()
    try:
        app_id = link.QueryInterface(propsys.IID_IPropertyStore).GetValue(pscon.PKEY_AppUserModel_ID).GetValue()
    except Exception:  # none set
        app_id = ""
    try:
        flags = int(link.QueryInterface(shell.IID_IShellLinkDataList).GetFlags())
    except Exception:
        flags = 0
    return {
        "target": link.GetPath(SLGP_RAWPATH)[0], "arguments": link.GetArguments(), "folder": link.GetWorkingDirectory(),
        "icon": icon, "icon_index": index, "description": link.GetDescription(), "app_id": str(app_id or ""), "flags": flags,
    }


def _write_link(path: Path, fields: dict) -> None:
    import pythoncom
    from win32com.propsys import propsys, pscon
    from win32com.shell import shell, shellcon

    link = _new_link()
    link.SetPath(fields["target"])
    link.SetArguments(fields["arguments"])
    link.SetWorkingDirectory(fields["folder"])
    link.SetIconLocation(fields["icon"], fields["icon_index"])
    link.SetDescription(fields["description"])
    link.SetShowCmd(SW_SHOWNORMAL)
    data = link.QueryInterface(shell.IID_IShellLinkDataList)
    data.SetFlags(data.GetFlags() | SLDF_FORCE_NO_LINKINFO | SLDF_FORCE_NO_LINKTRACK)
    # The taskbar id last, as Microsoft says: after the rest is set.
    store = link.QueryInterface(propsys.IID_IPropertyStore)
    store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(fields["app_id"], pythoncom.VT_LPWSTR))
    store.Commit()
    link.QueryInterface(pythoncom.IID_IPersistFile).Save(str(path), 0)
    try:  # Explorer shows the new icon now, not the picture it kept
        shell.SHChangeNotify(shellcon.SHCNE_UPDATEITEM, shellcon.SHCNF_PATHW, str(path), None)
    except Exception:
        pass


class WinLinks:
    """Windows' .lnk files, read and written with Windows' own IShellLink (pywin32)."""

    def read(self, path: Path) -> dict | None:
        """What a shortcut holds (its taskbar id "" when it has none); None when there is no such file."""
        path = Path(path)
        if not path.is_file():
            return None
        return _in_com(_read_link, path)

    def write(self, path: Path, fields: dict) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _in_com(_write_link, path, fields)


def read_link(path) -> dict | None:
    """What the shortcut at ``path`` holds (the launcher check reads CloseDesk's back with this)."""
    return WinLinks().read(Path(path))


class Windows:
    """CloseDesk.lnk on the Desktop and in the Start menu; and on the taskbar, once the person pins the open window
    (Windows copies the Start menu shortcut there): that one is only ever kept right, never made."""

    script = "CloseDesk.bat"
    places_said = {"Desktop": "on your Desktop", "Start menu": "in your Start menu", "Taskbar": "on your taskbar"}
    # A shortcut whose program is gone (the folder without its environment: swapped by hand, being set up again)
    # gets Windows' "Problem with Shortcut", which offers to delete it.
    lost_without_environment = True
    # Made by Windows when the person pins CloseDesk, never by CloseDesk.
    pinned = "Taskbar"

    def __init__(self, root, *, links=None, folders=None, icon_dir=None, icon_source=None) -> None:
        self.root = Path(root)
        self.links = links or WinLinks()
        self.folders = folders or known_folders
        self.icon_dir = Path(icon_dir) if icon_dir else user_dir()
        self.icon_source = Path(icon_source) if icon_source else STATIC / "closedesk.ico"

    def places(self, saved: dict | None = None) -> list[tuple[str, Path, bool]]:
        """(name, the shortcut there, whether it is only kept right: never made)."""
        return [(name, Path(folder) / f"{NAME}.lnk", name == self.pinned) for name, folder in self.folders().items()]

    def fallback(self):
        return None

    def icon(self) -> Path:
        """CloseDesk's icon, copied into the person's own folder: an update doesn't take it away for a moment, OneDrive
        doesn't keep it online only, and its name changes with the picture (Windows keeps pictures of icons by name)."""
        data = self.icon_source.read_bytes()
        path = self.icon_dir / f"closedesk-{hashlib.sha256(data).hexdigest()[:8]}.ico"
        _write(path, data, 0o644)
        return path

    def wanted(self) -> dict:
        root = self.root
        return {
            "target": str(root / ".venv" / "Scripts" / "pythonw.exe"),
            "arguments": f'"{root / "scripts" / "launch.py"}" --app',
            "folder": str(root),
            "icon": str(self.icon()),
            "icon_index": 0,
            "description": NAME,
            "app_id": APP_ID,
        }

    def look(self, path: Path):
        found = self.links.read(path)
        if found is None:
            return None
        if found.get("app_id") != APP_ID:
            return THEIRS
        return found.get("folder") or ""

    def put(self, path: Path) -> bool:
        wanted = self.wanted()
        found = self.links.read(path)
        if found is not None and found.get("app_id") == APP_ID:
            same = all(
                _same(found.get(key, ""), value) if key in ("target", "folder", "icon") else found.get(key) == value
                for key, value in wanted.items()
            )
            if same:
                return False
        self.links.write(path, wanted)
        return True

    def said(self, done: dict, failed: list, *, explicit: bool) -> Said:
        where = lambda names: _join([self.places_said.get(name, name) for name in names])  # noqa: E731
        lines = []
        if done["made"]:
            lines.append(f"Added the CloseDesk shortcut {where(done['made'])}: it opens CloseDesk in a window of its own.")
        if done["moved"]:
            lines.append(f"The CloseDesk shortcut {where(done['moved'])} now opens CloseDesk from this folder (CloseDesk's folder was moved).")
        if done["back"]:
            lines.append(
                f"The CloseDesk shortcut {where(done['back'])} was put back: Windows offers to delete it while "
                "CloseDesk's folder is being set up."
            )
        if done["fresh"]:
            lines.append(f"The CloseDesk shortcut {where(done['fresh'])} was brought up to date.")
        if done["kept"] and explicit:
            lines.append(f"The CloseDesk shortcut is {where(done['kept'])}.")
        if done["theirs"]:
            lines.append(f"There is already a shortcut named CloseDesk {where(done['theirs'])}, so it was left as it is.")
        if done["elsewhere"]:
            here = [name for key in ("made", "moved", "fresh", "kept") for name in done[key]]
            use = (
                f"On this computer, use the one {where(here)}." if here
                else f"On this computer, {self.script} in CloseDesk's folder opens it (or add this computer's own in Settings)."
            )
            lines.append(
                f"The CloseDesk shortcut {where(done['elsewhere'])} opens CloseDesk from a folder this computer hasn't got: "
                f"it is another computer's, shared with this one (as OneDrive shares a Desktop), so it was left as it is "
                f"for that one. {use}"
            )
        for name, why in failed:
            did = "check" if name in done["unread"] else "put"
            lines.append(f"CloseDesk couldn't {did} its shortcut {where([name])} ({why}).")
        ok = any(done[key] for key in ("made", "moved", "back", "fresh", "kept"))
        if failed and not ok:
            lines.append(f"{self.script} in CloseDesk's folder still opens it.")
        return Said(ok, " ".join(lines))


# macOS --------------------------------------------------------------------------------------------------------------


def mac_interpreter(sys_module=None, config_var=None) -> Path | None:
    """The program that really runs this Python, for CloseDesk.app to link to (None when it isn't there). python.org's
    and Homebrew's Python (a framework): bin/python3.x only starts Python.app's program by its own path, which the Dock
    would call Python, so it is that program. Any other (uv's): the file behind the links. (launch.py has a copy.)"""
    sys_module = sys_module or sys
    config_var = config_var or sysconfig.get_config_var
    framework = config_var("PYTHONFRAMEWORK") or ""
    if framework:
        path = Path(sys_module.base_prefix) / "Resources" / "Python.app" / "Contents" / "MacOS" / framework
    else:
        path = Path(os.path.realpath(getattr(sys_module, "_base_executable", "") or sys_module.executable))
    return path if path.is_file() else None


def _lsregister(app: Path) -> None:
    """Tell Launch Services about the app now, so Spotlight and the Dock know it (and its new icon) at once. Not one in
    the tests' or the launcher check's folder (``CLOSEDESK_SHORTCUT_DIR``): Spotlight would offer a CloseDesk that is
    gone once they end."""
    if os.environ.get(DIR_ENV):
        return
    try:
        subprocess.run([LSREGISTER, "-f", str(app)], stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass


def _guarded(root: Path, home: Path) -> str:
    """The folder macOS guards that CloseDesk's is in, in words ("" when none)."""
    for name, words in GUARDED.items():
        try:
            if root.is_relative_to(home / name):
                return words
        except ValueError:
            pass
    if root.is_relative_to("/Volumes"):
        return "a disk other than the Mac's own"
    return ""


def settings_folders(root: Path) -> list[tuple[str, Path, str]]:
    """The folders CloseDesk reads and writes that its .env can put outside CloseDesk's folder: (what, where, the
    setting that names it). A relative one is in CloseDesk's folder, as CloseDesk reads it."""
    try:
        from controller_inbox.config import Settings

        settings = Settings()
        named = [
            ("CloseDesk's data folder", settings.data_dir, "CONTROLLER_INBOX_DATA_DIR"),
            ("the mail drop folder", settings.inbox_dir, "CONTROLLER_INBOX_INBOX_DIR"),
            ("the AP cost codes folder", settings.cost_codes_folder, "CONTROLLER_INBOX_COST_CODES_DIR"),
        ]
    except Exception:  # a setting that isn't valid: CloseDesk says so itself as it starts
        return []
    found = []
    for what, folder, setting in named:
        folder = Path(folder).expanduser()
        found.append((what, Path(os.path.normpath(folder if folder.is_absolute() else Path(root) / folder)), setting))
    return found


def mac_script(root: Path, *, guarded: str = "") -> str:
    """CloseDesk.app's program. Opened from the Dock or Spotlight there is no Terminal: what it says goes to the app's
    log (launch.py --app's), and a folder that is gone is said in an alert. ``guarded``: which folder macOS guards, and
    where ("CloseDesk's folder is in your Documents folder"), when one is."""
    if guarded:
        does = (
            f"# CloseDesk.app: {guarded}, which macOS lets an app like this one into only after asking,\n"
            "# so this opens CloseDesk.command in Terminal (CloseDesk in the browser), as a double-click on it does."
        )
    else:
        does = "# CloseDesk.app: opens CloseDesk in a window of its own (scripts/launch.py --app in CloseDesk's folder)."
    lines = [
        "#!/bin/bash",
        does,
        "# CloseDesk wrote this, and writes it again when its folder moves: a change made here doesn't last.",
        f"ROOT={shlex.quote(str(root))}",
        'LOG="$HOME/Library/Logs/CloseDesk/CloseDesk-app.log"',
        'mkdir -p "${LOG%/*}" 2>/dev/null',
        'exec >>"$LOG" 2>&1',
        "# Opened from the Dock, an app finds only the system's programs: Homebrew's too (Tesseract, which reads scans).",
        'export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH" PYTHONUTF8=1',
        "# With the kind of processor it runs as (arm64, or x86_64: an Intel Mac, or Rosetta on Apple's).",
        'echo "$(date "+%Y-%m-%d %H:%M:%S") CloseDesk.app opened ($(/usr/bin/uname -m))"',
        "# This app's own folder, before anything changes the working folder.",
        'APP_DIR="$(cd "${0%/*}" 2>/dev/null && pwd -P)"',
        'if [ ! -f "$ROOT/CloseDesk.command" ]; then',
        '  /usr/bin/osascript -e "on run argv" -e "display alert (item 1 of argv) message (item 2 of argv)" -e "end run" \\',
        "    \"CloseDesk can't find its folder\" \"It was $ROOT. Double-click CloseDesk.command in CloseDesk's folder, where it is now: after that, this opens CloseDesk again.\"",
        "  exit 1",
        "fi",
        'cd "$ROOT" || exit 1',
    ]
    if guarded:
        lines.append('exec /usr/bin/open -a Terminal "$ROOT/CloseDesk.command"')
    else:
        lines += [
            "# Not set up yet, or its Python is gone: CloseDesk.command sets it up in Terminal, showing what it does.",
            'if [ ! -x "$ROOT/.venv/bin/python" ]; then',
            '  exec /usr/bin/open -a Terminal "$ROOT/CloseDesk.command"',
            "fi",
            "# Where this app is: it can be moved (to /Applications, say). launch.py starts Python through its link, and",
            "# records it, so that it is kept up to date and follows a moved folder.",
            'export CLOSEDESK_APP_BUNDLE="$(cd "$APP_DIR/../.." 2>/dev/null && pwd -P)"',
            'exec "$ROOT/.venv/bin/python" "$ROOT/scripts/launch.py" --app',
        ]
    return "\n".join(lines) + "\n"


def _bundle_version(root: Path) -> str:
    """This copy's version number (2026.11.16), or 1.0 for a developer's copy."""
    try:
        for line in (root / ".git_archival.txt").read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition(":")
            if name.strip() == "date" and "$" not in value:
                return version.number(value.strip()) or "1.0"
    except OSError:
        pass
    return "1.0"


def info_plist(root: Path) -> dict:
    number = _bundle_version(root)
    return {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": NAME,
        "CFBundleExecutable": NAME,
        "CFBundleIconFile": f"{NAME}.icns",
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": number,
        "CFBundleVersion": number,
        "LSApplicationCategoryType": "public.app-category.business",
        "LSMinimumSystemVersion": "11.0",
        # Not LSMultipleInstancesProhibited: that refuses CloseDesk to anyone else on this Mac (Fast User Switching)
        # while one person has it open, before CloseDesk can say why. A second click in one session brings the open
        # CloseDesk forward without it (Launch Services does).
        # Without it the window is blurry on a Retina screen.
        "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
        # CloseDesk is on this computer (127.0.0.1), over plain http.
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        # The folder it opens, read back when the folder moves.
        "CloseDeskFolder": str(root),
    }


class Mac:
    """CloseDesk.app in Applications in the person's own folder."""

    script = "CloseDesk.command"
    places_said = {"Applications": "in Applications in your user folder", "CloseDesk's folder": "in CloseDesk's folder"}
    # Without its environment, CloseDesk.app opens CloseDesk.command, which sets it up: nothing offers to delete it.
    lost_without_environment = False
    _FIND = object()

    def __init__(self, root, *, apps=None, home=None, macos=None, interpreter=_FIND, register=None, folders=_FIND) -> None:
        self.root = Path(root)
        self.home = Path(home) if home else Path.home()
        named = os.environ.get(DIR_ENV, "")
        self.apps = Path(apps) if apps else Path(named) if named else self.home / "Applications"
        self.macos = tuple(macos) if macos else _macos()
        self.interpreter = mac_interpreter() if interpreter is Mac._FIND else interpreter
        self.register = register or _lsregister
        # Where macOS guards what CloseDesk opens from CloseDesk.app: CloseDesk's folder, or a folder its settings put
        # elsewhere (the data, the mail drop, the cost codes), which CloseDesk started by the app reads as the app.
        self.folders = settings_folders(self.root) if folders is Mac._FIND else list(folders)
        self.guarded, self.guarded_folder = _guarded(self.root, self.home), None
        for what, folder, setting in [] if self.guarded else self.folders:
            words = _guarded(folder, self.home)
            if words:
                self.guarded, self.guarded_folder = words, (what, folder, setting)
                break

    def guarded_said(self) -> str:
        """Which folder macOS guards, and where ("" when none)."""
        if not self.guarded:
            return ""
        if self.guarded_folder is None:
            return f"CloseDesk's folder is in {self.guarded}"
        what, folder, _ = self.guarded_folder
        return f"{what[0].upper()}{what[1:]} ({folder}) is in {self.guarded}"

    def places(self, saved: dict | None = None) -> list[tuple[str, Path, bool]]:
        """(name, the app there, whether it is only kept right: never made). Besides the two places CloseDesk makes it,
        each place the person opened it from (moved to /Applications, say: launch.py --app records it, ``APPS_KEY``)."""
        places = [("Applications", self.apps / f"{NAME}.app", False)]
        in_folder = self.root / f"{NAME}.app"
        if in_folder.exists():  # made there when Applications couldn't be written
            places.append(("CloseDesk's folder", in_folder, False))
        for named in opened_from(saved or {}):
            app = Path(named)
            if any(_same(app, path) for _, path, _ in places) or not app.exists():
                continue
            places.append((f"in {app.parent}", app, True))
        return places

    def fallback(self):
        return ("CloseDesk's folder", self.root / f"{NAME}.app")

    def look(self, app: Path):
        if not app.exists():
            return None
        try:
            info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
        except (OSError, ValueError, plistlib.InvalidFileException):
            return THEIRS
        if not isinstance(info, dict) or info.get("CFBundleIdentifier") != BUNDLE_ID:
            return THEIRS
        if _same(app.parent, self.root):
            return str(self.root)  # the one in CloseDesk's folder is that folder's, wherever the folder went
        return str(info.get("CloseDeskFolder") or "")

    def icon(self) -> bytes:
        # macOS 26 rounds an app's icon itself, and puts one with clear edges on a grey tile: the full square there.
        return (STATIC / mac_icon_name(self.macos)).read_bytes()

    def put(self, app: Path) -> bool:
        contents = app / "Contents"
        changed = False
        # Info.plist first: an app cut short is still known as CloseDesk's, and put right at the next start.
        for name, data, mode in (
            ("Info.plist", plistlib.dumps(info_plist(self.root), sort_keys=True), 0o644),
            ("PkgInfo", b"APPL????", 0o644),
            (f"MacOS/{NAME}", mac_script(self.root, guarded=self.guarded_said()).encode("utf-8"), 0o755),
            (f"Resources/{NAME}.icns", self.icon(), 0o644),
        ):
            changed |= _write(contents / name, data, mode)
        link = contents / "MacOS" / LINKED_PYTHON
        if self.interpreter is not None:
            changed |= _symlink(link, Path(self.interpreter))
        elif link.is_symlink():
            link.unlink()
            changed = True
        if changed:
            self.register(app)
        return changed

    def said(self, done: dict, failed: list, *, explicit: bool) -> Said:
        where = lambda names: _join([self.places_said.get(name, name) for name in names])  # noqa: E731
        if not self.guarded:
            guarded = ""
        elif self.guarded_folder is None:
            guarded = (
                f" {self.guarded_said()}, which macOS lets an app like this into only after asking, so from there it "
                "opens CloseDesk in Terminal and your browser, as CloseDesk.command does. For the window, move the "
                "CloseDesk folder into your home folder and double-click CloseDesk.command once."
            )
        else:
            guarded = (
                f" {self.guarded_said()}, which macOS lets an app like this into only after asking, so CloseDesk.app "
                "opens CloseDesk in Terminal and your browser, as CloseDesk.command does. For the window, move that "
                f"folder elsewhere in your home folder, change {self.guarded_folder[2]} in the .env file in CloseDesk's "
                "folder to match, and double-click CloseDesk.command once."
            )
        window = "" if self.guarded else ": it opens CloseDesk in a window of its own"
        lines = []
        if "Applications" in done["made"]:
            lines.append(
                f"Added CloseDesk to Applications in your user folder{window}. Find it with Spotlight, and keep it in the "
                f"Dock to open CloseDesk with a click.{guarded}"
            )
        elif done["made"]:
            lines.append(f"CloseDesk.app {where(done['made'])} was made again{window}. Drag it to the Dock to open CloseDesk with a click.{guarded}")
        if done["fallback"]:
            why = next((why for name, why in failed if name == "Applications"), "")
            lines.append(
                f"CloseDesk couldn't add itself to Applications in your user folder ({why}), so CloseDesk.app is in "
                f"CloseDesk's folder: drag it to the Dock to open CloseDesk with a click.{guarded}"
            )
            failed = [item for item in failed if item[0] != "Applications"]
        if done["moved"]:
            lines.append(f"CloseDesk.app {where(done['moved'])} now opens CloseDesk from this folder (CloseDesk's folder was moved).")
        if done["fresh"]:
            lines.append(f"CloseDesk.app {where(done['fresh'])} was brought up to date.{guarded}")
        if done["kept"] and explicit:
            if "Applications" in done["kept"]:
                lines.append("CloseDesk is in Applications in your user folder: find it with Spotlight, or keep it in the Dock.")
            else:
                lines.append(f"CloseDesk.app is {where(done['kept'])}: drag it to the Dock to open CloseDesk with a click.")
        if done["theirs"]:
            lines.append(f"There is already an app named CloseDesk {where(done['theirs'])}, so it was left as it is.")
        if done["elsewhere"]:
            lines.append(
                f"CloseDesk.app {where(done['elsewhere'])} opens CloseDesk from a folder this Mac hasn't got, so it was left "
                "as it is. To have it open this CloseDesk, use Settings → Add CloseDesk to Applications again."
            )
        for name, why in failed:
            did = "check" if name in done["unread"] else "put"
            lines.append(f"CloseDesk couldn't {did} CloseDesk.app {where([name])} ({why}).")
        ok = any(done[key] for key in ("made", "moved", "back", "fresh", "kept", "fallback"))
        if failed and not ok:
            lines.append(f"{self.script} in CloseDesk's folder still opens it.")
        return Said(ok, " ".join(lines))


def _write(path: Path, data: bytes, mode: int) -> bool:
    """Write ``data`` to ``path`` (whole, never half) unless it holds it already. True when it was written. (Windows
    has no such modes: there the content alone counts.)"""
    try:
        if (
            path.is_file() and not path.is_symlink() and path.read_bytes() == data
            and (os.name == "nt" or stat.S_IMODE(path.stat().st_mode) == mode)
        ):
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.new")
    try:
        temporary.write_bytes(data)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return True


def _symlink(link: Path, target: Path) -> bool:
    try:
        if link.is_symlink() and os.readlink(link) == str(target):
            return False
    except OSError:
        pass
    link.parent.mkdir(parents=True, exist_ok=True)
    temporary = link.with_name(f".{link.name}.{os.getpid()}.{threading.get_ident()}.new")
    if temporary.is_symlink() or temporary.exists():
        temporary.unlink()
    os.symlink(target, temporary)
    os.replace(temporary, link)
    return True


# Keeping it -------------------------------------------------------------------------------------------------------


_HERE = object()


def system_here(*, platform: str | None = None, root=None):
    """The shortcut this computer has (Windows' or macOS's), for CloseDesk's folder; None on Linux."""
    platform = platform or sys.platform
    root = Path(root or version.ROOT)
    if platform == "win32":
        return Windows(root)
    if platform == "darwin":
        return Mac(root)
    return None


def _read_record(record: Path) -> dict:
    try:
        found = json.loads(record.read_text(encoding="utf-8"))
        return found if isinstance(found, dict) else {}
    except (OSError, ValueError):
        return {}


def opened_from(saved: dict) -> list[str]:
    """macOS: the CloseDesk.app bundles the person opened CloseDesk from, as recorded (``APPS_KEY``)."""
    apps = saved.get(APPS_KEY)
    return [str(app) for app in apps if isinstance(app, str) and app] if isinstance(apps, list) else []


def _folders_before(saved: dict) -> list[str]:
    """The folders this computer's shortcut opened before a move that a shortcut hasn't followed yet (``BEFORE_KEY``)."""
    found = saved.get(BEFORE_KEY)
    return [str(folder) for folder in found if isinstance(folder, str) and folder] if isinstance(found, list) else []


def _tend(system, *, explicit: bool, record: Path, new_environment: bool = False) -> Said:
    saved = _read_record(record)
    first = explicit or not saved.get("made")
    # The folder this computer's shortcut last opened. Only a shortcut naming that folder, gone, is this computer's
    # after a move: one naming any other folder this computer hasn't got is another computer's (a Desktop OneDrive
    # keeps in step between two), and pointing it here would take it from that computer, for good. (With the folders
    # it opened before a move that a shortcut couldn't follow then: one opening those is this computer's too.)
    known = str(saved.get("folder") or "")
    before = _folders_before(saved)
    ours_before = [folder for folder in [known, *before] if folder and not _same(folder, system.root)]

    def opened_ours(found: str) -> bool:
        """A shortcut opening a folder this computer's opened, now gone: CloseDesk's folder was moved from it."""
        return any(_same(folder, found) for folder in ours_before) and not _closedesk_at(found)

    # Where this computer's shortcuts were at the start before (None: a record that didn't say, so every place).
    had = saved.get("places")
    had = {str(name) for name in had} if isinstance(had, list) else None
    # CloseDesk's folder moved since this computer made its shortcut: what the move cost (a shortcut Windows' "Problem
    # with Shortcut" offered to delete) is put back, as at the first start; only where this computer had one, never on
    # a Desktop OneDrive shares with a computer whose shortcut it was. Deleted where the folder stayed, it isn't.
    moved_here = bool(known) and not _same(known, system.root) and not _closedesk_at(known)
    # The folder's environment was just made (the first start, or made again: an update by hand, the folders swapped;
    # its Python removed): meanwhile, Windows offered to delete a shortcut whose pythonw.exe was gone. Put back the
    # same way, in the same folder.
    set_up_here = new_environment and getattr(system, "lost_without_environment", False) and not moved_here
    done: dict[str, list[str]] = {
        key: [] for key in ("made", "moved", "back", "fresh", "kept", "theirs", "fallback", "elsewhere", "unread")
    }
    failed: list[tuple[str, str]] = []
    for name, path, kept_only in system.places(saved):
        try:
            found = system.look(path)
        except Exception as exc:
            # One that can't be read (online only on a Desktop OneDrive keeps, with OneDrive not running; damaged) is
            # said, and counted as this computer's, as one that couldn't be written is: the other places are kept all
            # the same.
            failed.append((name, _why(exc)))
            done["unread"].append(name)
            continue
        if kept_only:
            # Only kept right, never made or taken over: the one pinned to the taskbar (Windows makes it), a CloseDesk.app
            # the person moved elsewhere. Put right when it opens this folder, or the folder this one moved from.
            if found is None or found is THEIRS:
                continue
            if _same(found, system.root):
                kind = "fresh"
            elif opened_ours(found):
                kind = "moved"
            else:
                continue
        elif found is None:
            if moved_here and not explicit and (had is None or name in had):
                kind = "moved"
            elif set_up_here and not explicit and saved.get("made") and (had is None or name in had):
                kind = "back"
            elif not first:
                continue  # the person deleted it: it stays deleted
            else:
                kind = "made"
        elif found is THEIRS:
            if not explicit:
                done["theirs"].append(name)
                continue
            kind = "made"
        elif _same(found, system.root):
            kind = "fresh"
        elif explicit:
            kind = "made"
        elif _closedesk_at(found):
            continue  # another CloseDesk folder's, still there
        elif opened_ours(found):
            kind = "moved"
        else:
            if first:  # said once: the first time this computer finds it
                done["elsewhere"].append(name)
            continue  # another computer's
        try:
            changed = system.put(path)
        except Exception as exc:
            failed.append((name, _why(exc)))
            continue
        done[kind if changed else "kept"].append(name)
    made = any(done[key] for key in ("made", "moved", "back", "fresh", "kept"))
    fallback = system.fallback()
    if first and failed and not made and fallback is not None:
        name, path = fallback
        try:
            system.put(path)
            done["fallback"].append(name)
        except Exception as exc:
            failed.append((name, _why(exc)))
    # Where this computer's shortcuts are now (one that couldn't be written this time still counts).
    mine = sorted(
        {name for key in ("made", "moved", "back", "fresh", "kept", "fallback") for name in done[key]} | {name for name, _ in failed}
    )
    # A place that couldn't be read or written as the folder moved still opens the folder it moved from: kept, so that
    # it follows at a later start. Once every place could be, they are forgotten.
    still_before = []
    if failed:
        for folder in ours_before:
            if not _closedesk_at(folder) and not any(_same(folder, kept) for kept in still_before):
                still_before.append(folder)
    still_before = still_before[:BEFORE_MOST]
    # This start's folder has no shortcut of its own while the folder this computer's shortcuts open is another
    # CloseDesk, still there (a second copy, a new download beside the old one): they are still that folder's, and
    # follow it when it moves or is deleted. Recorded as this folder's, they would be taken for another computer's.
    # (One that couldn't be read may well be that folder's too.)
    own = any(done[key] for key in ("made", "moved", "back", "fresh", "kept", "fallback"))
    another_copy = not own and not explicit and bool(known) and not _same(known, system.root) and _closedesk_at(known)
    # Recorded once one is made, or another computer's was found (said once: from then on it is left as it is quietly);
    # again when the folder moved, or when where this computer has its shortcuts changed (one deleted, say).
    if not another_copy and (
        (first and (made or done["fallback"] or done["elsewhere"])) or done["moved"] or done["back"]
        or (saved.get("made") and (had is None or had != set(mine) or moved_here))
        or still_before != before
    ):
        apps = [app for app in opened_from(saved) if Path(app).exists()]
        try:
            record.parent.mkdir(parents=True, exist_ok=True)
            record.write_text(json.dumps({
                "made": saved.get("made") if saved.get("made") and not first else time.strftime("%Y-%m-%d %H:%M:%S"),
                "folder": str(system.root), "places": mine, **({APPS_KEY: apps} if apps else {}),
                **({BEFORE_KEY: still_before} if still_before else {}),
            }), encoding="utf-8")
        except OSError:
            pass
    return system.said(done, failed, explicit=explicit)


def _keep(system, *, explicit: bool, record, new_environment: bool = False) -> Said:
    if system is _HERE:
        system = system_here()
    if system is None:
        if explicit:
            return Said(True, "There is no CloseDesk shortcut on Linux: CloseDesk.command opens CloseDesk in your browser.")
        return Said(True, "")
    record = Path(record) if record else record_path()
    with _lock:
        try:
            return _tend(system, explicit=explicit, record=record, new_environment=new_environment)
        except Exception as exc:  # pywin32 missing or broken, a folder Windows wouldn't name
            return Said(False, f"CloseDesk couldn't check its shortcut ({_why(exc)}). {system.script} in CloseDesk's folder still opens it.")


def upkeep(system=_HERE, *, record=None, new_environment: bool = False) -> Said:
    """As a launcher starts CloseDesk: the shortcut made the first time, and kept right after that (see the module's
    note). Its message is "" when nothing needed doing. ``new_environment``: launch.py has just made the folder's
    environment for this start (``NEW_ENVIRONMENT_ENV``)."""
    return _keep(system, explicit=False, record=record, new_environment=new_environment)


def make(system=_HERE, *, record=None) -> Said:
    """Asked for (Settings, ``launch.py --shortcut``): the shortcut made again, opening this folder's CloseDesk."""
    return _keep(system, explicit=True, record=record)


def upkeep_soon(*, new_environment: bool = False) -> threading.Thread:
    """``upkeep`` beside CloseDesk as it opens (nothing waits for it), saying what it did."""

    def work() -> None:
        try:
            said = upkeep(new_environment=new_environment)
        except Exception as exc:  # never in CloseDesk's way
            print(f"CloseDesk couldn't check its shortcut ({_why(exc)}).", flush=True)
            return
        if said.message:
            print(said.message, flush=True)

    thread = threading.Thread(target=work, name="closedesk-shortcut", daemon=True)
    thread.start()
    return thread


def offer(*, platform: str | None = None) -> dict:
    """What Settings offers: the button that makes the shortcut again, and what it is for."""
    platform = platform or sys.platform
    if platform == "win32":
        return {
            "offered": True,
            "label": "Add the CloseDesk shortcut again",
            "help": "The CloseDesk shortcut on your Desktop and in your Start menu opens CloseDesk in a window of its own. "
            "Deleted it, or moved CloseDesk's folder? Add it again here.",
        }
    if platform == "darwin":
        return {
            "offered": True,
            "label": "Add CloseDesk to Applications again",
            "help": "CloseDesk in Applications (in your user folder) opens CloseDesk in a window of its own: find it with "
            "Spotlight, or keep it in the Dock. Deleted it, or moved CloseDesk's folder? Add it again here.",
        }
    return {"offered": False, "label": "", "help": ""}
