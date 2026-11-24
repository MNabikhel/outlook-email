"""Runs the real launcher (CloseDesk.command or CloseDesk.bat) the way a person would, on this computer:

    python tests/launcher_smoke.py

A copy of this checkout is unpacked as a download would be (into a folder with a space in its name), then:
first start (set-up from nothing), second start (nothing to do), the folder renamed, the dashboard really
answering, an update that keeps the mail, and an update from Settings' Update button (CloseDesk stops, the launcher
fetches the newer version and starts it again). CI runs this on macOS, Windows and Linux, once with the Python
the machine has (nothing may be downloaded) and once asking CloseDesk to download its own, as a person without
the rights to install Python would (CLOSEDESK_OWN_PYTHON=1 makes the first start ``--get-python``).
Not a pytest file: it installs for real and takes minutes.
"""

from __future__ import annotations

import io
import json
import os
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


def main() -> int:
    # The launcher's window text can hold characters a Windows console's code page lacks ("—" read back as "\ufffd"):
    # printed as "?", not a crash of the test after CloseDesk did its part.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    work = Path(tempfile.mkdtemp(prefix="closedesk smoke "))
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
    finally:
        stop(process)

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

    if WINDOWS and not own:
        install_manager_py(work, folder)

    shutil.rmtree(work, ignore_errors=True)
    print("Launcher smoke test passed.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
