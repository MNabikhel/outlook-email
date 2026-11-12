"""Classic Outlook sync (outlook_sync.py): mail is copied in once, follows what changed in Outlook, and a deletion in
Outlook removes CloseDesk's copy, while nothing is ever written to Outlook and a hiccup in Outlook removes nothing.

Outlook itself is faked here (``FakeOutlook``): it offers only the reads ``OutlookSession`` offers, so a sync that
tried to change anything in Outlook would fail."""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from controller_inbox import folder_mail, outlook_desktop, outlook_sync, web
from controller_inbox.folder_mail import ingest_folder
from controller_inbox.models import RawMessage
from controller_inbox.outlook_desktop import FolderInfo, NotInOutlook, OutlookError, Seen
from controller_inbox.overnight import outlook_run, run_overnight
from controller_inbox.pipeline import ingest_demo, process_message
from helpers import PAGE
from msgfactory import PDF, require, write_msg

NOW = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
STORE = "store-1"


@pytest.fixture(autouse=True)
def _agreed(store):
    """These tests are about syncing itself: the user has agreed to it (Settings → Sync inbox, tested apart)."""
    store.set_state(outlook_sync.CONSENT_KEY, "2026-09-01T00:00:00+00:00")


class FakeOutlook:
    """Outlook with folders and messages, offering only what ``OutlookSession`` reads."""

    def __init__(self, folders=("Inbox", "Inbox/Vendors", "Archive", "Deleted Items", "Junk Email")):
        require()  # each message is copied as a real .msg file
        self.infos = [
            FolderInfo(path, STORE, f"folder-{path}", path.count("/") + 1, inbox=path == "Inbox", trash=path in ("Deleted Items", "Junk Email"))
            for path in folders
        ]
        self.messages: dict[str, dict] = {}
        self.broken: set[str] = set()  # folders whose table can't be read
        self.confused: set[str] = set()  # messages Outlook can't say anything about just now
        self.copies = 0

    def add(self, entry, subject, *, folder="Inbox", minutes=0, unread=True, flagged=False, message_id=None, attachments=None, sender="ap@northwind.com"):
        self.messages[entry] = {
            "folder": folder,
            "subject": subject,
            "received": NOW - timedelta(minutes=minutes),
            "unread": unread,
            "flagged": flagged,
            "message_id": f"<{entry}@northwind.com>" if message_id is None else message_id,
            "attachments": attachments or [],
            "sender": sender,
        }

    # What OutlookSession offers ------------------------------------------------------------------------

    def account(self):
        return "maya@taz.com"

    def folders(self):
        return self.infos

    def scan(self, path, since):
        if path in self.broken:
            raise OutlookError("Outlook is busy")
        found = [
            Seen(entry, STORE, m["folder"], m["received"], m["unread"], m["flagged"], "normal", m["message_id"])
            for entry, m in self.messages.items()
            if m["folder"] == path and m["received"] >= since
        ]
        return sorted(found, key=lambda s: s.received, reverse=True)

    def save_copy(self, entry_id, store_id, dest):
        m = self.messages[entry_id]
        self.copies += 1
        write_msg(dest, m["subject"], f"Hello,\n\n{m['subject']}. Thanks.", sender_email=m["sender"], sender_name="Northwind AP",
                  sent=m["received"], message_id=m["message_id"], attachments=m["attachments"])

    def find_message(self, message_id):
        return [
            Seen(entry, STORE, m["folder"], m["received"], m["unread"], m["flagged"], "normal", m["message_id"])
            for entry, m in self.messages.items()
            if message_id and m["message_id"] == message_id
        ]

    def locate(self, entry_id, store_id):
        if entry_id in self.confused:
            raise OutlookError("Outlook didn't answer")
        if entry_id not in self.messages:
            raise NotInOutlook(entry_id)
        return self.messages[entry_id]["folder"]


@pytest.fixture
def outlook():
    return FakeOutlook()


def sync(store, settings, outlook, *, now=NOW):
    return outlook_sync.sync(store, settings, outlook, now=now)


def emails(store):
    return {e.subject: e for e in store.list_emails(limit=500)}


def _pdf() -> bytes:
    from controller_inbox.demo import make_pdf

    return make_pdf([["Invoice INV-7741", "Total due $4,210.00"]])


# Copying in ---------------------------------------------------------------------------------------------


def test_first_sync_copies_the_inbox_with_attachments_and_replaces_the_sample(store, settings, outlook, as_of_now):
    ingest_demo(store, settings, now=as_of_now)
    outlook.add("e1", "Invoice INV-7741 from Northwind", attachments=[("INV-7741.pdf", _pdf(), PDF)])
    outlook.add("e2", "Lunch on Friday?", minutes=30, unread=False)
    outlook.add("e3", "Vendor statement", folder="Inbox/Vendors")
    report = sync(store, settings, outlook)

    assert report["new"] == 3 and not report["failed"], report
    assert report.get("sample_cleared"), "the first real mail replaces the sample mailbox"
    mail = emails(store)
    assert set(mail) == {"Invoice INV-7741 from Northwind", "Lunch on Friday?", "Vendor statement"}
    invoice = mail["Invoice INV-7741 from Northwind"]
    assert invoice.source == "outlook" and invoice.sender_email == "ap@northwind.com"
    assert [a.filename for a in invoice.attachments] == ["INV-7741.pdf"]
    assert "4,210.00" in invoice.attachments[0].extracted_text
    assert invoice.is_read is False and mail["Lunch on Friday?"].is_read is True
    # The copy is kept where Open in Outlook finds it; the staging copy is gone.
    copy = outlook_sync.copy_path(settings, invoice.id)
    assert invoice.source_path == str(copy) and copy.is_file()
    assert not list((settings.data_dir / "outlook-copying").glob("*.msg"))
    assert store.outlook_folder_of(mail["Vendor statement"].id) == "Inbox/Vendors"


def test_syncing_again_copies_nothing_twice(store, settings, outlook):
    outlook.add("e1", "Invoice INV-7741 from Northwind")
    sync(store, settings, outlook)
    copies = outlook.copies
    report = sync(store, settings, outlook, now=NOW + timedelta(minutes=15))
    assert report["new"] == 0 and outlook.copies == copies, "a message copied before isn't opened again"
    outlook.add("e2", "New PO 4500123", minutes=-10)
    report = sync(store, settings, outlook, now=NOW + timedelta(minutes=30))
    assert report["new"] == 1 and outlook.copies == copies + 1
    assert len(store.list_emails(limit=50)) == 2


def test_mail_older_than_the_window_is_not_copied(store, settings, outlook):
    outlook.add("old", "Last quarter", minutes=60 * 24 * 40)
    outlook.add("new", "This week", minutes=60)
    sync(store, settings, outlook)
    assert set(emails(store)) == {"This week"}
    store.set_state(outlook_sync.DAYS_KEY, "90")
    sync(store, settings, outlook)
    assert set(emails(store)) == {"This week", "Last quarter"}


