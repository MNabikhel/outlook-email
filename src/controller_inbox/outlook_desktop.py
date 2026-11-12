"""Classic Outlook on this Windows computer, read only.

CloseDesk reads the mail Outlook already has, through Outlook's own programming interface (COM), the way any
add-in or script on the computer can: no Microsoft 365 app, no IT approval, nothing leaves the computer. It only
ever *reads*: it lists folders, reads a folder's table of messages, finds a message by its id and saves a copy of
it as a .msg file (``SaveAs``, which writes the file and leaves the message as it is). It never calls ``Save``,
``Delete``, ``Move``, ``Display``, ``Send`` or ``Close``, and never sets a property, so a message's folder, read
state, flag and categories in Outlook stay exactly as they were. ``tests/test_outlook_desktop.py`` checks that this
file calls only the reads listed there, so a call that isn't one fails until it has been looked at.

The calendar is read the same way (``calendar``): the meetings in a window, each property read off the item, nothing
saved. The one thing set is ``IncludeRecurrences`` on the list of calendar items CloseDesk asked for, which tells
Outlook to list each occurrence of a repeating meeting; it is a setting of that list, held only while it is read,
and changes nothing in Outlook. The meeting's text (where Teams links and dial-in PINs are) isn't read.

Outlook for Windows must be the classic one (the "New Outlook" has no such interface). If Outlook isn't running,
Windows starts it in the background when CloseDesk asks for it.
"""

from __future__ import annotations

import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from controller_inbox.clock import aware_utc

# Outlook's numbers for what is read here.
_INBOX, _DELETED, _JUNK, _CALENDAR = 6, 3, 23, 9
_BUSY = {0: "free", 1: "tentative", 2: "busy", 3: "away", 4: "working elsewhere"}
_CANCELLED = {5, 7}  # olMeetingCanceled, olMeetingReceivedAndCanceled
MAX_CALENDAR_ITEMS = 3000  # read at most, however the window's filter was understood
_MAIL_FOLDER = 0  # Folder.DefaultItemType for mail
_PUBLIC_FOLDERS = 2  # Store.ExchangeStoreType for Exchange Public Folders
_SAVE_AS_MSG_UNICODE = 9  # olMSGUnicode
# Message classes copied: email (signed and encrypted too) and meeting requests and replies. Read receipts,
# delivery reports, tasks and contacts are not mail to file.
MAIL_CLASSES = ("IPM.Note", "IPM.Schedule.Meeting")
_MESSAGE_ID = "http://schemas.microsoft.com/mapi/proptag/0x1035001F"  # PR_INTERNET_MESSAGE_ID
_FLAG_STATUS = "http://schemas.microsoft.com/mapi/proptag/0x10900003"  # PR_FLAG_STATUS: 2 is flagged
_COLUMNS = ("EntryID", "ReceivedTime", "UnRead", "MessageClass", "Importance", _MESSAGE_ID, _FLAG_STATUS)
# MAPI_E_NOT_FOUND: the message is gone from Outlook (deleted for good, or Deleted Items emptied).
_NOT_FOUND = (-2147221233, 0x8004010F)
_IMPORTANCE = {0: "low", 1: "normal", 2: "high"}
# RPC_E_CALL_REJECTED and RPC_E_SERVERCALL_RETRYLATER: Outlook has a window or prompt open and won't answer until it
# is closed (its own "A program is trying to access" prompt among them).
_BUSY_CODES = (-2147418111, -2147417846)
BUSY = "Outlook is busy: close any window or prompt open in Outlook (choose Allow if it asks about access), then click Sync now."


class OutlookUnavailable(RuntimeError):
    """Classic Outlook can't be reached from here: not Windows, pywin32 missing, or Outlook not installed."""


class OutlookError(RuntimeError):
    """Outlook answered with an error (busy, a security prompt was refused)."""


class NotInOutlook(OutlookError):
    """The message is no longer in Outlook."""


@dataclass
class FolderInfo:
    path: str  # "Inbox", "Inbox/Vendors", or "Mailbox name/Inbox" for a second mailbox
    store_id: str
    entry_id: str
    depth: int
    inbox: bool = False  # the default mailbox's Inbox
    trash: bool = False  # Deleted Items or Junk Email


@dataclass
class Seen:
    """One message in a folder's table, without opening it."""

    entry_id: str
    store_id: str
    folder: str
    received: datetime
    unread: bool
    flagged: bool
    importance: str
    message_id: str


