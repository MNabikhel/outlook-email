"""Runs the real launcher (CloseDesk.command or CloseDesk.bat) the way a person would, on this computer:

    python tests/launcher_smoke.py

A copy of this checkout is unpacked as a download would be (into a folder with a space in its name), then:
first start (set-up from nothing), second start (nothing to do), the folder renamed, the dashboard really
answering, and an update that keeps the mail. CI runs this on macOS, Windows and Linux, once with the Python
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
import time
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


def main() -> int:
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
        # A second CloseDesk started while this one is open shows it and leaves, instead of failing on the port.
        out = run(folder, "--no-browser", "--port", str(port))
        assert "already open in another window" in out, out
    finally:
        stop(process)

    # An update: a newer download, with the mail and settings left alone.
    (folder / "data").mkdir(exist_ok=True)
    (folder / "data" / "keep.txt").write_text("my mail", encoding="utf-8")
    (folder / ".env").write_text("CONTROLLER_INBOX_HIGH_AMOUNT=12345\n", encoding="utf-8")
    newer = work / "newer" / "outlook-email-main"
    export(newer, commit="b" * 40)
    (newer / "docs" / "ONLY-IN-THE-UPDATE.txt").write_text("new", encoding="utf-8")
    archive = work / "newer.zip"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(newer.rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(newer.parent).as_posix())
    archive.write_bytes(buffer.getvalue())
    out = run(folder, "--update", env={"CLOSEDESK_UPDATE_URL": archive.as_uri()})
    assert "CloseDesk was updated" in out and "Everything CloseDesk needs is in order." in out, out
    assert (folder / "docs" / "ONLY-IN-THE-UPDATE.txt").exists()
    assert (folder / "data" / "keep.txt").read_text() == "my mail"
    assert (folder / ".env").read_text() == "CONTROLLER_INBOX_HIGH_AMOUNT=12345\n"
    assert (folder / ".previous" / "pyproject.toml").exists()
    assert "b" * 7 in out, "the check after the update doesn't show the new version"

    out = run(folder, "--update", env={"CLOSEDESK_UPDATE_URL": archive.as_uri()})
    assert "already the newest" in out

    if WINDOWS and not own:
        # Windows' Python install manager has its own "py", which ends below 0 for a version it hasn't got. Taken
        # for a Python that runs, CloseDesk stopped on "No runtime installed that matches 3.12".
        manager = work / "install manager"
        manager.mkdir()
        (manager / "py.cmd").write_text(
            '@echo [ERROR] No runtime installed that matches 3.12. Try running "py install 3.12".\r\n@exit /b -1\r\n',
            encoding="ascii",
        )
        shutil.rmtree(folder / ".venv")
        out = run(folder, "--check", env={"PATH": f"{manager}{os.pathsep}{os.environ['PATH']}"})
        assert "No runtime installed" not in out and "Everything CloseDesk needs is in order." in out, out

    shutil.rmtree(work, ignore_errors=True)
    print("Launcher smoke test passed.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
