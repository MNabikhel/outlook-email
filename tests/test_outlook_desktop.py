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

# What outlook_desktop.py may call, and on what. Outlook's object model is reached through COM, where a name is matched
# whatever its case ("item.delete()" calls Delete), so the names used are listed, as spelled, rather than the ones
# that change something (which none may match in any case): a name that isn't listed here fails the test until someone
# has checked that it only reads.
COM_CALLS = {
    "Dispatch": {"win32com.client"}, "GetActiveObject": {"win32com.client"},
    "CoInitialize": {"pythoncom"}, "CoUninitialize": {"pythoncom"}, "GetLocaleInfo": {"win32api"},
    "GetNamespace": {"app"}, "GetDefaultFolder": {"self.ns", "store"}, "GetRootFolder": {"store"}, "GetItemFromID": {"self.ns"},
    "Item": {"children", "self.ns.Stores"}, "GetTable": {"folder"}, "GetNextRow": {"table"}, "GetValues": {"table.GetNextRow()"},
    # Columns.Add and RemoveAll shape the table CloseDesk asked for; Sort and Restrict order and filter a list of items.
    "RemoveAll": {"table.Columns"}, "Add": {"table.Columns"}, "Sort": {"table", "items"}, "Restrict": {"items"},
    "GetFirst": {"listed"}, "GetNext": {"listed"},
    "SaveAs": {"item"},  # writes a .msg file and leaves the message as it is: the only way a copy is made
}
# Python's own methods on Python values (strings, dates, lists, sets), and this file's own.
PYTHON_CALLS = {
    "append", "astimezone", "date", "get", "group", "isoformat", "lower", "mkdir", "now", "pop", "replace", "split",
    "startswith", "strftime", "strip", "sub",
}
PYTHON_CALLS_ON = {"add": {"bins", "codes", "names", "trash"}, "sort": {"stores"}, "_item": {"self"}, "folders": {"self"}, "__init__": {"super()"}}
# What Outlook's objects are read for (and the methods above): any other name is something new of Outlook's.
COM_NAMES = set(COM_CALLS) | {
    "AllDayEvent", "BusyStatus", "Columns", "Count", "DefaultItemType", "End", "EndOfTable", "EntryID", "Folders",
    "GlobalAppointmentID", "IncludeRecurrences", "IsRecurring", "Items", "Location", "MeetingStatus", "Name", "OptionalAttendees",
    "Organizer", "Parent", "RequiredAttendees", "Start", "StoreID", "Stores", "Subject",
}
# Python's own attributes read in the file (of datetimes, exceptions, modules and this file's classes).
PYTHON_NAMES = PYTHON_CALLS | set(PYTHON_CALLS_ON) | {
    "__name__", "_by_path", "_folders", "args", "client", "cut_short", "entry_id", "ns", "parent", "path", "platform",
    "store_id", "utc", "year",
}
READ_BY_NAME = {"ExchangeStoreType", "DisplayName", "hresult", "excepinfo", "args"}  # getattr(obj, "...", default)
# Python's own functions it calls, besides its own and those it imports by name.
BUILTINS = {"any", "bool", "getattr", "isinstance", "len", "range", "set", "str", "super", "type"}
# The modules it may import: none that reach a method by a name made up at run time (operator.methodcaller,
# functools.partial, importlib).
IMPORTS = {"__future__", "contextlib", "controller_inbox.clock", "dataclasses", "datetime", "pathlib", "pythoncom", "re", "sys", "typing",
           "win32api", "win32com.client"}
# Outlook's methods that change, send, open or close something: none may be on the lists above, in any case.
WRITES = {
    "Save", "Delete", "Move", "MoveTo", "Copy", "CopyTo", "Display", "Send", "Close", "Reply", "ReplyAll", "Forward", "Quit",
    "SetProperty", "SetProperties", "DeleteProperty", "DeleteProperties", "MarkAsTask", "ClearTaskFlag", "PrintOut", "Import",
    "Execute", "ShowCategoriesDialog", "AddMember", "Remove", "Respond", "Accept", "Decline", "ClearConversationIndex", "Undelete",
    "Empty", "GetAssociatedAppointment", "CreateItem", "CreateItemFromTemplate", "AddStore", "RemoveStore", "Logoff", "InvokeTypes",
}


def _targets(node) -> list:
    """What a statement writes to: assignment targets (each part of a tuple unpacked), loop and comprehension
    variables, ``with ... as`` names, ``:=`` names and what ``del`` removes."""
    if isinstance(node, ast.Assign):
        found = list(node.targets)
    elif isinstance(node, (ast.AugAssign, ast.AnnAssign, ast.For, ast.AsyncFor, ast.comprehension, ast.NamedExpr)):
        found = [node.target]
    elif isinstance(node, ast.withitem):
        found = [node.optional_vars] if node.optional_vars is not None else []
    elif isinstance(node, ast.Delete):
        found = list(node.targets)
    else:
        return []
    out = []
    while found:
        target = found.pop()
        if isinstance(target, (ast.Tuple, ast.List)):
            found.extend(target.elts)
        elif isinstance(target, ast.Starred):
            found.append(target.value)
        else:
            out.append(target)
    return out


