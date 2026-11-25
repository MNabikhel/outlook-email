"""The CloseDesk shortcut (shortcut.py): what opens CloseDesk in its window with a double-click. On Windows, CloseDesk.lnk
on the Desktop and in the Start menu, running the environment's pythonw on launch.py --app, with CloseDesk's icon and
the taskbar id its window gives itself; on macOS, CloseDesk.app in the Applications folder of the person's own folder.
Made once, the first time a launcher starts CloseDesk; kept pointing at the folder when it moves; never put back once
the person deleted it, unless they ask (Settings, launch.py --shortcut).

Windows' and macOS's parts are run here with stand-ins (a fake pywin32, a fake lsregister); the Mac app's script is run
for real with bash. The real thing runs in CI on Windows and macOS (tests/launcher_smoke.py, and the Windows-only test
at the end)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import plistlib
import stat
import subprocess
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from controller_inbox import app_window, shortcut

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "controller_inbox" / "static"


def _closedesk(folder: Path) -> Path:
    """A CloseDesk folder, as far as the shortcut can tell (its launch.py, and its environment's pythonw)."""
    (folder / "scripts").mkdir(parents=True)
    (folder / "scripts" / "launch.py").write_text("", encoding="utf-8")
    (folder / "CloseDesk.command").write_text("", encoding="utf-8")
    return folder


# Windows ------------------------------------------------------------------------------------------------------------


class Links:
    """Windows' .lnk files, standing in: what each holds, and what was written."""

    def __init__(self) -> None:
        self.files: dict[Path, dict] = {}
        self.written: list[Path] = []
        self.refuse: dict[Path, Exception] = {}

    def read(self, path: Path) -> dict | None:
        found = self.files.get(Path(path))
        return dict(found) if found is not None else None

    def write(self, path: Path, fields: dict) -> None:
        path = Path(path)
        if path in self.refuse:
            raise self.refuse[path]
        self.files[path] = dict(fields)
        self.written.append(path)


@pytest.fixture
def windows(tmp_path):
    """CloseDesk on a Windows computer: its folder, the person's Desktop and Start menu, and its own folder."""
    root = _closedesk(tmp_path / "Close Desk")
    places = {"Desktop": tmp_path / "OneDrive - Firm" / "Desktop", "Start menu": tmp_path / "Start Menu" / "Programs"}
    links = Links()
    system = shortcut.Windows(root, links=links, folders=lambda: dict(places), icon_dir=tmp_path / "LocalAppData" / "CloseDesk")
    return SimpleNamespace(
        root=root, places=places, links=links, system=system, record=tmp_path / "LocalAppData" / "CloseDesk" / "shortcut.json",
        desktop=places["Desktop"] / "CloseDesk.lnk", start=places["Start menu"] / "CloseDesk.lnk", tmp=tmp_path,
    )


def _upkeep(w):
    return shortcut.upkeep(w.system, record=w.record)


def _make(w):
    return shortcut.make(w.system, record=w.record)


def test_the_first_launcher_start_puts_the_shortcut_on_the_desktop_and_in_the_start_menu(windows):
    said = _upkeep(windows)
    assert said.ok and set(windows.links.written) == {windows.desktop, windows.start}
    link = windows.links.files[windows.desktop]
    root = windows.root
    # pythonw: no console window; the folder as its working folder (CloseDesk reads its .env there).
    assert link["target"] == str(root / ".venv" / "Scripts" / "pythonw.exe")
    assert link["arguments"] == f'"{root / "scripts" / "launch.py"}" --app'
    assert link["folder"] == str(root)
    # The taskbar groups the window under the shortcut, and pinning it pins the shortcut: the window's own id.
    assert link["app_id"] == app_window.APP_ID == "CloseDesk.CloseDesk"
    assert link["description"] == "CloseDesk"
    assert windows.links.files[windows.start] == link
    assert "Desktop" in said.message and "Start menu" in said.message and "window of its own" in said.message
    assert json.loads(windows.record.read_text(encoding="utf-8"))["folder"] == str(root)


def test_the_shortcuts_icon_is_a_copy_of_closedesks_kept_in_the_persons_own_folder(windows):
    _upkeep(windows)
    icon = Path(windows.links.files[windows.desktop]["icon"])
    data = (STATIC / "closedesk.ico").read_bytes()
    # Outside the CloseDesk folder (an update replaces that; OneDrive may keep it online only), and named for what it
    # holds, so Windows doesn't show an old picture it kept of an icon with the same name.
    assert icon.parent == windows.tmp / "LocalAppData" / "CloseDesk"
    assert icon.name == f"closedesk-{hashlib.sha256(data).hexdigest()[:8]}.ico" and icon.read_bytes() == data
    assert windows.links.files[windows.desktop]["icon_index"] == 0


def test_a_shortcut_that_is_right_is_left_as_it_is(windows):
    # Rewritten at every start, a Desktop that OneDrive keeps in step between two computers fills with conflict copies.
    _upkeep(windows)
    windows.links.written.clear()
    said = _upkeep(windows)
    assert said.ok and said.message == "" and windows.links.written == []


def test_a_shortcut_the_person_deleted_isnt_put_back_at_the_next_start(windows):
    _upkeep(windows)
    del windows.links.files[windows.desktop]
    windows.links.written.clear()
    said = _upkeep(windows)
    assert windows.desktop not in windows.links.files and windows.links.written == [] and said.message == ""


def test_asking_for_it_again_makes_it_again(windows):
    _upkeep(windows)
    del windows.links.files[windows.desktop]
    del windows.links.files[windows.start]
    said = _make(windows)
    assert said.ok and set(windows.links.files) == {windows.desktop, windows.start}
    assert "Desktop" in said.message and "Start menu" in said.message


def test_asking_when_the_shortcut_is_there_says_where_it_is(windows):
    _upkeep(windows)
    windows.links.written.clear()
    said = _make(windows)
    assert said.ok and windows.links.written == []
    assert "Desktop" in said.message and "Start menu" in said.message


def test_a_moved_folder_is_found_again_by_its_shortcuts_at_the_next_start(windows, tmp_path):
    _upkeep(windows)
    moved = tmp_path / "Somewhere else" / "CloseDesk"
    moved.parent.mkdir()
    windows.root.rename(moved)
    system = shortcut.Windows(moved, links=windows.links, folders=lambda: dict(windows.places), icon_dir=windows.system.icon_dir)
    said = shortcut.upkeep(system, record=windows.record)
    for path in (windows.desktop, windows.start):
        link = windows.links.files[path]
        assert link["folder"] == str(moved) and link["target"] == str(moved / ".venv" / "Scripts" / "pythonw.exe"), path
    assert said.ok and "moved" in said.message


def test_a_shortcut_to_another_closedesk_folder_that_is_still_there_is_left_alone(windows, tmp_path):
    # Two copies of CloseDesk: the shortcut opens the one it was made for.
    _upkeep(windows)
    other = _closedesk(tmp_path / "CloseDesk copy")
    system = shortcut.Windows(other, links=windows.links, folders=lambda: dict(windows.places), icon_dir=windows.system.icon_dir)
    windows.links.written.clear()
    said = shortcut.upkeep(system, record=windows.record)
    assert windows.links.written == [] and said.message == ""
    assert windows.links.files[windows.desktop]["folder"] == str(windows.root)
    # Asked for from the copy, it opens the copy.
    shortcut.make(system, record=windows.record)
    assert windows.links.files[windows.desktop]["folder"] == str(other)


def test_a_closedesk_shortcut_the_person_made_is_left_alone_unless_they_ask(windows):
    # Their own shortcut named CloseDesk (to CloseDesk.bat, say) has no CloseDesk taskbar id.
    theirs = {"target": str(windows.root / "CloseDesk.bat"), "arguments": "", "folder": str(windows.root), "icon": "",
              "icon_index": 0, "description": "", "app_id": ""}
    windows.links.files[windows.desktop] = dict(theirs)
    said = _upkeep(windows)
    assert windows.links.files[windows.desktop] == theirs and windows.links.written == [windows.start]
    assert said.ok and "Start menu" in said.message
    _make(windows)
    assert windows.links.files[windows.desktop]["app_id"] == app_window.APP_ID


def test_a_desktop_that_cant_be_written_is_said_and_the_start_menu_still_gets_it(windows):
    # Controlled Folder Access, or a policy: one plain line, and CloseDesk carries on.
    windows.links.refuse[windows.desktop] = PermissionError(13, "Access is denied")
    said = _upkeep(windows)
    assert said.ok and windows.links.written == [windows.start]
    assert "Desktop" in said.message and "Access is denied" in said.message
    assert windows.record.exists()


def test_when_no_shortcut_could_be_made_it_is_tried_again_next_start(windows):
    for path in (windows.desktop, windows.start):
        windows.links.refuse[path] = OSError("The device is not ready")
    said = _upkeep(windows)
    assert not said.ok and "couldn't" in said.message and "CloseDesk.bat" in said.message
    assert not windows.record.exists()
    windows.links.refuse.clear()
    assert _upkeep(windows).ok and set(windows.links.files) == {windows.desktop, windows.start}


def test_after_an_update_with_a_new_icon_the_shortcut_shows_it(windows, monkeypatch, tmp_path):
    _upkeep(windows)
    before = windows.links.files[windows.desktop]["icon"]
    new_icon = tmp_path / "new.ico"
    new_icon.write_bytes(b"\x00\x00\x01\x00 a new picture")
    monkeypatch.setattr(windows.system, "icon_source", new_icon)
    said = _upkeep(windows)
    after = windows.links.files[windows.desktop]["icon"]
    assert after != before and Path(after).read_bytes() == new_icon.read_bytes()
    assert said.ok and set(windows.links.written[-2:]) == {windows.desktop, windows.start}


def test_closedesk_started_twice_at_once_makes_the_shortcut_once(windows):
    # Two quick double-clicks: both CloseDesks keep the shortcut as they start.
    threads = [threading.Thread(target=_upkeep, args=(windows,)) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert set(windows.links.files) == {windows.desktop, windows.start}


# The shortcut, written with pywin32 (Windows' own IShellLink), with a stand-in pywin32 -------------------------------


class _Com:
    """pywin32's COM objects, standing in: every call made, in order."""

    VT_LPWSTR = 31
    CLSCTX_INPROC_SERVER = 1
    IID_IPersistFile = "IPersistFile"

    def __init__(self, existing=None) -> None:
        self.calls: list[tuple] = []
        self.existing = existing or {}
        self.threads: list[str] = []

    # pythoncom
    def CoInitialize(self):
        self.calls.append(("CoInitialize",))
        self.threads.append(threading.current_thread().name)

    def CoUninitialize(self):
        self.calls.append(("CoUninitialize",))

    def CoCreateInstance(self, clsid, outer, context, iid):
        self.calls.append(("CoCreateInstance", clsid, iid))
        return _Link(self)


class _Link:
    def __init__(self, com: _Com) -> None:
        self.com = com
        self.values = dict(com.existing)

    def __getattr__(self, name):
        def call(*args):
            self.com.calls.append((name, *args))
            getter = {
                "GetPath": (self.values.get("target", ""), None),
                "GetArguments": self.values.get("arguments", ""),
                "GetWorkingDirectory": self.values.get("folder", ""),
                "GetIconLocation": (self.values.get("icon", ""), self.values.get("icon_index", 0)),
                "GetDescription": self.values.get("description", ""),
                "GetFlags": self.values.get("flags", 0),
                "GetValue": SimpleNamespace(GetValue=lambda: self.values.get("app_id")),
            }
            if name == "QueryInterface":
                return self
            return getter.get(name)

        return call


@pytest.fixture
def pywin32(monkeypatch):
    """A stand-in pywin32 in sys.modules, as the Windows writer imports it."""
    com = _Com()
    pythoncom = types.ModuleType("pythoncom")
    for name in ("CoInitialize", "CoUninitialize", "CoCreateInstance"):
        setattr(pythoncom, name, getattr(com, name))
    pythoncom.VT_LPWSTR, pythoncom.CLSCTX_INPROC_SERVER, pythoncom.IID_IPersistFile = 31, 1, "IPersistFile"
    shell = SimpleNamespace(
        CLSID_ShellLink="ShellLink", IID_IShellLink="IShellLink", IID_IShellLinkDataList="IShellLinkDataList",
        FOLDERID_Desktop="Desktop", FOLDERID_Programs="Programs",
        SHGetKnownFolderPath=lambda fid, flags=0, token=None: {"Desktop": r"C:\Users\ana\OneDrive - Firm\Desktop",
                                                               "Programs": r"C:\Users\ana\AppData\Roaming\Microsoft\Windows\Start Menu\Programs"}[fid],
        SHChangeNotify=lambda *args: com.calls.append(("SHChangeNotify", *args)),
    )
    shellcon = SimpleNamespace(SHCNE_UPDATEITEM=0x2000, SHCNF_PATHW=5)
    propsys = SimpleNamespace(IID_IPropertyStore="IPropertyStore", PROPVARIANTType=lambda value, vt: ("PROPVARIANT", value, vt))
    pscon = SimpleNamespace(PKEY_AppUserModel_ID="PKEY_AppUserModel_ID")
    win32com = types.ModuleType("win32com")
    shell_package = types.ModuleType("win32com.shell")
    shell_package.shell, shell_package.shellcon = shell, shellcon
    propsys_package = types.ModuleType("win32com.propsys")
    propsys_package.propsys, propsys_package.pscon = propsys, pscon
    for name, module in (("pythoncom", pythoncom), ("win32com", win32com), ("win32com.shell", shell_package),
                         ("win32com.propsys", propsys_package)):
        monkeypatch.setitem(sys.modules, name, module)
    return com


def test_the_shortcut_is_written_as_windows_documents_it(pywin32, tmp_path):
    path = tmp_path / "Desktop" / "CloseDesk.lnk"
    fields = {"target": r"C:\CloseDesk\.venv\Scripts\pythonw.exe", "arguments": r'"C:\CloseDesk\scripts\launch.py" --app',
              "folder": r"C:\CloseDesk", "icon": r"C:\Users\ana\AppData\Local\CloseDesk\closedesk-1234abcd.ico",
              "icon_index": 0, "description": "CloseDesk", "app_id": app_window.APP_ID}
    shortcut.WinLinks().write(path, fields)
    calls = [call for call in pywin32.calls if call[0] not in ("QueryInterface",)]
    names = [call[0] for call in calls]
    assert names[0] == "CoInitialize" and names[-1] == "CoUninitialize", "COM is started in the thread that writes"
    assert ("CoCreateInstance", "ShellLink", "IShellLink") in calls
    assert ("SetPath", fields["target"]) in calls and ("SetArguments", fields["arguments"]) in calls
    assert ("SetWorkingDirectory", fields["folder"]) in calls and ("SetIconLocation", fields["icon"], 0) in calls
    assert ("SetDescription", "CloseDesk") in calls and ("SetShowCmd", 1) in calls
    # A moved folder: Windows' own "Problem with Shortcut", not a search that could find some other pythonw.exe.
    assert ("SetFlags", 0x100 | 0x40000) in calls
    # The taskbar id, set after the rest (as Microsoft says), then saved.
    id_set = calls.index(("SetValue", "PKEY_AppUserModel_ID", ("PROPVARIANT", "CloseDesk.CloseDesk", 31)))
    assert id_set > names.index("SetShowCmd") and names[id_set + 1] == "Commit"
    assert names.index("Save") > id_set and ("Save", str(path), 0) in calls
    assert path.parent.is_dir()


def test_a_shortcut_is_read_back_with_its_taskbar_id(pywin32, tmp_path):
    path = tmp_path / "CloseDesk.lnk"
    path.write_bytes(b"L")
    pywin32.existing = {"target": r"C:\CloseDesk\.venv\Scripts\pythonw.exe", "arguments": "--app", "folder": r"C:\CloseDesk",
                        "icon": r"C:\x.ico", "icon_index": 0, "description": "CloseDesk", "app_id": "CloseDesk.CloseDesk",
                        "flags": 0x40100}
    found = shortcut.WinLinks().read(path)
    assert found["app_id"] == "CloseDesk.CloseDesk" and found["folder"] == r"C:\CloseDesk" and found["flags"] == 0x40100
    assert ("Load", str(path), 0) in pywin32.calls
    assert shortcut.WinLinks().read(tmp_path / "none.lnk") is None


def test_a_shortcut_with_no_taskbar_id_reads_as_someone_elses(pywin32, tmp_path):
    path = tmp_path / "CloseDesk.lnk"
    path.write_bytes(b"L")
    pywin32.existing = {"target": r"C:\CloseDesk\CloseDesk.bat"}
    assert shortcut.WinLinks().read(path)["app_id"] == ""


def test_the_desktop_and_start_menu_are_windows_known_folders(pywin32, monkeypatch):
    # A Desktop moved into OneDrive is where Windows says, never %USERPROFILE%\Desktop built by hand.
    monkeypatch.delenv(shortcut.DIR_ENV, raising=False)
    places = shortcut.known_folders()
    assert places["Desktop"] == Path(r"C:\Users\ana\OneDrive - Firm\Desktop")
    assert places["Start menu"] == Path(r"C:\Users\ana\AppData\Roaming\Microsoft\Windows\Start Menu\Programs")


def test_a_shortcut_folder_named_for_the_check_keeps_it_off_the_desktop(monkeypatch, tmp_path):
    # The tests and the launcher check (CI) never put a shortcut on the computer's Desktop.
    monkeypatch.setenv(shortcut.DIR_ENV, str(tmp_path / "shortcuts"))
    places = shortcut.known_folders()
    assert places == {"Desktop": tmp_path / "shortcuts" / "Desktop", "Start menu": tmp_path / "shortcuts" / "Start menu"}
    assert shortcut.user_dir() == tmp_path / "shortcuts"


def test_the_tests_never_reach_the_computers_desktop():
    # conftest names a folder of its own for every test session.
    assert os.environ.get(shortcut.DIR_ENV)


# macOS --------------------------------------------------------------------------------------------------------------


@pytest.fixture
def mac(tmp_path):
    root = _closedesk(tmp_path / "home" / "Close Desk's mail")
    python = tmp_path / "Python.framework" / "Versions" / "3.12" / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    registered: list[Path] = []
    apps = tmp_path / "home" / "Applications"
    system = shortcut.Mac(root, apps=apps, home=tmp_path / "home", macos=(15, 6), interpreter=python, register=registered.append)
    return SimpleNamespace(root=root, apps=apps, app=apps / "CloseDesk.app", python=python, registered=registered, system=system,
                           record=tmp_path / "support" / "shortcut.json", tmp=tmp_path)


def _plist(app: Path) -> dict:
    with (app / "Contents" / "Info.plist").open("rb") as handle:
        return plistlib.load(handle)


def test_the_first_launcher_start_puts_closedesk_in_applications(mac):
    said = shortcut.upkeep(mac.system, record=mac.record)
    assert said.ok and "Applications" in said.message
    info = _plist(mac.app)
    # The name the Dock, Spotlight and the menu bar show; one id, kept for good (macOS keys what it remembers to it).
    assert info["CFBundleName"] == info["CFBundleDisplayName"] == "CloseDesk"
    assert info["CFBundleIdentifier"] == shortcut.BUNDLE_ID
    assert info["CFBundleExecutable"] == "CloseDesk" and info["CFBundlePackageType"] == "APPL"
    assert info["CFBundleIconFile"] == "CloseDesk.icns"
    assert info["NSHighResolutionCapable"] is True, "without it the window is blurry on a Retina screen"
    assert info["LSMultipleInstancesProhibited"] is True and info["CloseDeskFolder"] == str(mac.root)
    script = mac.app / "Contents" / "MacOS" / "CloseDesk"
    assert script.stat().st_mode & stat.S_IXUSR
    assert (mac.app / "Contents" / "Resources" / "CloseDesk.icns").read_bytes() == (STATIC / "closedesk.icns").read_bytes()
    # The Python that runs CloseDesk, as the app's own: started through this, the Dock says CloseDesk, not Python.
    link = mac.app / "Contents" / "MacOS" / "CloseDesk-python"
    assert link.is_symlink() and os.readlink(link) == str(mac.python)
    assert mac.registered == [mac.app], "Launch Services is told, so Spotlight and the Dock know it at once"
    assert mac.app.is_dir() and not mac.app.is_symlink(), "Spotlight doesn't index an app that is a link"


def test_macos_26_gets_the_icon_filled_to_its_edges(mac):
    # macOS 26 rounds an app's icon itself, and shrinks one with clear edges onto a grey tile.
    system = shortcut.Mac(mac.root, apps=mac.apps, home=mac.tmp / "home", macos=(26, 1), interpreter=mac.python, register=mac.registered.append)
    shortcut.upkeep(system, record=mac.record)
    icon = (mac.app / "Contents" / "Resources" / "CloseDesk.icns").read_bytes()
    assert icon == (STATIC / "closedesk-full.icns").read_bytes()


def test_nothing_signed_is_copied_into_the_app(mac):
    shortcut.upkeep(mac.system, record=mac.record)
    files = [path for path in mac.app.rglob("*") if path.is_file() and not path.is_symlink()]
    assert sorted(path.name for path in files) == ["CloseDesk", "CloseDesk.icns", "Info.plist", "PkgInfo"]
    assert (mac.app / "Contents" / "MacOS" / "CloseDesk").read_bytes().startswith(b"#!/bin/bash\n")


def test_the_apps_script_opens_closedesk_in_its_window_from_its_folder(mac, tmp_path):
    # Run for real (bash is here too), with a stand-in for the environment's Python that says what it was asked.
    shortcut.upkeep(mac.system, record=mac.record)
    told = tmp_path / "told.txt"
    python = mac.root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/bash\nprintf "%s\\n" "$PWD" "$PATH" "$@" > {told}\n', encoding="utf-8")
    python.chmod(0o755)
    done = subprocess.run(["/bin/bash", str(mac.app / "Contents" / "MacOS" / "CloseDesk")], env={"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"},
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done
    folder, path, *args = told.read_text(encoding="utf-8").splitlines()
    assert args == [str(mac.root / "scripts" / "launch.py"), "--app"] and folder == str(mac.root)
    # Opened from the Dock, an app's PATH is only the system's: Homebrew's Tesseract (the OCR) is added.
    assert "/opt/homebrew/bin" in path.split(":")
    log = tmp_path / "home" / "Library" / "Logs" / "CloseDesk" / "CloseDesk-app.log"
    assert log.exists(), "what it says goes to the app's log, as launch.py --app's does"


def test_the_apps_script_says_when_the_folder_is_gone(mac, tmp_path):
    shortcut.upkeep(mac.system, record=mac.record)
    script = (mac.app / "Contents" / "MacOS" / "CloseDesk").read_text(encoding="utf-8")
    assert "can't find its folder" in script and "CloseDesk.command" in script
    os.rename(mac.root, tmp_path / "gone")
    done = subprocess.run(["/bin/bash", str(mac.app / "Contents" / "MacOS" / "CloseDesk")], env={"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"},
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 1


def test_without_its_environment_the_app_sets_closedesk_up_in_terminal(mac):
    shortcut.upkeep(mac.system, record=mac.record)
    script = (mac.app / "Contents" / "MacOS" / "CloseDesk").read_text(encoding="utf-8")
    assert '/usr/bin/open -a Terminal "$ROOT/CloseDesk.command"' in script


def test_a_folder_name_with_quotes_and_spaces_is_kept_whole_in_the_script(mac):
    shortcut.upkeep(mac.system, record=mac.record)
    script = (mac.app / "Contents" / "MacOS" / "CloseDesk").read_text(encoding="utf-8")
    line = next(line for line in script.splitlines() if line.startswith("ROOT="))
    done = subprocess.run(["/bin/bash", "-c", f'{line}\nprintf "%s" "$ROOT"'], capture_output=True, text=True, timeout=10)
    assert done.stdout == str(mac.root)


@pytest.mark.parametrize("where", ["Documents", "Desktop", "Downloads", "Library/Mobile Documents/com~apple~CloudDocs"])
def test_in_a_folder_macos_guards_the_app_opens_closedesk_command_in_terminal(where, tmp_path):
    # An app whose main program is a script, opening files in Documents, Desktop, Downloads or iCloud Drive, runs into
    # macOS's privacy checks; Terminal asks for them as it should. CloseDesk opens from there in the browser, as before.
    home = tmp_path / "home"
    root = _closedesk(home / where / "CloseDesk")
    system = shortcut.Mac(root, apps=home / "Applications", home=home, macos=(15, 0), interpreter=None, register=lambda app: None)
    said = shortcut.upkeep(system, record=tmp_path / "shortcut.json")
    script = (home / "Applications" / "CloseDesk.app" / "Contents" / "MacOS" / "CloseDesk").read_text(encoding="utf-8")
    assert script.rstrip().endswith('exec /usr/bin/open -a Terminal "$ROOT/CloseDesk.command"')
    assert "launch.py" not in script.split("ROOT=", 1)[1].split("\n", 1)[1]
    assert "home folder" in said.message, "it says how to get the window: the folder in the home folder"


def test_a_mac_app_that_is_right_isnt_written_again(mac):
    shortcut.upkeep(mac.system, record=mac.record)
    before = {path: path.lstat().st_mtime_ns for path in mac.app.rglob("*")}
    mac.registered.clear()
    said = shortcut.upkeep(mac.system, record=mac.record)
    assert said.message == "" and mac.registered == []
    assert {path: path.lstat().st_mtime_ns for path in mac.app.rglob("*")} == before


def test_a_deleted_mac_app_isnt_put_back_unless_asked(mac):
    import shutil

    shortcut.upkeep(mac.system, record=mac.record)
    shutil.rmtree(mac.app)
    assert shortcut.upkeep(mac.system, record=mac.record).message == "" and not mac.app.exists()
    said = shortcut.make(mac.system, record=mac.record)
    assert said.ok and _plist(mac.app)["CloseDeskFolder"] == str(mac.root)


def test_a_moved_folder_is_found_again_by_the_mac_app(mac, tmp_path):
    shortcut.upkeep(mac.system, record=mac.record)
    moved = tmp_path / "home" / "CloseDesk"
    mac.root.rename(moved)
    system = shortcut.Mac(moved, apps=mac.apps, home=tmp_path / "home", macos=(15, 6), interpreter=mac.python, register=mac.registered.append)
    said = shortcut.upkeep(system, record=mac.record)
    assert said.ok and "moved" in said.message
    assert _plist(mac.app)["CloseDeskFolder"] == str(moved)
    assert str(moved) in (mac.app / "Contents" / "MacOS" / "CloseDesk").read_text(encoding="utf-8")


def test_a_new_python_is_linked_in_its_place(mac, tmp_path):
    # The environment made again with another Python (the old one was removed): the app runs the new one.
    shortcut.upkeep(mac.system, record=mac.record)
    newer = tmp_path / "python3.13"
    newer.write_text("", encoding="utf-8")
    system = shortcut.Mac(mac.root, apps=mac.apps, home=tmp_path / "home", macos=(15, 6), interpreter=newer, register=mac.registered.append)
    shortcut.upkeep(system, record=mac.record)
    assert os.readlink(mac.app / "Contents" / "MacOS" / "CloseDesk-python") == str(newer)


def test_without_rights_to_applications_the_app_is_made_in_closedesks_folder(mac):
    # Some Macs have an ~/Applications owned by the system (here: one that can't hold a folder at all).
    mac.apps.parent.mkdir(parents=True, exist_ok=True)
    mac.apps.write_text("not a folder", encoding="utf-8")
    said = shortcut.upkeep(mac.system, record=mac.record)
    in_folder = mac.root / "CloseDesk.app"
    assert said.ok and _plist(in_folder)["CloseDeskFolder"] == str(mac.root)
    assert "CloseDesk's folder" in said.message and "Dock" in said.message
    assert mac.record.exists()
    # Kept there from then on: the folder moved, the app in it opens it where it went.
    mac.registered.clear()
    assert shortcut.upkeep(mac.system, record=mac.record).message == "" and mac.registered == []


def test_the_interpreter_linked_is_the_one_that_really_runs(tmp_path):
    # python.org's and Homebrew's Python (a framework): bin/python3.12 only starts Python.app's program, by its own path
    # (the Dock would say Python), so the link is to that program. uv's Python: the real file behind the links.
    prefix = tmp_path / "Python.framework" / "Versions" / "3.12"
    program = prefix / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
    program.parent.mkdir(parents=True)
    program.write_text("", encoding="utf-8")
    framework = SimpleNamespace(base_prefix=str(prefix), _base_executable=str(prefix / "bin" / "python3.12"), executable="x")
    assert shortcut.mac_interpreter(framework, {"PYTHONFRAMEWORK": "Python"}.get) == program
    real = tmp_path / "uv" / "cpython-3.12" / "bin" / "python3.12"
    real.parent.mkdir(parents=True)
    real.write_text("", encoding="utf-8")
    (real.parent / "python3").symlink_to(real)
    standalone = SimpleNamespace(base_prefix=str(real.parent.parent), _base_executable=str(real.parent / "python3"), executable="x")
    assert shortcut.mac_interpreter(standalone, {"PYTHONFRAMEWORK": ""}.get) == real
    gone = SimpleNamespace(base_prefix=str(tmp_path / "nowhere"), _base_executable=str(tmp_path / "nowhere" / "python"), executable="x")
    assert shortcut.mac_interpreter(gone, {"PYTHONFRAMEWORK": "Python"}.get) is None


def test_launch_py_finds_the_same_interpreter_as_the_app_links_to(tmp_path):
    # launch.py (only the standard library, before CloseDesk is importable) checks the app's link with its own copy.
    spec = importlib.util.spec_from_file_location("launch_mac_python", ROOT / "scripts" / "launch.py")
    launch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launch)
    prefix = tmp_path / "Versions" / "3.12"
    program = prefix / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
    program.parent.mkdir(parents=True)
    program.write_text("", encoding="utf-8")
    for fake, config in (
        (SimpleNamespace(base_prefix=str(prefix), _base_executable="", executable="x"), {"PYTHONFRAMEWORK": "Python"}),
        (SimpleNamespace(base_prefix=str(prefix), _base_executable=str(program), executable="x"), {"PYTHONFRAMEWORK": ""}),
    ):
        assert launch.mac_interpreter(fake, config.get) == shortcut.mac_interpreter(fake, config.get) == program
    assert launch.DIR_ENV == shortcut.DIR_ENV and launch.LINKED_PYTHON == shortcut.LINKED_PYTHON


# Where it is offered, and from where it is kept --------------------------------------------------------------------


def test_linux_has_no_shortcut(tmp_path):
    assert shortcut.system_here(platform="linux") is None
    assert shortcut.upkeep(None, record=tmp_path / "x.json").message == ""
    said = shortcut.make(None, record=tmp_path / "x.json")
    assert said.ok is False and "CloseDesk.command" in said.message
    assert shortcut.offer(platform="linux")["offered"] is False


def test_settings_offers_it_on_windows_and_macos():
    windows, mac = shortcut.offer(platform="win32"), shortcut.offer(platform="darwin")
    assert windows["offered"] and windows["label"] == "Add the CloseDesk shortcut again"
    assert "Desktop" in windows["help"] and "Start menu" in windows["help"]
    assert mac["offered"] and "Applications" in mac["help"]


def test_upkeep_in_the_background_says_what_it_did(monkeypatch, capsys):
    monkeypatch.setattr(shortcut, "upkeep", lambda *a, **k: shortcut.Said(True, "Added the CloseDesk shortcut."))
    thread = shortcut.upkeep_soon()
    thread.join(5)
    assert "Added the CloseDesk shortcut." in capsys.readouterr().out


def test_upkeep_in_the_background_never_stops_closedesk(monkeypatch, capsys):
    def broken(*a, **k):
        raise RuntimeError("pywin32 is broken")

    monkeypatch.setattr(shortcut, "upkeep", broken)
    thread = shortcut.upkeep_soon()
    thread.join(5)
    assert not thread.is_alive()


# The real thing, on Windows (CI) -------------------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' own shortcuts")
def test_a_real_shortcut_is_written_and_read_back_on_windows(tmp_path):
    root = _closedesk(tmp_path / "Close Desk")
    places = {"Desktop": tmp_path / "Desktop", "Start menu": tmp_path / "Programs"}
    system = shortcut.Windows(root, folders=lambda: dict(places), icon_dir=tmp_path / "icons")
    said = shortcut.upkeep(system, record=tmp_path / "shortcut.json")
    assert said.ok, said.message
    link = shortcut.read_link(places["Desktop"] / "CloseDesk.lnk")
    assert link["app_id"] == "CloseDesk.CloseDesk"
    assert Path(link["target"]) == root / ".venv" / "Scripts" / "pythonw.exe"
    assert link["arguments"] == f'"{root / "scripts" / "launch.py"}" --app' and Path(link["folder"]) == root
    assert Path(link["icon"]).parent == tmp_path / "icons" and link["flags"] & 0x40100 == 0x40100
    modified = (places["Desktop"] / "CloseDesk.lnk").stat().st_mtime_ns
    assert shortcut.upkeep(system, record=tmp_path / "shortcut.json").message == ""
    assert (places["Desktop"] / "CloseDesk.lnk").stat().st_mtime_ns == modified, "a shortcut that is right isn't written again"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' taskbar id")
def test_the_window_takes_the_shortcuts_taskbar_id_on_windows():
    import ctypes

    app_window.set_app_id()
    found = ctypes.c_wchar_p()
    shell32 = ctypes.windll.shell32
    shell32.GetCurrentProcessExplicitAppUserModelID.argtypes = [ctypes.POINTER(ctypes.c_wchar_p)]
    assert shell32.GetCurrentProcessExplicitAppUserModelID(ctypes.byref(found)) == 0
    try:
        assert found.value == "CloseDesk.CloseDesk"
    finally:
        ctypes.windll.ole32.CoTaskMemFree(found)
