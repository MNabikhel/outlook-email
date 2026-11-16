"""Which CloseDesk this copy is. A ZIP downloaded from GitHub carries its commit and date in ``.git_archival.txt``
(written by GitHub: see .gitattributes); a developer's checkout doesn't, and says so.

A download's version number is its date, year first: the copy of November 16, 2026 is "2026.11.16", and is
shown with the first seven characters of its commit, "CloseDesk 2026.11.16 (e8926db)"."""

from __future__ import annotations

import re
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
    return found.get("commit", ""), found.get("date", "")


def number(date: str) -> str:
    """The version number for a download's date: "2026-11-16" (or "2026-11-16T09:30:00Z") is "2026.11.16"; ""
    when the date isn't one."""
    found = re.match(r"\s*(\d{4})-(\d{2})-(\d{2})", date or "")
    return ".".join(found.groups()) if found else ""


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