class CalendarRead(list):
    """The meetings ``OutlookSession.calendar`` read, and whether it stopped at ``MAX_CALENDAR_ITEMS`` with more
    left (``cut_short``): the meetings past the last one read weren't looked at, so they aren't gone from Outlook.
    ``unreadable``: how many meetings Outlook wouldn't hand over (they aren't gone from Outlook either)."""

    def __init__(self, meetings=(), *, cut_short: bool = False, unreadable: int = 0) -> None:
        super().__init__(meetings)
        self.cut_short = cut_short
        self.unreadable = unreadable


def available() -> str:
    """"" when classic Outlook can be read on this computer; else why not, in a sentence."""
    if sys.platform != "win32":
        return "Reading Outlook directly works on Windows with classic Outlook. Use the drop folder on this computer."
    try:
        import win32com.client  # noqa: F401
    except ImportError:
        return "The pywin32 package is missing. Double-click CloseDesk again so it installs it."
    return ""


@contextmanager
def connect(*, start: bool = True) -> Iterator["OutlookSession"]:
    """Classic Outlook, for the length of the ``with``. Each thread that talks to Outlook sets COM up for itself.
    ``start``: start Outlook when it isn't running (Sync now, CloseDesk starting). The automatic sync passes False,
    so it never opens Outlook in the background (where a sign-in prompt could wait unseen); it syncs once Outlook is open."""
    why = available()
    if why:
        raise OutlookUnavailable(why)
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        try:
            if start:
                app = win32com.client.Dispatch("Outlook.Application")
            else:
                try:
                    app = win32com.client.GetActiveObject("Outlook.Application")
                except Exception:
                    raise OutlookUnavailable("Outlook isn't open, so it wasn't synced. It syncs once Outlook is open.") from None
            namespace = app.GetNamespace("MAPI")
        except OutlookUnavailable:
            raise
        except Exception as exc:
            raise OutlookUnavailable(
                "Classic Outlook didn't answer. Check that Outlook (not the New Outlook) is installed and signed in, "
                f"then try again ({_said(exc)})."
            ) from None
        yield OutlookSession(namespace)
    finally:
        pythoncom.CoUninitialize()


