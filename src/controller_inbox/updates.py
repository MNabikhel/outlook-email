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
asks again the next time CloseDesk opens. Offline, nothing is asked and nothing is in the way: what a later asking
finds (the retry a minute on, the daily one) is said in Settings and the top bar, never in a question popping up
later in the run.

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

# The newest commit on main, as GitHub's list of commits one long: its sha, date and message, without what it changed
# (GitHub's answer for the one commit carries every changed file's patch, hundreds of kilobytes for a merge).
LATEST_URL = "https://api.github.com/repos/MNabikhel/outlook-email/commits?sha=main&per_page=1"
# The exit code that asks the launcher to update CloseDesk and start it again (76 is "make the environment again").
UPDATE_AND_RESTART = 75
# Set by scripts/launch.py when it starts CloseDesk for a launcher, which acts on UPDATE_AND_RESTART.
LAUNCHER_ENV = "CLOSEDESK_LAUNCHER"
# Set by scripts/launch.py for the CloseDesk app (``--app``, the shortcut): no console window, so what CloseDesk says
# goes to the log named in APP_LOG_ENV (app_window.py).
APP_ENV = "CLOSEDESK_APP"
APP_LOG_ENV = "CLOSEDESK_APP_LOG"
# Set by the CloseDesk window for the CloseDesk it starts: a file written the moment that CloseDesk is to stop to update.
# It takes a few seconds to stop, and stops answering at once; the window learns from the file, so that closing it
# meanwhile asks first and the update still runs (app_window.py).
WINDOW_ASKED_ENV = "CLOSEDESK_UPDATE_ASKED_MARK"
STATE_KEY = "update_latest"
CHECK_EVERY = timedelta(days=1)
# How often the open CloseDesk looks whether a day has passed, and how long it waits after starting.
TICK_SECONDS = 3600
FIRST_TICK_SECONDS = 60
TIMEOUT_SECONDS = 10
# How long the asking as CloseDesk opens waits for GitHub (and curl after it): the question comes in that time, or not.
# The whole of the asking, however slowly an answer comes in (a slow link, a proxy that holds it).
START_TIMEOUT_SECONDS = 4
# The most an answer can be (GitHub's is a few kilobytes): more is not GitHub's answer.
LONGEST_ANSWER = 2_000_000
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
UPDATING = "CloseDesk closes, fetches the new version and opens again by itself in a minute or two. Your mail and settings are kept."
BUSY = "CloseDesk is processing mail now. It can update once that is done."
# Update now while CloseDesk read new mail by itself (as it opened, or the automatic Outlook sync): that is stopped.
READ_AGAIN = "What it was reading by itself is picked up when it opens again."
# Update now from a page shown before CloseDesk was updated (another tab, another window): the page's version is gone.
UPDATED_MEANWHILE = (
    "CloseDesk was updated meanwhile (in another tab or window), so there is nothing to update now. Reload the page to "
    "see the new version."
)
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


def _ask_github(url: str, timeout: float = TIMEOUT_SECONDS) -> Any:
    return _fetch_json(url, timeout=timeout)


def _read_within(url: str, deadline: float) -> bytes:
    """GET ``url``, the whole answer by ``deadline`` (time.monotonic()), else httpx.ReadTimeout. httpx's own timeout is
    for each step (connecting, each piece read), not for the whole: an answer that trickles in went on for minutes."""
    import httpx

    left = max(0.1, deadline - time.monotonic())
    chunks: list[bytes] = []
    size = 0
    with httpx.stream("GET", url, timeout=left, follow_redirects=True, headers=HEADERS) as reply:
        reply.raise_for_status()
        for chunk in reply.iter_bytes():
            chunks.append(chunk)
            size += len(chunk)
            if time.monotonic() > deadline:
                raise httpx.ReadTimeout(f"no whole answer in {left:.0f} seconds")
            if size > LONGEST_ANSWER:
                raise httpx.ReadError("the answer is too long to be GitHub's")
    return b"".join(chunks)


