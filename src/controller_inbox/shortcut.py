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

It is made once, the first time a launcher starts CloseDesk (``upkeep``, from cli._serve), and that is remembered, so
a shortcut the person deleted isn't put back. At each start after that, one still there is kept right: pointed at this
folder when the folder it opened is gone (CloseDesk was moved or renamed), and given what changed after an update (a
new icon). One that opens another CloseDesk folder, still there, is left alone, as is a shortcut named CloseDesk that
isn't CloseDesk's own. Nothing that is right is written again (OneDrive would copy a Desktop's changes between two
computers, with conflicts). ``make`` (Settings, ``launch.py --shortcut``) makes it again, whatever was there.

``CLOSEDESK_SHORTCUT_DIR`` keeps all of it in that folder instead (the tests and the launcher check), never on the
person's Desktop or in their Applications.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform as _platform
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
from controller_inbox.app_window import APP_ID
from controller_inbox.app_window import user_dir as _closedesk_dir

NAME = "CloseDesk"
DIR_ENV = "CLOSEDESK_SHORTCUT_DIR"
# CloseDesk.app's id, kept for good: macOS keeps what it remembers about an app (its place in the Dock, what it was
# allowed) under it.
BUNDLE_ID = "io.github.mnabikhel.closedesk"
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


def known_folders() -> dict[str, Path]:
    """This person's Desktop and Start menu, where Windows says they are (a Desktop moved into OneDrive is followed)."""
    named = os.environ.get(DIR_ENV, "")
    if named:
        return {"Desktop": Path(named) / "Desktop", "Start menu": Path(named) / "Start menu"}
    from win32com.shell import shell

    return {
        "Desktop": Path(shell.SHGetKnownFolderPath(shell.FOLDERID_Desktop)),
        "Start menu": Path(shell.SHGetKnownFolderPath(shell.FOLDERID_Programs)),
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
    """CloseDesk.lnk on the Desktop and in the Start menu."""

    script = "CloseDesk.bat"
    places_said = {"Desktop": "on your Desktop", "Start menu": "in your Start menu"}

    def __init__(self, root, *, links=None, folders=None, icon_dir=None, icon_source=None) -> None:
        self.root = Path(root)
        self.links = links or WinLinks()
        self.folders = folders or known_folders
        self.icon_dir = Path(icon_dir) if icon_dir else user_dir()
        self.icon_source = Path(icon_source) if icon_source else STATIC / "closedesk.ico"

    def places(self) -> list[tuple[str, Path]]:
        return [(name, Path(folder) / f"{NAME}.lnk") for name, folder in self.folders().items()]

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
        if done["fresh"]:
            lines.append(f"The CloseDesk shortcut {where(done['fresh'])} was brought up to date.")
        if done["kept"] and explicit:
            lines.append(f"The CloseDesk shortcut is {where(done['kept'])}.")
        if done["theirs"]:
            lines.append(f"There is already a shortcut named CloseDesk {where(done['theirs'])}, so it was left as it is.")
        for name, why in failed:
            lines.append(f"CloseDesk couldn't put its shortcut {where([name])} ({why}).")
        ok = any(done[key] for key in ("made", "moved", "fresh", "kept"))
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


def _macos() -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in _platform.mac_ver()[0].split(".") if part.isdigit()) or (0,)
    except (ValueError, AttributeError):
        return (0,)


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


