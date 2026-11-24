"""Whether a newer CloseDesk is out, and updating from inside CloseDesk.

When CloseDesk opens, and once a day at most while it is open, it asks GitHub for the newest commit on main: one
request, and nothing about the mail is in it. The answer (its commit, date and title) is kept in the database with
the copy that asked, and Settings says when it is newer than this copy. Once this copy is updated, the answer was
for the copy before: it isn't offered. When GitHub can't be reached, Settings says so, quietly. Only a downloaded
copy asks; a developer's checkout is updated with git. ``CONTROLLER_INBOX_UPDATE_CHECK=false`` in .env turns the
asking off.

The asking as CloseDesk opens waits a few seconds at most, behind the page, and its answer is the question the
first page shown asks (``Question``): "A new version of CloseDesk is ready", with Update now and Not now. It is
asked once each time CloseDesk opens, only when a launcher started it (it starts the new version), and Not now
asks again the next time CloseDesk opens. Offline, nothing is asked and nothing is in the way.

Settings' Update button stops CloseDesk with ``UPDATE_AND_RESTART``. The launcher that started it (CloseDesk.command
or CloseDesk.bat) then fetches the newest files, as CloseDesk-Update does, and starts CloseDesk again. Started any
other way, nothing would start it again, so the button then says to double-click CloseDesk-Update instead.
"""

from __future__ import annotations

import functools
import json
import logging
import math
import os
import re
import shutil
import subprocess
import textwrap
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from controller_inbox import version

log = logging.getLogger(__name__)

LATEST_URL = "https://api.github.com/repos/MNabikhel/outlook-email/commits/main"
# The exit code that asks the launcher to update CloseDesk and start it again (76 is "make the environment again").
UPDATE_AND_RESTART = 75
# Set by scripts/launch.py when it starts CloseDesk for a launcher, which acts on UPDATE_AND_RESTART.
LAUNCHER_ENV = "CLOSEDESK_LAUNCHER"
STATE_KEY = "update_latest"
CHECK_EVERY = timedelta(days=1)
# How often the open CloseDesk looks whether a day has passed, and how long it waits after starting.
TICK_SECONDS = 3600
FIRST_TICK_SECONDS = 60
TIMEOUT_SECONDS = 10
# How long the asking as CloseDesk opens waits for GitHub (and curl after it): the question comes in that time, or not.
START_TIMEOUT_SECONDS = 4
# A console program started without this flashes a console window up in the CloseDesk app on Windows (0 elsewhere).
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

EXPLAIN = (
    "When CloseDesk opens, and once a day while it is open, it asks GitHub for the newest version's number; "
    "nothing about your mail is sent."
)
TURN_OFF = "To stop it asking, put CONTROLLER_INBOX_UPDATE_CHECK=false in the .env file in CloseDesk's folder."
DEVELOPER = "This is a developer's copy (not a download): update it with git. It doesn't look for newer versions."
CHECK_OFF = "Looking for newer versions is turned off (CONTROLLER_INBOX_UPDATE_CHECK=false in .env)."
UNREACHED = "CloseDesk couldn't reach GitHub on {day} to look for a newer version; CloseDesk-Update in its folder still updates it."
BY_HAND = "To update, close CloseDesk and double-click CloseDesk-Update in its folder: your mail and settings are kept."
UPDATING = "Updating CloseDesk. It opens again by itself in a minute or two; your mail and settings are kept."
BUSY = "CloseDesk is working on your mail now (the bar at the top shows it). Update when it has finished."
NO_LAUNCHER = (
    "CloseDesk wasn't started by double-clicking it, so it can't start itself again after an update. "
    "Close CloseDesk, then double-click CloseDesk-Update in its folder: your mail and settings are kept."
)
UPDATE_DEVELOPER = "This is a developer's copy (not a download): update it with git (git pull)."


def asks(settings) -> bool:
    """Whether this copy looks for newer versions: a download, with the check left on."""
    return bool(getattr(settings, "update_check", True)) and bool(version.installed()[0])


KEPT = {"sha", "date", "title", "checked_at", "from", "failed", "retry"}