def _get_within(url: str, deadline: float) -> bytes:
    """``_read_within``, given up on at ``deadline`` (a read that waits is let go of, and ends by itself soon after)."""
    import httpx

    found: dict[str, Any] = {}

    def read() -> None:
        try:
            found["body"] = _read_within(url, deadline)
        except BaseException as exc:  # handed to the asking
            found["error"] = exc

    reader = threading.Thread(target=read, name="update-ask-read", daemon=True)
    reader.start()
    reader.join(max(0.0, deadline - time.monotonic()) + 0.05)
    if "error" in found:
        raise found["error"]
    if "body" not in found:
        raise httpx.ReadTimeout("no whole answer in time")
    return found["body"]


def _fetch_json(url: str, *, timeout: float = TIMEOUT_SECONDS) -> Any:
    """GET ``url`` as JSON, in ``timeout`` seconds in all. With Python's own certificates first; when that fails (a
    company proxy that inspects HTTPS signs with a certificate only the computer trusts), with curl, which trusts what
    the computer trusts, as CloseDesk-Update does, in the time that is left (a second at least: a certificate is
    refused at once, so curl's try is the one that counts then). With under half a second left, curl isn't tried."""
    import httpx

    started = time.monotonic()
    try:
        return json.loads(_get_within(url, started + timeout))
    except (httpx.TransportError, httpx.HTTPStatusError) as exc:
        curl = shutil.which("curl")
        rest = timeout - (time.monotonic() - started)
        if not curl or rest < 0.5:
            raise
        left = max(1, math.ceil(rest))
        # Not --compressed: the answer is a few kilobytes, and Windows' own curl from before its June 2024 update has no
        # zlib and refuses the option before asking anything.
        cmd = [curl, "-fsSL", "--max-time", str(left)]
        for name, value in HEADERS.items():
            cmd += ["-H", f"{name}: {value}"]
        # Read as it comes (GitHub's answer is UTF-8): as text, Windows would read it in its own code page, garbling
        # "→" in what's new and failing outright on "”".
        done = subprocess.run(
            [*cmd, url], capture_output=True, timeout=left + 5, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW
        )
        if done.returncode != 0:
            raise OSError(f"{exc}; curl too: {_text(done.stderr).strip() or done.returncode}") from exc
        return json.loads(done.stdout)


def _text(said) -> str:
    """What a program wrote, as text: it is UTF-8 here, or close enough to read."""
    return said.decode("utf-8", "replace") if isinstance(said, bytes) else str(said or "")


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
    before = saved
    saved = {"checked_at": now.isoformat(), "from": mine}
    try:
        asking = ask or functools.partial(_ask_github, timeout=START_TIMEOUT_SECONDS if at_start else TIMEOUT_SECONDS)
        reply = asking(LATEST_URL)
        if isinstance(reply, list):  # GitHub's list of commits, one long
            reply = reply[0] if reply else {}
        sha = str(reply["sha"]).strip().lower()
        date = str(reply["commit"]["committer"]["date"])
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or not version.number(date):
            raise ValueError(f"not a commit: {sha!r} {date!r}")
        saved.update(sha=sha, date=version.utc_day(date), title=whats_new(str(reply["commit"].get("message") or "")))
    except Exception as exc:  # no internet, a proxy, GitHub busy: said quietly in Settings, asked again tomorrow
        # (or at the next tick, after the brief asking as CloseDesk opened)
        log.info("Couldn't ask GitHub for the newest CloseDesk: %s", exc)
        if before.get("from") == mine:
            # What this copy's last answer found stays known (Settings and the top bar keep offering a newer version
            # found at 9:00 after an opening on a train at 10:00), beside "couldn't reach GitHub".
            saved.update({key: before[key] for key in ("sha", "date", "title") if before.get(key)})
        saved["failed"] = "1"
        if at_start:
            saved["retry"] = "1"
    store.set_state(STATE_KEY, json.dumps(saved))
    return saved


