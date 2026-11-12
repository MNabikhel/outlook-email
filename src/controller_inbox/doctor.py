"""``controller_inbox doctor`` (and ``CloseDesk --check``): what is and isn't in order on this computer, in plain
words, with what to do about each thing that isn't. The launchers put the set-up right on their own (Python, the
packages); this looks at the rest: the settings, the folders, the database, the port, and what is optional.
"""

from __future__ import annotations

import platform
import shutil
import socket
import sqlite3
import sys
from dataclasses import dataclass

from controller_inbox import version

OK, NOTE, PROBLEM = "ok", "note", "problem"
# The newest Python CloseDesk's tests run on. A newer one is tried all the same.
TESTED_UP_TO = (3, 14)


@dataclass
class Check:
    state: str
    text: str
    todo: str = ""


def listening(host: str, port: int) -> bool:
    """Whether something answers on the port."""
    target = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
    try:
        with socket.create_connection((target, port), timeout=0.5):
            return True
    except OSError:
        return False


def is_closedesk(host: str, port: int) -> bool:
    """Whether what answers on the port is a CloseDesk dashboard."""
    import httpx

    target = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
    try:
        reply = httpx.get(f"http://{target}:{port}/health", timeout=3.0)
        body = reply.json()
    except (httpx.HTTPError, ValueError):
        return False
    # "app" since this check was added; an older CloseDesk still running answers with these three.
    return isinstance(body, dict) and (body.get("app") == "closedesk" or {"ok", "emails", "graph"} <= set(body))


def free_port(host: str, port: int, *, span: int = 20) -> int:
    """``port`` when nothing listens on it, else the next one free (0 when none of the next ``span`` is)."""
    for candidate in range(port, port + span + 1):
        if not listening(host, candidate):
            return candidate
    return 0


def _settings() -> tuple[object | None, Check]:
    from pydantic import ValidationError

    from controller_inbox.config import Settings

    try:
        settings = Settings()
    except ValidationError as exc:
        lines = "; ".join(
            f"CONTROLLER_INBOX_{str(error['loc'][0]).upper()}: {error['msg']}" if error.get("loc") else error["msg"]
            for error in exc.errors()[:4]
        )
        return None, Check(PROBLEM, f"Settings: a line in .env isn't valid ({lines})", "Correct that line in .env, or delete it to use the default.")
    except ValueError as exc:
        return None, Check(PROBLEM, f"Settings: {exc}", "Correct that line in .env.")
    return settings, Check(OK, "Settings: read")


def _folders(settings) -> Check:
    try:
        settings.ensure_data_dir()
        for folder in (settings.data_dir, settings.inbox_incoming):
            probe = folder / ".closedesk-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
    except OSError as exc:
        return Check(
            PROBLEM,
            f"Folders: CloseDesk can't write here ({exc})",
            "Move the CloseDesk folder somewhere you can write, such as your home folder, and open it again.",
        )
    return Check(OK, f"Folders: mail goes in {settings.inbox_incoming.resolve()}")


def _database(settings) -> Check:
    from controller_inbox.store import Store

    try:
        store = Store(settings.db_path)
        with store.connect() as conn:
            verdict = conn.execute("PRAGMA quick_check").fetchone()[0]
        if verdict != "ok":
            raise sqlite3.DatabaseError(str(verdict))
        emails = store.counts()["emails"]
    except (sqlite3.Error, OSError) as exc:
        return Check(
            PROBLEM,
            f"Database: {settings.db_path} can't be read ({exc})",
            "Close any other CloseDesk window and open it again. If this stays, put back a copy of the data folder "
            "from a backup, or move the database file aside to start with an empty board.",
        )
    return Check(OK, f"Database: in order, {emails} email{'' if emails == 1 else 's'}")


def _port(settings) -> Check:
    host, port = settings.host, settings.port
    if not listening(host, port):
        return Check(OK, f"Dashboard: will open at http://{host}:{port}/")
    if is_closedesk(host, port):
        return Check(NOTE, f"Dashboard: CloseDesk is already running at http://{host}:{port}/")
    other = free_port(host, port + 1)
    if not other:
        return Check(PROBLEM, f"Dashboard: port {port} and the twenty after it are all in use", "Set CONTROLLER_INBOX_PORT in .env to a free port.")
    return Check(
        NOTE,
        f"Dashboard: another program uses port {port}, so CloseDesk will use {other}",
        f"If that program is LM Studio's server, set it back to its own port (1234). Or set CONTROLLER_INBOX_PORT={other} in .env.",
    )


def _python() -> Check:
    found = sys.version_info[:2]
    text = f"Python {platform.python_version()}"
    if found > TESTED_UP_TO:
        return Check(NOTE, f"{text}: newer than CloseDesk has been tried on ({TESTED_UP_TO[0]}.{TESTED_UP_TO[1]})")
    return Check(OK, text)