def known(store) -> dict[str, str]:
    """What the last asking found: {"sha", "date", "title" (the latest change's, ``whats_new``), "checked_at",
    "from" (the commit of the copy that asked), "failed" (when GitHub couldn't be reached), "retry" (when that was the
    brief asking as CloseDesk opened)}, any of them missing when not known."""
    try:
        saved = json.loads(store.get_state(STATE_KEY) or "{}")
    except ValueError:
        return {}
    return {key: str(value) for key, value in saved.items() if key in KEPT} if isinstance(saved, dict) else {}


HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "CloseDesk"}


def _ask_github(url: str, timeout: float = TIMEOUT_SECONDS) -> dict[str, Any]:
    return _fetch_json(url, timeout=timeout)


def _fetch_json(url: str, *, timeout: float = TIMEOUT_SECONDS) -> dict[str, Any]:
    """GET ``url`` as JSON, in about ``timeout`` seconds. With Python's own certificates first; when that fails (a
    company proxy that inspects HTTPS signs with a certificate only the computer trusts), with curl, which trusts what
    the computer trusts, as CloseDesk-Update does, in the time that is left (a second at least: a certificate is
    refused at once, so curl's try is the one that counts then)."""
    import httpx

    started = time.monotonic()
    try:
        reply = httpx.get(url, timeout=timeout, follow_redirects=True, headers=HEADERS)
        reply.raise_for_status()
        return reply.json()
    except (httpx.TransportError, httpx.HTTPStatusError) as exc:
        curl = shutil.which("curl")
        if not curl:
            raise
        left = max(1, math.ceil(timeout - (time.monotonic() - started)))
        cmd = [curl, "-fsSL", "--max-time", str(left)]
        for name, value in HEADERS.items():
            cmd += ["-H", f"{name}: {value}"]
        done = subprocess.run(
            [*cmd, url], capture_output=True, text=True, timeout=left + 5, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW
        )
        if done.returncode != 0:
            raise OSError(f"{exc}; curl too: {done.stderr.strip() or done.returncode}") from exc
        return json.loads(done.stdout)


def check(
    store, settings, *, now: datetime | None = None, ask: Callable[[str], dict] | None = None, at_start: bool = False
) -> dict[str, str]:
    """Ask GitHub for the newest commit on main when a day has passed since the last asking (one that didn't get
    an answer counts: it is not asked again and again while the internet is down), or when the last asking was
    by the copy before an update. ``at_start``: CloseDesk is opening, so it is asked whenever it was last asked, and
    waited on for ``START_TIMEOUT_SECONDS`` at most; when that got no answer, the daily asking tries again at its next
    tick (with longer to wait) rather than the next day. What is known after it."""
    if not asks(settings):
        return {}
    now = now or datetime.now(timezone.utc)
    mine = version.installed()[0].lower()
    saved = known(store)
    try:
        last = datetime.fromisoformat(saved.get("checked_at", ""))
    except ValueError:
        last = None
    recent = last is not None and last.tzinfo is not None and timedelta(0) <= now - last < CHECK_EVERY
    if recent and saved.get("from") == mine and not at_start and not saved.get("retry"):
        return saved
    saved = {"checked_at": now.isoformat(), "from": mine}
    try:
        asking = ask or functools.partial(_ask_github, timeout=START_TIMEOUT_SECONDS if at_start else TIMEOUT_SECONDS)
        reply = asking(LATEST_URL)
        sha = str(reply["sha"]).strip().lower()
        date = str(reply["commit"]["committer"]["date"])
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or not version.number(date):
            raise ValueError(f"not a commit: {sha!r} {date!r}")
        saved.update(sha=sha, date=version.utc_day(date), title=whats_new(str(reply["commit"].get("message") or "")))
    except Exception as exc:  # no internet, a proxy, GitHub busy: said quietly in Settings, asked again tomorrow
        log.info("Couldn't ask GitHub for the newest CloseDesk: %s", exc)
        saved["failed"] = "1"
        if at_start:
            saved["retry"] = "1"
    store.set_state(STATE_KEY, json.dumps(saved))
    return saved


# A line of a commit message that only says who wrote it ("Co-Authored-By: ...", "Signed-off-by: ...").
TRAILER = re.compile(r"[A-Za-z][A-Za-z-]*: ")
MERGED_PR = re.compile(r"Merge pull request #\d+ ")
WHATS_NEW_CHARS = 160