def _not_read_only(source: str) -> list[str]:
    """What in this source isn't on the lists of reads above, by line: a call or a name of Outlook's not listed (in
    any case), a call of something that isn't a listed function, an import not listed, and a property set on
    anything but this file's own objects."""
    tree = ast.parse(source)
    known = COM_NAMES | PYTHON_NAMES  # as spelled here: "item.UNREAD" or "item.delete" isn't
    writes = {name.lower() for name in WRITES}
    functions = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))} | BUILTINS
    functions |= {alias.asname or alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}
    found = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Attribute) and (node.attr not in known or node.attr.lower() in writes):
            found.append(f"line {line}: .{node.attr}")
        if isinstance(node, ast.Import):
            found += [f"line {line}: import {alias.name}" for alias in node.names if alias.name not in IMPORTS]
        if isinstance(node, ast.ImportFrom) and node.module not in IMPORTS:
            found.append(f"line {line}: from {node.module} import")
        for target in _targets(node):
            # The one property set: the calendar's list of items is told to list each occurrence of a repeating
            # meeting. It is a setting of the list CloseDesk asked for, not of anything kept in Outlook.
            if isinstance(target, ast.Attribute) and ast.unparse(target.value) != "self" and ast.unparse(target) != "items.IncludeRecurrences":
                found.append(f"line {line}: sets {ast.unparse(target)}")
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            on = ast.unparse(func.value)
            if not (on in COM_CALLS.get(func.attr, ()) or func.attr in PYTHON_CALLS or on in PYTHON_CALLS_ON.get(func.attr, ())):
                found.append(f"line {line}: {on}.{func.attr}()")
        elif isinstance(func, ast.Name):
            if func.id not in functions:
                found.append(f"line {line}: {func.id}()")  # setattr, eval, or a method kept in a variable first
            elif func.id == "getattr":
                named = node.args[1] if len(node.args) > 1 else None
                if not (isinstance(named, ast.Constant) and named.value in READ_BY_NAME):
                    found.append(f"line {line}: {ast.unparse(node)}")
        else:
            found.append(f"line {line}: {ast.unparse(node)}")  # getattr(item, "Delete")(), a call of a call
    return found


def test_the_reader_only_reads_outlook():
    assert _not_read_only(SOURCE.read_text(encoding="utf-8")) == [], "outlook_desktop.py must only read Outlook"
    writes = {name.lower() for name in WRITES}
    assert not writes & {name.lower() for name in [*COM_CALLS, *PYTHON_CALLS, *PYTHON_CALLS_ON, *COM_NAMES, *PYTHON_NAMES]}


def test_the_read_only_check_catches_what_isnt_a_read():
    for bad in (
        "item.Delete()", "app.Quit()", "self.ns.Application.Session.Logoff()", "folder.Items.Add(1)", "item.delete()",
        "namespace.Import(path)", "explorer.CommandBars.ExecuteMso('x')", "getattr(item, 'Delete')()", "getattr(item, name)",
        "setattr(item, 'UnRead', False)", "move = item.Move", "item.Parent.Folders.add('New')", "item._oleobj_.InvokeTypes(1)",
        "move = item.delete\n    move()", "move = item.Display\n    move()", "item.subject", "item.UNREAD",
        "import operator\n    operator.methodcaller('Delete')(item)", "from operator import attrgetter", "from functools import partial",
        "for item.UnRead in [False]:\n        pass", "with open(name) as item.Subject:\n        pass", "item.UnRead, x = False, 1",
        "[0 for item.UnRead in [False]]", "del item.Categories", "item.FlagStatus += 1", "item.Subject: str = ''",
    ):
        assert _not_read_only(f"def f(item, app, folder, namespace, explorer, name):\n    {bad}\n"), bad
    assert _not_read_only("def f(self):\n    self._folders = []\n    for self.path in []:\n        pass\n") == []


def test_only_the_reader_talks_to_outlook():
    # The read-only checks above read outlook_desktop.py: any other file reaching Outlook would escape them.
    package = SOURCE.parent
    users = sorted(path.name for path in package.rglob("*.py") if re.search(r"\bwin32com\b", path.read_text(encoding="utf-8")))
    assert users == ["outlook_desktop.py"], users


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


# The calendar ---------------------------------------------------------------------------------------------------


class Appointment(ReadOnly):
    def __init__(self, subject, start, end, *, all_day=False, recurring=False, status=1, gid="G1", busy=2):
        self.Subject, self.Start, self.End, self.AllDayEvent = subject, start, end, all_day
        self.Location, self.Organizer = "Board room", "Maya Chen"
        self.RequiredAttendees, self.OptionalAttendees = "Maya Chen; Priya Raman", ""
        self.BusyStatus, self.MeetingStatus, self.IsRecurring, self.GlobalAppointmentID = busy, status, recurring, gid
        self.freeze()

    @property
    def Body(self):
        raise AssertionError("the meeting's text is not read")


