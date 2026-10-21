"""Classic Outlook sync in the harder cases found in review: archiving, deleted folders, other languages, folders Outlook won't open, reused Message-IDs, mail dropped in by hand too, and the automatic sync on start."""

from __future__ import annotations

import time

import pytest

from controller_inbox import outlook_desktop, outlook_sync, web, web_api
from controller_inbox.folder_mail import ingest_folder
from controller_inbox.outlook_desktop import FolderInfo, OutlookSession
from test_outlook_desktop import Collection, ComError, Folder, Namespace, Store as FakeStore
from test_outlook_sync import STORE, FakeOutlook, emails, sync


@pytest.fixture
def outlook():
    return FakeOutlook()


# --- Deletion safety ---------------------------------------------------------------------------------------


def test_archiving_to_an_unsynced_folder_with_a_new_entry_id_must_not_delete_the_copy(store, settings, outlook):
    """Exchange (and any move to another store, e.g. a PST or Online Archive) gives a moved item a new EntryID.
    Moving to a folder that isn't synced is documented to keep CloseDesk's copy; here it is deleted."""
    outlook.add("e1", "Invoice INV-7741 from Northwind")
    sync(store, settings, outlook)
    moved = outlook.messages.pop("e1")
    outlook.messages["e1-archived"] = {**moved, "folder": "Archive"}  # same Message-ID, new EntryID, unsynced folder
    report = sync(store, settings, outlook)
    assert report["removed"] == 0, "archived in Outlook, not deleted"
    assert emails(store), "CloseDesk's copy (with its tasks) was deleted"


def test_mail_of_a_folder_deleted_in_outlook_is_removed(store, settings, outlook):
    """Deleting a folder in Outlook moves it under Deleted Items. Its path is then neither scanned nor trash, so
    the rows still say "Inbox/Vendors" (not read) and the deleted mail is never removed."""
    outlook.add("v1", "Vendor statement", folder="Inbox/Vendors")
    sync(store, settings, outlook)
    assert emails(store)
    outlook.infos = [i for i in outlook.infos if i.path != "Inbox/Vendors"] + [
        FolderInfo("Deleted Items/Vendors", STORE, "folder-dv", 2, trash=False)  # what OutlookSession.folders() says
    ]
    outlook.messages["v1"]["folder"] = "Deleted Items/Vendors"
    sync(store, settings, outlook)
    assert not emails(store), "the folder (and its mail) was deleted in Outlook"


def test_mail_synced_then_dropped_by_hand_survives_deletion_in_outlook(store, settings, outlook):
    """The reverse order of the 'dropped by hand stays' rule: synced first, then the user drops the .msg too.
    The row stays owned=1, so deleting in Outlook deletes the user's own dropped copy."""
    outlook.add("e1", "Invoice INV-7741 from Northwind")
    sync(store, settings, outlook)
    outlook.save_copy("e1", STORE, settings.inbox_incoming / "invoice.msg")  # the user drags the same message in
    ingest_folder(store, settings)
    assert len(store.list_emails(limit=50)) == 1
    del outlook.messages["e1"]
    sync(store, settings, outlook)
    assert emails(store), "the copy the user dropped in by hand was deleted"


# --- Duplicates / identity ---------------------------------------------------------------------------------


def test_two_messages_sharing_a_message_id_are_both_copied(store, settings, outlook):
    """Scan-to-email copiers reuse one Message-ID. ingest_folder handles that (_another_message); the Outlook
    sync counts the second scan as 'already' and never copies it."""
    outlook.add("s1", "Scan 0001 - invoice", message_id="<scan@copier.local>")
    outlook.add("s2", "Scan 0002 - W-9", minutes=5, message_id="<scan@copier.local>")
    report = sync(store, settings, outlook)
    assert set(emails(store)) == {"Scan 0001 - invoice", "Scan 0002 - W-9"}, report


def test_a_new_message_reusing_a_deleted_ones_message_id_is_copied(store, settings, outlook):
    """The 'moved with a new id' heuristic takes a brand-new message with a reused Message-ID for the old,
    deleted one: the deleted mail stays and the new mail is never copied."""
    outlook.add("s1", "Scan 0001 - invoice", minutes=60, message_id="<scan@copier.local>")
    sync(store, settings, outlook)
    del outlook.messages["s1"]
    outlook.add("s2", "Scan 0002 - W-9", message_id="<scan@copier.local>")
    sync(store, settings, outlook)
    assert set(emails(store)) == {"Scan 0002 - W-9"}


def test_message_without_message_id_in_two_synced_folders_is_one_email(store, settings, outlook, monkeypatch):
    from controller_inbox import folder_mail

    real = folder_mail._parse_msg

    def parse(path):  # a message with no Internet Message-ID header (Outlook-internal, some imports)
        raw = real(path)
        raw.internet_message_id = ""
        return raw

    monkeypatch.setattr(folder_mail, "_parse_msg", parse)
    outlook.add("n1", "Internal note", message_id="")
    outlook.add("n2", "Internal note", folder="Inbox/Vendors", message_id="")  # Outlook copy: new EntryID
    sync(store, settings, outlook)
    assert len(store.list_emails(limit=50)) == 1


# --- Folder handling ---------------------------------------------------------------------------------------


