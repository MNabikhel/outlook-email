"""Which CloseDesk this copy is. A ZIP downloaded from GitHub carries its commit and date in ``.git_archival.txt``
(written by GitHub: see .gitattributes); a developer's checkout doesn't, and says so."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


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


def describe() -> str:
    commit, date = installed()
    if not commit:
        return "a developer's copy (not a download)"
    return f"the version of {date} ({commit[:7]})" if date else f"version {commit[:7]}"