# A line of a commit message that only says who wrote it ("Co-Authored-By: ...", "Signed-off-by: ..."). A title such
# as "Fix: the totals" isn't one.
TRAILER = re.compile(r"[A-Za-z]+(?:-[A-Za-z]+)+: ")
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
    # A version number is the day: two from one day are told apart by their commits.
    same_day = newer and mine == theirs
    if same_day:
        mine, theirs = version.short(commit, date), version.short(newest, latest.get("date", ""))
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
        "message": (
            f"A newer CloseDesk is ready: {theirs}; you have {mine}." if same_day
            else f"A newer CloseDesk is ready ({theirs}; you have {mine})." if newer else ""
        ),
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


def job_state(snapshot: dict) -> str:
    """The job's state as far as Update now goes: "running" only for work the person asked for (Process new mail, a scan
    being read, Sync now). What CloseDesk reads by itself (its reading as it opens, the automatic Outlook sync) doesn't
    hold the update back: it is stopped, and what it hadn't finished is picked up when CloseDesk opens again, as when
    CloseDesk is closed. With a local model that reading takes many minutes at every opening."""
    state = str(snapshot.get("state") or "")
    if state == "running" and (snapshot.get("about") or {}).get("auto"):
        return "idle"
    return state


def updating_message(app_state) -> str:
    """What Update now answers: CloseDesk updates, and, when it stopped what it was reading by itself, that that is
    picked up when it opens again."""
    return f"{UPDATING} {READ_AGAIN}" if getattr(app_state, "update_cut_reading", False) else UPDATING


def refusal(job_state: str) -> str:
    """Why CloseDesk can't stop to update now ("" when it can)."""
    if not version.installed()[0]:
        return UPDATE_DEVELOPER
    if not launcher_here():
        return NO_LAUNCHER
    if job_state == "running":
        return BUSY
    return ""


def stop_to_update(app_state, job_state: str, *, shown: str = "") -> str:
    """Settings' Update button: ask the server to stop once this request is answered, so that the launcher updates
    CloseDesk and starts it again (``cli._serve`` then returns UPDATE_AND_RESTART). Why it can't ("" when asked).
    ``shown``: the commit the page that asked showed; another one means CloseDesk was updated since that page loaded."""
    mine = version.installed()[0]
    if shown and mine and shown.strip().lower() != mine.lower():
        return UPDATED_MEANWHILE
    question = getattr(app_state, "update_question", None)
    why = refusal(job_state)
    if why:
        if why == BUSY and question is not None:
            question.hold()
        return why
    stop = getattr(app_state, "stop_server", None)
    if stop is None:  # served some other way than ``cli._serve``: nothing would start it again
        return NO_LAUNCHER
    # From here no new work starts (the automatic Outlook sync, say), so none is cut off when CloseDesk stops. Work
    # that started after the page looked holds the update back.
    job = getattr(app_state, "job", None)
    if job is not None and not job.close(stopping_automatic=True):
        if question is not None:
            question.hold()
        return BUSY
    # What CloseDesk was reading by itself was asked to stop (job.close): it is picked up when CloseDesk opens again.
    app_state.update_cut_reading = job is not None and job.snapshot()["state"] == "running"
    app_state.update_asked = True
    # Asked for (from Settings, say): the question isn't put to the page that waits for CloseDesk to stop.
    if question is not None:
        question.answer()
    _tell_the_window()
    stop()
    return ""


