"""Which CloseDesk this copy is. A ZIP downloaded from GitHub carries its commit and date in ``.git_archival.txt``
(written by GitHub: see .gitattributes); a developer's checkout doesn't, and says so.

A download's version number is its date, year first: the copy of November 16, 2026 is "2026.11.16", and is
shown with the first seven characters of its commit, "CloseDesk 2026.11.16 (e8926db)". The date is the UTC one, as
GitHub gives it for the newest version: a commit made at 22:12 at -06:00 on November 12 is "2026.11.13" on both sides."""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

DEVELOPER = "a developer's copy (not a download)"


def installed() -> tuple[str, str]:
    """(commit, date) this copy was downloaded at, or ("", "") when it isn't a download."""
    found = {}
    try:
        for line in (ROOT / ".git_archival.txt").read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition(":")
            if "$" not in value:
                found[name.strip()] = value.strip()
    except OSError:
        pass
    return found.get("commit", ""), utc_day(found.get("date", ""))


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
    """This copy's version, as "2026.11.16 (e8926db)", or that it is a developer's copy."""
    return short(*installed())


def label() -> str:
    """"CloseDesk 2026.11.16 (e8926db)", or "CloseDesk, a developer's copy (not a download)"."""
    commit, date = installed()
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