def whats_new(message: str) -> str:
    """The latest change's title, from its commit message: for GitHub's merge of a pull request ("Merge pull request
    #73 from ...", then the pull request's title) the title, else the first line, without a squashed one's "(#73)".
    "" when it says nothing a user would read (a merge of branches). Kept to a line, in plain characters."""
    lines = [line.strip() for line in str(message or "").splitlines() if line.strip()]
    if not lines:
        return ""
    first = lines[0]
    if MERGED_PR.match(first):
        titles = [line for line in lines[1:] if not TRAILER.match(line)]
        first = titles[0] if titles else ""
    elif first.startswith("Merge "):
        return ""
    first = re.sub(r"\s*\(#\d+\)$", "", first)
    first = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", first)  # a terminal's colour codes
    first = " ".join("".join(ch for ch in first if ch.isprintable()).split())
    return textwrap.shorten(first, WHATS_NEW_CHARS, placeholder=" …") if len(first) > WHATS_NEW_CHARS else first


def launcher_here() -> bool:
    """Whether a launcher started this CloseDesk, and so starts it again after an update."""
    return os.environ.get(LAUNCHER_ENV) == "1"


def status(store, settings) -> dict[str, Any]:
    """This copy, and whether a newer one is ready, as Settings and the top bar show it."""
    commit, date = version.installed()
    on = bool(getattr(settings, "update_check", True))
    latest = known(store) if commit and on else {}
    if latest.get("from") != commit.lower():  # asked by the copy before an update: asked again soon
        latest = {}
    newest = latest.get("sha", "")
    newer = bool(commit and newest and newest != commit.lower())
    # A copy newer than main (downloaded from another branch) is not offered an older one.
    if newer and date and latest.get("date") and latest["date"] < date:
        newer = False
    mine, theirs = version.number(date) or commit[:7], version.number(latest.get("date", "")) or newest[:7]
    if not commit:
        note = DEVELOPER
    elif not on:
        note = CHECK_OFF
    else:
        note = f"{EXPLAIN} {TURN_OFF}"
        if latest.get("failed"):
            note += " " + UNREACHED.format(day=_day(latest.get("checked_at", "")))
    return {
        "label": version.label(),
        "number": version.number(date),
        "commit": commit,
        "developer": not commit,
        "check_on": on and bool(commit),
        "note": note,
        "newer": newer,
        "latest": theirs if newer else "",
        "message": f"A newer CloseDesk is ready ({theirs}; you have {mine})." if newer else "",
        "mine": mine,
        "whats_new": latest.get("title", "") if newer else "",
        "launcher": launcher_here(),
        "by_hand": BY_HAND,
    }


def _day(stamp: str) -> str:
    """"November 16" for a saved time of asking."""
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return "the last try"
    when = when.astimezone()  # the day on this computer
    return f"{when:%B} {when.day}"


def refusal(job_state: str) -> str:
    """Why CloseDesk can't stop to update now ("" when it can)."""
    if not version.installed()[0]:
        return UPDATE_DEVELOPER
    if not launcher_here():
        return NO_LAUNCHER
    if job_state == "running":
        return BUSY
    return ""


def stop_to_update(app_state, job_state: str) -> str:
    """Settings' Update button: ask the server to stop once this request is answered, so that the launcher updates
    CloseDesk and starts it again (``cli._serve`` then returns UPDATE_AND_RESTART). Why it can't ("" when asked)."""
    why = refusal(job_state)
    if why:
        return why
    stop = getattr(app_state, "stop_server", None)
    if stop is None:  # served some other way than ``cli._serve``: nothing would start it again
        return NO_LAUNCHER
    # From here no new work starts (the automatic Outlook sync, say), so none is cut off when CloseDesk stops. Work
    # that started after the page looked holds the update back.
    job = getattr(app_state, "job", None)
    if job is not None and not job.close():
        return BUSY
    app_state.update_asked = True
    # Asked for (from Settings, say): the question isn't put to the page that waits for CloseDesk to stop.
    question = getattr(app_state, "update_question", None)
    if question is not None:
        question.answer()
    stop()
    return ""