def _tell_the_window() -> None:
    """Started by the CloseDesk window: it is told CloseDesk stops to update, in the file it named (``WINDOW_ASKED_ENV``),
    before CloseDesk stops answering."""
    mark = os.environ.get(WINDOW_ASKED_ENV, "")
    if not mark:
        return
    try:
        os.makedirs(os.path.dirname(mark) or ".", exist_ok=True)
        with open(mark, "w", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()}\n")
    except OSError:  # the window learns from the exit code instead, once CloseDesk has stopped
        log.warning("Couldn't tell the window CloseDesk stops to update", exc_info=True)


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
# The app has no console window: what went wrong is in its log.
UNFINISHED_IN_APP = "The update didn't finish, so CloseDesk opened as it was. Your mail and settings are kept; what went wrong is written in {log}."
DONE = "The mail is done: CloseDesk can update now."
# Update now found nothing newer to download: GitHub's list of changes names a newer version before its download has it.
NEWEST_TITLE = "No newer version to download yet"
NEWEST_SAID = (
    "GitHub lists a newer CloseDesk, but its download is still the version you have (it can be a few minutes behind), "
    "so CloseDesk opened as it was, with your mail and settings. Try Update in Settings again in a few minutes, or "
    "let CloseDesk ask the next time it opens."
)
# The commit CloseDesk was when it stopped to update: opened again as the same one, the update didn't finish.
ASKED_FROM_KEY = "update_asked_from"
# Unless the update found this copy the newest already, and said so in this file in .venv (launch.py's NEWEST_NOTE).
NEWEST_NOTE = "closedesk-newest.txt"
# How long after CloseDesk opens an answer to the asking may come and still be put to the person: the asking's few
# seconds, and curl's second after them. One that comes later (a computer too busy to ask in time) would pop up over
# what the person started meanwhile: it is for Settings and the top bar. (The first page asks for the answer for half
# a minute, static/ui/update_ask.js, so a page that loads late still gets one that came in time.)
ASK_WAIT_SECONDS = START_TIMEOUT_SECONDS + 4


class Question:
    """Whether to update, asked once each time CloseDesk opens: ``begin`` asks GitHub behind the page as it opens,
    and the first page shown (in either UI) asks the person when a newer version is out, until they answer. Not now
    is asked again only the next time CloseDesk opens (a new Question), not on each page. Asked only when a launcher
    started CloseDesk, which starts the new version; and said when an update asked for before didn't finish.

    What is asked is what that asking as CloseDesk opened found, kept here: a newer version found later in the run
    (the retry after an asking that got no answer, the daily asking) is for Settings and the top bar, not a question
    on whatever page is opened next."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._begun = False  # nothing is asked of a page served without CloseDesk opening (a test's, say)
        self._checking = False
        self._answered = False
        self._failed = False
        self._newest = False
        self._found: dict[str, Any] | None = None  # status() once the asking as CloseDesk opened answered, in time
        self._held = False  # Update now was refused while mail was processed: said done when it is

    def begin(self, store, settings, *, failed: bool = False, newest: bool = False) -> None:
        """CloseDesk is opening: ask GitHub (``START_TIMEOUT_SECONDS`` at most) in the background, so the page opens
        meanwhile. ``failed``: the update asked for before CloseDesk stopped didn't finish; ``newest``: it found
        nothing newer to download (``after_update``), so the same question isn't asked again this time."""
        self._failed = failed
        self._newest = newest and not failed
        self._begun = True
        if not asks(settings):
            return
        self._checking = True
        started = time.monotonic()

        def ask() -> None:
            found = None
            try:
                # Unanswered (offline, GitHub slow): no question, even about a version found before, which Settings
                # and the top bar still offer.
                if not check(store, settings, at_start=True).get("failed"):
                    found = status(store, settings)
            except Exception:  # nothing in the way: no question this time
                log.warning("Looking for a newer CloseDesk as it opened failed", exc_info=True)
            finally:
                if time.monotonic() - started > ASK_WAIT_SECONDS:
                    log.info("GitHub answered after the first page stopped waiting: nothing is asked this time.")
                    found = None
                # Both at once: a page looking in between would read "done, nothing found" and not ask again.
                with self._lock:
                    self._found = found
                    self._checking = False

        threading.Thread(target=ask, name="update-check-start", daemon=True).start()

    def checking(self) -> bool:
        """Whether GitHub is still being asked (the page asks again shortly)."""
        with self._lock:
            return self._checking

    def answer(self) -> None:
        """Not now (or OK): not asked again until CloseDesk opens again."""
        with self._lock:
            self._answered = True

    def hold(self) -> None:
        """Update now was refused while mail is processed: the page asked next (the classic Today page loads again
        when the mail is done) says it can update now, once it can."""
        with self._lock:
            self._held = True

    def view(self, store, settings, job_state: str) -> dict[str, Any]:
        """What the page asks, if anything: ``ask`` when it should, ``checking`` while the answer isn't in yet."""
        here = launcher_here()
        with self._lock:
            open_ = here and self._begun and not self._answered
            shown = self._found or {}
            held = self._held
            checking = open_ and self._checking
        newest = open_ and self._newest
        # Just after Update now found nothing newer to download, the same question would only lead there again.
        newer = bool(not checking and shown.get("newer")) and not newest
        failed = open_ and self._failed
        return {
            "ask": bool(open_ and not checking and (newer or failed or newest)),
            "checking": checking,
            "newer": newer,
            "failed": failed,
            "title": UNFINISHED_TITLE if failed else NEWEST_TITLE if newest else ASK_TITLE,
            "failed_message": unfinished_message() if failed else "",
            "note": NEWEST_SAID if newest else "",
            "message": f"CloseDesk {shown['latest']} is out; you have {shown['mine']}." if newer else "",
            "whats_new": shown.get("whats_new", "") if newer else "",
            "how": ASK_HOW,
            # Update now while mail is processed is refused with BUSY; DONE says it can be done once that's over
            # (``held``: it was refused so, on this page or one before it).
            "busy": job_state == "running",
            "held": held,
            "busy_message": BUSY,
            "done_message": DONE,
            # Update now from a page shown before CloseDesk was updated (another tab): refused so, and not offered again.
            "meanwhile_message": UPDATED_MEANWHILE,
            "updating": UPDATING,
            # The version the page shows: Update now says it, and is refused once CloseDesk was updated since.
            "commit": version.installed()[0],
        }


