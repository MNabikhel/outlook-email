"""Runs the real launcher (CloseDesk.command or CloseDesk.bat) the way a person would, on this computer:

    python tests/launcher_smoke.py

A copy of this checkout is unpacked as a download would be (into a folder with a space in its name), then:
first start (set-up from nothing), second start (nothing to do), the folder renamed, the dashboard really
answering, the CloseDesk shortcut made (CloseDesk.lnk on Windows, CloseDesk.app on a Mac, kept in a folder of the
check's, never on the computer's Desktop), the shortcut's start (launch.py --app, without its window), an update that
keeps the mail, an update from Settings' Update button (CloseDesk stops, the launcher fetches the newer version and
starts it again), a deleted shortcut left deleted until launch.py --shortcut, and the shortcut finding the folder again
after it moved. CI runs this on macOS, Windows and Linux, once with the Python
the machine has (nothing may be downloaded) and once asking CloseDesk to download its own, as a person without
the rights to install Python would (CLOSEDESK_OWN_PYTHON=1 makes the first start ``--get-python``).

    python tests/launcher_smoke.py --window

opens the CloseDesk window itself, for real (CI's window job, on Windows and macOS; see window_check): as the shortcut
starts it, onto a CloseDesk already open, and through the shortcut as a double-click opens it, each time with
CloseDesk's page loaded in it and CloseDesk stopped with it, then Update now in it; on a Mac the Dock has to call it
CloseDesk.

Not a pytest file: it installs for real and takes minutes.
"""

from __future__ import annotations

import io
import json
import os
import plistlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WINDOWS = os.name == "nt"


def launcher(folder: Path, *args: str) -> list[str]:
    if WINDOWS:
        return ["cmd", "/c", str(folder / "CloseDesk.bat"), *args]
    return ["bash", str(folder / "CloseDesk.command"), *args]


def run(folder: Path, *args: str, env: dict | None = None) -> str:
    """The launcher's output; fails loudly when it exits with an error. No keyboard: a "press a key" must not hang."""
    started = time.monotonic()
    done = subprocess.run(
        launcher(folder, *args), cwd=folder, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace", env={**os.environ, **(env or {})}, timeout=1500,
    )
    print(f"--- {' '.join(args) or '(start)'}: exit {done.returncode} in {time.monotonic() - started:.0f}s\n{done.stdout}", flush=True)
    log = folder / ".venv" / "closedesk-setup.log"
    if done.returncode != 0:
        if log.exists():
            print("--- set-up log (end)\n" + "\n".join(log.read_text(errors="replace").splitlines()[-60:]), flush=True)
        raise SystemExit(f"FAILED: launcher {' '.join(args)} exited {done.returncode}")
    return done.stdout


def export(dest: Path, *, commit: str) -> None:
    """This checkout as GitHub's ZIP would hold it: tracked files (and those not yet committed), no .git."""
    files = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=REPO, capture_output=True, text=True, check=True).stdout.split("\n")
    for name in filter(None, files):
        source = REPO / name
        if source.is_file():
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    (dest / ".git_archival.txt").write_text(f"commit: {commit}\ndate: 2026-01-01\n", encoding="utf-8")


def newer_zip(work: Path, commit: str, name: str) -> Path:
    """A newer download of this checkout, at ``commit``, as GitHub's ZIP of main would hold it."""
    newer = work / name / "outlook-email-main"
    export(newer, commit=commit)
    (newer / "docs" / "ONLY-IN-THE-UPDATE.txt").write_text(name, encoding="utf-8")
    archive = work / f"{name}.zip"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(newer.rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(newer.parent).as_posix())
    archive.write_bytes(buffer.getvalue())
    return archive


def health(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as reply:
            return json.load(reply)
    except OSError:
        return None


def update_from_settings(folder: Path, archive: Path, *, before: str, after: str) -> None:
    """Start CloseDesk with its launcher, press Settings' Update button as the page does, and see the launcher fetch
    the update and CloseDesk answer again with the newer version, without anyone starting it again."""
    port = free_port()
    output: list[str] = []
    process = subprocess.Popen(
        launcher(folder, "--no-browser", "--port", str(port)), cwd=folder, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=not WINDOWS,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0,
        # No asking GitHub from here: the update comes from the ZIP made above.
        env={**os.environ, "CLOSEDESK_UPDATE_URL": archive.as_uri(), "CONTROLLER_INBOX_UPDATE_CHECK": "false"},
    )
    # Read as it comes, so a full pipe never stops the launcher.
    reader = threading.Thread(target=lambda: output.extend(line.decode(errors="replace") for line in process.stdout), daemon=True)
    reader.start()
    try:
        answer = None
        for _ in range(180):
            answer = health(port)
            if answer or process.poll() is not None:
                break
            time.sleep(1)
        assert answer and answer.get("commit") == before, f"CloseDesk didn't start: {answer!r}\n{''.join(output)}"
        page = {"X-CloseDesk": "1", "Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"}
        # The start reads new mail behind the page first; the button waits for that, as a person would.
        for _ in range(120):
            with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/api/state", headers=page), timeout=10) as reply:
                if json.load(reply)["job"]["state"] != "running":
                    break
            time.sleep(1)
        asked = urllib.request.Request(f"http://127.0.0.1:{port}/api/update", method="POST", data=b"{}", headers=page)
        try:
            with urllib.request.urlopen(asked, timeout=30) as reply:
                said = json.load(reply)
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"FAILED: the Update button was refused ({exc.code}): {exc.read().decode(errors='replace')}") from None
        assert said.get("ok") and "opens again by itself" in said["message"], said
        print(f"--- Update pressed: {said['message']}", flush=True)
        answer = None
        for _ in range(600):
            answer = health(port)
            if answer and answer.get("commit") == after:
                break
            assert process.poll() is None, f"the launcher ended (exit {process.returncode}) instead of starting CloseDesk again\n{''.join(output)}"
            time.sleep(1)
        assert answer and answer.get("commit") == after, f"CloseDesk didn't come back with the update: {answer!r}\n{''.join(output)}"
        print(f"--- after the update CloseDesk answers again: {answer}", flush=True)
    finally:
        stop(process)
        reader.join(timeout=10)
        print("--- the launcher's window through the update\n" + "".join(output), flush=True)
    said = "".join(output)
    assert "CloseDesk was updated" in said and "Everything CloseDesk needs is in order." in said, said
    assert (folder / "docs" / "ONLY-IN-THE-UPDATE.txt").read_text(encoding="utf-8") == archive.stem


