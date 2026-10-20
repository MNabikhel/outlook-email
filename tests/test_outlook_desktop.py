"""The classic Outlook reader (outlook_desktop.py) only reads: no call in it can change a message, and against fake
Outlook objects that refuse every write it lists folders, reads a folder's table, finds and copies messages."""

from __future__ import annotations

import ast
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from controller_inbox import outlook_desktop
from controller_inbox.outlook_desktop import NotInOutlook, OutlookError, OutlookSession

SOURCE = Path(outlook_desktop.__file__)

# Everything in Outlook's object model that changes, sends, opens or moves an item or folder.
WRITES = {
    "Save", "Delete", "Move", "MoveTo", "Copy", "CopyTo", "Display", "Send", "Close", "Reply", "ReplyAll", "Forward",
    "SetProperty", "SetProperties", "DeleteProperty", "DeleteProperties", "MarkAsTask", "ClearTaskFlag", "PrintOut",
    "ShowCategoriesDialog", "AddMember", "Remove", "Add",  # Columns.Add is checked apart: it only shapes the table read
    "Respond", "Accept", "Decline", "ClearConversationIndex", "Undelete", "Empty",
}


def test_the_reader_makes_no_call_that_changes_outlook():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            calls.append((node.func.attr, ast.unparse(node.func.value)))
    bad = [(name, on) for name, on in calls if name in WRITES and not (name == "Add" and on == "table.Columns")]
    assert not bad, f"outlook_desktop.py must only read Outlook: {bad}"
    # SaveAs writes a file and leaves the message as it is; it's the only way a copy is made.
    assert [on for name, on in calls if name == "SaveAs"] == ["item"]


def test_the_reader_sets_no_property_on_an_outlook_object():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, (ast.AugAssign, ast.AnnAssign)) else []
        for target in targets:
            if isinstance(target, ast.Attribute):
                assert ast.unparse(target.value) == "self", f"sets {ast.unparse(target)} (line {node.lineno})"


# Fake Outlook objects: reading works, any write raises -------------------------------------------------


class ReadOnly:
    """An Outlook object whose properties can't be set once it's made."""

    _frozen = False

    def __setattr__(self, name, value):
        if self._frozen:
            raise AssertionError(f"CloseDesk tried to set {name} on an Outlook object")
        object.__setattr__(self, name, value)

    def freeze(self):
        object.__setattr__(self, "_frozen", True)
        return self


class Collection(ReadOnly):
    def __init__(self, items):
        self._items = list(items)
        self.Count = len(self._items)
        self.freeze()

    def Item(self, index):
        return self._items[index - 1]


class Folder(ReadOnly):
    def __init__(self, name, entry, store="S1", children=(), rows=(), item_type=0):
        self.Name = name
        self.EntryID = entry
        self.StoreID = store
        self.DefaultItemType = item_type
        self.Folders = Collection(children)
        self.rows = list(rows)
        self.Parent = None
        for child in children:
            object.__setattr__(child, "Parent", self)
        self.freeze()

    def GetTable(self, where, kind):
        assert kind == 0
        if not where:
            return Table(self.rows)
        # find_message's filter: the Message-ID column equal to one value.
        wanted = re.search(r"= '(.*)'$", where).group(1).replace("''", "'")
        return Table([row for row in self.rows if row[5] == wanted])


class Columns(ReadOnly):
    def __init__(self):
        self.names = []
        self.freeze()

    def RemoveAll(self):
        self.names.clear()

    def Add(self, name):
        self.names.append(name)


class Row:
    def __init__(self, values):
        self._values = values

    def GetValues(self):
        return self._values


class Table(ReadOnly):
    def __init__(self, rows):
        self.Columns = Columns()
        self._rows = rows
        self._at = 0
        self._order = None
        self.freeze()

    @property
    def EndOfTable(self):
        return self._at >= len(self._rows)

    def Sort(self, column, descending):
        assert (column, descending) == ("[ReceivedTime]", True)
        object.__setattr__(self, "_rows", sorted(self._rows, key=lambda row: row[1], reverse=True))

    def GetNextRow(self):
        row = self._rows[self._at]
        object.__setattr__(self, "_at", self._at + 1)
        assert list(self.Columns.names) == list(outlook_desktop._COLUMNS)
        return Row(row)