def unfinished_message() -> str:
    """Why CloseDesk opened again as it was, and where to read what went wrong: the launcher's window, or the app's log."""
    log = os.environ.get(APP_LOG_ENV, "") if os.environ.get(APP_ENV) == "1" else ""
    return UNFINISHED_IN_APP.format(log=log) if log else UNFINISHED


def remember_update_asked(store) -> None:
    """CloseDesk stops to update (Settings' or the question's Update button): which version it was."""
    store.set_state(ASKED_FROM_KEY, version.installed()[0])


def update_unfinished(store) -> bool:
    """Whether CloseDesk stopped to update and opened again as the same version (``after_update``'s "failed")."""
    return after_update(store) == "failed"


def after_update(store) -> str:
    """What became of the update CloseDesk last stopped for, read once as it opens again: "failed" (it opened again as
    the same version: no internet, a file in use, the window closed meanwhile), "newest" (the update found this copy
    the newest download already, and said so in ``NEWEST_NOTE``: nothing went wrong, GitHub's download is behind its
    list of changes), or "" (updated, or no update asked for). While the update still runs in CloseDesk's folder
    (CloseDesk opened meanwhile), nothing is read: it is for the start after that update."""
    from controller_inbox.app_window import update_running

    before = store.get_state(ASKED_FROM_KEY) or ""
    if before and update_running(version.ROOT):
        return ""
    if before:
        store.set_state(ASKED_FROM_KEY, "")
    note = version.ROOT / ".venv" / NEWEST_NOTE
    try:
        newest = note.read_text(encoding="utf-8").strip().lower()
    except OSError:
        newest = ""
    else:
        try:
            note.unlink()
        except OSError:
            pass
    mine = version.installed()[0]
    if not (before and mine) or before != mine:
        return ""
    return "newest" if newest == mine.lower() else "failed"