def test_folders_picked_with_and_without_subfolders(store, settings, outlook):
    outlook.add("a", "In the inbox")
    outlook.add("b", "In vendors", folder="Inbox/Vendors")
    outlook.add("c", "Archived", folder="Archive")
    outlook.add("d", "Binned", folder="Deleted Items")
    outlook_sync.remember_folders(store, outlook)
    outlook_sync.save_config(store, auto=False, interval=15, days=30, folders=["Inbox"], subfolders=False)
    sync(store, settings, outlook)
    assert set(emails(store)) == {"In the inbox"}
    outlook_sync.save_config(store, auto=False, interval=15, days=30, folders=["Inbox", "Archive", "Deleted Items"], subfolders=True)
    sync(store, settings, outlook)
    assert set(emails(store)) == {"In the inbox", "In vendors", "Archived"}, "Deleted Items is never copied"


def test_mail_dropped_in_by_hand_is_not_filed_again_and_stays_when_deleted_in_outlook(store, settings, outlook):
    # The same message as the one in Outlook: its Message-ID, subject, sender and sent time.
    write_msg(settings.inbox_incoming / "invoice.msg", "Invoice INV-7741 from Northwind", "Hello", sender_email="ap@northwind.com",
              message_id="<e1@northwind.com>", sent=NOW)
    ingest_folder(store, settings)
    outlook.add("e1", "Invoice INV-7741 from Northwind")
    report = sync(store, settings, outlook)
    assert report["new"] == 0 and report["already"] == 1
    assert len(store.list_emails(limit=50)) == 1 and emails(store)["Invoice INV-7741 from Northwind"].source == "folder"
    del outlook.messages["e1"]
    report = sync(store, settings, outlook)
    assert report["removed"] == 0 and len(store.list_emails(limit=50)) == 1, "the user's own copy stays, and isn't counted as deleted"


# Following Outlook ---------------------------------------------------------------------------------------


def test_read_flag_and_move_in_outlook_follow_here(store, settings, outlook):
    outlook.add("e1", "Invoice INV-7741 from Northwind")
    sync(store, settings, outlook)
    outlook.messages["e1"].update(unread=False, flagged=True)
    report = sync(store, settings, outlook)
    assert report["updated"] == 1
    email = emails(store)["Invoice INV-7741 from Northwind"]
    assert email.is_read is True
    assert store.outlook_items()["e1"]["flagged"] == 1
    outlook.messages["e1"]["folder"] = "Inbox/Vendors"
    report = sync(store, settings, outlook)
    assert report["moved"] == 1 and store.outlook_folder_of(email.id) == "Inbox/Vendors"


def test_deleted_in_outlook_removes_the_copy_but_keeps_corrections_and_fraud_log(store, settings, outlook):
    outlook.add("e1", "Invoice INV-7741 from Northwind", attachments=[("INV-7741.pdf", _pdf(), PDF)])
    outlook.add("e2", "Keep me", minutes=5)
    sync(store, settings, outlook)
    email = emails(store)["Invoice INV-7741 from Northwind"]
    store.add_correction(
        {"id": "c1", "email_id": email.id, "previous_category": email.category.value, "corrected_category": "ap_invoice",
         "reason": "it's an invoice", "sender_email": email.sender_email, "subject": email.subject, "created_at": NOW.isoformat()}
    )
    with store.connect() as conn:
        conn.execute("INSERT INTO fraud_log(at, email_id, event) VALUES ('2026-10-20', ?, 'checked')", (email.id,))
    outlook.messages["e1"]["folder"] = "Deleted Items"
    report = sync(store, settings, outlook)

    assert report["removed"] == 1
    assert set(emails(store)) == {"Keep me"}
    assert store.get_email(email.id) is None and "e1" not in store.outlook_items()
    assert not outlook_sync.copy_path(settings, email.id).exists()
    assert not (settings.inbox_extracted / email.id).exists()
    assert store.correction_count() == 1, "what a correction taught stays learned"
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM fraud_log WHERE email_id = ?", (email.id,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM attachments WHERE email_id = ?", (email.id,)).fetchone()[0] == 0


def test_deleted_for_good_removes_the_copy(store, settings, outlook):
    outlook.add("e1", "Invoice INV-7741 from Northwind")
    sync(store, settings, outlook)
    del outlook.messages["e1"]
    assert sync(store, settings, outlook)["removed"] == 1
    assert not emails(store)


def test_moved_to_a_folder_that_isnt_synced_stays_here(store, settings, outlook):
    outlook.add("e1", "Invoice INV-7741 from Northwind")
    sync(store, settings, outlook)
    outlook.messages["e1"]["folder"] = "Archive"
    report = sync(store, settings, outlook)
    assert report["removed"] == 0 and report["moved"] == 1
    email = emails(store)["Invoice INV-7741 from Northwind"]
    assert store.outlook_folder_of(email.id) == "Archive"
    # Later deleted from the archive: CloseDesk no longer follows that folder, so its copy stays.
    del outlook.messages["e1"]
    assert sync(store, settings, outlook)["removed"] == 0 and emails(store)


def test_moved_with_a_new_outlook_id_keeps_one_copy(store, settings, outlook):
    outlook.add("e1", "Invoice INV-7741 from Northwind")
    sync(store, settings, outlook)
    original = emails(store)["Invoice INV-7741 from Northwind"].id
    moved = outlook.messages.pop("e1")
    outlook.messages["e1-new"] = {**moved, "folder": "Inbox/Vendors"}
    copies = outlook.copies
    report = sync(store, settings, outlook)
    assert report["removed"] == 0 and report["new"] == 0 and outlook.copies == copies
    assert list(emails(store).values())[0].id == original
    assert set(store.outlook_items()) == {"e1-new"}


def test_a_folder_outlook_cant_read_removes_nothing(store, settings, outlook):
    outlook.add("e1", "Invoice INV-7741 from Northwind")
    outlook.add("e2", "Vendor statement", folder="Inbox/Vendors")
    sync(store, settings, outlook)
    outlook.broken.add("Inbox")
    report = sync(store, settings, outlook)
    assert report["removed"] == 0 and report["failed"] and len(emails(store)) == 2


def test_outlook_unsure_where_a_message_is_removes_nothing(store, settings, outlook):
    outlook.add("e1", "Invoice INV-7741 from Northwind")
    sync(store, settings, outlook)
    outlook.messages["e1"]["folder"] = "Gone?"
    outlook.confused.add("e1")
    assert sync(store, settings, outlook)["removed"] == 0 and emails(store)


def test_a_message_older_than_the_window_is_no_longer_followed(store, settings, outlook):
    outlook.add("e1", "Invoice INV-7741 from Northwind")
    sync(store, settings, outlook)
    later = NOW + timedelta(days=40)
    assert sync(store, settings, outlook, now=later)["removed"] == 0
    del outlook.messages["e1"]
    assert sync(store, settings, outlook, now=later)["removed"] == 0 and emails(store)


