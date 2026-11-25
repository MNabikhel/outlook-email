"""Which CloseDesk this copy is. A ZIP downloaded from GitHub carries its commit and date in ``.git_archival.txt``
(written by GitHub: see .gitattributes); a developer's checkout doesn't, and says so.

A download's version number is its date, year first: the copy of November 16, 2026 is "2026.11.16", and is
shown with the first seven characters of its commit, "CloseDesk 2026.11.16 (e8926db)". The date is the UTC one, as
GitHub gives it for the newest version: a commit made at 22:12 at -06:00 on November 12 is "2026.11.13" on both sides.

``installed`` is the version of the files in the folder; ``running`` the one an open CloseDesk started as, which it
still is after CloseDesk-Update replaced those files under it (``remember_running``, as CloseDesk opens)."""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

DEVELOPER = "a developer's copy (not a download)"


def _archival() -> dict[str, str]:
    """What GitHub wrote into this copy's .git_archival.txt ({} for a developer's checkout)."""
    found = {}
    try:
        for line in (ROOT / ".git_archival.txt").read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition(":")
            if "$" not in value:
                found[name.strip()] = value.strip()
    except OSError:
        pass
    return found


def installed() -> tuple[str, str]:
    """(commit, date) the files in this copy's folder were downloaded at, or ("", "") when it isn't a download."""
    found = _archival()
    return found.get("commit", ""), utc_day(found.get("date", ""))


def installed_at() -> str:
    """When this copy's commit was made, whole ("2026-11-25T14:50:00+00:00", its time and offset as GitHub wrote
    them), or "" when that isn't known: two versions of one day are told apart by it (``later``)."""
    return _archival().get("date", "") if installed()[0] else ""


# What this CloseDesk started as, for its folder (``remember_running``).
_running: dict[str, object] = {}


def remember_running() -> None:
    """As CloseDesk opens: the version it starts as, which it stays while it is open, whatever is put in its folder."""
    _running.clear()
    _running.update(root=str(ROOT), found=_archival())


def _started_as() -> dict[str, str]:
    if _running.get("root") == str(ROOT):
        return dict(_running["found"])  # type: ignore[arg-type]
    return _archival()  # not opened here (a program of its own: the self-check, say): the folder's


def running() -> tuple[str, str]:
    """(commit, date) of the CloseDesk running here: the version it started as (``installed`` where it didn't
    remember one)."""
    found = _started_as()
    return found.get("commit", ""), utc_day(found.get("date", ""))


def running_at() -> str:
    """``installed_at`` for the CloseDesk running here."""
    found = _started_as()
    return found.get("date", "") if found.get("commit") else ""


def moment(date: str) -> datetime | None:
    """The moment a date with its time and offset says ("2026-11-12T22:12:03-06:00", "2026-11-13T04:12:03Z"); None
    for a day alone, or what isn't a date."""
    date = (date or "").strip()
    if "T" not in date:
        return None
    try:
        when = datetime.fromisoformat(date.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


def later(theirs: str, mine: str) -> bool | None:
    """Whether the commit dated ``theirs`` was made after the one dated ``mine``: by the moment when both say theirs,
    else by the day (UTC). None when either isn't a date."""
    at_theirs, at_mine = moment(theirs), moment(mine)
    if at_theirs is not None and at_mine is not None:
        return at_theirs > at_mine
    day_theirs, day_mine = utc_day(theirs), utc_day(mine)
    if not day_theirs or not day_mine:
        return None
    return day_theirs >= day_mine


def utc_day(date: str) -> str:
    """"2026-11-13" for a date with its time and offset ("2026-11-12T22:12:03-06:00" or "2026-11-13T04:12:03Z"),
    the day in UTC; a day alone ("2026-11-13") as it is; "" when it isn't a date."""
    date = (date or "").strip()
    try:
        when = datetime.fromisoformat(date.replace("Z", "+00:00"))
    except ValueError:
        found = re.match(r"(\d{4}-\d{2}-\d{2})", date)
        return found.group(1) if found else ""
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc)
    return when.date().isoformat()


def number(date: str) -> str:
    """The version number for a download's date: "2026-11-16" (or "2026-11-16T09:30:00Z") is "2026.11.16", the day
    in UTC; "" when the date isn't one."""
    found = utc_day(date)
    return found.replace("-", ".") if found else ""


def short(commit: str, date: str) -> str:
    """"2026.11.16 (e8926db)" for a commit and its date; the commit alone when there is no date."""
    if not commit:
        return DEVELOPER
    found = number(date)
    return f"{found} ({commit[:7]})" if found else f"version {commit[:7]}"


def describe() -> str:
    """The version running, as "2026.11.16 (e8926db)", or that it is a developer's copy."""
    return short(*running())


def label() -> str:
    """"CloseDesk 2026.11.16 (e8926db)" (the version running), or "CloseDesk, a developer's copy (not a download)"."""
    commit, date = running()
    return f"CloseDesk {short(commit, date)}" if commit else f"CloseDesk, {DEVELOPER}"


def whose() -> dict[str, str]:
    """Whose CloseDesk this is, as /health says it to a CloseDesk starting on the same computer: {"folder": its
    folder's fingerprint, "user": the fingerprint of the person running it}. Every person on a shared computer (Remote
    Desktop, fast user switching) has the same 127.0.0.1, and one computer can have two CloseDesk folders: only one
    that is this folder's, run by this person, is shown instead of starting this one. Fingerprints, not the folder or
    the name: any program on the computer can ask."""
    return {"folder": _fingerprint(os.path.normcase(str(ROOT))), "user": _fingerprint(_person())}


def _person() -> str:
    """Who runs this CloseDesk: the account's number (macOS, Linux), or its domain and name (Windows)."""
    if os.name != "nt":
        return f"uid {os.getuid()}"
    import getpass

    try:
        name = getpass.getuser()
    except Exception:  # no name to be found
        name = os.environ.get("USERPROFILE", "")
    return f"{os.environ.get('USERDOMAIN', '')}\\{name}".lower()


def _fingerprint(text: str) -> str:
    return hashlib.sha256(f"closedesk\n{text}".encode("utf-8", "replace")).hexdigest()[:16]


def is_mine(health: dict) -> bool:
    """Whether a CloseDesk's /health answer is this folder's CloseDesk, run by this person. An older CloseDesk, which
    doesn't say whose it is, is taken to be this one, as before."""
    said = {key: health.get(key) for key in ("folder", "user")}
    if not any(said.values()):
        return True
    return said == whose()