class OutlookSession:
    """What CloseDesk reads from Outlook. Every call here only reads."""

    def __init__(self, namespace) -> None:
        self.ns = namespace
        self._folders: list[FolderInfo] | None = None
        self._by_path: dict[str, object] = {}
        self._left_out = False  # a mailbox couldn't be opened, so the folder list isn't all of Outlook's

    def account(self) -> str:
        try:
            return str(self.ns.GetDefaultFolder(_INBOX).Parent.Name)
        except Exception:
            return ""

    def folders(self) -> list[FolderInfo]:
        """Every mail folder in every mailbox Outlook has open, the default mailbox first, each folder before its
        subfolders. A path is its folder names from the mailbox down, joined with "/"; folders of the default
        mailbox have no mailbox name in front, and two mailboxes or data files with one name are told apart by a
        number ("Outlook Data File (2)/Inbox"). A folder Outlook won't open (no permission, offline) is left out;
        the others are still listed. Deleted Items and Junk, and every folder inside them, are marked ``trash``."""
        if self._folders is not None:
            return self._folders
        try:
            inbox = self.ns.GetDefaultFolder(_INBOX)
            default_store = str(inbox.StoreID)
            inbox_id = str(inbox.EntryID)
            count = self.ns.Stores.Count
        except Exception as exc:
            raise OutlookError(trouble("Outlook's folders couldn't be listed just now", exc)) from None
        trash: set[str] = set()
        for kind in (_DELETED, _JUNK):
            try:
                trash.add(str(self.ns.GetDefaultFolder(kind).EntryID))
            except Exception:
                pass
        found: list[FolderInfo] = []
        stores: list[tuple[str, object]] = []
        for i in range(1, count + 1):
            try:
                store = self.ns.Stores.Item(i)
                if getattr(store, "ExchangeStoreType", None) == _PUBLIC_FOLDERS:
                    continue  # can hold thousands of folders, none of them anyone's mail
                stores.append((str(store.StoreID), store))
            except Exception:  # a mailbox that is offline or no longer shared, or a data file that is missing
                self._left_out = True
        stores.sort(key=lambda pair: pair[0] != default_store)
        names: set[str] = set()
        for store_id, store in stores:
            try:
                root = store.GetRootFolder()
            except Exception:  # a mailbox that is offline or no longer shared
                self._left_out = True
                continue
            # Each mailbox's own Deleted Items and Junk, whatever their names in this Outlook's language.
            bins = set(trash)
            for kind in (_DELETED, _JUNK):
                try:
                    bins.add(str(store.GetDefaultFolder(kind).EntryID))
                except Exception:
                    pass
            prefix = ""
            if store_id != default_store:
                name = str(getattr(store, "DisplayName", "") or "Mailbox")
                prefix, number = name, 2
                while prefix in names:
                    prefix, number = f"{name} ({number})", number + 1
                names.add(prefix)
            binned: list[str] = []
            for folder, path, depth in _walk(root, prefix, 0):
                try:
                    if folder.DefaultItemType != _MAIL_FOLDER:
                        continue
                    entry = str(folder.EntryID)
                    english_bin = depth == 1 and str(folder.Name).lower() in ("deleted items", "junk email", "junk e-mail")
                except Exception:
                    continue
                in_bin = entry in bins or (bool(prefix) and english_bin) or any(path.startswith(b + "/") for b in binned)
                if in_bin and not any(path.startswith(b + "/") for b in binned):
                    binned.append(path)
                found.append(FolderInfo(path, store_id, entry, depth, entry == inbox_id, in_bin))
                self._by_path[path] = folder
        self._folders = found
        return found

    def find_message(self, message_id: str) -> list[Seen]:
        """Where a message with this Internet Message-ID is now, in any folder: how a message moved to a folder that
        isn't synced, where Outlook gave it a new id, is told from one that was deleted. Read from each folder's table
        filtered on the Message-ID, so nothing is opened. Raises ``OutlookError`` when it isn't found and a folder or
        mailbox couldn't be looked in (an archive offline just now): not found there isn't gone from Outlook."""
        if not message_id:
            return []
        quoted = message_id.replace("'", "''")
        where = f'@SQL="{_MESSAGE_ID}" = \'{quoted}\''
        found: list[Seen] = []
        infos = self.folders()
        missed = self._left_out
        for info in infos:
            folder = self._by_path.get(info.path)
            try:
                table = folder.GetTable(where, 0)
                table.Columns.RemoveAll()
                for column in _COLUMNS:
                    table.Columns.Add(column)
                while not table.EndOfTable:
                    entry_id, received, unread, _cls, importance, mid, flag = table.GetNextRow().GetValues()
                    found.append(
                        Seen(str(entry_id), info.store_id, info.path, local_time(received) or datetime.now(timezone.utc),
                             bool(unread), flag == 2, _IMPORTANCE.get(importance, "normal"), str(mid or "").strip())
                    )
            except Exception:  # a folder Outlook won't open: the others are still looked in
                missed = True
        if missed and not found:
            raise OutlookError("Outlook couldn't look in every folder just now, so where a message went isn't known yet.")
        return found

    def scan(self, path: str, since: datetime) -> list[Seen]:
        """The messages in one folder received since ``since``, newest first, read from the folder's table (fast,
        and nothing is opened)."""
        self.folders()
        folder = self._by_path.get(path)
        if folder is None:
            raise OutlookError(f"The folder {path} isn't in Outlook any more.")
        try:
            return self._scan(folder, path, since)
        except Exception as exc:
            raise OutlookError(trouble(f"The folder {path} couldn't be read just now", exc)) from None

    def _scan(self, folder, path: str, since: datetime) -> list[Seen]:
        store_id = str(folder.StoreID)
        table = folder.GetTable("", 0)  # olUserItems
        table.Columns.RemoveAll()
        for column in _COLUMNS:
            table.Columns.Add(column)
        table.Sort("[ReceivedTime]", True)
        cutoff = aware_utc(since)
        seen: list[Seen] = []
        while not table.EndOfTable:
            entry_id, received, unread, message_class, importance, message_id, flag = table.GetNextRow().GetValues()
            when = local_time(received)
            if when is not None and when < cutoff:
                break  # newest first: the rest are older
            if not str(message_class or "").startswith(MAIL_CLASSES):
                continue
            seen.append(
                Seen(
                    entry_id=str(entry_id),
                    store_id=store_id,
                    folder=path,
                    received=when or datetime.now(timezone.utc),
                    unread=bool(unread),
                    flagged=flag == 2,
                    importance=_IMPORTANCE.get(importance, "normal"),
                    message_id=str(message_id or "").strip(),
                )
            )
        return seen

    def calendar(self, start: datetime, end: datetime) -> CalendarRead:
        """The meetings in the default calendar between ``start`` and ``end``, each occurrence of a repeating one,
        earliest first: subject, times (UTC; dates for all-day ones), place, organizer, attendees, busy, cancelled,
        and the series id. The meeting's text isn't read. ``cut_short`` says Outlook listed more than
        ``MAX_CALENDAR_ITEMS`` (counting those left out for ending before ``start``) and the rest weren't read."""
        try:
            items = self.ns.GetDefaultFolder(_CALENDAR).Items
            items.Sort("[Start]")
            # Must be set after Sort and before Restrict for Outlook to list each occurrence.
            items.IncludeRecurrences = True
            listed = items.Restrict(f"[End] >= '{_jet_date(start - timedelta(days=1))}' AND [Start] <= '{_jet_date(end + timedelta(days=1))}'")
        except Exception as exc:
            raise OutlookError(f"Outlook's calendar couldn't be read just now ({_said(exc)}).") from None
        low, high = aware_utc(start), aware_utc(end)
        found: list[dict] = []
        cut_short = False
        unreadable = 0
        try:
            item = listed.GetFirst()
            for _ in range(MAX_CALENDAR_ITEMS):
                if item is None:
                    break
                try:
                    meeting = _meeting(item)
                except Exception:  # Outlook wouldn't hand over its details (its access prompt refused, or busy)
                    unreadable += 1
                    meeting = None
                if meeting is not None:
                    begins = meeting.pop("_starts")
                    if begins > high:
                        break  # earliest first: the rest start later
                    if meeting.pop("_ends") >= low:
                        found.append(meeting)
                item = listed.GetNext()
            else:
                cut_short = item is not None
        except Exception as exc:
            raise OutlookError(trouble("Outlook's calendar couldn't be read just now", exc)) from None
        if unreadable and not found:
            raise OutlookError(
                "Outlook didn't let CloseDesk read your calendar. If Outlook asked whether to allow access, choose Allow; "
                "the calendar is read again with the next sync."
            )
        return CalendarRead(found, cut_short=cut_short, unreadable=unreadable)

    def save_copy(self, entry_id: str, store_id: str, dest: Path) -> None:
        """Save a copy of the message as a .msg file. The message in Outlook is not changed."""
        item = self._item(entry_id, store_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            item.SaveAs(str(dest), _SAVE_AS_MSG_UNICODE)
        except Exception as exc:
            raise OutlookError(
                "Outlook wouldn't let CloseDesk copy this message. If Outlook asked whether to allow access, choose "
                f"Allow; IT may also block programs from reading Outlook ({_said(exc)})."
            ) from None

    def locate(self, entry_id: str, store_id: str) -> str:
        """The folder the message is in now. Raises ``NotInOutlook`` when it's gone."""
        item = self._item(entry_id, store_id)
        parent = item.Parent
        self.folders()
        entry = str(parent.EntryID)
        for info in self._folders or []:
            if info.entry_id == entry:
                return info.path
        return str(parent.Name)

    def _item(self, entry_id: str, store_id: str):
        try:
            return self.ns.GetItemFromID(entry_id, store_id)
        except Exception as exc:
            if _not_found(exc):
                raise NotInOutlook(entry_id) from None
            raise OutlookError(f"Outlook couldn't find a message just now ({_said(exc)}).") from None


def _walk(folder, path: str, depth: int):
    """Every folder under ``folder``. A folder whose subfolders Outlook won't list (no permission, offline) is still
    yielded; its subfolders are left out."""
    try:
        children = folder.Folders
        count = children.Count
    except Exception:
        return
    for i in range(1, count + 1):
        try:
            child = children.Item(i)
            child_path = f"{path}/{child.Name}" if path else str(child.Name)
        except Exception:
            continue
        yield child, child_path, depth + 1
        yield from _walk(child, child_path, depth + 1)


def _meeting(item) -> dict | None:
    """One calendar item's details, or None for one with no start. Raises when Outlook won't hand them over (its
    access prompt refused, or busy): ``calendar`` counts those, so they aren't taken for deleted."""
    starts, ends = local_time(item.Start), local_time(item.End)
    if starts is None:
        return None
    ends = ends or starts
    all_day = bool(item.AllDayEvent)
    people = [p.strip()[:200] for field in (item.RequiredAttendees, item.OptionalAttendees) for p in str(field or "")[:50_000].split(";") if p.strip()]
    start = item.Start.date().isoformat() if all_day else starts.isoformat(timespec="seconds")
    end = item.End.date().isoformat() if all_day else ends.isoformat(timespec="seconds")
    return {
        "uid": str(item.GlobalAppointmentID or "") or f"{item.Subject}|{start}",
        "start": start,
        "end": end,
        "all_day": all_day,
        "subject": str(item.Subject or "")[:300],
        "location": str(item.Location or "")[:300],
        # The name Outlook shows: asking Outlook for an address can make it warn that a program is reading
        # e-mail addresses, and looks each one up in the directory.
        "organizer": str(item.Organizer or "")[:200],
        "attendees": people[:60],
        "busy": _BUSY.get(item.BusyStatus, ""),
        "cancelled": item.MeetingStatus in _CANCELLED,
        "recurring": bool(item.IsRecurring),
        "_starts": starts,
        "_ends": ends,
    }


def _jet_date(value: datetime) -> str:
    """A date the way Outlook's item filter reads it: in this computer's short date format (dd/MM/yyyy in much of
    the world), else as Outlook in the US writes it."""
    pattern = "%m/%d/%Y"
    try:
        import win32api

        written = win32api.GetLocaleInfo(0x0400, 0x1F)  # LOCALE_USER_DEFAULT, LOCALE_SSHORTDATE ("M/d/yyyy")
        pattern = _strftime_pattern(str(written)) or pattern
    except Exception:
        pass
    local = aware_utc(value).astimezone()
    return local.strftime(pattern)


# The parts of Windows' short date pattern, as strftime writes them. A month written as a name ("MMM": Oct) is
# written as its number, in the same place: Outlook reads numbers in this computer's order whatever its language.
_DATE_PARTS = {"yyyyy": "%Y", "yyyy": "%Y", "yy": "%y", "y": "%y", "MMMM": "%m", "MMM": "%m", "MM": "%m", "M": "%m", "dd": "%d", "d": "%d"}


def _strftime_pattern(windows: str) -> str:
    """Windows' short date pattern ("dd/MM/yyyy", "M/d/yyyy", "yyyy-MM-dd", "dd-MMM-yy") as a strftime one, leading
    zeros kept (Outlook reads "05/10/2026" as well as "5/10/2026"). Quoted words and the weekday's name are left out;
    a pattern with anything else in it, or without a day, month and year, gives "" (Outlook's US order)."""
    text = re.sub(r"'[^']*'|dddd?[,.]?\s*", "", windows)
    out = re.sub(r"y+|M+|d+", lambda m: _DATE_PARTS[m.group(0)] if m.group(0) in _DATE_PARTS else "?", text)
    whole = ("%Y" in out or "%y" in out) and "%m" in out and "%d" in out
    other = re.sub(r"[^A-Za-z?]", "", re.sub(r"%[Yymd]", "", out))
    return out if whole and not other else ""


def local_time(value) -> datetime | None:
    """A time from an Outlook table, in UTC. Outlook gives this computer's local time (pywin32 labels it UTC, or not
    at all), so its clock reading is taken as local. Outlook's "no date" (year 4501) is None."""
    if value is None or not isinstance(value, datetime) or value.year >= 4500:
        return None
    naive = value.replace(tzinfo=None)
    return naive.astimezone(timezone.utc)


def _codes(exc: Exception) -> set:
    """The error numbers a COM error carries: its own, and Outlook's in its details."""
    args = getattr(exc, "args", ())
    codes = {getattr(exc, "hresult", None), args[0] if args and isinstance(args[0], int) else None}
    info = getattr(exc, "excepinfo", None) or (args[2] if len(args) > 2 else None)
    if isinstance(info, tuple) and len(info) > 5:
        codes.add(info[5])
    return codes


def _not_found(exc: Exception) -> bool:
    return any(code in _NOT_FOUND for code in _codes(exc) if code is not None)


def trouble(what: str, exc: Exception) -> str:
    """Why ``what`` didn't happen, in a sentence for the user: "``what`` (Outlook's own words)." Outlook busy with a
    window or prompt open is said as that, with what to do."""
    if any(code in _BUSY_CODES for code in _codes(exc) if code is not None):
        return BUSY
    return f"{what} ({_said(exc)})."


def _said(exc: Exception) -> str:
    """Outlook's own words for an error: the text in its details, else the error's text, never its numbers."""
    args = getattr(exc, "args", ())
    info = args[2] if len(args) > 2 else None
    if isinstance(info, tuple) and len(info) > 2 and info[2]:
        return str(info[2]).strip()[:200]
    if len(args) > 1 and isinstance(args[0], int) and isinstance(args[1], str) and args[1]:
        return args[1].strip()[:200]  # a COM error without details: (number, text, None, None)
    return (str(exc) or type(exc).__name__)[:200]