def test_one_message_outlook_wont_copy_doesnt_stop_the_others(store, settings, outlook, monkeypatch):
    outlook.add("e1", "Blocked one")
    outlook.add("e2", "Fine one", minutes=5)
    real = outlook.save_copy

    def save_copy(entry_id, store_id, dest):
        if entry_id == "e1":
            raise OutlookError("Outlook wouldn't let CloseDesk copy this message.")
        real(entry_id, store_id, dest)

    monkeypatch.setattr(outlook, "save_copy", save_copy)
    report = sync(store, settings, outlook)
    assert report["new"] == 1 and len(report["failed"]) == 1 and set(emails(store)) == {"Fine one"}
    monkeypatch.setattr(outlook, "save_copy", real)
    assert sync(store, settings, outlook)["new"] == 1, "tried again next sync"


# Settings and when it runs -------------------------------------------------------------------------------


def test_settings_are_checked_and_survive_loading_the_sample(store, settings, outlook, as_of_now):
    assert outlook_sync.config(store) == {"auto": False, "interval": 15, "days": 30, "folders": [], "subfolders": True, "consent": True, "consented_at": "2026-09-01T00:00:00+00:00", "calendar": False}
    outlook_sync.remember_folders(store, outlook)
    with pytest.raises(ValueError):
        outlook_sync.save_config(store, auto=True, interval=7, days=30, folders=[], subfolders=True)
    with pytest.raises(ValueError):
        outlook_sync.save_config(store, auto=True, interval=15, days=12, folders=[], subfolders=True)
    outlook_sync.save_config(store, auto=True, interval=30, days=90, folders=["Inbox/Vendors", "Nope"], subfolders=False)
    assert outlook_sync.config(store) == {"auto": True, "interval": 30, "days": 90, "folders": ["Inbox/Vendors"], "subfolders": False, "consent": True, "consented_at": "2026-09-01T00:00:00+00:00", "calendar": False}
    store.clear_mail()
    assert outlook_sync.config(store)["auto"] is True and outlook_sync.folder_list(store)


def test_due_follows_the_interval(store):
    assert not outlook_sync.due(store, NOW), "off by default"
    store.set_state(outlook_sync.AUTO_KEY, "1")
    assert outlook_sync.due(store, NOW), "never synced"
    store.set_state(outlook_sync.LAST_SYNC_KEY, NOW.isoformat())
    assert not outlook_sync.due(store, NOW + timedelta(minutes=14))
    assert outlook_sync.due(store, NOW + timedelta(minutes=15))


def test_describe():
    assert outlook_sync.describe({"new": 3, "removed": 1}) == "3 new, 1 deleted in Outlook"
    assert outlook_sync.describe({"failed": [{}]}) == "nothing new; 1 couldn't be read"
    busy = {"folder": "Inbox/AP", "error": "Outlook is busy (try again)."}
    assert outlook_sync.describe({"new": 1, "failed": [busy]}) == "1 new; 1 couldn't be read: Outlook is busy (try again)"
    # As kept for Settings (a count and the first reason), and as kept before there was a reason.
    assert outlook_sync.describe({"failed": 2, "problem": "Outlook is busy."}) == "nothing new; 2 couldn't be read: Outlook is busy"
    assert outlook_sync.describe({"failed": 1}) == "nothing new; 1 couldn't be read"


def test_settings_still_open_after_a_sync_where_something_couldnt_be_read(store, settings, outlook):
    # The last result kept "failed" as a count and the card ran len() on it: the whole Settings page answered 500.
    outlook.add("e1", "Invoice INV-7741 from Northwind", minutes=1)
    outlook.add("v1", "Vendor statement", folder="Inbox/Vendors", minutes=2)
    outlook.broken.add("Inbox/Vendors")
    sync(store, settings, outlook)
    client = TestClient(web.create_app(settings, store))
    reply = client.get("/api/settings", headers=PAGE)
    assert reply.status_code == 200
    data = reply.json()["outlook"]
    assert data["last_result"] == "1 new; 1 couldn't be read: Outlook is busy" and data["last_error"] == ""


def test_a_picked_folder_gone_from_outlook_says_why_and_isnt_a_sync_that_worked(store, settings, outlook, monkeypatch):
    # Only the count was kept ("1 couldn't be read"), and a sync that read no folder at all stamped itself as one
    # that worked and cleared the banner.
    monkeypatch.setattr(outlook_desktop, "available", lambda: "")

    @contextmanager
    def connect(**_options):
        yield outlook

    monkeypatch.setattr(outlook_desktop, "connect", connect)
    outlook.add("e1", "Invoice INV-7741 from Northwind", minutes=1)
    outlook_run(store, settings, now=NOW)
    store.set_state(outlook_sync.FOLDERS_KEY, json.dumps(["Inbox/AP"]))  # picked, then renamed in Outlook
    result = outlook_run(store, settings, now=NOW + timedelta(minutes=15))
    said = "The folder Inbox/AP isn't in Outlook any more. Pick it again in Settings → Sync inbox → Advanced."
    assert result["message"] == said
    data = TestClient(web.create_app(settings, store)).get("/api/settings", headers=PAGE).json()["outlook"]
    assert data["last_error"] == said and data["last_result"] == "1 new", "the last sync that worked is still the one shown"
    assert store.get_state(outlook_sync.LAST_SYNC_KEY) == NOW.isoformat()
    # Picked along with the Inbox: the sync works, and the card says what couldn't be read.
    store.set_state(outlook_sync.FOLDERS_KEY, json.dumps(["Inbox", "Inbox/AP"]))
    result = outlook_run(store, settings, now=NOW + timedelta(minutes=30))
    assert result["message"] == f"Outlook synced: nothing new; 1 couldn't be read: {said}"
    data = TestClient(web.create_app(settings, store)).get("/api/settings", headers=PAGE).json()["outlook"]
    assert data["last_error"] == "" and data["last_result"] == f"nothing new; 1 couldn't be read: {said[:-1]}"


@pytest.fixture
def connected(monkeypatch, outlook):
    """Classic Outlook "installed": connect() hands out the fake."""

    @contextmanager
    def connect(**_options):
        yield outlook

    monkeypatch.setattr(outlook_desktop, "available", lambda: "")
    monkeypatch.setattr(outlook_desktop, "connect", connect)
    return outlook


def test_starting_closedesk_syncs_only_when_automatic_sync_is_on(store, settings, connected):
    connected.add("e1", "Invoice INV-7741 from Northwind", minutes=1)
    result = run_overnight(store, settings, sync_graph=False, now=NOW)
    assert result["outlook"] == {} and not emails(store)
    store.set_state(outlook_sync.AUTO_KEY, "1")
    result = run_overnight(store, settings, sync_graph=False, now=NOW)
    assert result["outlook"]["new"] == 1 and result["outlook_message"] == "Outlook synced: 1 new."
    assert set(emails(store)) == {"Invoice INV-7741 from Northwind"}


