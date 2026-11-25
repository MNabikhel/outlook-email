"""What the CloseDesk window's check in CI (``tests/launcher_smoke.py --window``) reads on a Mac: macOS's list of the apps
it has running (``lsappinfo list``), from which it tells whether the Dock and the menu bar call the window CloseDesk or
Python. The check itself opens the real window, so it runs in CI on Windows and macOS only."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

LISTING = """\
 1) "loginwindow" ASN:0x0-0x1001:
    bundleID="com.apple.loginwindow"
    bundle path="/System/Library/CoreServices/loginwindow.app"
    executable path="/System/Library/CoreServices/loginwindow.app/Contents/MacOS/loginwindow"
    pid = 401 type="Foreground" flavor=3 Version="2.0" fileType="APPL" creator="lgnw" Arch=ARM64
    launch time =  2026/11/25 01:02:03 ( 2 hours ago )

 2) "CloseDesk" ASN:0x0-0x2b02b:
    bundleID="io.github.mnabikhel.closedesk"
    bundle path="/var/folders/x/T/closedesk window abc/shortcuts/CloseDesk.app"
    executable path="/var/folders/x/T/closedesk window abc/shortcuts/CloseDesk.app/Contents/MacOS/CloseDesk-python"
    pid = 5123 type="Foreground" flavor=3 Version="1.0" fileType="APPL" creator="????" Arch=ARM64
    parentASN="Terminal" ASN:0x0-0x1d01d:

 3) "Python" ASN:0x0-0x2c02c:
    bundleID="org.python.python"
    bundle path="/Library/Frameworks/Python.framework/Versions/3.12/Resources/Python.app"
    pid = 5200 type="Foreground" flavor=3 Version="3.12.10" fileType="APPL" creator="????" Arch=ARM64
 4) "Helper" ASN:0x0-0x2d02d:
    bundleID=[ NULL ]
    pid = 77 type="BackgroundOnly"
"""


def smoke():
    spec = importlib.util.spec_from_file_location("launcher_smoke_under_test", ROOT / "tests" / "launcher_smoke.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_check_reads_each_app_macos_has_running():
    apps = smoke().parse_apps(LISTING)
    assert [app["name"] for app in apps] == ["loginwindow", "CloseDesk", "Python", "Helper"]
    closedesk = apps[1]
    assert closedesk["bundle"] == "io.github.mnabikhel.closedesk" and closedesk["pid"] == 5123
    assert closedesk["path"].endswith("shortcuts/CloseDesk.app")
    assert apps[3]["bundle"] == "" and apps[3]["pid"] == 77, "an app without an id"


def test_the_check_finds_the_window_by_its_program_or_by_closedesks_id(monkeypatch):
    module = smoke()
    monkeypatch.setattr(module, "mac_apps", lambda: (module.parse_apps(LISTING), LISTING))
    by_pid = module.Sightings()
    by_pid.look(pid=5123)
    assert [app["name"] for app in by_pid.seen] == ["CloseDesk"]
    by_id = module.Sightings()
    by_id.look()
    assert [app["pid"] for app in by_id.seen] == [5123]


def test_the_dock_has_to_say_closedesk_and_show_no_python(monkeypatch):
    module = smoke()
    listing = LISTING.split(" 3)")[0]  # CloseDesk only
    monkeypatch.setattr(module, "mac_apps", lambda: (module.parse_apps(listing), listing))
    seen = module.Sightings()
    seen.look(pid=5123)
    assert seen.problems("the window") == []

    monkeypatch.setattr(module, "mac_apps", lambda: (module.parse_apps(LISTING), LISTING))
    seen.look(pid=5123)
    assert seen.problems("the window") and "a Python too" in seen.problems("the window")[0]

    as_python = module.Sightings()
    as_python.look(pid=5200)  # the window, taken by macOS for Python's
    said = as_python.problems("the window")
    assert any("'Python' (org.python.python), not CloseDesk" in line for line in said)


def test_a_window_macos_never_listed_fails_the_check(monkeypatch):
    module = smoke()
    monkeypatch.setattr(module, "mac_apps", lambda: ([], "nothing"))
    seen = module.Sightings()
    seen.look(pid=1)
    assert "never listed the window" in seen.problems("the window")[0]