# Linux's window check (the stand-in window): where the app's words are kept, as its log is on Windows and macOS.
_LOG: list[Path] = []


def app_log() -> Path:
    """Where launch.py --app writes what it says on Windows and macOS (its app_log_path)."""
    if _LOG:
        return _LOG[0]
    if WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "CloseDesk" / "CloseDesk-app.log"
    return Path.home() / "Library" / "Logs" / "CloseDesk" / "CloseDesk-app.log"


def app_start(folder: Path) -> None:
    """The CloseDesk shortcut's start, as it will run it (launch.py --app, on the environment's pythonw on Windows),
    with no window and no browser (--no-browser): the set-up checked without a console, what it says in its log, and
    CloseDesk answering. (The window itself is checked where it can open.)"""
    port = free_port()
    python = folder / ".venv" / ("Scripts/pythonw.exe" if WINDOWS else "bin/python")
    logged = sys.platform in ("win32", "darwin")
    output: list[str] = []
    process = subprocess.Popen(
        [str(python), str(folder / "scripts" / "launch.py"), "--app", "--no-browser", "--port", str(port)], cwd=folder,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=not WINDOWS,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0,
        env={**os.environ, "CONTROLLER_INBOX_UPDATE_CHECK": "false"},
    )
    reader = threading.Thread(target=lambda: output.extend(line.decode(errors="replace") for line in process.stdout), daemon=True)
    reader.start()
    try:
        answer = None
        for _ in range(180):
            answer = health(port)
            if answer or process.poll() is not None:
                break
            time.sleep(1)
        log = app_log().read_text(encoding="utf-8", errors="replace") if logged and app_log().exists() else ""
        assert answer and answer.get("app") == "closedesk", (
            f"the app's start didn't answer (exit {process.poll()})\n{''.join(output)}\n--- its log (end)\n{log[-4000:]}")
        print(f"--- the app's start ({python.name} launch.py --app) answers on port {port}", flush=True)
    finally:
        stop(process)
        reader.join(timeout=10)
    said = app_log().read_text(encoding="utf-8", errors="replace") if logged else "".join(output)
    opening = f"CloseDesk opening (the app), in {folder.resolve()}"
    assert opening in said, said[-4000:]
    assert "The CloseDesk window opens" in said[said.rindex(opening):], said[-4000:]
    if logged:
        assert "CloseDesk opening" not in "".join(output), "the app's words went to a console, not its log"
    if sys.platform == "darwin":
        # Python started through the link in CloseDesk.app (the Dock says CloseDesk), and it found CloseDesk.
        assert "The window opens as CloseDesk.app" in said[said.rindex(opening):], said[-4000:]
    print(f"--- the app said what it did in {'its log, ' + str(app_log()) if logged else 'the terminal'}", flush=True)


# The CloseDesk shortcut --------------------------------------------------------------------------------------------


def shortcuts_made(shortcuts: Path) -> list[Path]:
    """Where the CloseDesk shortcut goes here (in the check's own folder, CLOSEDESK_SHORTCUT_DIR)."""
    if WINDOWS:
        return [shortcuts / "Desktop" / "CloseDesk.lnk", shortcuts / "Start menu" / "CloseDesk.lnk"]
    if sys.platform == "darwin":
        return [shortcuts / "CloseDesk.app"]
    return []


def opens(folder: Path, place: Path) -> str:
    """The folder the shortcut at ``place`` opens, read back as Windows or macOS would; checked to be CloseDesk's."""
    if WINDOWS:
        # Read with Windows' own IShellLink (pywin32, in the environment CloseDesk made).
        done = subprocess.run(
            [str(folder / ".venv" / "Scripts" / "python.exe"), "-c",
             "import json, sys; from controller_inbox import shortcut; print(json.dumps(shortcut.read_link(sys.argv[1])))", str(place)],
            capture_output=True, text=True, timeout=120,
        )
        assert done.returncode == 0, done.stdout + done.stderr
        link = json.loads(done.stdout.strip().splitlines()[-1])
        assert link["app_id"] == "CloseDesk.CloseDesk", link
        opened = link["folder"]
        assert Path(link["target"]) == Path(opened) / ".venv" / "Scripts" / "pythonw.exe", link
        assert link["arguments"] == f'"{Path(opened) / "scripts" / "launch.py"}" --app', link
        assert Path(link["icon"]).is_file() and link["flags"] & 0x40100 == 0x40100, link
        return opened
    with (place / "Contents" / "Info.plist").open("rb") as handle:
        info = plistlib.load(handle)
    assert info["CFBundleName"] == "CloseDesk" and info["CFBundleExecutable"] == "CloseDesk", info
    lint = subprocess.run(["plutil", "-lint", str(place / "Contents" / "Info.plist")], capture_output=True, text=True)
    assert lint.returncode == 0, lint.stdout + lint.stderr
    script = place / "Contents" / "MacOS" / "CloseDesk"
    assert os.access(script, os.X_OK), "the app's program can't be run"
    python = place / "Contents" / "MacOS" / "CloseDesk-python"
    assert python.is_symlink() and Path(os.path.realpath(python)).is_file(), "the app's link to Python leads nowhere"
    marks = subprocess.run(["xattr", "-lr", str(place)], capture_output=True, text=True).stdout
    assert "com.apple.quarantine" not in marks, "made on this Mac, the app must carry no downloaded-from-the-internet mark"
    return info["CloseDeskFolder"]