def test_sync_now_runs_even_when_automatic_sync_is_off(store, settings, connected):
    connected.add("e1", "Invoice INV-7741 from Northwind", minutes=1)
    result = outlook_run(store, settings, now=NOW)
    assert result["kind"] == "outlook" and result["outlook"]["new"] == 1 and "quiet" not in result
    again = outlook_run(store, settings, now=NOW + timedelta(minutes=5))
    assert again["quiet"] is True and again["message"] == "Outlook synced: nothing new."


def test_outlook_missing_is_said_and_the_rest_of_the_run_happens(store, settings, monkeypatch):
    monkeypatch.setattr(outlook_desktop, "available", lambda: "")

    @contextmanager
    def connect(**_options):
        raise outlook_desktop.OutlookUnavailable("Classic Outlook didn't answer.")
        yield

    monkeypatch.setattr(outlook_desktop, "connect", connect)
    store.set_state(outlook_sync.AUTO_KEY, "1")
    result = run_overnight(store, settings, sync_graph=False, now=NOW)
    assert result["outlook"] == {"note": "Classic Outlook didn't answer."} and result["ok"]
    assert outlook_run(store, settings, now=NOW)["message"] == "Classic Outlook didn't answer."


def test_a_sync_that_didnt_work_shows_on_settings_until_one_does(store, settings, outlook, monkeypatch):
    # A failed sync wasn't kept anywhere: the card stayed green and said "Not synced yet".
    monkeypatch.setattr(outlook_desktop, "available", lambda: "")
    broken = True

    @contextmanager
    def connect(**_options):
        if broken:
            raise RuntimeError("the MAPI store is offline")
        yield outlook

    monkeypatch.setattr(outlook_desktop, "connect", connect)
    client = TestClient(web.create_app(settings, store))
    outlook_run(store, settings, now=NOW)
    data = client.get("/api/settings", headers=PAGE).json()["outlook"]
    assert data["last_error"] == "Outlook couldn't be read this time (the MAPI store is offline)."
    assert data["last_error_at"] and data["last_sync"] == ""
    broken = False
    outlook.add("e1", "Invoice INV-7741 from Northwind", minutes=1)
    outlook_run(store, settings, now=NOW + timedelta(minutes=15))
    data = client.get("/api/settings", headers=PAGE).json()["outlook"]
    assert data["last_error"] == "" and data["last_result"] == "1 new"
    broken = True
    outlook_run(store, settings, now=NOW + timedelta(minutes=30))
    assert client.get("/api/settings", headers=PAGE).json()["outlook"]["last_error"].startswith("Outlook couldn't be read")
    outlook_sync.disconnect(store)
    assert client.get("/api/settings", headers=PAGE).json()["outlook"]["last_error"] == "", "connecting again starts afresh"


class ComError(Exception):
    """Like pywintypes.com_error: (hresult, text, excepinfo, arg), and str() is that tuple."""


def test_outlook_busy_on_the_card_is_said_in_plain_words(store, settings, outlook, monkeypatch):
    monkeypatch.setattr(outlook_desktop, "available", lambda: "")

    def busy():
        raise ComError(-2147418111, "Call was rejected by callee.", None, None)  # a window or prompt open in Outlook

    outlook.folders = busy

    @contextmanager
    def connect(**_options):
        yield outlook

    monkeypatch.setattr(outlook_desktop, "connect", connect)
    assert outlook_run(store, settings, now=NOW)["message"] == outlook_desktop.BUSY
    assert TestClient(web.create_app(settings, store)).get("/api/settings", headers=PAGE).json()["outlook"]["last_error"] == outlook_desktop.BUSY


def test_outlooks_own_error_is_said_once_not_wrapped_again(store, settings, monkeypatch):
    monkeypatch.setattr(outlook_desktop, "available", lambda: "")
    said = "Outlook couldn't find a message just now (The server is busy.)."

    @contextmanager
    def connect(**_options):
        raise outlook_desktop.OutlookError(said)
        yield

    monkeypatch.setattr(outlook_desktop, "connect", connect)
    result = outlook_run(store, settings, now=NOW)
    assert result["message"] == said and outlook_sync.last_failure(store)["error"] == said


# The Settings page ---------------------------------------------------------------------------------------


def test_settings_api_without_classic_outlook(store, settings):
    client = TestClient(web.create_app(settings, store))
    data = client.get("/api/settings", headers=PAGE).json()["outlook"]
    assert data["available"] is (outlook_desktop.available() == "")
    assert data["intervals"] == [5, 15, 30, 60] and data["day_choices"] == [7, 30, 90, 365]
    reply = client.post("/api/settings/outlook", headers=PAGE, json={"auto": True, "interval": 5, "days": 7, "folders": [], "subfolders": True})
    assert reply.status_code == 200 and "every 5 minutes" in reply.json()["message"]
    assert client.post("/api/settings/outlook", headers=PAGE, json={"interval": 3}).status_code == 400
    assert client.post("/api/settings/outlook", headers={"X-CloseDesk": "1", "Origin": "https://evil.example"}, json={"auto": False}).status_code == 403
    if outlook_desktop.available():
        assert client.post("/api/outlook/sync", headers=PAGE).status_code == 409


def test_settings_api_check_and_sync_now(store, settings, connected):
    connected.add("e1", "Invoice INV-7741 from Northwind", minutes=1)
    client = TestClient(web.create_app(settings, store))
    reply = client.post("/api/settings/outlook/check", headers=PAGE)
    assert reply.status_code == 200 and "maya@taz.com" in reply.json()["message"] and "3 mail folders" in reply.json()["message"]
    data = client.get("/api/settings", headers=PAGE).json()["outlook"]
    assert data["available"] and [f["path"] for f in data["folder_list"]][:2] == ["Inbox", "Inbox/Vendors"]
    reply = client.post("/api/outlook/sync", headers=PAGE)
    assert reply.status_code == 200 and reply.json()["started"]
    app_job = client.app.state.job
    for _ in range(200):
        if app_job.snapshot()["state"] != "running":
            break
        import time

        time.sleep(0.05)
    snap = app_job.snapshot()
    assert snap["state"] == "done", snap
    assert snap["result"]["outlook"]["new"] == 1
    data = client.get("/api/settings", headers=PAGE).json()["outlook"]
    assert data["copied"] == 1 and data["last_result"] == "1 new"
    assert json.loads(store.get_state(outlook_sync.LAST_RESULT_KEY))["new"] == 1


def test_settings_say_no_when_the_page_sends_false(store, settings):
    client = TestClient(web.create_app(settings, store))
    reply = client.post("/api/settings/outlook", headers=PAGE, json={"auto": "false", "interval": 15, "days": 30, "folders": [], "subfolders": "false"})
    assert reply.status_code == 200
    assert outlook_sync.config(store)["auto"] is False and outlook_sync.config(store)["subfolders"] is False
    assert client.post("/api/settings/outlook", headers=PAGE, json={"auto": True, "days": 0}).status_code == 400, "0 days isn't a choice"