class CalendarItems:
    """Outlook's Items of a calendar: Sort, then IncludeRecurrences, then Restrict, in that order."""

    def __init__(self, appointments):
        self._all = appointments
        self.IncludeRecurrences = False
        self.sorted = False

    def Sort(self, column):
        assert column == "[Start]" and not self.IncludeRecurrences, "sorted before IncludeRecurrences is set"
        self.sorted = True

    def Restrict(self, where):
        assert self.sorted and self.IncludeRecurrences, "IncludeRecurrences set before Restrict"
        self.where = where
        return Listed(sorted(self._all, key=lambda a: a.Start))


class Listed:
    def __init__(self, items):
        self._items, self._at = items, 0

    def GetFirst(self):
        self._at = 0
        return self.GetNext()

    def GetNext(self):
        if self._at >= len(self._items):
            return None
        self._at += 1
        return self._items[self._at - 1]


class CalendarFolder(ReadOnly):
    def __init__(self, items):
        self.Items = items
        self.freeze()


def test_the_calendar_lists_each_meeting_in_the_window_without_its_text(outlook):
    session, _items = outlook
    day = datetime(2026, 10, 5)
    appointments = [
        Appointment("Long ago", local(2026, 1, 5, 9), local(2026, 1, 5, 10)),
        Appointment("AP standup", local(2026, 10, 6, 9), local(2026, 10, 6, 9, 15), recurring=True, gid="G-standup"),
        Appointment("AP standup", local(2026, 10, 13, 9), local(2026, 10, 13, 9, 15), recurring=True, gid="G-standup"),
        Appointment("Close day", local(2026, 10, 30, 0), local(2026, 10, 31, 0), all_day=True, gid="G-close", busy=3),
        Appointment("Cancelled review", local(2026, 10, 8, 14), local(2026, 10, 8, 15), status=5, gid="G-x"),
        Appointment("Next year", local(2027, 6, 1, 9), local(2027, 6, 1, 10)),
    ]
    items = CalendarItems(appointments)
    object.__setattr__(session.ns, "_defaults", {**session.ns._defaults, 9: CalendarFolder(items)})
    found = session.calendar(day.replace(tzinfo=timezone.utc), datetime(2026, 11, 30, tzinfo=timezone.utc))
    assert [(m["subject"], m["uid"]) for m in found] == [
        ("AP standup", "G-standup"), ("Cancelled review", "G-x"), ("AP standup", "G-standup"), ("Close day", "G-close")
    ]
    close = found[-1]
    assert (close["start"], close["end"], close["all_day"], close["busy"]) == ("2026-10-30", "2026-10-31", True, "away")
    assert found[1]["cancelled"] and found[0]["recurring"] and found[0]["attendees"] == ["Maya Chen", "Priya Raman"]
    assert re.search(r"\[End\] >= '\d\d/\d\d/\d{4}' AND \[Start\] <= '\d\d/\d\d/\d{4}'", items.where)


def test_windows_short_date_patterns_become_outlooks_filter_dates():
    assert outlook_desktop._strftime_pattern("dd/MM/yyyy") == "%d/%m/%Y"
    assert outlook_desktop._strftime_pattern("M/d/yyyy") == "%m/%d/%Y"
    assert outlook_desktop._strftime_pattern("yyyy-MM-dd") == "%Y-%m-%d"
    assert outlook_desktop._strftime_pattern("") == ""


def test_the_calendar_says_when_outlooks_limit_cut_it_short(outlook, monkeypatch):
    session, _items = outlook
    # Long meetings that ended before the window come first (Outlook lists them by start) and are left out.
    old = [Appointment(f"Old {n}", local(2026, 1, 1, 9), local(2026, 1, 2, 9), gid=f"G-old{n}") for n in range(5)]
    kept = [Appointment("AP standup", local(2026, 10, 6, 9), local(2026, 10, 6, 9, 15), gid="G-standup")]
    later = [Appointment("Later", local(2026, 11, 6, 9), local(2026, 11, 6, 10), gid="G-later")]
    window = (datetime(2026, 10, 5, tzinfo=timezone.utc), datetime(2026, 11, 30, tzinfo=timezone.utc))

    def read():
        object.__setattr__(session.ns, "_defaults", {**session.ns._defaults, 9: CalendarFolder(CalendarItems(old + kept + later))})
        return session.calendar(*window)

    monkeypatch.setattr(outlook_desktop, "MAX_CALENDAR_ITEMS", 6)
    found = read()
    assert [m["subject"] for m in found] == ["AP standup"] and found.cut_short
    monkeypatch.setattr(outlook_desktop, "MAX_CALENDAR_ITEMS", 3000)
    found = read()
    assert [m["subject"] for m in found] == ["AP standup", "Later"] and not found.cut_short