def wait_for_shortcuts(folder: Path, shortcuts: Path, *, wait: float = 120) -> None:
    """The shortcut, made (or put right) beside CloseDesk's start, opening ``folder``."""
    places = shortcuts_made(shortcuts)
    if not places:
        assert not shortcuts.exists() or not any(shortcuts.iterdir()), "no CloseDesk shortcut on Linux"
        print("--- no CloseDesk shortcut here (Linux), as it should be", flush=True)
        return
    deadline = time.monotonic() + wait
    found: dict = {}
    while time.monotonic() < deadline:
        found = {}
        for place in places:
            if place.exists():
                try:
                    found[place] = opens(folder, place)
                except (OSError, ValueError, KeyError, plistlib.InvalidFileException):
                    pass  # being written
        if len(found) == len(places) and all(Path(opened) == folder.resolve() for opened in found.values()):
            break
        time.sleep(1)
    assert len(found) == len(places), f"the CloseDesk shortcut wasn't made: {found} (of {places})"
    for place, opened in found.items():
        assert Path(opened) == folder.resolve(), f"{place} opens {opened}, not {folder}"
    print(f"--- the CloseDesk shortcut opens {folder}: {', '.join(str(place) for place in places)}", flush=True)


def start_dashboard(folder: Path) -> subprocess.Popen:
    """CloseDesk started by its launcher, answering; stop it with stop()."""
    port = free_port()
    process = subprocess.Popen(
        launcher(folder, "--no-browser", "--port", str(port)), cwd=folder, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=not WINDOWS,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0,
        env={**os.environ, "CONTROLLER_INBOX_UPDATE_CHECK": "false"},
    )
    process.port = port
    for _ in range(300):
        answer = health(port)
        if answer and answer.get("app") == "closedesk":
            return process
        if process.poll() is not None:
            break
        time.sleep(1)
    stop(process)
    raise SystemExit(f"FAILED: the dashboard didn't answer\n{process.stdout.read().decode(errors='replace')}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def stop(process: subprocess.Popen) -> None:
    if WINDOWS:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()


def drive_letters() -> set[str]:
    """The drive letters Windows has now (a network folder pushd lent a letter to among them)."""
    return {letter for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{letter}:\\")}


def from_network_share(folder: Path) -> None:
    """CloseDesk.bat opened by the name Windows gives a mapped network drive (\\\\server\\share\\...), as the shortcut
    and the window open it where the window can't open: cmd can't be in such a folder, so CloseDesk.bat has to lend
    it a drive letter (pushd), and give it back as it ends. Here this computer's own C: drive, as a network share.
    (The environment was made under C:\\, so this start puts the folder right for its new name first.)"""
    local = folder.resolve()
    share = Path(f"\\\\localhost\\{local.drive[0]}$") / local.relative_to(local.anchor)
    if not share.is_dir():
        raise SystemExit(f"FAILED: this computer's C: drive can't be reached as the network share {share}")
    before = drive_letters()
    # Started in that folder, as Windows starts it from the shortcut: cmd says it can't be in it, and works elsewhere.
    done = subprocess.run(
        ["cmd", "/c", str(share / "CloseDesk.bat"), "--check"], cwd=share, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, errors="replace", timeout=1500,
    )
    print(f"--- CloseDesk.bat --check from {share}: exit {done.returncode}\n{done.stdout}", flush=True)
    assert done.returncode == 0 and "Everything CloseDesk needs is in order." in done.stdout, done.stdout
    assert "can't open file" not in done.stdout, done.stdout
    left = drive_letters() - before
    assert not left, f"CloseDesk.bat didn't give back the drive letter it lent the network folder: {sorted(left)}"
    print(f"--- CloseDesk.bat works from a network share ({share}), and gave back the drive letter it used", flush=True)


def install_manager_py(work: Path, folder: Path) -> None:
    """Windows' Python install manager has its own py.exe, which ends below 0 for a version it hasn't got. The
    launcher took that for a Python that runs and stopped on "No runtime installed that matches 3.12". A py.exe
    that does the same is built with the C# compiler Windows comes with, and put first on the PATH."""
    manager = work / "install manager"
    manager.mkdir()
    source = manager / "py.cs"
    source.write_text(
        'class Py { static int Main() { System.Console.Error.WriteLine('
        '"[ERROR] No runtime installed that matches 3.12. Try running \\"py install 3.12\\"."); return -1; } }',
        encoding="ascii",
    )
    compilers = sorted(Path(os.environ.get("WINDIR", r"C:\Windows"), "Microsoft.NET").glob("Framework*/v4*/csc.exe"))
    if not compilers:
        print("--- no C# compiler on this computer: the Python install manager's py.exe isn't tried", flush=True)
        return
    subprocess.run([str(compilers[-1]), "/nologo", f"/out:{manager / 'py.exe'}", str(source)], check=True, capture_output=True)
    done = subprocess.run([str(manager / "py.exe"), "-3.12"], capture_output=True, text=True)
    assert done.returncode != 0 and "No runtime installed" in done.stderr, (done.returncode, done.stderr)
    shutil.rmtree(folder / ".venv")
    out = run(folder, "--check", env={"PATH": f"{manager}{os.pathsep}{os.environ['PATH']}"})
    assert "No runtime installed" not in out and "Everything CloseDesk needs is in order." in out, out


# The CloseDesk window itself (--window) ----------------------------------------------------------------------------

MAC = sys.platform == "darwin"
BUNDLE_ID = "io.github.mnabikhel.closedesk"  # CloseDesk.app's (shortcut.BUNDLE_ID)
LSREGISTER = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
# The window's check (app_window): it closes itself once CloseDesk's page says it loaded, after staying open a while for
# this check to look at it; a window that can't open fails the check instead of opening the browser, and nothing waits
# for a click. No asking GitHub: whether an update is out isn't what is checked here.
WINDOW_CHECK = {"CLOSEDESK_APP_SMOKE": "120", "CLOSEDESK_APP_SMOKE_HOLD": "10", "CONTROLLER_INBOX_UPDATE_CHECK": "false"}
SHOWED = "The CloseDesk window showed "
DOCK_NAME = f"macOS shows the window as CloseDesk ({BUNDLE_ID})."


def env_python(folder: Path, *, windowless: bool = False) -> Path:
    """The environment's Python; on Windows its pythonw.exe (no console window) when asked for, as the shortcut runs.
    (Linux's window check: one that writes to the app's log, as the app does on Windows and macOS.)"""
    if WINDOWS:
        return folder / ".venv" / "Scripts" / ("pythonw.exe" if windowless else "python.exe")
    if windowless and _LOG:
        return _LOG[0].with_name("python-to-the-log")
    return folder / ".venv" / "bin" / "python"


def stand_in_window(folder: Path, work: Path) -> None:
    """Linux has no window: the window's check runs with the stand-in pywebview (tests/stand_in_window), everything
    else for real, and the app's words kept in a log as on Windows and macOS."""
    _LOG.append(work / "CloseDesk-app.log")
    python = env_python(folder, windowless=True)
    python.write_text(f'#!/bin/bash\nexec "{folder / ".venv" / "bin" / "python"}" "$@" >>"{_LOG[0]}" 2>&1\n', encoding="utf-8")
    python.chmod(0o755)
    os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO / "tests" / "stand_in_window"), os.environ.get("PYTHONPATH")]))
    os.environ["CLOSEDESK_STAND_IN_WINDOW"] = "1"