def mac_script(root: Path, *, guarded: str = "") -> str:
    """CloseDesk.app's program. Opened from the Dock or Spotlight there is no Terminal: what it says goes to the app's
    log (launch.py --app's), and a folder that is gone is said in an alert."""
    if guarded:
        does = (
            f"# CloseDesk.app: CloseDesk's folder is in {guarded}, which macOS lets an app like this one into only after\n"
            "# asking, so this opens CloseDesk.command in Terminal (CloseDesk in the browser), as a double-click on it does."
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
        'echo "$(date "+%Y-%m-%d %H:%M:%S") CloseDesk.app opened"',
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
        # A second double-click brings the open CloseDesk forward.
        "LSMultipleInstancesProhibited": True,
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
    _FIND = object()

    def __init__(self, root, *, apps=None, home=None, macos=None, interpreter=_FIND, register=None) -> None:
        self.root = Path(root)
        self.home = Path(home) if home else Path.home()
        named = os.environ.get(DIR_ENV, "")
        self.apps = Path(apps) if apps else Path(named) if named else self.home / "Applications"
        self.macos = tuple(macos) if macos else _macos()
        self.interpreter = mac_interpreter() if interpreter is Mac._FIND else interpreter
        self.register = register or _lsregister
        self.guarded = _guarded(self.root, self.home)

    def places(self) -> list[tuple[str, Path]]:
        places = [("Applications", self.apps / f"{NAME}.app")]
        in_folder = self.root / f"{NAME}.app"
        if in_folder.exists():  # made there when Applications couldn't be written
            places.append(("CloseDesk's folder", in_folder))
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
        name = "closedesk-full.icns" if self.macos[0] >= 26 else "closedesk.icns"
        return (STATIC / name).read_bytes()

    def put(self, app: Path) -> bool:
        contents = app / "Contents"
        changed = False
        # Info.plist first: an app cut short is still known as CloseDesk's, and put right at the next start.
        for name, data, mode in (
            ("Info.plist", plistlib.dumps(info_plist(self.root), sort_keys=True), 0o644),
            ("PkgInfo", b"APPL????", 0o644),
            (f"MacOS/{NAME}", mac_script(self.root, guarded=self.guarded).encode("utf-8"), 0o755),
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
        guarded = (
            f" CloseDesk's folder is in {self.guarded}, which macOS lets an app like this into only after asking, so from "
            "there it opens CloseDesk in Terminal and your browser, as CloseDesk.command does. For the window, move the "
            "CloseDesk folder into your home folder and double-click CloseDesk.command once."
        ) if self.guarded else ""
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
            lines.append(f"CloseDesk.app {where(done['fresh'])} was brought up to date.")
        if done["kept"] and explicit:
            if "Applications" in done["kept"]:
                lines.append("CloseDesk is in Applications in your user folder: find it with Spotlight, or keep it in the Dock.")
            else:
                lines.append(f"CloseDesk.app is {where(done['kept'])}: drag it to the Dock to open CloseDesk with a click.")
        if done["theirs"]:
            lines.append(f"There is already an app named CloseDesk {where(done['theirs'])}, so it was left as it is.")
        for name, why in failed:
            lines.append(f"CloseDesk couldn't put CloseDesk.app {where([name])} ({why}).")
        ok = any(done[key] for key in ("made", "moved", "fresh", "kept", "fallback"))
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


def _tend(system, *, explicit: bool, record: Path) -> Said:
    first = explicit or not record.exists()
    # The folder this computer's shortcut last opened: one naming a folder it never did is another computer's (a
    # Desktop OneDrive keeps in step between two), and pointing it here would have the two take turns rewriting it.
    known = str(_read_record(record).get("folder") or "")
    done: dict[str, list[str]] = {key: [] for key in ("made", "moved", "fresh", "kept", "theirs", "fallback")}
    failed: list[tuple[str, str]] = []
    for name, path in system.places():
        found = system.look(path)
        if found is None:
            if not first:
                continue  # the person deleted it: it stays deleted
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
        elif _closedesk_at(found) or (known and not _same(known, found)):
            continue  # another CloseDesk folder's, still there; or another computer's
        else:
            kind = "moved"
        try:
            changed = system.put(path)
        except Exception as exc:
            failed.append((name, _why(exc)))
            continue
        done[kind if changed else "kept"].append(name)
    made = any(done[key] for key in ("made", "moved", "fresh", "kept"))
    fallback = system.fallback()
    if first and failed and not made and fallback is not None:
        name, path = fallback
        try:
            system.put(path)
            done["fallback"].append(name)
        except Exception as exc:
            failed.append((name, _why(exc)))
    if (first and (made or done["fallback"])) or done["moved"]:
        try:
            record.parent.mkdir(parents=True, exist_ok=True)
            record.write_text(json.dumps({"made": time.strftime("%Y-%m-%d %H:%M:%S"), "folder": str(system.root)}), encoding="utf-8")
        except OSError:
            pass
    return system.said(done, failed, explicit=explicit)


def _keep(system, *, explicit: bool, record) -> Said:
    if system is _HERE:
        system = system_here()
    if system is None:
        if explicit:
            return Said(True, "There is no CloseDesk shortcut on Linux: CloseDesk.command opens CloseDesk in your browser.")
        return Said(True, "")
    record = Path(record) if record else record_path()
    with _lock:
        try:
            return _tend(system, explicit=explicit, record=record)
        except Exception as exc:  # pywin32 missing or broken, a folder Windows wouldn't name
            return Said(False, f"CloseDesk couldn't check its shortcut ({_why(exc)}). {system.script} in CloseDesk's folder still opens it.")


def upkeep(system=_HERE, *, record=None) -> Said:
    """As a launcher starts CloseDesk: the shortcut made the first time, and kept right after that (see the module's
    note). Its message is "" when nothing needed doing."""
    return _keep(system, explicit=False, record=record)


def make(system=_HERE, *, record=None) -> Said:
    """Asked for (Settings, ``launch.py --shortcut``): the shortcut made again, opening this folder's CloseDesk."""
    return _keep(system, explicit=True, record=record)


def upkeep_soon() -> threading.Thread:
    """``upkeep`` beside CloseDesk as it opens (nothing waits for it), saying what it did."""

    def work() -> None:
        try:
            said = upkeep()
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