def test_a_picked_folder_renamed_in_outlook_is_reported(store, settings, outlook):
    outlook_sync.remember_folders(store, outlook)
    outlook_sync.save_config(store, auto=True, interval=15, days=30, folders=["Archive"], subfolders=True)
    outlook.add("a1", "Archived", folder="Archive")
    sync(store, settings, outlook)
    outlook.infos = [FolderInfo("Archive 2026" if i.path == "Archive" else i.path, i.store_id, i.entry_id, i.depth, i.inbox, i.trash)
                     for i in outlook.infos]
    report = sync(store, settings, outlook)
    assert report["folders"] or report["failed"], "syncs nothing at all, silently ('nothing new')"


def _shared_namespace(bin_name: str):
    inbox = Folder("Inbox", "F-inbox")
    deleted = Folder("Deleted Items", "F-deleted", children=[Folder("Vendors", "F-del-vendors")])
    junk = Folder("Junk Email", "F-junk")
    root = Folder("maya@taz.com", "F-root", children=[inbox, deleted, junk])
    shared_inbox = Folder("Posteingang", "F-ap-inbox", store="S2")
    shared_bin = Folder(bin_name, "F-ap-deleted", store="S2")
    shared_root = Folder("AP Team", "F-ap-root", store="S2", children=[shared_inbox, shared_bin])
    return Namespace([FakeStore("S1", "maya@taz.com", root, {3: deleted, 23: junk}), FakeStore("S2", "AP Team", shared_root, {3: shared_bin})],
                     {6: inbox, 3: deleted, 23: junk}, {})


def test_a_shared_mailboxs_deleted_items_in_a_german_outlook_is_trash():
    """Only English names identify another mailbox's bins; Store.GetDefaultFolder(olFolderDeletedItems) would."""
    folders = {f.path: f for f in OutlookSession(_shared_namespace("Gelöschte Elemente")).folders()}
    assert folders["AP Team/Gelöschte Elemente"].trash, "deletions in the shared mailbox are taken for moves"


def test_a_folder_inside_deleted_items_is_trash():
    folders = {f.path: f for f in OutlookSession(_shared_namespace("Deleted Items")).folders()}
    assert folders["Deleted Items/Vendors"].trash, "mail in a deleted folder counts as moved, not deleted"


class _Denied:
    """A shared folder the user may see but not open: Outlook raises on reading its subfolders."""

    Name, EntryID, StoreID, DefaultItemType = "Restricted", "F-denied", "S2", 0

    @property
    def Folders(self):
        raise ComError(-2147352567, "Exception occurred.", (4096, "Microsoft Outlook", "You don't have appropriate permission.", None, 0, -2147024891), None)


def test_one_folder_outlook_wont_open_doesnt_stop_every_sync():
    inbox = Folder("Inbox", "F-inbox")
    root = Folder("maya@taz.com", "F-root", children=[inbox])
    shared_root = Folder("AP Team", "F-ap-root", store="S2")
    object.__setattr__(shared_root, "Folders", Collection([_Denied()]))
    ns = Namespace([FakeStore("S1", "maya@taz.com", root), FakeStore("S2", "AP Team", shared_root)], {6: inbox, 3: inbox, 23: inbox}, {})
    paths = [f.path for f in OutlookSession(ns).folders()]  # raises: the whole sync fails, every time
    assert "Inbox" in paths


def test_two_data_files_with_the_same_name_get_distinct_paths():
    inbox = Folder("Inbox", "F-inbox")
    root = Folder("maya@taz.com", "F-root", children=[inbox])
    pst1 = Folder("Outlook Data File", "P1", store="P1", children=[Folder("Inbox", "P1-inbox", store="P1")])
    pst2 = Folder("Outlook Data File", "P2", store="P2", children=[Folder("Inbox", "P2-inbox", store="P2")])
    ns = Namespace([FakeStore("S1", "maya@taz.com", root), FakeStore("P1", "Outlook Data File", pst1),
                    FakeStore("P2", "Outlook Data File", pst2)], {6: inbox, 3: inbox, 23: inbox}, {})
    paths = [f.path for f in OutlookSession(ns).folders()]
    assert len(paths) == len(set(paths)), f"one path names two folders; only the last is ever scanned: {paths}"


# --- Ticker ------------------------------------------------------------------------------------------------


class _Stop(Exception):
    pass


def test_automatic_sync_runs_on_start_soon_after_the_computer_boots(store, settings, monkeypatch):
    """`tried = 0.0` is compared with time.monotonic(), which counts from boot: CloseDesk started at login
    (2 minutes after boot) waits until 15 minutes after boot for its 'on start' sync."""
    monkeypatch.setattr(outlook_desktop, "available", lambda: "")
    store.set_state(outlook_sync.AUTO_KEY, "1")
    targets = []
    monkeypatch.setattr(web_api.threading, "Thread", lambda target, **kw: type("T", (), {"start": lambda self: targets.append(target)})())
    app = web.create_app(settings, store)
    app.state.start_outlook_sync()
    started = []
    monkeypatch.setattr(app.state.job, "start", lambda target, about=None: started.append(about) or True)
    sleeps = []

    def sleep(_seconds):
        sleeps.append(1)
        if len(sleeps) > 2:
            raise _Stop

    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr(time, "monotonic", lambda: 120.0)
    with pytest.raises(_Stop):
        targets[0]()
    assert started, "no sync on start"