def in_env(folder: Path, code: str) -> str:
    """What ``code`` prints, run by the environment CloseDesk made."""
    done = subprocess.run(
        [str(env_python(folder)), "-c", code], cwd=folder, capture_output=True, text=True, errors="replace", timeout=180,
        env={**os.environ, "CONTROLLER_INBOX_UPDATE_CHECK": "false"},
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout.strip().splitlines()[-1] if done.stdout.strip() else ""


def log_size() -> int:
    try:
        return app_log().stat().st_size
    except OSError:
        return 0


def log_since(start: int) -> str:
    """What the app wrote in its log after ``start`` (all of it when the log was started afresh meanwhile)."""
    try:
        data = app_log().read_bytes()
    except OSError:
        return ""
    return data[start if start <= len(data) else 0 :].decode("utf-8", errors="replace")


def listening(port: int) -> bool:
    """Whether anything takes connections on ``port`` (CloseDesk, still there)."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def parse_apps(listing: str) -> list[dict]:
    """The apps in ``lsappinfo list``'s output: each one's name (what the Dock and the menu bar say), app id, pid and
    folder. A block starts with its number and its name in quotes: `` 3) "CloseDesk" ASN:0x0-0x1d01d:``."""
    apps = []
    for block in re.split(r"\n(?=[ \t]*\d+\)[ \t]+\")", "\n" + listing):
        head = re.match(r'\s*\d+\)\s+"([^"]*)"', block)
        if not head:
            continue
        bundle = re.search(r'bundleID="([^"]*)"', block)
        pid = re.search(r"\bpid\s*=\s*(\d+)", block)
        path = re.search(r'bundle path="([^"]*)"', block)
        apps.append({
            "name": head.group(1), "bundle": bundle.group(1) if bundle else "", "pid": int(pid.group(1)) if pid else 0,
            "path": path.group(1) if path else "",
        })
    return apps


def mac_apps() -> tuple[list[dict], str]:
    """The apps macOS has running now (what the Dock shows), and lsappinfo's own words."""
    try:
        done = subprocess.run(["/usr/bin/lsappinfo", "list"], capture_output=True, text=True, errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"lsappinfo couldn't be run: {exc}"
    return parse_apps(done.stdout), done.stdout


class Sightings:
    """What macOS called the window while it was open (lsappinfo): every name it was given, and any Python beside it."""

    def __init__(self) -> None:
        self.seen: list[dict] = []
        self.pythons: list[dict] = []
        self.listing = ""

    def look(self, *, pid: int = 0) -> None:
        apps, self.listing = mac_apps()
        for app in apps:
            mine = app["pid"] == pid if pid else app["bundle"] == BUNDLE_ID
            if mine and app not in self.seen:
                self.seen.append(app)
            if app["name"] == "Python" and app not in self.pythons:
                self.pythons.append(app)

    def problems(self, what: str) -> list[str]:
        if not self.seen:
            return [f"{what}: lsappinfo never listed the window. Its last listing:\n{self.listing[-3000:]}"]
        problems = [
            f"{what}: macOS called the window {app['name']!r} ({app['bundle'] or 'no app id'}), not CloseDesk"
            for app in self.seen if app["name"] != "CloseDesk" or app["bundle"] != BUNDLE_ID
        ]
        return problems + [f"{what}: the Dock showed a Python too: {app}" for app in self.pythons]


def screenshot(folder: Path, name: str) -> threading.Thread | None:
    """The screen now, kept with the check's results (CLOSEDESK_SMOKE_SHOTS) when it can be taken: taken beside the
    check, which goes on looking at the window meanwhile."""
    shots = os.environ.get("CLOSEDESK_SMOKE_SHOTS", "")
    if not shots:
        return None
    Path(shots).mkdir(parents=True, exist_ok=True)
    target = Path(shots) / f"{name.replace(' ', '-')}.png"

    def take() -> None:
        try:
            subprocess.run(
                [str(env_python(folder)), "-c", "import sys; from PIL import ImageGrab; ImageGrab.grab().save(sys.argv[1])",
                 str(target)], capture_output=True, timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        print(f"--- screenshot: {target}" if target.exists() else f"--- no screenshot could be taken ({name})", flush=True)

    thread = threading.Thread(target=take, daemon=True)
    thread.start()
    return thread


def keep_log() -> None:
    """The app's log, kept with the check's results (CLOSEDESK_SMOKE_SHOTS)."""
    shots = os.environ.get("CLOSEDESK_SMOKE_SHOTS", "")
    if shots and app_log().exists():
        Path(shots).mkdir(parents=True, exist_ok=True)
        shutil.copy2(app_log(), Path(shots) / app_log().name)


def shown(log: str) -> str:
    """The page the window said it showed."""
    return log[log.rindex(SHOWED) + len(SHOWED) :].splitlines()[0]


def pages_shown(log: str) -> list[str]:
    """Every page the window said it showed."""
    return [line.split(SHOWED, 1)[1].strip() for line in log.splitlines() if SHOWED in line]


def both_uis(log: str, what: str) -> None:
    """The window showed the first page (a classic one: the check's folder has no mail) and the workspace, each with
    its scripts run in the window's own engine."""
    pages = pages_shown(log)
    assert "/app" in pages and any(page != "/app" for page in pages), f"the CloseDesk window ({what}) didn't show both UIs: {pages}"


def open_window(folder: Path, port: int, *, name: str, attached: bool = False) -> list[str]:
    """launch.py --app as the shortcut runs it (pythonw on Windows), with the window's check: the real window opens,
    shows CloseDesk's page and closes itself; it ends with 0, and CloseDesk stops with it (``attached`` to a CloseDesk
    already open: that one stays open). On a Mac what macOS called the window is given back, any problem with it said,
    so the other starts are checked and shown too."""
    start = log_size()
    output: list[str] = []
    process = subprocess.Popen(
        [str(env_python(folder, windowless=True)), str(folder / "scripts" / "launch.py"), "--app", "--port", str(port)],
        cwd=folder, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=not WINDOWS,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0, env={**os.environ, **WINDOW_CHECK},
    )
    reader = threading.Thread(target=lambda: output.extend(line.decode(errors="replace") for line in process.stdout), daemon=True)
    reader.start()
    seen = Sightings()
    shot = None
    try:
        deadline = time.monotonic() + 300
        while process.poll() is None and time.monotonic() < deadline:
            if MAC:
                seen.look(pid=process.pid)  # exec keeps the pid: launch.py becomes the window's Python
            if shot is None and SHOWED in log_since(start):
                shot = screenshot(folder, name) or False
            time.sleep(0.5)
        if process.poll() is None:
            raise SystemExit(f"FAILED: the CloseDesk window ({name}) didn't close by itself\n--- its log\n{log_since(start)[-6000:]}")
        # Whether CloseDesk stopped with the window is seen before anything left over is stopped below.
        for _ in range(60):
            if attached or not listening(port):
                break
            time.sleep(0.5)
        stopped = not listening(port)
    finally:
        stop(process)
        reader.join(timeout=10)
        if shot:
            shot.join(timeout=60)
    log = log_since(start)
    said = f"--- its log\n{log[-6000:]}\n--- its output\n{''.join(output)[-2000:]}"
    assert process.returncode == 0, f"the CloseDesk window ({name}) ended with {process.returncode}\n{said}"
    assert SHOWED in log, f"the CloseDesk window ({name}) didn't show CloseDesk's page\n{said}"
    both_uis(log, name)
    print(f"--- the CloseDesk window ({name}) showed {', '.join(pages_shown(log))} and closed by itself, exit 0", flush=True)
    if attached:
        assert health(port), f"closing the window stopped the CloseDesk it didn't start\n{said}"
        print("--- the CloseDesk that was already open is still open", flush=True)
    else:
        assert stopped, f"CloseDesk didn't stop with its window (port {port} still answered)\n{said}"
        print("--- CloseDesk stopped with its window", flush=True)
    if not MAC:
        return []
    print(f"--- what macOS called the window: {seen.seen}", flush=True)
    problems = seen.problems(name)
    if DOCK_NAME not in log:
        problems.append(f"{name}: the window's log doesn't say {DOCK_NAME!r}:\n{log[-3000:]}")
    return problems


def end_windows(folder: Path, shortcuts: Path) -> None:
    """Whatever is left of a CloseDesk window the check opened without being its parent (the double-click's): only what
    runs from the check's own folder, never another CloseDesk (or any other Python program) open on this computer."""
    if WINDOWS:
        # The programs whose command line names the check's folder (the window's Python runs from its .venv), each with
        # what it started. The folder is passed in the environment, never written into the script.
        listing = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($env:CLOSEDESK_CHECK_FOLDER) }"
             " | ForEach-Object { $_.ProcessId }"],
            capture_output=True, text=True, errors="replace", timeout=120, env={**os.environ, "CLOSEDESK_CHECK_FOLDER": str(folder)},
        )
        for pid in listing.stdout.split():
            if pid.isdigit() and int(pid) != os.getpid():
                subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True)
    elif MAC:
        mine = os.path.realpath(shortcuts) + os.sep
        for app in mac_apps()[0]:
            if app["bundle"] == BUNDLE_ID and app["pid"] and os.path.realpath(app["path"] or "/").startswith(mine):
                try:
                    os.kill(app["pid"], signal.SIGTERM)
                except OSError:
                    pass


