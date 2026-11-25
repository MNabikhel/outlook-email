"""``launch.py --app``, what the CloseDesk shortcut runs with pythonw (no console window): the same set-up as every
start, silently (what it says goes to a log; nothing it runs opens a console window), what goes wrong said in a message
box, and then the CloseDesk window (app_window.py, tested in test_app_window.py)."""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
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
    for name in ("CLOSEDESK_APP", "CLOSEDESK_APP_LOG", "CLOSEDESK_LAUNCHER", "CLOSEDESK_REBUILT", "CLOSEDESK_UPDATE_URL"):
        monkeypatch.delenv(name, raising=False)
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


def test_the_app_doesnt_start_while_an_update_is_putting_files_in_place(launch, monkeypatch):
    # The window closed during an update leaves the update to finish; opened again meanwhile, CloseDesk would start
    # from a half-replaced folder. (launch.py --update keeps its download in .update-* until it is done.)
    import time

    monkeypatch.setattr(launch, "in_venv", lambda: True)
    monkeypatch.setattr(launch, "ensure_packages", lambda: 0)
    handed: list = []
    monkeypatch.setattr(launch, "_hand_over", lambda cmd: handed.append(cmd) or 0)
    under_way = launch.ROOT / ".update-k2j3h4"
    under_way.mkdir()
    assert launch.main(["--app"]) == 0
    assert launch.told == [launch.UPDATING_NOW] and handed == []
    old = time.time() - 3600  # left behind by an update that was stopped long ago
    os.utime(under_way, (old, old))
    assert launch.main(["--app"]) == 0
    assert len(handed) == 1


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