class Item(ReadOnly):
    def __init__(self, parent):
        self.Parent = parent
        self.saved = []
        self.freeze()

    def SaveAs(self, path, kind):
        assert kind == 9  # .msg, Unicode
        Path(path).write_bytes(b"msg")
        self.saved.append(path)


class ComError(Exception):
    """Like pywintypes.com_error: (hresult, text, excepinfo, arg)."""


class Store(ReadOnly):
    def __init__(self, store_id, name, root, bins=None):
        self.StoreID = store_id
        self.DisplayName = name
        self._root = root
        self._bins = bins or {}  # Outlook's number for Deleted Items (3) or Junk (23) → that folder in this store
        self.freeze()

    def GetRootFolder(self):
        return self._root

    def GetDefaultFolder(self, kind):
        if kind not in self._bins:
            raise ComError(-2147352567, "Exception occurred.", (4096, "Microsoft Outlook", "No such folder.", None, 0, -2147221233), None)
        return self._bins[kind]


class Namespace(ReadOnly):
    def __init__(self, stores, defaults, items):
        self.Stores = Collection(stores)
        self._defaults = defaults
        self._items = items
        self.freeze()

    def GetDefaultFolder(self, kind):
        return self._defaults[kind]

    def GetItemFromID(self, entry_id, store_id):
        if entry_id not in self._items:
            raise ComError(-2147352567, "Exception occurred.", (4096, "Microsoft Outlook", "The item could not be found.", None, 0, -2147221233), None)
        return self._items[entry_id]


def local(year, month, day, hour, minute=0):
    """A time as Outlook hands it over: this computer's clock reading, labelled UTC by pywin32."""
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


@pytest.fixture
def outlook():
    now = datetime.now().replace(microsecond=0)
    rows = [
        ("E-new", local(*now.timetuple()[:5]), True, "IPM.Note", 2, "<new@x.com>", 2),
        ("E-receipt", local(*now.timetuple()[:5]), True, "REPORT.IPM.Note.IPNRN", 1, "", 0),
        ("E-meeting", local(*(now - timedelta(hours=1)).timetuple()[:5]), False, "IPM.Schedule.Meeting.Request", 1, "<m@x.com>", 0),
        ("E-old", local(*(now - timedelta(days=60)).timetuple()[:5]), False, "IPM.Note", 1, "<old@x.com>", 0),
    ]
    vendors = Folder("Vendors", "F-vendors")
    contacts = Folder("Contacts", "F-contacts", item_type=2)
    inbox = Folder("Inbox", "F-inbox", children=[vendors], rows=rows)
    deleted = Folder("Deleted Items", "F-deleted")
    junk = Folder("Junk Email", "F-junk")
    root = Folder("maya@taz.com", "F-root", children=[inbox, deleted, junk, contacts])
    shared_inbox = Folder("Inbox", "F-ap-inbox", store="S2")
    shared_bin = Folder("Deleted Items", "F-ap-deleted", store="S2")
    shared_root = Folder("AP Team", "F-ap-root", store="S2", children=[shared_inbox, shared_bin])
    items = {"E-new": Item(inbox), "E-archived": Item(vendors)}
    ns = Namespace(
        [Store("S2", "AP Team", shared_root), Store("S1", "maya@taz.com", root)],
        {6: inbox, 3: deleted, 23: junk},
        items,
    )
    return OutlookSession(ns), items


def test_folders_lists_every_mailbox_default_first_and_marks_the_bins(outlook):
    session, _ = outlook
    folders = {f.path: f for f in session.folders()}
    assert list(folders) == ["Inbox", "Inbox/Vendors", "Deleted Items", "Junk Email", "AP Team/Inbox", "AP Team/Deleted Items"]
    assert folders["Inbox"].inbox and not folders["AP Team/Inbox"].inbox
    assert [p for p, f in folders.items() if f.trash] == ["Deleted Items", "Junk Email", "AP Team/Deleted Items"]
    assert folders["Inbox/Vendors"].depth == 2
    assert session.account() == "maya@taz.com"