def closedesk_open_here() -> list[dict]:
    """A Mac: a CloseDesk open already (a real one, not the check's). It has CloseDesk.app's id, so macOS would bring it
    forward instead of opening the check's."""
    return [app for app in mac_apps()[0] if app["bundle"] == BUNDLE_ID] if MAC else []


def double_click(folder: Path, shortcuts: Path) -> list[str]:
    """The CloseDesk shortcut opened as a double-click opens it (Windows' shell on CloseDesk.lnk; macOS's open on
    CloseDesk.app, as the Dock and Spotlight do), with the window's check: the window shows CloseDesk's page and closes
    itself, and CloseDesk stops with it. A shortcut has no options: the port and the check go in the environment."""
    port = free_port()
    check = {**WINDOW_CHECK, "CONTROLLER_INBOX_PORT": str(port), "CLOSEDESK_SHORTCUT_DIR": str(shortcuts)}
    start = log_size()
    if WINDOWS:
        place = shortcuts / "Desktop" / "CloseDesk.lnk"
        before = {key: os.environ.get(key) for key in check}
        os.environ.update(check)  # what Windows' shell starts from here has this program's environment
        try:
            os.startfile(str(place))  # noqa: S606 (the double-click)
        finally:
            for key, value in before.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    else:
        place = shortcuts / "CloseDesk.app"
        cmd = ["/usr/bin/open"]
        for key, value in check.items():
            cmd += ["--env", f"{key}={value}"]
        done = subprocess.run([*cmd, str(place)], capture_output=True, text=True, errors="replace", timeout=120)
        assert done.returncode == 0, f"macOS couldn't open {place}: {done.stdout}{done.stderr}"
    what = f"{place.name} opened as a double-click opens it"
    seen = Sightings()
    shot = None
    up = finished = False
    try:
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if MAC:
                seen.look()
            up = up or bool(health(port))
            if shot is None and SHOWED in log_since(start):
                shot = screenshot(folder, "double-click") or False
            if shot is not None and not listening(port):
                finished = True
                break
            time.sleep(0.5)
    finally:
        if not finished:
            end_windows(folder, shortcuts)
        if shot:
            shot.join(timeout=60)
    log = log_since(start)
    assert finished, (f"the window of {what} didn't show CloseDesk's page and close by itself (CloseDesk answered on "
                      f"port {port}: {up})\n--- the log\n{log[-6000:]}")
    if MAC:
        # Started by macOS from the Dock or Spotlight, CloseDesk.app runs on the Mac's own kind of processor, not under
        # Rosetta (an environment set up for Apple's processor wouldn't load there).
        opened = f"CloseDesk.app opened ({os.uname().machine})"
        assert opened in log, f"macOS didn't run CloseDesk.app as {os.uname().machine}\n--- the log\n{log[-6000:]}"
    both_uis(log, what)
    print(f"--- the window of {what} showed {', '.join(pages_shown(log))}, closed by itself, and CloseDesk stopped with it", flush=True)
    if not MAC:
        return []
    print(f"--- what macOS called it: {seen.seen}", flush=True)
    return seen.problems(what)


