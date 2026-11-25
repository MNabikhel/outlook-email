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


# What the check closes when the double-clicked window didn't close by itself -------------------------------------------

MINE_AND_A_REAL_ONE = LISTING + """\
 5) "CloseDesk" ASN:0x0-0x3e03e:
    bundleID="io.github.mnabikhel.closedesk"
    bundle path="/Users/pat/Applications/CloseDesk.app"
    pid = 6100 type="Foreground" flavor=3 Version="2026.11.20" fileType="APPL" creator="????" Arch=ARM64
"""


def test_the_check_closes_only_the_closedesk_it_opened_on_a_mac(monkeypatch, tmp_path):
    # A developer's own CloseDesk (same app id) open while the check runs was ended outright, mid Process new mail.
    module = smoke()
    monkeypatch.setattr(module, "MAC", True)
    monkeypatch.setattr(module, "WINDOWS", False)
    shortcuts = Path("/var/folders/x/T/closedesk window abc/shortcuts")
    monkeypatch.setattr(module, "mac_apps", lambda: (module.parse_apps(MINE_AND_A_REAL_ONE), MINE_AND_A_REAL_ONE))
    ended: list = []
    monkeypatch.setattr(module.os, "kill", lambda pid, sig: ended.append(pid))
    module.end_windows(tmp_path / "Close Desk", shortcuts)
    assert ended == [5123], "the check's own, not /Users/pat/Applications/CloseDesk.app"


def test_the_check_closes_only_what_runs_from_its_own_folder_on_windows(monkeypatch, tmp_path):
    # It ended every pythonw.exe on the computer: the developer's own CloseDesk window, any other Python program.
    module = smoke()
    monkeypatch.setattr(module, "WINDOWS", True)
    monkeypatch.setattr(module, "MAC", False)
    ran: list = []

    def run(cmd, **options):
        ran.append((cmd, options))
        if cmd[0] == "powershell":
            return module.subprocess.CompletedProcess(cmd, 0, stdout="4242\r\n4343\r\n", stderr="")
        return module.subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", run)
    folder = tmp_path / "Close Desk"
    module.end_windows(folder, tmp_path / "shortcuts")
    (listing, options), *killed = ran
    assert options["env"]["CLOSEDESK_CHECK_FOLDER"] == str(folder), "the folder is passed in, not written into the script"
    assert str(folder) not in " ".join(listing)
    assert [cmd for cmd, _ in killed] == [["taskkill", "/F", "/T", "/PID", "4242"], ["taskkill", "/F", "/T", "/PID", "4343"]]
    assert not any("/IM" in cmd for cmd, _ in killed)


def test_the_check_wont_run_beside_a_closedesk_open_on_the_mac(monkeypatch):
    module = smoke()
    monkeypatch.setattr(module, "MAC", True)
    monkeypatch.setattr(module, "mac_apps", lambda: (module.parse_apps(MINE_AND_A_REAL_ONE), MINE_AND_A_REAL_ONE))
    monkeypatch.setattr(module, "export", lambda *a, **k: (_ for _ in ()).throw(AssertionError("nothing is set up")))
    assert module.window_check() == 1


def test_the_check_has_the_window_show_both_uis():
    module = smoke()
    log = "x\nThe CloseDesk window showed /\nThe CloseDesk window showed /app\n"
    assert module.pages_shown(log) == ["/", "/app"]
    module.both_uis(log, "the window")
    for only in ("The CloseDesk window showed /\n", "The CloseDesk window showed /app\n"):
        try:
            module.both_uis(only, "the window")
        except AssertionError:
            continue
        raise AssertionError(f"one page passed: {only!r}")


def test_a_window_onto_a_closedesk_already_open_has_to_show_that_one():
    # The window started a second CloseDesk on the next port (the open one too slow to answer its /health in time) and
    # showed it: exit 0, both pages, the first CloseDesk still open. The check passed, though the window never showed
    # the CloseDesk that was open. What the page itself says it was loaded from tells them apart.
    module = smoke()
    log = "x\nThe CloseDesk window showed / (port 8765)\nThe CloseDesk window showed /app (port 8765)\n"
    assert module.pages_shown(log) == ["/", "/app"] and module.ports_shown(log) == {"8765"}
    module.showed_only(log, 8765, "the window")
    second = "Another program is using port 8765, so CloseDesk uses 8766 this time.\n" + log.replace("8765", "8766")
    for wrong in (log.replace("/app (port 8765)", "/app (port 8766)"), second, "Opening http://127.0.0.1:8766/\n" + log):
        try:
            module.showed_only(wrong, 8765, "the window")
        except AssertionError:
            continue
        raise AssertionError(f"passed: {wrong!r}")
