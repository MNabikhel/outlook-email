"""Whether a newer CloseDesk is out, and updating from inside CloseDesk.

Once a day at most, while CloseDesk is open, it asks GitHub for the newest commit on main: one request, and
nothing about the mail is in it. The answer (its commit and date) is kept in the database, and Settings says
when it is newer than this copy. Only a downloaded copy asks; a developer's checkout is updated with git.
``CONTROLLER_INBOX_UPDATE_CHECK=false`` in .env turns the asking off.

Settings' Update button stops CloseDesk with ``UPDATE_AND_RESTART``. The launcher that started it (CloseDesk.command
or CloseDesk.bat) then fetches the newest files, as CloseDesk-Update does, and starts CloseDesk again. Started any
other way, nothing would start it again, so the button then says to double-click CloseDesk-Update instead.
"""

from __future__ import annotations

import json
import logging
import os
import re
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

EXPLAIN = "Once a day, while CloseDesk is open, it asks GitHub for the newest version's number; nothing about your mail is sent."
TURN_OFF = "To stop it asking, put CONTROLLER_INBOX_UPDATE_CHECK=false in the .env file in CloseDesk's folder."
DEVELOPER = "This is a developer's copy (not a download): update it with git. It doesn't look for newer versions."
CHECK_OFF = "Looking for newer versions is turned off (CONTROLLER_INBOX_UPDATE_CHECK=false in .env)."
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


def known(store) -> dict[str, str]:
    """What the last asking found: {"sha", "date", "checked_at"}, any of them missing when not known."""
    try:
        saved = json.loads(store.get_state(STATE_KEY) or "{}")
    except ValueError:
        return {}
    return {key: str(value) for key, value in saved.items() if key in {"sha", "date", "checked_at"}} if isinstance(saved, dict) else {}


def _ask_github(url: str) -> dict[str, Any]:
    import httpx

    reply = httpx.get(
        url,
        timeout=TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "CloseDesk"},
    )
    reply.raise_for_status()
    return reply.json()


def check(store, settings, *, now: datetime | None = None, ask: Callable[[str], dict] | None = None) -> dict[str, str]:
    """Ask GitHub for the newest commit on main when a day has passed since the last asking (one that didn't get
    an answer counts: it is not asked again and again while the internet is down). What is known after it."""
    if not asks(settings):
        return {}
    now = now or datetime.now(timezone.utc)
    saved = known(store)
    try:
        last = datetime.fromisoformat(saved.get("checked_at", ""))
    except ValueError:
        last = None
    if last is not None and last.tzinfo is not None and timedelta(0) <= now - last < CHECK_EVERY:
        return saved
    saved["checked_at"] = now.isoformat()
    try:
        reply = (ask or _ask_github)(LATEST_URL)
        sha = str(reply["sha"]).strip().lower()
        date = str(reply["commit"]["committer"]["date"])
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or not version.number(date):
            raise ValueError(f"not a commit: {sha!r} {date!r}")
        saved.update(sha=sha, date=date[:10])
    except Exception as exc:  # no internet, a proxy, GitHub busy: said nowhere, asked again tomorrow
        log.info("Couldn't ask GitHub for the newest CloseDesk: %s", exc)
    store.set_state(STATE_KEY, json.dumps(saved))
    return saved


def launcher_here() -> bool:
    """Whether a launcher started this CloseDesk, and so starts it again after an update."""
    return os.environ.get(LAUNCHER_ENV) == "1"


def status(store, settings) -> dict[str, Any]:
    """This copy, and whether a newer one is ready, as Settings and the top bar show it."""
    commit, date = version.installed()
    on = bool(getattr(settings, "update_check", True))
    latest = known(store) if commit and on else {}
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
        "launcher": launcher_here(),
        "by_hand": BY_HAND,
    }


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
    app_state.update_asked = True
    stop()
    return ""


def start_checking(store, settings) -> None:
    """While CloseDesk is open: ask once a day at most, the first time a minute after it opens (not while the
    page and the new mail are still loading)."""
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