def test_the_automatic_sync_says_outlook_is_closed_once(store, settings, monkeypatch):
    monkeypatch.setattr(outlook_desktop, "available", lambda: "")

    @contextmanager
    def connect(**options):
        assert options == {"start": False}, "the automatic sync never starts Outlook"
        raise outlook_desktop.OutlookUnavailable("Outlook isn't open, so it wasn't synced. It syncs once Outlook is open.")
        yield

    monkeypatch.setattr(outlook_desktop, "connect", connect)
    first = outlook_run(store, settings, now=NOW, auto=True)
    again = outlook_run(store, settings, now=NOW + timedelta(minutes=15), auto=True)
    assert first["quiet"] is False and again["quiet"] is True


def test_the_scheduled_morning_run_never_starts_outlook(store, settings, outlook, monkeypatch):
    # The 6:30 task ("overnight") started Outlook unseen, with CloseDesk closed, where a sign-in prompt could wait.
    from controller_inbox import cli

    asked = []

    @contextmanager
    def connect(**options):
        asked.append(options)
        yield outlook

    monkeypatch.setattr(outlook_desktop, "available", lambda: "")
    monkeypatch.setattr(outlook_desktop, "connect", connect)
    monkeypatch.setattr(cli, "load_settings", lambda *a, **k: settings)
    store.set_state(outlook_sync.AUTO_KEY, "1")
    settings.ensure_data_dir()
    assert cli.main(["overnight", "--no-graph"]) == 0
    run_overnight(store, settings, sync_graph=False, now=NOW)  # CloseDesk starting: Outlook is started for it
    assert asked == [{"start": False}, {"start": True}]


def test_an_error_from_a_sync_ended_by_disconnect_isnt_shown_after_connecting_again(store, settings, monkeypatch):
    monkeypatch.setattr(outlook_desktop, "available", lambda: "")

    @contextmanager
    def connect(**_options):
        outlook_sync.disconnect(store)  # clicked while Outlook was being started
        raise outlook_desktop.OutlookUnavailable("Classic Outlook didn't answer.")
        yield

    monkeypatch.setattr(outlook_desktop, "connect", connect)
    outlook_run(store, settings, now=NOW)
    assert outlook_sync.last_failure(store) == {}, "kept after Disconnect"
    outlook_sync.note_failure(store, "Outlook isn't open, so it wasn't synced.", NOW)
    outlook_sync.connect(store, calendar=False)
    assert outlook_sync.last_failure(store) == {}, "connecting again starts afresh"


def test_disconnecting_while_the_folders_are_read_stops_the_reading(store, settings):
    # The folders' tables went on being read from Outlook, one after another, after the user said stop.
    fake = FakeOutlook(folders=("Inbox", *(f"Inbox/F{n}" for n in range(5)), "Deleted Items"))
    scan = fake.scan
    read = []

    def scanning(path, since):
        read.append(path)
        outlook_sync.disconnect(store)
        return scan(path, since)

    fake.scan = scanning
    outlook_sync.sync(store, settings, fake, now=NOW)
    assert read == ["Inbox"] and not store.get_state(outlook_sync.LAST_SYNC_KEY)


def test_a_first_message_that_cant_be_read_brings_the_sample_back(store, settings, outlook, as_of_now, monkeypatch):
    from controller_inbox import pipeline

    ingest_demo(store, settings, now=as_of_now)
    sample = len(store.list_emails(limit=100))
    outlook.add("e1", "Invoice INV-7741 from Northwind")

    real = pipeline.process_message

    def broken(raw, *args, **kwargs):
        if raw.source == "outlook":
            raise RuntimeError("unreadable")
        return real(raw, *args, **kwargs)

    monkeypatch.setattr(pipeline, "process_message", broken)
    report = sync(store, settings, outlook)
    assert report["failed"] and len(store.list_emails(limit=100)) == sample, "the board isn't left empty"
    # Said in words on Settings → Sync inbox ("1 couldn't be read: ..."), not as the bare error.
    assert report["failed"][0]["error"] == "A message in Inbox couldn't be copied (unreadable)."


def test_outlook_message_without_message_id_synced_and_dropped_is_stored_twice(store, settings):
    """Without a Message-ID, outlook_sync._copy_in names the email by its subject, sender, time and text, but the drop
    folder names the same message by the bytes of its file (mail_identity.file_id). So a message synced from classic
    Outlook and also saved into the drop folder (to bring its attachment along) is filed twice, with two sets of tasks
    and a "possible duplicate invoice" on the copy."""
    from controller_inbox import outlook_sync
    from controller_inbox.folder_mail import ingest_folder
    from msgfactory import write_msg
    from test_outlook_sync import FakeOutlook

    class ExchangeOutlook(FakeOutlook):
        # Internal Exchange mail saved as .msg: no transport headers, so no Message-ID.
        def save_copy(self, entry_id, store_id, dest):
            m = self.messages[entry_id]
            write_msg(dest, m["subject"], f"Hello,\n\n{m['subject']}. Thanks.", sender_email=m["sender"],
                      sender_name="Northwind AP", sent=m["received"], exchange=True)

    outlook = ExchangeOutlook()
    outlook.add("e1", "Invoice INV-7741 from Northwind", message_id="", sender="ap@taz.com")
    outlook_sync.sync(store, settings, outlook, now=NOW)
    assert len(store.list_emails(limit=50)) == 1
    m = outlook.messages["e1"]
    settings.ensure_data_dir()
    write_msg(settings.inbox_incoming / "Invoice INV-7741.msg", m["subject"], f"Hello,\n\n{m['subject']}. Thanks.",
              sender_email=m["sender"], sender_name="Northwind AP", sent=m["received"], exchange=True)
    ingest_folder(store, settings, now=NOW)

    stored = store.list_emails(limit=50)
    assert len(stored) == 1, [(e.id, e.source, e.flags) for e in stored]


def test_two_files_with_one_name_synced_from_outlook_each_keep_their_original(store, settings):
    """A vendor sends two invoices both called "Invoice.pdf". Dropped in as a .msg, the second is renamed
    "Invoice (2).pdf"; synced from classic Outlook, both are written to inbox/extracted/<id>/Invoice.pdf, the second
    over the first, so the first has no original: no Preview, no Page, no Download."""
    from controller_inbox import agent
    from controller_inbox.demo import make_pdf
    from msgfactory import PDF
    from test_outlook_sync import FakeOutlook, sync

    outlook = FakeOutlook()
    first = make_pdf([["Invoice INV-7741", "Total due $4,210.00"]])
    second = make_pdf([["Invoice INV-7742", "Total due $980.00"]])
    outlook.add("e1", "Two invoices from Northwind", attachments=[("Invoice.pdf", first, PDF), ("Invoice.pdf", second, PDF)])
    report = sync(store, settings, outlook)
    assert report["new"] == 1 and not report["failed"], report
    [email] = store.list_emails(limit=10)
    assert len(email.attachments) == 2
    originals = [agent.original_file(settings, email, att) for att in email.attachments]
    assert all(path is not None for path in originals), [(a.filename, p) for a, p in zip(email.attachments, originals)]