def press_update(port: int) -> str:
    """Update now, pressed as the page presses it once the mail CloseDesk reads as it opens is read. Its message."""
    page = {"X-CloseDesk": "1", "Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"}
    for _ in range(120):
        with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/api/state", headers=page), timeout=10) as reply:
            if json.load(reply)["job"]["state"] != "running":
                break
        time.sleep(1)
    asked = urllib.request.Request(f"http://127.0.0.1:{port}/api/update", method="POST", data=b"{}", headers=page)
    try:
        with urllib.request.urlopen(asked, timeout=30) as reply:
            said = json.load(reply)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"FAILED: Update now was refused ({exc.code}): {exc.read().decode(errors='replace')}") from None
    assert said.get("ok") and "opens again by itself" in said["message"], said
    return said["message"]


def update_in_window(folder: Path, archive: Path, *, before: str, after: str) -> None:
    """Update now in the window, as the question as CloseDesk opens (or Settings) presses it: the window stays up and
    says CloseDesk is updating while launch.py --update runs (no console window), then shows the new version, which
    closes the window (the check's) once its page loaded; CloseDesk stops with it."""
    port = free_port()
    start = log_size()
    output: list[str] = []
    process = subprocess.Popen(
        [str(env_python(folder, windowless=True)), str(folder / "scripts" / "launch.py"), "--app", "--port", str(port)],
        cwd=folder, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=not WINDOWS,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0,
        # Held open until Update now is pressed; the update comes from the ZIP made for it, not GitHub.
        env={**os.environ, **WINDOW_CHECK, "CLOSEDESK_APP_SMOKE_HOLD": "240", "CLOSEDESK_UPDATE_URL": archive.as_uri()},
    )
    reader = threading.Thread(target=lambda: output.extend(line.decode(errors="replace") for line in process.stdout), daemon=True)
    reader.start()
    commits: list[str] = []
    try:
        deadline = time.monotonic() + 300
        while SHOWED not in log_since(start):
            assert process.poll() is None and time.monotonic() < deadline, (
                f"the window didn't show CloseDesk (exit {process.poll()})\n--- its log\n{log_since(start)[-6000:]}")
            time.sleep(0.5)
        answer = health(port)
        assert answer and answer.get("commit") == before, answer
        print(f"--- Update now pressed in the window: {press_update(port)}", flush=True)
        updating = False
        deadline = time.monotonic() + 600
        while process.poll() is None and time.monotonic() < deadline:
            answer = health(port)
            if answer and answer.get("commit") not in commits:
                commits.append(answer["commit"])
            updating = updating or "Updating CloseDesk..." in log_since(start)
            time.sleep(0.2)
        if process.poll() is None:
            raise SystemExit(f"FAILED: the window didn't close after the update\n--- its log\n{log_since(start)[-6000:]}")
        for _ in range(60):
            if not listening(port):
                break
            time.sleep(0.5)
        stopped = not listening(port)
    finally:
        stop(process)
        reader.join(timeout=10)
    log = log_since(start)
    said = f"--- its log\n{log[-8000:]}\n--- its output\n{''.join(output)[-2000:]}"
    assert process.returncode == 0, f"the window ended with {process.returncode} after the update\n{said}"
    assert updating and "CloseDesk was updated" in log, f"the window didn't run the update\n{said}"
    # (The new version answers only for the moment its page takes to load: its commit is shown when it was caught.)
    assert log.rindex(SHOWED) > log.index("CloseDesk was updated"), f"the window didn't show the new version\n{said}"
    both_uis(log[log.index("CloseDesk was updated"):], "the new version")
    assert stopped, f"CloseDesk didn't stop with its window after the update\n{said}"
    assert (folder / "docs" / "ONLY-IN-THE-UPDATE.txt").read_text(encoding="utf-8") == archive.stem
    print(f"--- the window updated CloseDesk to {after[:7]}, showed the new version, and closed (versions that answered: "
          f"{', '.join(commit[:7] for commit in commits)})", flush=True)


