"""Setting CloseDesk up and keeping it set up: what the launchers run on every start (scripts/launch.py), the
self-check (``doctor``), a dashboard port that is taken, and the one-step update."""

from __future__ import annotations

import importlib.util
import json
import socket
import sys
import zipfile
from pathlib import Path

import pytest

from controller_inbox import cli, doctor, version, web
from liveserver import serving

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def launch(tmp_path, monkeypatch):
    """scripts/launch.py, pointed at an empty CloseDesk folder in tmp_path with nothing left to install."""
    spec = importlib.util.spec_from_file_location("launch_under_test", ROOT / "scripts" / "launch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "CloseDesk"
    (root / ".venv").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "controller-inbox"\ndependencies = ["fastapi>=0.1"]\n'
        '[project.optional-dependencies]\nocr = ["surely-not-installed-ocr>=1"]\ntables = ["surely-not-installed-tables>=1"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module, "VENV", root / ".venv")
    monkeypatch.setattr(module, "STATE", root / ".venv" / "closedesk-setup.json")
    monkeypatch.setattr(module, "LOG", root / ".venv" / "closedesk-setup.log")
    monkeypatch.setattr(module, "runs_this_folder", lambda: True)
    for name in ("CLOSEDESK_MANAGED_TRIED", "CLOSEDESK_PYTHON", "CLOSEDESK_REBUILT", "CLOSEDESK_UPDATE_URL"):
        monkeypatch.delenv(name, raising=False)
    module.installs = []
    module.install_works = True

    def install(*what):
        module.installs.append(what)
        return module.install_works

    monkeypatch.setattr(module, "install", install)
    return module


# Every start: install what is missing, and only that --------------------------------------------------------


def test_a_start_with_everything_in_place_installs_nothing_but_tries_each_add_on_once(launch):
    assert launch.ensure_packages() == 0
    assert launch.installs == [("surely-not-installed-ocr>=1",), ("surely-not-installed-tables>=1",)]
    launch.installs.clear()
    assert launch.ensure_packages() == 0
    assert launch.installs == [], "an add-on that can't install here isn't tried on every start"


def test_an_add_on_is_tried_again_when_the_update_changes_what_it_needs(launch):
    launch.ensure_packages()
    launch.installs.clear()
    text = (launch.ROOT / "pyproject.toml").read_text(encoding="utf-8")
    (launch.ROOT / "pyproject.toml").write_text(text.replace("surely-not-installed-ocr>=1", "surely-not-installed-ocr>=2"), encoding="utf-8")
    launch.ensure_packages()
    assert launch.installs == [("surely-not-installed-ocr>=2",)]


def test_a_package_an_update_added_is_installed_before_starting(launch):
    launch.ensure_packages()
    launch.installs.clear()
    text = (launch.ROOT / "pyproject.toml").read_text(encoding="utf-8")
    (launch.ROOT / "pyproject.toml").write_text(text.replace('"fastapi>=0.1"', '"fastapi>=0.1", "surely-new-package>=1"'), encoding="utf-8")
    assert launch.ensure_packages() == 0
    assert launch.installs == [("-e", ".")]


def test_a_folder_that_was_moved_or_replaced_is_installed_again(launch, monkeypatch):
    """The environment would otherwise run the code of the folder it was set up in (or none, once that is gone)."""
    launch.ensure_packages()
    launch.installs.clear()
    monkeypatch.setattr(launch, "runs_this_folder", lambda: False)
    assert launch.ensure_packages() == 0
    assert launch.installs == [("-e", ".")]


def test_runs_this_folder_only_when_the_installed_code_is_this_folders(launch, monkeypatch, tmp_path):
    monkeypatch.undo()  # the real runs_this_folder
    spec = importlib.util.spec_from_file_location("launch_real", ROOT / "scripts" / "launch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.runs_this_folder() is True  # the tests run this checkout's code
    monkeypatch.setattr(module, "ROOT", tmp_path)
    assert module.runs_this_folder() is False


# The computer's Python is used; none is downloaded unasked --------------------------------------------------


def test_the_environment_is_made_with_the_python_that_runs_this_whatever_its_version(launch, monkeypatch):
    handed = []
    monkeypatch.setattr(launch, "_hand_over", lambda cmd: handed.append(cmd) or 0)
    assert launch.create_environment(["--check"]) == 0
    assert launch.venv_python().exists()
    made_with = (launch.VENV / "pyvenv.cfg").read_text(encoding="utf-8")
    assert "uv =" not in made_with, "a Python was fetched, not the computer's used"
    assert handed and handed[0][0] == str(launch.venv_python()) and handed[0][-1] == "--check"


def test_an_install_that_fails_has_the_environment_made_again_once_then_says_why(launch, monkeypatch, capsys):
    monkeypatch.setattr(launch, "runs_this_folder", lambda: False)
    launch.install_works = False
    assert launch.ensure_packages() == launch.REBUILD
    monkeypatch.setenv("CLOSEDESK_REBUILT", "1")
    assert launch.ensure_packages() == 1
    out = capsys.readouterr().out
    assert "couldn't install what it needs" in out and "Python 3.12 is the safe choice" in out


def test_nothing_in_the_start_up_downloads_a_python():
    """Fetching a Python is the launchers' --get-python, done only when asked: launch.py has no part in it."""
    source = (ROOT / "scripts" / "launch.py").read_text(encoding="utf-8")
    assert "python install" not in source and '"venv", "--python"' not in source and "only-managed" not in source
    for launcher in ("CloseDesk.command", "CloseDesk.bat"):
        text = (ROOT / launcher).read_text(encoding="utf-8")
        calls = [line for line in text.splitlines() if ("make_managed" in line or "call :managed" in line) and not line.lstrip().startswith(("#", "rem", "make_managed()"))]
        assert len(calls) == 1, f"{launcher}: the download must be reachable from --get-python only: {calls}"


def test_the_start_runs_the_everyday_command_a_check_or_a_named_command(launch, monkeypatch):
    monkeypatch.setattr(launch, "in_venv", lambda: True)
    handed = []
    monkeypatch.setattr(launch, "_hand_over", lambda cmd: handed.append(cmd[1:]) or 0)
    launch.main(["--no-browser"])
    launch.main(["--check"])
    launch.main(["--command", "overnight", "--limit", "3"])
    assert handed == [
        ["-m", "controller_inbox", "run", "--no-browser"],
        ["-m", "controller_inbox", "doctor"],
        ["-m", "controller_inbox", "overnight", "--limit", "3"],
    ]


# The one-step update ------------------------------------------------------------------------------------------


def _download(tmp_path, *, commit="b" * 40, name="controller-inbox", extra=None) -> Path:
    """A ZIP as GitHub makes it: one folder holding the new version."""
    path = tmp_path / "download.zip"
    with zipfile.ZipFile(path, "w") as bundle:
        top = "outlook-email-main/"
        bundle.writestr(top + "pyproject.toml", f'[project]\nname = "{name}"\ndependencies = []\n')
        bundle.writestr(top + "scripts/launch.py", "# the new launcher\n")
        bundle.writestr(top + "src/controller_inbox/__init__.py", "NEW = True\n")
        bundle.writestr(top + "CloseDesk.command", "#!/bin/bash\n# new\n")
        bundle.writestr(top + ".git_archival.txt", f"commit: {commit}\ndate: 2026-11-10\n")
        bundle.writestr(top + "inbox/incoming/.gitkeep", "")
        for item, text in (extra or {}).items():
            bundle.writestr(item, text)
    return path


def _installed(launch) -> None:
    root = launch.ROOT
    (root / "src" / "controller_inbox").mkdir(parents=True)
    (root / "src" / "controller_inbox" / "__init__.py").write_text("OLD = True\n", encoding="utf-8")
    (root / "src" / "controller_inbox" / "gone_in_the_new_version.py").write_text("", encoding="utf-8")
    (root / "data").mkdir()
    (root / "data" / "closedesk.db").write_text("my mail", encoding="utf-8")
    (root / "inbox" / "incoming").mkdir(parents=True)
    (root / "inbox" / "incoming" / "invoice.msg").write_text("dropped", encoding="utf-8")
    (root / ".env").write_text("CONTROLLER_INBOX_PORT=8765\n", encoding="utf-8")
    (root / ".git_archival.txt").write_text(f"commit: {'a' * 40}\ndate: 2026-10-21\n", encoding="utf-8")


def test_an_update_replaces_closedesks_files_and_keeps_the_mail_and_settings(launch, monkeypatch, tmp_path):
    _installed(launch)
    monkeypatch.setenv("CLOSEDESK_UPDATE_URL", _download(tmp_path).as_uri())
    assert launch.update() == 0
    root = launch.ROOT
    assert (root / "src" / "controller_inbox" / "__init__.py").read_text() == "NEW = True\n"
    assert not (root / "src" / "controller_inbox" / "gone_in_the_new_version.py").exists()
    assert (root / "data" / "closedesk.db").read_text() == "my mail"
    assert (root / "inbox" / "incoming" / "invoice.msg").read_text() == "dropped"
    assert (root / ".env").read_text() == "CONTROLLER_INBOX_PORT=8765\n"
    assert (root / ".venv").is_dir()
    assert (root / ".previous" / "src" / "controller_inbox" / "__init__.py").read_text() == "OLD = True\n"
    assert launch.installed_commit() == "b" * 40
    assert [item.name for item in root.iterdir() if item.name.startswith(".update-")] == []


def test_an_update_to_the_version_already_here_changes_nothing(launch, monkeypatch, tmp_path, capsys):
    _installed(launch)
    monkeypatch.setenv("CLOSEDESK_UPDATE_URL", _download(tmp_path, commit="a" * 40).as_uri())
    assert launch.update() == 0
    assert "already the newest" in capsys.readouterr().out
    assert (launch.ROOT / "src" / "controller_inbox" / "__init__.py").read_text() == "OLD = True\n"


@pytest.mark.parametrize(
    "download",
    [
        lambda tmp: _download(tmp, name="something-else"),
        lambda tmp: _download(tmp, extra={"outlook-email-main/../../outside.txt": "x"}),
        lambda tmp: _download(tmp, extra={"a-second-folder/file.txt": "x"}),
        lambda tmp: (tmp / "not-a.zip").write_text("<html>blocked by the proxy</html>") and (tmp / "not-a.zip"),
        lambda tmp: tmp / "missing.zip",
    ],
    ids=["another project", "a path outside the folder", "two folders", "not a zip", "no download"],
)
def test_a_download_that_isnt_closedesk_changes_nothing(launch, monkeypatch, tmp_path, download):
    _installed(launch)
    monkeypatch.setenv("CLOSEDESK_UPDATE_URL", download(tmp_path).as_uri())
    assert launch.update() == 1
    assert (launch.ROOT / "src" / "controller_inbox" / "__init__.py").read_text() == "OLD = True\n"
    assert not (tmp_path / "outside.txt").exists()
    assert (launch.ROOT / "data" / "closedesk.db").read_text() == "my mail"


def test_an_update_that_cant_be_put_in_place_leaves_closedesk_as_it_was(launch, monkeypatch, tmp_path):
    _installed(launch)
    monkeypatch.setenv("CLOSEDESK_UPDATE_URL", _download(tmp_path).as_uri())
    real = launch.shutil.move

    def move(source, target):
        if Path(source).name == "src" and ".update-" in str(source):
            raise PermissionError("the file is in use")
        return real(source, target)

    monkeypatch.setattr(launch.shutil, "move", move)
    assert launch.update() == 1
    assert (launch.ROOT / "src" / "controller_inbox" / "__init__.py").read_text() == "OLD = True\n"
    assert (launch.ROOT / "pyproject.toml").read_text().startswith("[project]") and "fastapi" in (launch.ROOT / "pyproject.toml").read_text()


# Which version this is ----------------------------------------------------------------------------------------


def test_a_download_knows_its_version_and_a_checkout_says_it_is_one(monkeypatch, tmp_path):
    assert version.describe() == "a developer's copy (not a download)"  # the placeholder GitHub fills in is still there
    monkeypatch.setattr(version, "ROOT", tmp_path)
    (tmp_path / ".git_archival.txt").write_text("commit: 0ec5783d4cbd3e0414a927b22be4549cb7c1405d\ndate: 2026-10-21\n", encoding="utf-8")
    assert version.installed() == ("0ec5783d4cbd3e0414a927b22be4549cb7c1405d", "2026-10-21")
    assert version.describe() == "the version of 2026-10-21 (0ec5783)"


# The self-check -----------------------------------------------------------------------------------------------


@pytest.fixture
def here(tmp_path, monkeypatch):
    """A CloseDesk folder of its own: the settings the check reads are this folder's."""
    monkeypatch.chdir(tmp_path)
    for name in ("CONTROLLER_INBOX_DATA_DIR", "CONTROLLER_INBOX_INBOX_DIR", "CONTROLLER_INBOX_PORT", "CONTROLLER_INBOX_HOST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CONTROLLER_INBOX_LLM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("CONTROLLER_INBOX_PORT", str(_unused_port()))
    return tmp_path


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_the_check_passes_on_a_new_folder_and_makes_what_it_needs(here):
    found = doctor.checks()
    assert [check for check in found if check.state == doctor.PROBLEM] == []
    assert (here / "inbox" / "incoming").is_dir() and (here / "data").is_dir()
    assert "Everything CloseDesk needs is in order." in doctor.report(found)


def test_the_check_names_the_line_in_env_that_isnt_valid(here, capsys):
    (here / ".env").write_text("CONTROLLER_INBOX_POLL_SECONDS=often\n", encoding="utf-8")
    assert doctor.main() == 1
    out = capsys.readouterr().out
    assert "CONTROLLER_INBOX_POLL_SECONDS" in out and "FIX" in out and "Traceback" not in out


def test_starting_with_a_bad_setting_says_what_to_fix_instead_of_a_traceback(here, capsys):
    (here / ".env").write_text("CONTROLLER_INBOX_POLL_SECONDS=often\n", encoding="utf-8")
    assert cli.main(["status"]) == 1
    out = capsys.readouterr().out
    assert "CloseDesk can't start yet" in out and "CONTROLLER_INBOX_POLL_SECONDS" in out


def test_the_check_says_when_the_database_cant_be_read(here):
    (here / "data").mkdir()
    doctor.checks()  # makes the database
    from controller_inbox.config import Settings

    Settings().db_path.write_bytes(b"this is not a database at all" * 50)
    problems = [check for check in doctor.checks() if check.state == doctor.PROBLEM]
    assert len(problems) == 1 and "Database" in problems[0].text and problems[0].todo


# A dashboard port that is taken -------------------------------------------------------------------------------


def test_a_port_another_program_has_moves_closedesk_to_the_next_free_one(capsys):
    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        other.listen(1)
        taken = other.getsockname()[1]
        port, running = cli._port_to_use("127.0.0.1", taken)
    assert running is False and port > taken
    assert f"using port {taken}" in capsys.readouterr().out


def test_a_closedesk_already_open_is_shown_instead_of_starting_a_second(settings, store, monkeypatch):
    with serving(web.create_app(settings, store)) as base:
        port = int(base.rsplit(":", 1)[1])
        assert doctor.is_closedesk("127.0.0.1", port) is True
        assert cli._port_to_use("127.0.0.1", port) == (port, True)
        opened, served = [], []
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
        monkeypatch.setattr(cli, "_serve", lambda *a, **k: served.append(1) or 0)
        args = type("Args", (), {"no_serve": False, "no_browser": False, "limit": 0, "host": "127.0.0.1", "port": port})()
        assert cli._run(settings, store, args) == 0
        assert opened == [f"http://127.0.0.1:{port}/"] and served == []


def test_a_free_port_is_used_as_it_is():
    port = _unused_port()
    assert cli._port_to_use("127.0.0.1", port) == (port, False)


def test_the_setup_state_is_kept_as_json(launch):
    launch.ensure_packages()
    state = json.loads(launch.STATE.read_text(encoding="utf-8"))
    assert set(state["extras"]) == {"ocr", "tables"} and state["python"].startswith(f"{sys.version_info[0]}.{sys.version_info[1]}")


# The check, from Settings -------------------------------------------------------------------------------------


def test_settings_shows_which_copy_this_is_and_runs_the_check(settings, store):
    from fastapi.testclient import TestClient

    from helpers import PAGE

    client = TestClient(web.create_app(settings, store))
    about = client.get("/api/settings", headers=PAGE).json()["about"]
    assert about["version"] == version.describe() and about["python"].startswith(f"{sys.version_info[0]}.{sys.version_info[1]}")
    reply = client.get("/api/settings/check", headers=PAGE)
    assert reply.status_code == 200
    found = reply.json()["checks"]
    assert found[0]["text"].startswith("CloseDesk: ") and {item["state"] for item in found} <= {"ok", "note", "problem"}
    assert not any("Dashboard:" in item["text"] for item in found), "asked from the dashboard, the port would only find itself"
    assert client.get("/api/settings/check").status_code == 403, "only CloseDesk's own page may ask"


@pytest.mark.browser
def test_the_check_runs_from_settings_and_lists_what_it_found(site, page):
    page.goto(f"{site}/app/settings")
    page.wait_for_selector("#set-about")
    page.click("#set-about button:has-text('Check the set-up')")
    page.wait_for_selector("#set-about .set-check")
    lines = page.locator("#set-about .set-check").all_inner_texts()
    assert any(line.startswith("CloseDesk: ") for line in lines) and any("Database: in order" in line for line in lines), lines


def test_the_check_prints_in_a_windows_console_that_cant_show_every_character(here, monkeypatch):
    # A Windows console writes cp1252: a check whose text has a character it lacks ("→") must still print, not crash
    # the self-check (it did, in CI, with "Settings → Sync inbox").
    import io

    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", console)
    monkeypatch.setattr(doctor, "checks", lambda: [doctor.Check(doctor.OK, "Classic Outlook: not connected (Settings → Sync inbox) ✓")])
    assert doctor.main() == 0
    console.flush()
    assert "Classic Outlook: not connected" in raw.getvalue().decode("cp1252")
