"""Classic Outlook on this Windows computer, read only.

CloseDesk reads the mail Outlook already has, through Outlook's own programming interface (COM), the way any
add-in or script on the computer can: no Microsoft 365 app, no IT approval, nothing leaves the computer. It only
ever *reads*: it lists folders, reads a folder's table of messages, finds a message by its id and saves a copy of
it as a .msg file (``SaveAs``, which writes the file and leaves the message as it is). It never calls ``Save``,
``Delete``, ``Move``, ``Display``, ``Send`` or ``Close``, and never sets a property, so a message's folder, read
state, flag and categories in Outlook stay exactly as they were. ``tests/test_outlook_desktop.py`` checks this file
for those calls.

Outlook for Windows must be the classic one (the "New Outlook" has no such interface). If Outlook isn't running,
Windows starts it in the background when CloseDesk asks for it.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# Outlook's numbers for what is read here.
_INBOX, _DELETED, _JUNK = 6, 3, 23
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
        inbox = self.ns.GetDefaultFolder(_INBOX)
        default_store = str(inbox.StoreID)
        inbox_id = str(inbox.EntryID)
        trash: set[str] = set()
        for kind in (_DELETED, _JUNK):
            try:
                trash.add(str(self.ns.GetDefaultFolder(kind).EntryID))
            except Exception:
                pass
        found: list[FolderInfo] = []
        stores = [self.ns.Stores.Item(i) for i in range(1, self.ns.Stores.Count + 1)]
        stores.sort(key=lambda store: str(store.StoreID) != default_store)
        names: set[str] = set()
        for store in stores:
            try:
                if getattr(store, "ExchangeStoreType", None) == _PUBLIC_FOLDERS:
                    continue  # can hold thousands of folders, none of them anyone's mail
                store_id = str(store.StoreID)
                root = store.GetRootFolder()
            except Exception:  # a mailbox that is offline or no longer shared
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
        filtered on the Message-ID, so nothing is opened."""
        if not message_id:
            return []
        quoted = message_id.replace("'", "''")
        where = f'@SQL="{_MESSAGE_ID}" = \'{quoted}\''
        found: list[Seen] = []
        for info in self.folders():
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
                continue
        return found

    def scan(self, path: str, since: datetime) -> list[Seen]:
        """The messages in one folder received since ``since``, newest first, read from the folder's table (fast,
        and nothing is opened)."""
        self.folders()
        folder = self._by_path.get(path)
        if folder is None:
            raise OutlookError(f"The folder {path} isn't in Outlook any more.")
        store_id = str(folder.StoreID)
        table = folder.GetTable("", 0)  # olUserItems
        table.Columns.RemoveAll()
        for column in _COLUMNS:
            table.Columns.Add(column)
        table.Sort("[ReceivedTime]", True)
        cutoff = _utc(since)
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


def local_time(value) -> datetime | None:
    """A time from an Outlook table, in UTC. Outlook gives this computer's local time (pywin32 labels it UTC, or not
    at all), so its clock reading is taken as local. Outlook's "no date" (year 4501) is None."""
    if value is None or not isinstance(value, datetime) or value.year >= 4500:
        return None
    naive = value.replace(tzinfo=None)
    return naive.astimezone(timezone.utc)


def _utc(when: datetime) -> datetime:
    return when.astimezone(timezone.utc) if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _not_found(exc: Exception) -> bool:
    codes = {getattr(exc, "hresult", None)}
    info = getattr(exc, "excepinfo", None) or (exc.args[2] if len(getattr(exc, "args", ())) > 2 else None)
    if isinstance(info, tuple) and len(info) > 5:
        codes.add(info[5])
    return any(code in _NOT_FOUND for code in codes if code is not None)


def _said(exc: Exception) -> str:
    info = exc.args[2] if len(getattr(exc, "args", ())) > 2 else None
    if isinstance(info, tuple) and len(info) > 2 and info[2]:
        return str(info[2]).strip()[:200]
    return (str(exc) or type(exc).__name__)[:200]