def start_checking(store, settings) -> None:
    """While CloseDesk is open: ask once a day at most. (It asked as it opened, ``Question.begin``; when that got no
    answer in its few seconds, it is asked again a minute after CloseDesk opens.)"""
    if not asks(settings):
        return

    def tick() -> None:
        time.sleep(FIRST_TICK_SECONDS)
        while True:
            try:
                check(store, settings)
            except Exception:  # the next tick tries again
                log.warning("Looking for a newer CloseDesk failed", exc_info=True)
            time.sleep(TICK_SECONDS)

    threading.Thread(target=tick, name="update-check", daemon=True).start()


# The question asked as CloseDesk opens -------------------------------------------------------------------------

ASK_TITLE = "A new version of CloseDesk is ready"
ASK_HOW = "Updating takes a minute or two: CloseDesk closes, fetches the new version and opens again by itself. Your mail and settings are kept."
UNFINISHED_TITLE = "CloseDesk wasn't updated"
UNFINISHED = "The update didn't finish, so CloseDesk opened as it was. Your mail and settings are kept; CloseDesk's window says what went wrong."
DONE = "The mail is done: CloseDesk can update now."
# The commit CloseDesk was when it stopped to update: opened again as the same one, the update didn't finish.
ASKED_FROM_KEY = "update_asked_from"


class Question:
    """Whether to update, asked once each time CloseDesk opens: ``begin`` asks GitHub behind the page as it opens,
    and the first page shown (in either UI) asks the person when a newer version is out, until they answer. Not now
    is asked again only the next time CloseDesk opens (a new Question), not on each page. Asked only when a launcher
    started CloseDesk, which starts the new version; and said when an update asked for before didn't finish."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._begun = False  # nothing is asked of a page served without CloseDesk opening (a test's, say)
        self._checking = False
        self._answered = False
        self._failed = False

    def begin(self, store, settings, *, failed: bool = False) -> None:
        """CloseDesk is opening: ask GitHub (``START_TIMEOUT_SECONDS`` at most) in the background, so the page opens
        meanwhile. ``failed``: the update asked for before CloseDesk stopped didn't finish (``update_unfinished``)."""
        self._failed = failed
        self._begun = True
        if not asks(settings):
            return
        self._checking = True

        def ask() -> None:
            try:
                check(store, settings, at_start=True)
            except Exception:  # nothing in the way: no question this time
                log.warning("Looking for a newer CloseDesk as it opened failed", exc_info=True)
            finally:
                self._checking = False

        threading.Thread(target=ask, name="update-check-start", daemon=True).start()

    def checking(self) -> bool:
        """Whether GitHub is still being asked (the page asks again shortly)."""
        return self._checking

    def answer(self) -> None:
        """Not now (or OK): not asked again until CloseDesk opens again."""
        with self._lock:
            self._answered = True

    def view(self, store, settings, job_state: str) -> dict[str, Any]:
        """What the page asks, if anything: ``ask`` when it should, ``checking`` while the answer isn't in yet."""
        shown = status(store, settings)
        here = launcher_here()
        with self._lock:
            open_ = here and self._begun and not self._answered
        checking = open_ and self._checking
        newer = not checking and shown["newer"]
        failed = open_ and self._failed
        return {
            "ask": bool(open_ and not checking and (newer or failed)),
            "checking": checking,
            "newer": newer,
            "failed": failed,
            "title": UNFINISHED_TITLE if failed else ASK_TITLE,
            "failed_message": UNFINISHED if failed else "",
            "message": f"CloseDesk {shown['latest']} is out; you have {shown['mine']}." if newer else "",
            "whats_new": shown["whats_new"] if newer else "",
            "how": ASK_HOW,
            # Update now while mail is processed is refused with BUSY; DONE says it can be done once that's over.
            "busy": job_state == "running",
            "busy_message": BUSY,
            "done_message": DONE,
            "updating": UPDATING,
            # The page waits for CloseDesk to answer as another version once it stopped to update.
            "commit": shown["commit"],
        }


def remember_update_asked(store) -> None:
    """CloseDesk stops to update (Settings' or the question's Update button): which version it was."""
    store.set_state(ASKED_FROM_KEY, version.installed()[0])


def update_unfinished(store) -> bool:
    """Whether CloseDesk stopped to update and opened again as the same version: the update didn't finish (no internet,
    a file in use, the window closed meanwhile). Read once."""
    before = store.get_state(ASKED_FROM_KEY) or ""
    if before:
        store.set_state(ASKED_FROM_KEY, "")
    mine = version.installed()[0]
    return bool(before and mine) and before == mine