class NoHeaderOutlook(FakeOutlook):
    """Outlook whose saved copies carry no Internet headers: no Message-ID (an Exchange colleague's mail)."""

    def save_copy(self, entry_id, store_id, dest):
        m = self.messages[entry_id]
        self.copies += 1
        write_msg(dest, m["subject"], "Hello, see below.", sender_email=m["sender"], sender_name="Northwind AP",
                  sent=m["received"], exchange=True)


# Filed once, however it arrives ----------------------------------------------------------------------------
def test_outlook_message_without_a_subject_is_stored_without_one(store, settings):
    """A message from Outlook that has no subject is stored with an empty subject, not its copy's file name."""
    outlook = FakeOutlook()
    outlook.add("e1", "")
    report = sync(store, settings, outlook)
    assert report["new"] == 1, report
    [email] = store.list_emails(limit=10)
    assert not re.fullmatch(r"[0-9a-f]{24}", email.subject), email.subject
    assert email.subject == ""


def test_outlook_message_without_subject_or_message_id_in_two_folders_is_one_email(store, settings):
    """A message with no subject and no Message-ID, seen in two synced folders, is filed once."""
    outlook = NoHeaderOutlook()
    outlook.add("e1", "", message_id="")
    outlook.add("e2", "", message_id="", folder="Inbox/Vendors")
    report = sync(store, settings, outlook)
    assert (report["new"], report["already"]) == (1, 1), report
    assert len(store.list_emails(limit=10)) == 1


def test_outlook_message_reusing_a_message_id_without_a_subject_is_one_email(store, settings):
    """A subjectless Outlook message that reuses another email's Message-ID gets one id, in every synced folder."""
    settings.ensure_data_dir()
    write_msg(settings.inbox_incoming / "scan.msg", "Scan 1", "Scanned.", sender_email="scanner@taz.com",
              message_id="<scan@taz.com>", sent=NOW - timedelta(days=2))
    folder_mail.ingest_folder(store, settings, now=NOW)
    outlook = FakeOutlook()
    outlook.add("e2", "", message_id="<scan@taz.com>")
    outlook.add("e3", "", message_id="<scan@taz.com>", folder="Inbox/Vendors")
    report = sync(store, settings, outlook)
    assert (report["new"], report["already"]) == (1, 1), report
    assert len(store.list_emails(limit=10)) == 2


def test_outlook_message_already_read_from_microsoft_365_is_not_filed_again(store, settings):
    """A message already stored from Microsoft 365 is recognised by its Message-ID when Outlook sync meets it."""
    graph = RawMessage(
        id="AAMkAD-graph-1",
        subject="Invoice INV-7741 from Northwind",
        sender_name="Northwind AP",
        sender_email="ap@northwind.com",
        received_at=NOW,
        body_text="Hello,\n\nInvoice INV-7741 from Northwind. Thanks.",
        body_preview="",
        has_attachments=False,
        internet_message_id="<e1@northwind.com>",
        source="graph",
    )
    process_message(graph, store, settings, now=NOW)
    outlook = FakeOutlook()
    outlook.add("e1", "Invoice INV-7741 from Northwind")
    report = sync(store, settings, outlook)
    assert (report["new"], report["already"]) == (0, 1), report
    assert [e.id for e in store.list_emails(limit=10)] == ["AAMkAD-graph-1"]
    assert not store.outlook_items()["e1"]["owned"], "a deletion in Outlook doesn't remove the Microsoft 365 email"