def test_scan_reads_mail_in_the_window_newest_first_without_opening_anything(outlook):
    session, _ = outlook
    seen = session.scan("Inbox", datetime.now(timezone.utc) - timedelta(days=30))
    assert [s.entry_id for s in seen] == ["E-new", "E-meeting"], "receipts are skipped and older mail ends the read"
    new = seen[0]
    assert new.unread and new.flagged and new.importance == "high" and new.message_id == "<new@x.com>"
    assert new.folder == "Inbox" and new.store_id == "S1"
    assert abs(new.received - datetime.now(timezone.utc)) < timedelta(minutes=2), "Outlook's clock reading is local time"
    with pytest.raises(OutlookError):
        session.scan("Nope", datetime.now(timezone.utc))


def test_save_copy_and_locate(outlook, tmp_path):
    session, items = outlook
    dest = tmp_path / "copies" / "a.msg"
    session.save_copy("E-new", "S1", dest)
    assert dest.read_bytes() == b"msg" and items["E-new"].saved == [str(dest)]
    assert session.locate("E-new", "S1") == "Inbox"
    assert session.locate("E-archived", "S1") == "Inbox/Vendors"
    with pytest.raises(NotInOutlook):
        session.locate("E-gone", "S1")
    with pytest.raises(NotInOutlook):
        session.save_copy("E-gone", "S1", dest)


def test_an_outlook_error_that_isnt_not_found_is_not_a_deletion():
    class Busy(Namespace):
        def GetItemFromID(self, entry_id, store_id):
            raise ComError(-2147352567, "Exception occurred.", (4096, "Microsoft Outlook", "The server is busy.", None, 0, -2147467259), None)

    session = OutlookSession(Busy([], {}, {}))
    with pytest.raises(OutlookError) as caught:
        session.locate("E1", "S1")
    assert not isinstance(caught.value, NotInOutlook) and "busy" in str(caught.value)


def test_local_time():
    assert outlook_desktop.local_time(None) is None
    assert outlook_desktop.local_time(datetime(4501, 1, 1)) is None
    when = datetime(2026, 10, 20, 9, 30)
    assert outlook_desktop.local_time(when.replace(tzinfo=timezone.utc)) == when.astimezone(timezone.utc)


@pytest.mark.skipif(outlook_desktop.sys.platform == "win32", reason="on Windows this depends on pywin32 being installed")
def test_not_windows_says_so():
    assert "Windows" in outlook_desktop.available()
    with pytest.raises(outlook_desktop.OutlookUnavailable):
        with outlook_desktop.connect():
            pass


def test_find_message_looks_in_every_folder_by_message_id(outlook):
    session, _ = outlook
    assert [(s.entry_id, s.folder) for s in session.find_message("<new@x.com>")] == [("E-new", "Inbox")]
    assert session.find_message("<nobody@x.com>") == [] and session.find_message("") == []


def test_the_automatic_sync_never_starts_outlook(monkeypatch):
    import sys
    import types

    calls = []
    client = types.SimpleNamespace(
        Dispatch=lambda name: calls.append(("Dispatch", name)),
        GetActiveObject=lambda name: (_ for _ in ()).throw(OSError("not running")),
    )
    monkeypatch.setitem(sys.modules, "win32com", types.SimpleNamespace(client=client))
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    monkeypatch.setitem(sys.modules, "pythoncom", types.SimpleNamespace(CoInitialize=lambda: None, CoUninitialize=lambda: None))
    monkeypatch.setattr(outlook_desktop, "available", lambda: "")
    with pytest.raises(outlook_desktop.OutlookUnavailable, match="isn't open"):
        with outlook_desktop.connect(start=False):
            pass
    assert calls == [], "Outlook wasn't started in the background"
