"""``launch.py --app``, what the CloseDesk shortcut runs with pythonw (no console window): the same set-up as every
start, silently (what it says goes to a log; nothing it runs opens a console window), what goes wrong said in a message
box, and then the CloseDesk window (app_window.py, tested in test_app_window.py)."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from controller_inbox import app_window, updates

ROOT = Path(__file__).resolve().parent.parent
NO_WINDOW = 0x08000000  # subprocess.CREATE_NO_WINDOW on Windows


# launch.py --app: what the CloseDesk shortcut runs -------------------------------------------------------------------


@pytest.fixture
def launch(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("launch_app_under_test", ROOT / "scripts" / "launch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "CloseDesk"
    (root / ".venv").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "controller-inbox"\ndependencies = ["fastapi>=0.1"]\n'
        '[project.optional-dependencies]\nocr = []\ntables = []\nwindow = []\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module, "VENV", root / ".venv")
    monkeypatch.setattr(module, "STATE", root / ".venv" / "closedesk-setup.json")
    monkeypatch.setattr(module, "LOG", root / ".venv" / "closedesk-setup.log")
    monkeypatch.setattr(module, "runs_this_folder", lambda: True)
    monkeypatch.setattr(module, "app_log_path", lambda: tmp_path / "logs" / "CloseDesk-app.log")
    monkeypatch.setattr(module, "NO_WINDOW", NO_WINDOW)
    monkeypatch.setattr(module, "APP_LOGS", True)  # as on Windows and macOS, where the app has no console
    for name in ("CLOSEDESK_APP", "CLOSEDESK_APP_LOG", "CLOSEDESK_LAUNCHER", "CLOSEDESK_REBUILT", "CLOSEDESK_UPDATE_URL",
                 "CFProcessPath", "CLOSEDESK_APP_SMOKE", "CLOSEDESK_AFTER_UPDATE"):
        # Set, then removed: what launch.py itself sets in os.environ (--app sets the first three) is undone after the
        # test too, which removing a name that isn't set wouldn't do.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    module.told, module.noticed, module.opened = [], [], []
    monkeypatch.setattr(module, "tell", module.told.append)
    monkeypatch.setattr(module, "notice", module.noticed.append)
    monkeypatch.setattr(module, "open_launcher", lambda: module.opened.append(1))
    # Whatever sys.stdout the app makes for its log is closed and put back after the test.
    before = {id(sys.stdout), id(sys.stderr)}
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    yield module
    for stream in {sys.stdout, sys.stderr}:
        if stream is not None and id(stream) not in before:
            stream.close()


def test_the_launchers_names_for_the_app_are_closedesks(launch):
    assert launch.APP_ENV == updates.APP_ENV and launch.APP_LOG_ENV == updates.APP_LOG_ENV
    assert launch.UPDATE == updates.UPDATE_AND_RESTART and launch.REBUILD == app_window.REBUILD


def test_the_app_sets_up_then_opens_the_window(launch, monkeypatch):
    monkeypatch.setattr(launch, "in_venv", lambda: True)
    monkeypatch.setattr(launch, "ensure_packages", lambda: 0)
    handed: list = []
    monkeypatch.setattr(launch, "_hand_over", lambda cmd: handed.append((cmd, dict(os.environ))) or 0)
    assert launch.main(["--app", "--port", "8812"]) == 0
    (cmd, env), = handed
    assert cmd[1:] == ["-m", "controller_inbox", "app", "--port", "8812"]
    assert env["CLOSEDESK_APP"] == "1" and env["CLOSEDESK_LAUNCHER"] == "1"
    assert env["CLOSEDESK_APP_LOG"] == str(launch.app_log_path())
    sys.stdout.flush()
    assert "CloseDesk opening" in launch.app_log_path().read_text(encoding="utf-8"), "what the app says goes to its log"


def test_on_windows_the_window_is_opened_with_pythonw(launch, monkeypatch):
    monkeypatch.setattr(launch, "WINDOWS", True)
    scripts = launch.VENV / "Scripts"
    scripts.mkdir()
    (scripts / "pythonw.exe").write_text("")
    assert launch.app_python() == scripts / "pythonw.exe"
    (scripts / "pythonw.exe").unlink()
    assert launch.app_python() == launch.venv_python(), "no pythonw: python, with no console window"


def test_the_app_without_a_console_writes_to_its_log(launch, monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    launch.use_app_log()
    launch.say("installing")
    sys.stdout.flush()
    assert "installing" in launch.app_log_path().read_text(encoding="utf-8")
    launch._flush()  # a hand-over flushes what it said; with no console it must not fail


def test_a_long_log_is_started_afresh(launch):
    log = launch.app_log_path()
    log.parent.mkdir(parents=True)
    log.write_text("x" * (launch.APP_LOG_LIMIT + 1), encoding="utf-8")
    launch.use_app_log()
    launch.say("new start")
    sys.stdout.flush()
    assert log.read_text(encoding="utf-8").strip().endswith("new start") and log.stat().st_size < 1000
    assert log.with_name(log.name + ".old").exists()


def test_every_program_the_app_runs_has_no_console_window(launch, monkeypatch, tmp_path):
    # Every console program started from pythonw (uv, pip, curl, python) opens a console window of its own unless told
    # not to: in the app, none may.
    monkeypatch.setenv("CLOSEDESK_APP", "1")
    calls: list = []

    def record(kind):
        def run(cmd, *args, **options):
            calls.append((kind, cmd[0] if isinstance(cmd, list) else cmd, options))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        return run

    monkeypatch.setattr(launch.subprocess, "run", record("run"))
    monkeypatch.setattr(launch.subprocess, "call", lambda cmd, **options: calls.append(("call", cmd[0], options)) or 0)
    monkeypatch.setattr(launch.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(launch, "find_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr(launch, "WINDOWS", True)
    launch._run(["/usr/bin/uv", "pip", "install", "x"])
    launch._fetch("https://example.com/closedesk.zip", tmp_path / "x.zip")
    launch.create_environment(["--app"])
    launch._hand_over(["python", "-m", "controller_inbox", "app"])
    kinds = [kind for kind, _, _ in calls]
    assert kinds.count("run") >= 3 and "call" in kinds, calls
    for kind, program, options in calls:
        assert options.get("creationflags") == NO_WINDOW, (kind, program)
        assert options.get("stdin") == subprocess.DEVNULL, (kind, program)


def test_outside_the_app_the_launchers_console_is_used_as_before(launch, monkeypatch):
    calls: list = []
    monkeypatch.setattr(launch.subprocess, "call", lambda cmd, **options: calls.append(options) or 0)
    monkeypatch.setattr(launch, "WINDOWS", True)
    launch._hand_over(["python", "-m", "controller_inbox", "run"])
    assert "creationflags" not in calls[0] and "stdin" not in calls[0], "CloseDesk's window shows what it does"


def test_an_app_that_cant_install_what_it_needs_says_so_in_a_message_box(launch, monkeypatch):
    monkeypatch.setattr(launch, "in_venv", lambda: True)
    monkeypatch.setattr(launch, "ensure_packages", lambda: 1)
    monkeypatch.setattr(launch, "_hand_over", lambda cmd: pytest.fail("not started"))
    assert launch.main(["--app"]) == 1
    assert len(launch.told) == 1 and "couldn't install what it needs" in launch.told[0] and str(launch.LOG) in launch.told[0]


def test_an_app_whose_environment_must_be_made_again_opens_the_double_click_launcher(launch, monkeypatch):
    # A Python running from .venv can't remove it: the launcher does, in its window, and starts CloseDesk.
    monkeypatch.setattr(launch, "in_venv", lambda: True)
    monkeypatch.setattr(launch, "ensure_packages", lambda: launch.REBUILD)
    monkeypatch.setattr(launch, "_hand_over", lambda cmd: pytest.fail("not started"))
    assert launch.main(["--app"]) == 0
    assert launch.opened == [1] and launch.told == []


def test_installing_in_the_app_says_so_without_a_console(launch, monkeypatch):
    monkeypatch.setenv("CLOSEDESK_APP", "1")
    monkeypatch.setattr(launch, "runs_this_folder", lambda: False)
    monkeypatch.setattr(launch, "install", lambda *what: True)
    launch.write_state({"extras": {}, "python": "3.12.0"})
    assert launch.ensure_packages() == 0
    assert launch.noticed == [launch.INSTALLING], "nothing on screen for a minute would look like nothing happened"


def test_the_window_is_tried_again_at_each_app_start_until_it_installs(launch, monkeypatch):
    # The window's add-on (pywebview) that didn't install (offline, say) would mean the browser for good if it were
    # tried only once, as the other add-ons are.
    (launch.ROOT / "pyproject.toml").write_text(
        '[project]\nname = "controller-inbox"\ndependencies = []\n'
        '[project.optional-dependencies]\nwindow = ["surely-not-installed-window>=1"]\n', encoding="utf-8")
    monkeypatch.setattr(launch.check_deps, "LAUNCHER_EXTRAS", ("window",))
    installs: list = []
    monkeypatch.setattr(launch, "install", lambda *what: installs.append(what) or False)
    launch.ensure_packages()
    launch.ensure_packages()
    assert installs == [("surely-not-installed-window>=1",)], "the ordinary start tries it once"
    monkeypatch.setenv("CLOSEDESK_APP", "1")
    launch.ensure_packages()
    assert len(installs) == 2


def test_the_window_add_on_is_for_windows_and_macos_only():
    import tomllib

    from packaging.requirements import Requirement

    spec = importlib.util.spec_from_file_location("check_deps_under_test", ROOT / "scripts" / "check_deps.py")
    check_deps = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check_deps)
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    window = project["project"]["optional-dependencies"]["window"]
    assert window[0].startswith("pywebview>=6.2.1,<7"), "6.2 gives the window its icon; 7 changes what CloseDesk uses"
    assert "window" in check_deps.LAUNCHER_EXTRAS
    for platform, wanted in (("win32", True), ("darwin", True), ("linux", False)):
        assert Requirement(window[0]).marker.evaluate({"sys_platform": platform}) is wanted, platform
        assert check_deps.applies(window[0], platform=platform) is wanted, f"{platform}, read without packaging"
    assert not any("pywebview" in req for req in project["project"]["dependencies"]), "a window that can't install must not stop CloseDesk"


def test_the_launchers_message_box_and_notice(monkeypatch):
    # Windows: a message box; macOS: an alert (the notice: a notification that doesn't wait); elsewhere: the log only.
    spec = importlib.util.spec_from_file_location("launch_tell", ROOT / "scripts" / "launch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ran: list = []
    monkeypatch.setattr(module.subprocess, "run", lambda cmd, **options: ran.append(cmd))
    monkeypatch.setattr(module.subprocess, "Popen", lambda cmd, **options: ran.append(cmd))
    module.tell('CloseDesk couldn\'t install "it"', platform="darwin")
    module.notice("Installing", platform="darwin")
    assert ran[0][0] == "/usr/bin/osascript" and 'CloseDesk couldn\'t install "it"' in ran[0], "passed as it is, never quoted into a script"
    assert ran[1][0] == "/usr/bin/osascript" and "Installing" in ran[1]
    buffer = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buffer)
    module.tell("only said", platform="linux")
    assert "only said" in buffer.getvalue() and len(ran) == 2


def test_installs_from_the_app_name_the_console_python_not_pythonw(launch, monkeypatch, tmp_path):
    # The shortcut runs pythonw, which uv and pip are asked to install into: they run the Python they are given and
    # read what it prints, so they are given the python.exe beside it (started with no console window all the same).
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "pythonw.exe").write_text("")
    (scripts / "python.exe").write_text("")
    assert launch._console_python(str(scripts / "pythonw.exe")) == str(scripts / "python.exe")
    assert launch._console_python(str(scripts / "python.exe")) == str(scripts / "python.exe")
    (scripts / "python.exe").unlink()
    assert launch._console_python(str(scripts / "pythonw.exe")) == str(scripts / "pythonw.exe"), "none beside it: as it is"
    ran: list = []
    monkeypatch.setattr(launch, "_console_python", lambda executable=None: "the-console-python")
    monkeypatch.setattr(launch, "find_uv", lambda: "uv")
    monkeypatch.setattr(launch, "_run", lambda cmd, **_: ran.append(cmd) or False)
    monkeypatch.setattr(launch.importlib.util, "find_spec", lambda name: object())
    launch.install("x")
    assert ran == [["uv", "pip", "install", "--python", "the-console-python", "x"],
                   ["the-console-python", "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "x"]]


# The CloseDesk shortcut ---------------------------------------------------------------------------------------------


def test_launch_py_shortcut_sets_up_then_makes_the_shortcut_again(launch, monkeypatch):
    monkeypatch.setattr(launch, "in_venv", lambda: True)
    monkeypatch.setattr(launch, "ensure_packages", lambda: 0)
    handed: list = []
    monkeypatch.setattr(launch, "_hand_over", lambda cmd: handed.append(cmd) or 0)
    assert launch.main(["--shortcut"]) == 0
    assert handed == [[sys.executable, "-m", "controller_inbox", "shortcut"]]


def test_on_a_mac_the_window_runs_python_as_closedesk_apps_own(launch, monkeypatch, tmp_path):
    # Started through the link in CloseDesk.app, Python is that app to macOS: the Dock and the menu bar say CloseDesk.
    # The environment is named, as python3.x does for the program it starts.
    monkeypatch.setattr(launch, "in_venv", lambda: True)
    monkeypatch.setattr(launch, "ensure_packages", lambda: 0)
    linked = tmp_path / "CloseDesk.app" / "Contents" / "MacOS" / "CloseDesk-python"
    monkeypatch.setattr(launch, "linked_python", lambda: linked)
    monkeypatch.setattr(launch, "_runs_as_app", lambda link: True)
    monkeypatch.setenv("__PYVENV_LAUNCHER__", "")
    handed: list = []
    monkeypatch.setattr(launch, "_hand_over", lambda cmd: handed.append((cmd, dict(os.environ))) or 0)
    assert launch.main(["--app"]) == 0
    (cmd, env), = handed
    assert cmd == [str(linked), "-m", "controller_inbox", "app"]
    assert env["__PYVENV_LAUNCHER__"] == str(launch.venv_python())
    # Where macOS looks for the app a program belongs to, said outright too (it reads CFProcessPath before the path
    # the program was started by), so the Dock says CloseDesk however macOS resolves the link.
    assert env["CFProcessPath"] == str(linked)


def test_without_closedesk_app_the_window_runs_the_environments_python(launch, monkeypatch):
    monkeypatch.setattr(launch, "in_venv", lambda: True)
    monkeypatch.setattr(launch, "ensure_packages", lambda: 0)
    monkeypatch.setattr(launch, "linked_python", lambda: None)
    monkeypatch.setenv("__PYVENV_LAUNCHER__", "")
    monkeypatch.delenv("__PYVENV_LAUNCHER__")
    handed: list = []
    monkeypatch.setattr(launch, "_hand_over", lambda cmd: handed.append((cmd, dict(os.environ))) or 0)
    launch.main(["--app"])
    (cmd, env), = handed
    assert cmd[0] == str(launch.app_python()) and "__PYVENV_LAUNCHER__" not in env and "CFProcessPath" not in env


@pytest.mark.skipif(os.name == "nt", reason="CloseDesk.app's link to Python is a Mac's")
def test_closedesk_apps_python_is_used_only_when_it_is_this_environments(launch, monkeypatch, tmp_path):
    python = tmp_path / "Python.app" / "Contents" / "MacOS" / "Python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    monkeypatch.setenv("CLOSEDESK_SHORTCUT_DIR", str(tmp_path / "shortcuts"))
    monkeypatch.setattr(launch, "mac_interpreter", lambda *a: python)
    link = tmp_path / "shortcuts" / "CloseDesk.app" / "Contents" / "MacOS" / "CloseDesk-python"
    assert launch.linked_python(platform="darwin") is None, "no CloseDesk.app yet"
    link.parent.mkdir(parents=True)
    other = tmp_path / "python3.13"
    other.write_text("", encoding="utf-8")
    link.symlink_to(other)
    assert launch.linked_python(platform="darwin") is None, "another Python than the environment's can't run it"
    link.unlink()
    link.symlink_to(python)
    assert launch.linked_python(platform="darwin") == link
    assert launch.linked_python(platform="win32") is None and launch.linked_python(platform="linux") is None


def _app_bundle(folder: Path, python: Path, *, bundle_id: str = app_window.BUNDLE_ID) -> Path:
    import plistlib

    app = folder / "CloseDesk.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": bundle_id}))
    (app / "Contents" / "MacOS" / "CloseDesk-python").symlink_to(python)
    return app


@pytest.mark.skipif(os.name == "nt", reason="CloseDesk.app's link to Python is a Mac's")
def test_closedesk_app_opened_from_outside_applications_runs_python_as_itself(launch, monkeypatch, tmp_path):
    # Dragged to /Applications (and kept in the Dock from there): launch.py looked only in ~/Applications and CloseDesk's
    # folder, so the window ran as Python, the Dock showing a Python tile.
    python = tmp_path / "Python.app" / "Contents" / "MacOS" / "Python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    monkeypatch.setenv("CLOSEDESK_SHORTCUT_DIR", str(tmp_path / "shortcuts"))
    monkeypatch.setattr(launch, "mac_interpreter", lambda *a: python)
    app = _app_bundle(tmp_path / "Applications", python)
    assert launch.linked_python(platform="darwin") is None
    monkeypatch.setenv(launch.APP_BUNDLE_ENV, str(app))
    assert launch.linked_python(platform="darwin") == app / "Contents" / "MacOS" / "CloseDesk-python"
    other = _app_bundle(tmp_path / "Elsewhere", python, bundle_id="com.example.other")
    monkeypatch.setenv(launch.APP_BUNDLE_ENV, str(other))
    assert launch.linked_python(platform="darwin") is None, "only CloseDesk's own app"


@pytest.mark.skipif(os.name == "nt", reason="CloseDesk.app's link to Python is a Mac's")
def test_where_closedesk_app_was_opened_from_is_recorded_and_not_passed_on(launch, monkeypatch, tmp_path):
    python = tmp_path / "Python.app" / "Contents" / "MacOS" / "Python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    shortcuts = tmp_path / "shortcuts"
    shortcuts.mkdir()
    (shortcuts / "shortcut.json").write_text(json.dumps({"made": "2026-11-20 09:00:00", "folder": str(launch.ROOT)}), encoding="utf-8")
    monkeypatch.setenv("CLOSEDESK_SHORTCUT_DIR", str(shortcuts))
    monkeypatch.setattr(launch.sys, "platform", "darwin")
    monkeypatch.setattr(launch, "mac_interpreter", lambda *a: python)
    monkeypatch.setattr(launch, "in_venv", lambda: True)
    monkeypatch.setattr(launch, "ensure_packages", lambda: 0)
    monkeypatch.setattr(launch, "_runs_as_app", lambda link: True)
    app = _app_bundle(tmp_path / "Applications", python)
    monkeypatch.setenv(launch.APP_BUNDLE_ENV, str(app))
    monkeypatch.setenv("__PYVENV_LAUNCHER__", "")
    handed: list = []
    monkeypatch.setattr(launch, "_hand_over", lambda cmd: handed.append((cmd, dict(os.environ))) or 0)
    assert launch.main(["--app"]) == 0
    (cmd, env), = handed
    assert cmd[0] == str(app / "Contents" / "MacOS" / "CloseDesk-python") and launch.APP_BUNDLE_ENV not in env
    record = json.loads((shortcuts / "shortcut.json").read_text(encoding="utf-8"))
    assert record["apps"] == [str(app)] and record["folder"] == str(launch.ROOT), "the rest of the record is kept"
    launch.main(["--app"])
    assert json.loads((shortcuts / "shortcut.json").read_text(encoding="utf-8"))["apps"] == [str(app)], "recorded once"


def test_the_launchers_name_for_closedesk_apps_id_is_closedesks(launch, monkeypatch):
    from controller_inbox import shortcut

    assert launch.BUNDLE_ID == app_window.BUNDLE_ID and launch.APPS_KEY == shortcut.APPS_KEY
    # The record it adds to is the shortcut's own.
    monkeypatch.delenv(shortcut.DIR_ENV)
    assert launch.tools_dir() / "shortcut.json" == shortcut.record_path()


def test_a_python_that_doesnt_run_as_closedesk_app_isnt_used(launch, monkeypatch, tmp_path):
    # Tried first: started through the link, Python must find this environment, or the window wouldn't open at all.
    monkeypatch.setattr(launch, "in_venv", lambda: True)
    monkeypatch.setattr(launch, "ensure_packages", lambda: 0)
    monkeypatch.setattr(launch, "linked_python", lambda: tmp_path / "CloseDesk-python")
    monkeypatch.setattr(launch, "_runs_as_app", lambda link: False)
    monkeypatch.setenv("__PYVENV_LAUNCHER__", "")
    monkeypatch.delenv("__PYVENV_LAUNCHER__")
    handed: list = []
    monkeypatch.setattr(launch, "_hand_over", lambda cmd: handed.append((cmd, dict(os.environ))) or 0)
    launch.main(["--app"])
    (cmd, env), = handed
    assert cmd[0] == str(launch.app_python()) and "__PYVENV_LAUNCHER__" not in env and "CFProcessPath" not in env


def test_a_link_that_cant_be_started_leaves_python_as_it_is(launch, monkeypatch, tmp_path):
    monkeypatch.setattr(launch, "in_venv", lambda: True)
    monkeypatch.setattr(launch, "ensure_packages", lambda: 0)
    linked = tmp_path / "CloseDesk-python"
    monkeypatch.setattr(launch, "linked_python", lambda: linked)
    monkeypatch.setattr(launch, "_runs_as_app", lambda link: True)
    monkeypatch.setenv("__PYVENV_LAUNCHER__", "")
    monkeypatch.delenv("__PYVENV_LAUNCHER__")
    handed: list = []

    def hand_over(cmd):
        if cmd[0] == str(linked):
            raise OSError("Exec format error")
        handed.append((cmd, dict(os.environ)))
        return 0

    monkeypatch.setattr(launch, "_hand_over", hand_over)
    launch.main(["--app"])
    (cmd, env), = handed
    assert cmd[0] == str(launch.app_python()) and "__PYVENV_LAUNCHER__" not in env and "CFProcessPath" not in env


def test_in_the_window_check_nothing_waits_for_a_click(monkeypatch):
    # CI's check of the real window (CLOSEDESK_APP_SMOKE): what would be a message box is in the log only, or the check
    # would wait for a click no one makes.
    spec = importlib.util.spec_from_file_location("launch_smoke_tell", ROOT / "scripts" / "launch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("CLOSEDESK_APP_SMOKE", "60")
    ran: list = []
    monkeypatch.setattr(module.subprocess, "run", lambda cmd, **options: ran.append(cmd))
    monkeypatch.setattr(module.subprocess, "Popen", lambda cmd, **options: ran.append(cmd))
    buffer = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buffer)
    module.tell("CloseDesk couldn't install what it needs", platform="darwin")
    module.notice("Installing", platform="darwin")
    assert ran == [] and "couldn't install" in buffer.getvalue() and "Installing" in buffer.getvalue()


def test_the_try_runs_python_through_the_link_as_the_window_would(launch, monkeypatch, tmp_path):
    ran: list = []
    monkeypatch.setattr(launch.subprocess, "run", lambda cmd, **options: ran.append((cmd, options)) or SimpleNamespace(returncode=0))
    assert launch._runs_as_app(tmp_path / "CloseDesk-python") is True
    (cmd, options), = ran
    assert cmd == [str(tmp_path / "CloseDesk-python"), "-c", "import controller_inbox"]
    assert options["env"]["__PYVENV_LAUNCHER__"] == str(launch.venv_python()) and options["stdin"] == subprocess.DEVNULL
    monkeypatch.setattr(launch.subprocess, "run", lambda cmd, **options: SimpleNamespace(returncode=1))
    assert launch._runs_as_app(tmp_path / "CloseDesk-python") is False

    def cant(cmd, **options):
        raise OSError("Exec format error")

    monkeypatch.setattr(launch.subprocess, "run", cant)
    assert launch._runs_as_app(tmp_path / "CloseDesk-python") is False


# What goes wrong as the window opens is said, once --------------------------------------------------------------------


def test_a_window_that_ends_before_it_opens_says_so_in_a_message_box(launch, monkeypatch):
    # The window's process (pythonw, no console) failing before any window is there (a part of Python a company's
    # policy blocks, say) was only a line in the log: the person double-clicked and nothing happened.
    monkeypatch.setattr(launch, "in_venv", lambda: True)
    monkeypatch.setattr(launch, "ensure_packages", lambda: 0)
    monkeypatch.setattr(launch, "linked_python", lambda: None)
    monkeypatch.setattr(launch, "_hand_over", lambda cmd: 1)
    assert launch.main(["--app"]) == 1
    assert len(launch.told) == 1, launch.told
    assert "couldn't open" in launch.told[0] and str(launch.app_log_path()) in launch.told[0]


def test_what_the_window_said_itself_isnt_said_again(launch, monkeypatch):
    monkeypatch.setattr(launch, "in_venv", lambda: True)
    monkeypatch.setattr(launch, "ensure_packages", lambda: 0)
    monkeypatch.setattr(launch, "linked_python", lambda: None)
    for code in (0, launch.SAID):
        monkeypatch.setattr(launch, "_hand_over", lambda cmd, code=code: code)
        launch.main(["--app"])
    assert launch.told == [], "closed as usual, or a message box of the window's own"
    assert launch.SAID == app_window.SAID
    # CI's check of the window: what failed is in its log, and no one is there to close a message box.
    monkeypatch.setenv("CLOSEDESK_APP_SMOKE", "60")
    monkeypatch.setattr(launch, "_hand_over", lambda cmd: 1)
    assert launch.main(["--app"]) == 1 and launch.told == []


def test_a_log_an_open_closedesk_holds_is_added_to_not_lost(launch, monkeypatch):
    # Windows can't rename a file that is open, and an open CloseDesk window holds its log: a second start while the
    # log was long sent everything it said nowhere.
    log = launch.app_log_path()
    log.parent.mkdir(parents=True)
    log.write_text("x" * (launch.APP_LOG_LIMIT + 1), encoding="utf-8")

    def in_use(self, target):
        raise PermissionError(32, "The process cannot access the file because it is being used by another process")

    monkeypatch.setattr(launch.Path, "replace", in_use)
    launch.use_app_log()
    launch.say("the second start")
    sys.stdout.flush()
    assert log.read_text(encoding="utf-8").endswith("the second start\n")


# An update under way, and one that was stopped ------------------------------------------------------------------------


def _opens(launch, monkeypatch) -> list:
    monkeypatch.setattr(launch, "in_venv", lambda: True)
    monkeypatch.setattr(launch, "ensure_packages", lambda: 0)
    monkeypatch.setattr(launch, "linked_python", lambda: None)
    handed: list = []
    monkeypatch.setattr(launch, "_hand_over", lambda cmd: handed.append(cmd) or 0)
    return handed


def test_a_folder_left_by_an_update_that_was_stopped_doesnt_keep_closedesk_closed(launch, monkeypatch):
    # The update's console closed, the battery ran out: its download folder stays. The shortcut said "being updated"
    # for a quarter of an hour although nothing was.
    handed = _opens(launch, monkeypatch)
    (launch.ROOT / ".update-left-behind").mkdir()
    assert launch.main(["--app"]) == 0
    assert launch.told == [] and len(handed) == 1


def test_an_update_whose_program_is_gone_doesnt_count_and_what_it_left_goes(launch, monkeypatch):
    handed = _opens(launch, monkeypatch)
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    (launch.ROOT / ".update-abc123").mkdir()
    (launch.ROOT / launch.UPDATING_MARK).write_text(
        json.dumps({"pid": gone.pid, "started": time.time() - 5, "folder": ".update-abc123"}), encoding="utf-8")
    assert launch.update_under_way() is False
    assert not (launch.ROOT / launch.UPDATING_MARK).exists() and not (launch.ROOT / ".update-abc123").exists()
    assert launch.main(["--app"]) == 0 and len(handed) == 1


def test_the_app_doesnt_open_while_an_update_runs_from_its_start_to_the_end_of_its_check(launch, monkeypatch):
    # The update's own folder is there only while it downloads; after it come the check and new packages, which an
    # opening CloseDesk would race.
    seen: list = []
    monkeypatch.setattr(launch, "update", lambda: seen.append(("update", launch.update_under_way())) or 0)
    monkeypatch.setattr(launch, "_hand_over", lambda cmd: seen.append(("check", launch.update_under_way())) or 0)
    assert launch.main(["--update"]) == 0
    assert seen == [("update", True), ("check", True)]
    assert launch.update_under_way() is False, "said done once it is"
    handed = _opens(launch, monkeypatch)
    (launch.ROOT / launch.UPDATING_MARK).write_text(json.dumps({"pid": os.getpid(), "started": time.time()}), encoding="utf-8")
    assert launch.main(["--app"]) == 0
    assert launch.told == [launch.UPDATING_NOW] and handed == []


@pytest.mark.parametrize("args", [[], ["--no-browser"], ["--check"], ["--shortcut"], ["--command", "overnight"]])
def test_a_double_click_start_while_an_update_runs_says_so_and_doesnt_start(launch, monkeypatch, capsys, args):
    # The window's update carries on after the window is closed. CloseDesk.bat or CloseDesk.command opened meanwhile
    # started CloseDesk from a folder being replaced (old code in memory serving new pages, or "The update couldn't
    # be put in place"), and its first page said "CloseDesk wasn't updated" while the update was still running.
    handed = _opens(launch, monkeypatch)
    (launch.ROOT / launch.UPDATING_MARK).write_text(json.dumps({"pid": os.getpid(), "started": time.time()}), encoding="utf-8")
    assert launch.main(args) == 1, "the launcher's window stays open on the words (a code of 0 closes it)"
    assert launch.UPDATING_NOW in capsys.readouterr().out and handed == []


def test_a_second_update_while_one_runs_waits_for_it(launch, monkeypatch, capsys):
    # CloseDesk-Update double-clicked while the window's update carries on after its window closed: two updates
    # moving the same files at once.
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (launch.ROOT / launch.UPDATING_MARK).write_text(json.dumps({"pid": other.pid, "started": time.time()}), encoding="utf-8")
        monkeypatch.setattr(launch, "update", lambda: pytest.fail("a second update ran"))
        assert launch.main(["--update"]) == 1
        assert launch.UPDATING_NOW in capsys.readouterr().out
        assert json.loads((launch.ROOT / launch.UPDATING_MARK).read_text(encoding="utf-8"))["pid"] == other.pid, "the first's mark stays"
    finally:
        other.kill()
        other.wait()


def test_the_check_after_an_update_isnt_stopped_by_that_update(launch, monkeypatch):
    # From its start to the end of the check after it, the update says it runs: that check is its own.
    handed: list = []
    monkeypatch.setattr(launch, "update", lambda: 0)
    monkeypatch.setattr(launch, "_hand_over", lambda cmd: handed.append((cmd, dict(os.environ))) or 0)
    assert launch.main(["--update"]) == 0
    (cmd, env), = handed
    assert cmd[-1] == "--check" and env.get(launch.AFTER_UPDATE_ENV) == "1"
    # The check, as that program (the mark still names the update, as it does on a Mac or Linux, where the update
    # becomes the check).
    monkeypatch.setattr(launch, "in_venv", lambda: True)
    monkeypatch.setattr(launch, "ensure_packages", lambda: 0)
    (launch.ROOT / launch.UPDATING_MARK).write_text(json.dumps({"pid": os.getpid(), "started": time.time()}), encoding="utf-8")
    monkeypatch.setenv(launch.AFTER_UPDATE_ENV, "1")
    handed.clear()
    assert launch.main(["--check"]) == 0
    (cmd, env), = handed
    assert cmd[-1] == "doctor" and launch.AFTER_UPDATE_ENV not in env, "said to the check only, not to what it starts"


def test_an_update_said_to_run_long_ago_doesnt_count(launch):
    (launch.ROOT / launch.UPDATING_MARK).write_text(
        json.dumps({"pid": os.getpid(), "started": time.time() - launch.UPDATE_LONGEST - 60}), encoding="utf-8")
    assert launch.update_under_way() is False
    (launch.ROOT / launch.UPDATING_MARK).write_text("not what an update writes", encoding="utf-8")
    assert launch.update_under_way() is False


@pytest.mark.skipif(os.name == "nt", reason="Windows' is asked of Windows itself (below)")
def test_whether_an_updates_program_still_runs_here(launch):
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    assert launch._alive(os.getpid(), time.time()) is True
    assert launch._alive(gone.pid, time.time()) is False


def test_a_program_given_the_updates_number_later_isnt_the_update(launch):
    # A Mac or Linux: the update becomes the check after it (exec, the same number), whose end leaves the mark behind.
    # A program given that number later (a browser's helper, another user's) isn't the update: CloseDesk wouldn't open
    # for half an hour. Asked of the computer when it started (Windows: Windows itself; elsewhere ps).
    alive = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert launch._alive(alive.pid, time.time() + 5) is True
        assert launch._alive(alive.pid, time.time() - 3600) is False
        assert app_window.process_runs(alive.pid, time.time() + 5) is True
        assert app_window.process_runs(alive.pid, time.time() - 3600) is False
    finally:
        alive.kill()
        alive.wait()


@pytest.mark.skipif(os.name != "nt", reason="Windows' own answer")
def test_whether_an_updates_program_still_runs_on_windows(launch):
    # Asked of Windows itself (os.kill would end the program). A program started after the update said it was running
    # was given the same number later: it isn't the update.
    alive = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert launch._alive(alive.pid, time.time() + 5) is True
        assert launch._alive(alive.pid, time.time() - 3600) is False
    finally:
        alive.kill()
        alive.wait()
    assert launch._alive(alive.pid, time.time()) is False


def test_an_update_that_finds_this_the_newest_says_so_for_the_next_start(launch, monkeypatch, tmp_path):
    # Nothing went wrong: the next start must not say "CloseDesk wasn't updated".
    import zipfile

    (launch.ROOT / ".git_archival.txt").write_text(f"commit: {'a' * 40}\ndate: 2026-10-21\n", encoding="utf-8")
    archive = tmp_path / "download.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("outlook-email-main/pyproject.toml", '[project]\nname = "controller-inbox"\ndependencies = []\n')
        bundle.writestr("outlook-email-main/scripts/launch.py", "# the launcher\n")
        bundle.writestr("outlook-email-main/.git_archival.txt", f"commit: {'a' * 40}\ndate: 2026-10-21\n")
    monkeypatch.setenv("CLOSEDESK_UPDATE_URL", archive.as_uri())
    assert launch.update() == 0
    assert (launch.VENV / updates.NEWEST_NOTE).read_text(encoding="utf-8").strip() == "a" * 40
    assert launch.NEWEST_NOTE == updates.NEWEST_NOTE


def test_a_download_that_stalls_gives_up_instead_of_waiting_for_good(launch, monkeypatch, tmp_path):
    # A proxy that holds the connection open and sends nothing: curl would wait with no end, and the window with it.
    ran: list = []
    monkeypatch.setattr(launch.shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(launch.subprocess, "run", lambda cmd, **options: ran.append((cmd, options)) or SimpleNamespace(returncode=0))
    assert launch._fetch("https://example.com/closedesk.zip", tmp_path / "x.zip") is True
    (cmd, options), = ran
    for flag in ("--connect-timeout", "--speed-limit", "--speed-time", "--max-time"):
        assert flag in cmd, flag
    assert int(cmd[cmd.index("--max-time") + 1]) <= launch.DOWNLOAD_LONGEST
    assert options["timeout"] and options["timeout"] > launch.DOWNLOAD_LONGEST


def test_a_download_curl_couldnt_make_isnt_waited_on_for_good_either(launch, monkeypatch, tmp_path):
    # curl refused (a certificate it doesn't trust, say): Python's own download, which trickles on for ever, is given
    # up on within the same time.
    def refused(cmd, **options):
        return SimpleNamespace(returncode=60, stdout="", stderr="SSL certificate problem")

    class Trickle:
        """A reply that keeps sending a little, for ever."""

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, size=-1):
            time.sleep(0.01)
            return b"x"

    monkeypatch.setattr(launch.shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(launch.subprocess, "run", refused)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: Trickle())
    monkeypatch.setattr(launch, "DOWNLOAD_LONGEST", 0.3)
    started = time.monotonic()
    assert launch._fetch("https://example.com/closedesk.zip", tmp_path / "x.zip") is False
    assert time.monotonic() - started < 5


def test_the_update_the_window_starts_adds_to_the_end_of_the_log_on_windows(launch, monkeypatch):
    # Started by the window with the log as its output: Windows passes on the open file, not the way it was opened, so
    # written as passed on it would write over a second CloseDesk window's lines. Opened again here, for adding.
    log = launch.app_log_path()
    log.parent.mkdir(parents=True)
    log.write_text("another window's line\n", encoding="utf-8")
    monkeypatch.setattr(launch, "WINDOWS", True)
    monkeypatch.setenv("CLOSEDESK_APP", "1")
    monkeypatch.setenv("CLOSEDESK_APP_LOG", str(log))
    monkeypatch.setattr(launch, "update", lambda: launch.say("Looking for the newest CloseDesk...") or 1)
    assert launch.main(["--update"]) == 1
    sys.stdout.flush()
    assert log.read_text(encoding="utf-8") == "another window's line\nLooking for the newest CloseDesk...\n"
    assert sys.stdout.mode == "a"