def test_outlook_removal_that_fails_keeps_following_the_message(store, settings, monkeypatch):
    """A copy that can't be removed after a deletion in Outlook is still followed, so the next sync removes it."""
    outlook = FakeOutlook()
    outlook.add("e1", "Invoice INV-7741 from Northwind")
    sync(store, settings, outlook)
    del outlook.messages["e1"]

    def broken(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(store, "delete_emails", broken)
    try:
        sync(store, settings, outlook, now=NOW + timedelta(minutes=15))
    except RuntimeError:
        pass
    assert "e1" in store.outlook_items()
    monkeypatch.undo()
    report = sync(store, settings, outlook, now=NOW + timedelta(minutes=30))
    assert report["removed"] == 1 and not store.list_emails(limit=10)



# Settings → Sync inbox: nothing is read from Outlook until the user agrees -------------------------------------


def test_nothing_is_read_from_outlook_until_the_user_agrees(store, settings):
    store.set_state(outlook_sync.CONSENT_KEY, "")
    assert not outlook_sync.config(store)["consent"]

    class Refuses:
        def __getattr__(self, name):
            raise AssertionError(f"Outlook was read ({name}) before the user agreed")

    with pytest.raises(outlook_sync.NotConnected):
        outlook_sync.sync(store, settings, Refuses())
    assert not outlook_sync.due(store), "the automatic sync waits for the user's yes"


def test_someone_who_already_synced_is_not_asked_again(store):
    store.set_state(outlook_sync.CONSENT_KEY, "")
    store.set_state(outlook_sync.AUTO_KEY, "1")
    assert outlook_sync.config(store)["consent"], "they turned the sync on themselves before Settings asked"
    store.set_state(outlook_sync.AUTO_KEY, "0")
    store.set_state(outlook_sync.LAST_SYNC_KEY, "2026-09-01T00:00:00+00:00")
    assert outlook_sync.config(store)["consent"], "they synced by hand"
    assert not outlook_sync.config(store)["calendar"], "the calendar is a separate yes"


def test_connecting_turns_the_sync_on_and_disconnecting_keeps_the_copies(store):
    store.set_state(outlook_sync.CONSENT_KEY, "")
    outlook_sync.connect(store, calendar=True, now=datetime(2026, 10, 1, tzinfo=timezone.utc))
    cfg = outlook_sync.config(store)
    assert cfg["consent"] and cfg["auto"] and cfg["calendar"] and cfg["consented_at"].startswith("2026-10-01")
    store.clear_sample()
    assert outlook_sync.config(store)["consent"], "kept when the sample mailbox is loaded or cleared"
    outlook_sync.disconnect(store)
    cfg = outlook_sync.config(store)
    assert not cfg["consent"] and not cfg["auto"] and not cfg["calendar"]


def test_the_calendar_is_read_hourly_and_follows_outlook(store, settings):
    from controller_inbox import calendar_events

    now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
    calendar = [
        {"uid": "G1", "start": "2026-10-06T13:00:00+00:00", "end": "2026-10-06T13:15:00+00:00", "all_day": False, "subject": "AP standup",
         "location": "", "organizer": "Maya Chen", "attendees": [], "busy": "busy", "cancelled": False, "recurring": True},
        {"uid": "G2", "start": "2026-10-30", "end": "2026-10-31", "all_day": True, "subject": "Close day", "location": "", "organizer": "",
         "attendees": [], "busy": "away", "cancelled": False, "recurring": False},
    ]

    class Calendar:
        def calendar(self, start, end):
            assert start == now - calendar_events.WINDOW_BACK and end == now + calendar_events.WINDOW_AHEAD
            return [dict(item) for item in calendar]

    outlook_sync.set_calendar(store, True)

    assert outlook_sync.sync_calendar(store, Calendar(), now=now) == 2
    assert [m["subject"] for m in store.meetings_between("2026-10-01", "2026-11-01")] == ["AP standup", "Close day"]
    calendar.pop(0)  # deleted in Outlook
    outlook_sync.sync_calendar(store, Calendar(), now=now)
    assert [m["subject"] for m in store.meetings_between("2026-10-01", "2026-11-01")] == ["Close day"]
    assert not outlook_sync.calendar_due(store, now + timedelta(minutes=30))
    assert outlook_sync.calendar_due(store, now + timedelta(minutes=61))


def test_disconnecting_while_a_sync_runs_ends_it_there_and_is_final(store, settings):
    from controller_inbox import calendar_events

    outlook_sync.connect(store, calendar=True, now=datetime(2026, 10, 1, tzinfo=timezone.utc))
    store.replace_meetings("outlook", [calendar_events.Meeting(uid="G1", start="2026-10-06T13:00:00+00:00", end="2026-10-06T13:15:00+00:00",
                                                               subject="Standup", source="outlook")], start="2026-01-01", end="2027-01-01")

    class DisconnectsMidway:
        """Outlook with no mail: the user clicks Disconnect while its folders are being read."""

        def folders(self):
            outlook_sync.disconnect(store)
            return []

        def account(self):
            return ""

        def calendar(self, start, end):
            raise AssertionError("the calendar was read after Disconnect")

    outlook_sync.sync(store, settings, DisconnectsMidway())
    cfg = outlook_sync.config(store)
    assert not cfg["consent"] and not cfg["auto"] and not cfg["calendar"]
    assert not store.get_state(outlook_sync.LAST_SYNC_KEY), "no stamp that would read as a past sync"
    assert store.meetings_between("2026-01-01", "2027-01-01") == [], "Outlook's meetings go with Disconnect"


def test_saving_the_sync_settings_is_no_yes(store):
    store.set_state(outlook_sync.CONSENT_KEY, "")
    assert not outlook_sync.config(store)["consent"]
    outlook_sync.save_config(store, auto=True, interval=15, days=30, folders=[], subfolders=True)
    cfg = outlook_sync.config(store)
    assert not cfg["consent"] and not cfg["auto"] and not outlook_sync.due(store)


def test_turning_the_calendar_off_removes_outlooks_meetings_and_keeps_invitations(store):
    from controller_inbox import calendar_events

    outlook_sync.connect(store, calendar=True)
    store.replace_meetings("outlook", [calendar_events.Meeting(uid="G1", start="2026-10-06T13:00:00+00:00", end="2026-10-06T14:00:00+00:00", subject="Standup", source="outlook")],
                           start="2026-01-01", end="2027-01-01")
    store.save_invites("e1", [calendar_events.Meeting(uid="X", start="2026-10-07T13:00:00+00:00", end="2026-10-07T14:00:00+00:00", subject="Review", email_id="e1")])
    outlook_sync.set_calendar(store, False)
    assert [m["subject"] for m in store.meetings_between("2026-01-01", "2027-01-01")] == ["Review"]


def test_disconnecting_while_mail_is_copied_stops_the_copying(store, settings):
    fake = FakeOutlook()
    for n in range(5):
        fake.add(f"e{n}", f"Message {n}", minutes=n)
    copy = fake.save_copy
    asked = []

    def save_copy(entry_id, store_id, dest):
        asked.append(entry_id)
        copy(entry_id, store_id, dest)
        outlook_sync.disconnect(store)  # the user clicks Disconnect while the first message is copied

    fake.save_copy = save_copy
    report = outlook_sync.sync(store, settings, fake, now=NOW)
    assert len(asked) == 1, "no message is copied from Outlook after Disconnect"
    assert report["new"] <= 1 and not report["failed"]
    assert not store.get_state(outlook_sync.LAST_SYNC_KEY) and not store.get_state(outlook_sync.LAST_RESULT_KEY)


def test_disconnecting_or_turning_the_calendar_off_while_it_is_read_keeps_none_of_it(store, settings):
    for stop in (outlook_sync.disconnect, lambda store: outlook_sync.set_calendar(store, False)):
        outlook_sync.connect(store, calendar=True, now=NOW)
        store.set_state(outlook_sync.LAST_SYNC_KEY, "")
        fake = FakeOutlook()

        def calendar(start, end, stop=stop):
            stop(store)  # clicked while Outlook's calendar is being read
            return [{"uid": "abcd", "start": NOW.isoformat(), "end": NOW.isoformat(), "subject": "Board call", "all_day": False, "location": "",
                     "organizer": "", "attendees": [], "busy": "busy", "cancelled": False, "recurring": False}]

        fake.calendar = calendar
        outlook_sync.sync(store, settings, fake, now=NOW)
        with store.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM meetings WHERE source = 'outlook'").fetchone()[0] == 0
        assert not store.get_state(outlook_sync.CALENDAR_LAST_KEY)
        if stop is outlook_sync.disconnect:
            assert not store.get_state(outlook_sync.LAST_SYNC_KEY), "nothing stamped after Disconnect"


def test_the_calendar_page_and_the_chat_show_outlooks_meetings_only_while_it_is_connected(store, settings):
    from controller_inbox import assistant, calendar_events

    outlook_sync.connect(store, calendar=True, now=NOW)
    store.replace_meetings("outlook", [calendar_events.Meeting(uid="G1", start="2026-10-21T13:00:00+00:00", end="2026-10-21T13:15:00+00:00",
                                                               subject="Standup", source="outlook")], start="2026-10-01", end="2026-11-01")
    client = TestClient(web.create_app(settings, store))

    def shown():
        meetings = client.get("/api/calendar", params={"start": "2026-10-21", "end": "2026-10-21"}, headers=PAGE).json()["meetings"]
        source = assistant.calendar_source(store, settings, "2026-10-21")
        return [m["subject"] for m in meetings], bool(source and "Standup" in source.body_text)

    meeting_id = calendar_events.Meeting(uid="G1", start="2026-10-21T13:00:00+00:00", end="", source="outlook").id
    assert shown() == (["Standup"], True) and client.get(f"/api/calendar/{meeting_id}", headers=PAGE).status_code == 200
    # Left behind by a sync that was still running at Disconnect: not shown all the same.
    store.set_state(outlook_sync.CALENDAR_KEY, "0")
    assert shown() == ([], False) and client.get(f"/api/calendar/{meeting_id}", headers=PAGE).status_code == 404
    store.set_state(outlook_sync.CALENDAR_KEY, "1")
    store.set_state(outlook_sync.CONSENT_KEY, "revoked")
    assert shown() == ([], False)


def test_a_calendar_read_cut_short_by_outlooks_limit_keeps_the_meetings_past_it(store, settings):
    from controller_inbox import calendar_events, outlook_desktop

    now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
    outlook_sync.connect(store, calendar=True, now=now)
    later = calendar_events.Meeting(uid="G9", start="2026-12-01T13:00:00+00:00", end="2026-12-01T14:00:00+00:00", subject="Later", source="outlook")
    store.replace_meetings("outlook", [later], start="2026-01-01", end="2027-06-01")

    class CutShort:
        def calendar(self, start, end):
            # Outlook listed its limit, most of them meetings that ended before the window (left out), and stopped.
            return outlook_desktop.CalendarRead([{"uid": "G1", "start": "2026-10-06T13:00:00+00:00", "end": "2026-10-06T13:15:00+00:00",
                                                  "all_day": False, "subject": "AP standup", "location": "", "organizer": "", "attendees": [],
                                                  "busy": "busy", "cancelled": False, "recurring": True}], cut_short=True)

    outlook_sync.sync_calendar(store, CutShort(), now=now)
    assert [m["subject"] for m in store.meetings_between("2026-10-01", "2027-01-01")] == ["AP standup", "Later"]


def test_the_self_check_says_when_outlook_was_synced_in_the_apps_time_zone(store, settings):
    from controller_inbox import doctor

    outlook_sync.connect(store, calendar=True, now=NOW)
    store.set_state(outlook_sync.LAST_SYNC_KEY, "2026-10-20T15:00:00+00:00")
    store.set_state(outlook_sync.CALENDAR_LAST_KEY, "2026-10-20T14:30:00+00:00")
    store.set_state("timezone", "Europe/Berlin")  # chosen in Setup, over the settings file's New York
    said = doctor._outlook_sync(settings).text
    assert "17:00" in said and "16:30" in said and "UTC" not in said, said


def test_the_self_check_says_when_outlooks_syncs_dont_work(store, settings):
    # It said "connected, last synced Nov 01" in green while every sync since had failed.
    from controller_inbox import doctor

    outlook_sync.connect(store, calendar=False, now=NOW)
    store.set_state(outlook_sync.LAST_SYNC_KEY, "2026-10-20T15:00:00+00:00")
    outlook_sync.note_failure(store, "Outlook isn't open, so it wasn't synced. It syncs once Outlook is open.", NOW + timedelta(days=2))
    check = doctor._outlook_sync(settings)
    assert check.state == doctor.NOTE and "Outlook isn't open" in check.text and "last sync didn't work" in check.text
    assert "Sync now" in check.todo
    store.set_state(outlook_sync.LAST_ERROR_KEY, "")
    assert doctor._outlook_sync(settings).state == doctor.OK


def _meeting(uid, subject, start, end, *, all_day=False):
    return {"uid": uid, "start": start, "end": end, "all_day": all_day, "subject": subject, "location": "", "organizer": "",
            "attendees": [], "busy": "busy", "cancelled": False, "recurring": False}


def _keep(store, *meetings):
    from controller_inbox import calendar_events

    store.replace_meetings("outlook", [calendar_events.Meeting(**m, source="outlook") for m in meetings], start="2026-01-01", end="2027-06-01")


def test_a_calendar_outlook_couldnt_read_doesnt_undo_the_mail_sync(store, settings, outlook):
    # An error that wasn't Outlook's own (from listing the meetings) ended the sync after the mail was copied:
    # nothing stamped, the banner kept its old error, and the next sync tried the calendar again the same way.
    outlook_sync.connect(store, calendar=True, now=NOW)
    outlook.add("e1", "Invoice INV-7741 from Northwind", minutes=1)

    def calendar(start, end):
        raise ComError(-2147418111, "Call was rejected by callee.", None, None)

    outlook.calendar = calendar
    report = sync(store, settings, outlook)
    assert report["new"] == 1 and report["failed"] == [{"folder": "Calendar", "error": outlook_desktop.BUSY}]
    assert store.get_state(outlook_sync.LAST_SYNC_KEY) and not store.get_state(outlook_sync.CALENDAR_LAST_KEY)


def test_meetings_outlook_wouldnt_hand_over_arent_taken_for_deleted(store, settings):
    # Outlook's guard refused some meetings' details: they were skipped as if not there, and deleted here.
    now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
    outlook_sync.connect(store, calendar=True, now=now)
    _keep(store, _meeting("G1", "AP standup", "2026-10-06T13:00:00+00:00", "2026-10-06T13:15:00+00:00"),
          _meeting("G2", "Board call", "2026-10-07T13:00:00+00:00", "2026-10-07T14:00:00+00:00"))

    class Guarded:
        def calendar(self, start, end):
            return outlook_desktop.CalendarRead([_meeting("G1", "AP standup", "2026-10-06T13:00:00+00:00", "2026-10-06T13:15:00+00:00")], unreadable=1)

    outlook_sync.sync_calendar(store, Guarded(), now=now)
    assert [m["subject"] for m in store.meetings_between("2026-10-01", "2026-11-01")] == ["AP standup", "Board call"]


def test_the_calendar_keeps_meetings_on_its_first_day_that_ended_before_it(store):
    # The window's first day was cleared from midnight, though only meetings ending after the window's start are
    # read: one that morning was deleted, while one the day before was kept for good.
    outlook_sync.connect(store, calendar=True)
    now = datetime(2026, 10, 20, 15, tzinfo=timezone.utc)
    first = (now - timedelta(days=90)).date()
    before = first - timedelta(days=1)
    _keep(store, _meeting("A", "Day before", f"{before}T09:00:00+00:00", f"{before}T10:00:00+00:00"),
          _meeting("B", "First day", f"{first}T09:00:00+00:00", f"{first}T10:00:00+00:00"),
          _meeting("C", "Deleted in Outlook", f"{first}T16:00:00+00:00", f"{first}T17:00:00+00:00"))

    class Outlook:
        def calendar(self, start, end):
            return []

    outlook_sync.sync_calendar(store, Outlook(), now=now)
    assert [m["subject"] for m in store.meetings_between(str(before), str(first + timedelta(days=1)))] == ["Day before", "First day"]


def test_a_calendar_read_cut_short_keeps_the_meetings_it_may_not_have_reached(store):
    # Cut short at Outlook's limit, it removed every meeting up to the last start read: a second meeting at that
    # same time, and an all-day one on that date (Outlook lists it at local midnight), weren't read but went.
    now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
    outlook_sync.connect(store, calendar=True, now=now)
    _keep(store, _meeting("G0", "Deleted in Outlook", "2026-10-20T09:00:00+00:00", "2026-10-20T10:00:00+00:00"),
          _meeting("G2", "Second at 13:00", "2026-12-01T13:00:00+00:00", "2026-12-01T14:00:00+00:00"),
          _meeting("G3", "Close day", "2026-12-01", "2026-12-02", all_day=True))

    class CutShort:
        def calendar(self, start, end):
            return outlook_desktop.CalendarRead([_meeting("G1", "First at 13:00", "2026-12-01T13:00:00+00:00", "2026-12-01T14:00:00+00:00")], cut_short=True)

    outlook_sync.sync_calendar(store, CutShort(), now=now)
    assert sorted(m["subject"] for m in store.meetings_between("2026-10-01", "2027-01-01")) == ["Close day", "First at 13:00", "Second at 13:00"]