def _optional(settings) -> list[Check]:
    import importlib.util

    from controller_inbox import ocr, outlook_desktop, vision

    found = []
    engine = ocr.engine_name()
    if engine:
        found.append(Check(OK, f"Scanned pages and pictures: read with {engine}"))
    elif sys.version_info[:2] >= (3, 13):
        found.append(
            Check(
                NOTE,
                "Scanned pages and pictures: not read (the OCR add-on isn't built for this Python yet)",
                "It works on Python 3.11 and 3.12: install Python 3.12, delete the .venv folder and open CloseDesk again. Or install Tesseract.",
            )
        )
    else:
        found.append(Check(NOTE, "Scanned pages and pictures: not read (the OCR add-on couldn't be installed here)"))
    if not vision.can_render():
        found.append(Check(NOTE, "Page pictures: not drawn (pypdfium2 or Pillow is missing)"))
    if importlib.util.find_spec("camelot") is None:
        found.append(Check(NOTE, "PDF tables: read by the built-in reader only (the second reader, Camelot, isn't installed)"))
    if sys.platform == "win32":
        why = outlook_desktop.available()
        found.append(Check(NOTE, f"Classic Outlook: {why}") if why else _outlook_sync(settings))
    try:
        from controller_inbox.local_llm import check_model

        status = check_model(settings, timeout=2, use_cache=False)
        found.append(Check(OK, f"Local model: {status.model}") if status.active else Check(NOTE, "Local model: none running (optional; CloseDesk files mail without one)"))
    except Exception:  # a model server answering oddly is not a set-up problem
        found.append(Check(NOTE, "Local model: couldn't be asked (optional)"))
    try:
        free = shutil.disk_usage(settings.data_dir).free
        if free < 1_000_000_000:
            found.append(Check(NOTE, f"Disk: only {free // 1_000_000} MB free", "Free some space: attachments are kept as they came."))
    except OSError:
        pass
    return found


def _outlook_sync(settings) -> Check:
    """Classic Outlook can be read here: whether it is connected (Settings → Sync inbox), its calendar too, and when
    it was last synced."""
    from controller_inbox import outlook_sync
    from controller_inbox.clock import apply_saved_timezone, format_when
    from controller_inbox.store import Store

    try:
        store = Store(settings.db_path)
        apply_saved_timezone(settings, store)  # the time zone chosen in Setup, as the app shows times
        cfg = outlook_sync.config(store)
        last = store.get_state(outlook_sync.LAST_SYNC_KEY) or ""
        calendar = store.get_state(outlook_sync.CALENDAR_LAST_KEY) or ""
        failed = outlook_sync.last_failure(store)
    except Exception:
        return Check(OK, "Classic Outlook: can be read on this computer")
    if not cfg["consent"]:
        return Check(OK, "Classic Outlook: can be read on this computer; not connected (Settings, Sync inbox)")
    if failed:
        # Every sync since may have failed: said here too, not only on the Settings card.
        return Check(NOTE, f"Classic Outlook: last sync didn't work ({format_when(failed.get('at', ''), settings.tz)}): {failed['error']}",
                     "Open classic Outlook, then click Sync now in Settings, Sync inbox.")
    said = f"connected, last synced {format_when(last, settings.tz)}" if last else "connected, not synced yet"
    if cfg["calendar"]:
        said += f"; calendar read {format_when(calendar, settings.tz)}" if calendar else "; calendar on, not read yet"
    return Check(OK, f"Classic Outlook: {said}")


def checks(*, port: bool = True, settings=None) -> list[Check]:
    """Everything looked at, in order. Asked from the dashboard itself (Settings), ``settings`` are the ones it
    runs with, and ``port`` False leaves its port out: it would only find itself there."""
    found = [Check(OK, f"CloseDesk: {version.describe()}"), _python()]
    if settings is None:
        settings, read = _settings()
        found.append(read)
        if settings is None:
            return found
    else:
        found.append(Check(OK, "Settings: read"))
    folders = _folders(settings)
    found.append(folders)
    if folders.state != PROBLEM:
        found.append(_database(settings))
    if port:
        found.append(_port(settings))
    found.extend(_optional(settings))
    return found


def report(found: list[Check]) -> str:
    marks = {OK: "  ok   ", NOTE: "  note ", PROBLEM: "  FIX  "}
    lines = []
    for check in found:
        lines.append(marks[check.state] + check.text)
        if check.todo:
            lines.append("         -> " + check.todo)
    problems = sum(1 for check in found if check.state == PROBLEM)
    lines.append("")
    lines.append("Everything CloseDesk needs is in order." if not problems else f"{problems} thing{'' if problems == 1 else 's'} to put right (marked FIX).")
    return "\n".join(lines)


def main() -> int:
    found = checks()
    # A Windows console writes in its own code page: a character it lacks is shown as "?" rather than stopping the
    # check with an error.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):
            pass
    print(report(found))
    return 1 if any(check.state == PROBLEM for check in found) else 0