def window_check() -> int:
    """``--window``: the CloseDesk window, opened for real (CI's window job, on Windows and macOS). Set up by the
    launcher, the shortcut made with ``--shortcut`` and read back, then the window opened three ways: as the shortcut
    runs it, onto a CloseDesk already open, and through the shortcut itself as a double-click opens it. Each time
    CloseDesk's page has to load in it, the window closes itself, and CloseDesk stops with it. Then Update now in the
    window: it updates CloseDesk and shows the new version. On a Mac the Dock has to call it CloseDesk (lsappinfo),
    never Python. On Linux, where there is no window, its start has to say so, and the rest is checked with a stand-in
    for the window itself (tests/stand_in_window). Windows
    needs Microsoft Edge WebView2 Runtime (CI installs it first). CLOSEDESK_SMOKE_SHOTS: a folder for the screenshots
    and the app's log."""
    open_already = closedesk_open_here()
    if open_already:
        print(f"FAILED: CloseDesk is open on this Mac ({open_already}). Close it first: the check opens CloseDesk.app "
              "itself, and macOS would bring the open one forward instead.", flush=True)
        return 1
    work = Path(tempfile.mkdtemp(prefix="closedesk window "))
    shortcuts = work / "shortcuts"
    os.environ["CLOSEDESK_SHORTCUT_DIR"] = str(shortcuts)
    folder = work / "Close Desk"
    export(folder, commit="a" * 40)
    own = bool(os.environ.get("CLOSEDESK_OWN_PYTHON"))
    print(f"The CloseDesk window check in {folder} (own Python: {'yes' if own else 'no'})", flush=True)
    out = run(folder, "--get-python", "--check") if own else run(folder, "--check")
    assert "Everything CloseDesk needs is in order." in out, out
    if not (WINDOWS or MAC):
        done = subprocess.run(
            [str(env_python(folder)), str(folder / "scripts" / "launch.py"), "--app"], cwd=folder, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, errors="replace", timeout=300, env={**os.environ, **WINDOW_CHECK},
        )
        said = done.stdout + done.stderr
        assert done.returncode == 2 and "The CloseDesk window is for Windows and macOS" in said, (done.returncode, said)
        print("--- no window on Linux, and its start says so (a person's start opens the browser instead)", flush=True)
        # The rest of the window, for real, with a stand-in for the window itself.
        stand_in_window(folder, work)
        open_window(folder, free_port(), name="the stand-in, as the shortcut starts it")
        dashboard = start_dashboard(folder)
        try:
            open_window(folder, dashboard.port, name="the stand-in, onto a CloseDesk already open", attached=True)
        finally:
            stop(dashboard)
        update_in_window(folder, newer_zip(work, "b" * 40, "newer"), before="a" * 40, after="b" * 40)
        shutil.rmtree(work, ignore_errors=True)
        print("The CloseDesk window check passed (Linux: no window; the rest of it with a stand-in window).", flush=True)
        return 0
    found = in_env(folder, "from importlib.metadata import version; print(version('pywebview'))")
    runs = in_env(folder, "import sys; print('%d.%d' % sys.version_info[:2])")
    print(f"--- the window's part is installed: pywebview {found}, on Python {runs}", flush=True)
    named = os.environ.get("CLOSEDESK_PYTHON", "")
    if named and not own:
        wanted = subprocess.run([named, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"], capture_output=True,
                                text=True, timeout=60).stdout.strip()
        assert runs == wanted, f"the launcher set CloseDesk up with Python {runs}, not the {wanted} it was given ({named})"
    why = in_env(folder, "from controller_inbox import app_window; print(app_window.why_no_window())")
    assert why == "None", f"the window can't open on this computer: {why}"
    out = run(folder, "--shortcut")
    assert "couldn't" not in out, out
    wait_for_shortcuts(folder, shortcuts, wait=5)
    problems: list[str] = []
    try:
        problems += open_window(folder, free_port(), name="as the shortcut starts it")
        dashboard = start_dashboard(folder)
        try:
            problems += open_window(folder, dashboard.port, name="onto a CloseDesk already open", attached=True)
        finally:
            stop(dashboard)
        problems += double_click(folder, shortcuts)
        update_in_window(folder, newer_zip(work, "b" * 40, "newer"), before="a" * 40, after="b" * 40)
    finally:
        keep_log()
        if MAC:
            subprocess.run([LSREGISTER, "-u", str(shortcuts / "CloseDesk.app")], capture_output=True)
    if problems:
        print("\n".join(["", "FAILED: the Dock doesn't say CloseDesk:", *problems]), flush=True)
        return 1
    shutil.rmtree(work, ignore_errors=True)
    print("The CloseDesk window check passed.", flush=True)
    return 0


def main() -> int:
    # The launcher's window text can hold characters a Windows console's code page lacks ("—" read back as "\ufffd"):
    # printed as "?", not a crash of the test after CloseDesk did its part.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    if "--window" in sys.argv[1:]:
        return window_check()
    work = Path(tempfile.mkdtemp(prefix="closedesk smoke "))
    # The CloseDesk shortcut goes here, not on this computer's Desktop or in its Applications.
    shortcuts = work / "shortcuts"
    os.environ["CLOSEDESK_SHORTCUT_DIR"] = str(shortcuts)
    folder = work / "Close Desk"
    export(folder, commit="a" * 40)
    print(f"Launcher smoke test in {folder} (own Python: {os.environ.get('CLOSEDESK_OWN_PYTHON', 'no')})", flush=True)

    own = bool(os.environ.get("CLOSEDESK_OWN_PYTHON"))
    out = run(folder, "--get-python", "--check") if own else run(folder, "--check")
    assert "Everything CloseDesk needs is in order." in out, "the first start didn't end with everything in order"
    assert (folder / ".venv").is_dir()
    made_with = (folder / ".venv" / "pyvenv.cfg").read_text()
    if own:
        assert "uv =" in made_with, "--get-python didn't give CloseDesk a Python of its own"
        assert "Python 3.12" in out, out
    else:
        assert "uv =" not in made_with and "Downloading a Python" not in out, "a Python was downloaded without being asked for"

    out = run(folder, "--check")
    assert "Installing" not in out and "etting" not in out.split("ok ")[0], "the second start set something up again"

    moved = work / "Close Desk renamed"
    folder.rename(moved)
    folder = moved
    out = run(folder, "--check")
    assert "moved, renamed or replaced" in out and "Everything CloseDesk needs is in order." in out

    port = free_port()
    process = subprocess.Popen(
        launcher(folder, "--no-browser", "--port", str(port)), cwd=folder, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=not WINDOWS,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0,
    )
    try:
        answer = None
        for _ in range(180):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as reply:
                    answer = json.load(reply)
                break
            except OSError:
                if process.poll() is not None:
                    break
                time.sleep(1)
        if not answer or answer.get("app") != "closedesk":
            stop(process)
            raise SystemExit(f"FAILED: the dashboard didn't answer: {answer!r}\n{process.stdout.read().decode(errors='replace')}")
        print(f"--- the dashboard answers on port {port}: {answer}", flush=True)
        # As it opened, CloseDesk asked GitHub behind the page, for the question its first page asks: answered, or
        # given up on, within seconds (whether a newer version is out depends on GitHub, so it is only shown).
        question = {"checking": True}
        for _ in range(30):
            ask = urllib.request.Request(f"http://127.0.0.1:{port}/api/update/question", headers={"X-CloseDesk": "1"})
            with urllib.request.urlopen(ask, timeout=10) as reply:
                question = json.load(reply)
            if not question["checking"]:
                break
            time.sleep(1)
        assert not question["checking"], f"the asking as CloseDesk opened didn't end: {question!r}"
        print(f"--- the update question as it opened: ask={question['ask']} {question['message']!r}", flush=True)
        # A second CloseDesk started while this one is open shows it and leaves, instead of failing on the port.
        out = run(folder, "--no-browser", "--port", str(port))
        assert "already open in another window" in out, out
        # The first start of the dashboard made the CloseDesk shortcut (the starts with --check before it didn't).
        wait_for_shortcuts(folder, shortcuts)
    finally:
        stop(process)

    # The CloseDesk shortcut's start, without the window.
    app_start(folder)
    # The person deletes the shortcut (the Desktop's, or the Mac's app): it isn't put back by the starts below.
    places = shortcuts_made(shortcuts)
    if places and places[0].is_dir():
        shutil.rmtree(places[0])
    elif places:
        places[0].unlink()

    # An update: a newer download, with the mail and settings left alone.
    (folder / "data").mkdir(exist_ok=True)
    (folder / "data" / "keep.txt").write_text("my mail", encoding="utf-8")
    (folder / ".env").write_text("CONTROLLER_INBOX_HIGH_AMOUNT=12345\n", encoding="utf-8")
    archive = newer_zip(work, "b" * 40, "newer")
    out = run(folder, "--update", env={"CLOSEDESK_UPDATE_URL": archive.as_uri()})
    assert "CloseDesk was updated" in out and "Everything CloseDesk needs is in order." in out, out
    assert (folder / "docs" / "ONLY-IN-THE-UPDATE.txt").exists()
    assert (folder / "data" / "keep.txt").read_text() == "my mail"
    assert (folder / ".env").read_text() == "CONTROLLER_INBOX_HIGH_AMOUNT=12345\n"
    assert (folder / ".previous" / "pyproject.toml").exists()
    assert "b" * 7 in out, "the check after the update doesn't show the new version"

    out = run(folder, "--update", env={"CLOSEDESK_UPDATE_URL": archive.as_uri()})
    assert "already the newest" in out

    # The Update button in Settings: CloseDesk stops, the launcher updates it and starts the new one.
    update_from_settings(folder, newer_zip(work, "c" * 40, "newest"), before="b" * 40, after="c" * 40)
    assert (folder / "data" / "keep.txt").read_text() == "my mail"

    # The deleted shortcut stayed deleted through those starts; asked for, it is made again.
    if places:
        assert not places[0].exists(), f"the shortcut the person deleted was put back: {places[0]}"
    out = run(folder, "--shortcut")
    if places:
        assert "CloseDesk" in out and "couldn't" not in out, out
    else:
        assert "no CloseDesk shortcut on Linux" in out, out
    wait_for_shortcuts(folder, shortcuts, wait=5)

    # The folder moved: its next start points the shortcut at it.
    moved = work / "Close Desk moved"
    folder.rename(moved)
    folder = moved
    process = start_dashboard(folder)
    try:
        wait_for_shortcuts(folder, shortcuts)
    finally:
        stop(process)

    if WINDOWS:
        from_network_share(folder)
    if WINDOWS and not own:
        install_manager_py(work, folder)

    shutil.rmtree(work, ignore_errors=True)
    print("Launcher smoke test passed.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
