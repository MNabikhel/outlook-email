"""Copy mail from classic Outlook into CloseDesk, and keep the copies in step with Outlook. Read only.

Each sync reads the chosen folders' tables (``outlook_desktop.OutlookSession.scan``) for mail received in the last
``days`` days and compares them with what was copied before (the ``outlook_items`` table):

- a message not copied yet is saved as a .msg copy and read like one dropped in the drop folder;
- a copied message that was read or unread, flagged or moved in Outlook gets the same here;
- a copied message that is gone from the chosen folders is looked up: deleted (in Deleted Items, Junk, or gone
  for good) removes CloseDesk's copy; moved to a folder that isn't synced keeps it, noting where it went.

Nothing is ever written to Outlook. A folder that can't be read this time removes nothing, and only a message
Outlook says it doesn't have any more (or that is in Deleted Items or Junk) counts as deleted, so a hiccup in
Outlook can't empty the board.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from controller_inbox.config import Settings
from controller_inbox.outlook_desktop import FolderInfo, NotInOutlook, OutlookError, Seen
from controller_inbox.store import Store

log = logging.getLogger(__name__)

AUTO_KEY = "outlook_auto"
INTERVAL_KEY = "outlook_interval"
DAYS_KEY = "outlook_days"
FOLDERS_KEY = "outlook_folders"
SUBFOLDERS_KEY = "outlook_subfolders"
FOLDER_LIST_KEY = "outlook_folder_list"
ACCOUNT_KEY = "outlook_account"
LAST_SYNC_KEY = "outlook_last_sync"
LAST_RESULT_KEY = "outlook_last_result"

INTERVALS = (5, 15, 30, 60)  # minutes between automatic syncs while CloseDesk is open
DAYS = (7, 30, 90, 365)  # how far back mail is copied
DEFAULT_INTERVAL = 15
DEFAULT_DAYS = 30

Progress = Callable[[str, int, int, str], None]


def config(store: Store) -> dict:
    """The sync settings saved from Settings → Outlook."""
    interval = _int(store.get_state(INTERVAL_KEY), DEFAULT_INTERVAL)
    days = _int(store.get_state(DAYS_KEY), DEFAULT_DAYS)
    try:
        folders = [str(path) for path in json.loads(store.get_state(FOLDERS_KEY) or "[]")]
    except ValueError:
        folders = []
    return {
        "auto": store.get_state(AUTO_KEY) == "1",
        "interval": interval if interval in INTERVALS else DEFAULT_INTERVAL,
        "days": days if days in DAYS else DEFAULT_DAYS,
        "folders": folders,
        "subfolders": store.get_state(SUBFOLDERS_KEY) != "0",
    }


def save_config(store: Store, *, auto: bool, interval: int, days: int, folders: list[str], subfolders: bool) -> None:
    if interval not in INTERVALS:
        raise ValueError(f"Choose how often to sync: every {', '.join(map(str, INTERVALS))} minutes.")
    if days not in DAYS:
        raise ValueError(f"Choose how far back to copy: {', '.join(map(str, DAYS))} days.")
    known = {row["path"] for row in folder_list(store)}
    picked = [path for path in dict.fromkeys(folders) if not known or path in known]
    store.set_state(AUTO_KEY, "1" if auto else "0")
    store.set_state(INTERVAL_KEY, str(interval))
    store.set_state(DAYS_KEY, str(days))
    store.set_state(FOLDERS_KEY, json.dumps(picked))
    store.set_state(SUBFOLDERS_KEY, "1" if subfolders else "0")


def folder_list(store: Store) -> list[dict]:
    """Outlook's mail folders as last read (Check Outlook, or a sync)."""
    try:
        return list(json.loads(store.get_state(FOLDER_LIST_KEY) or "[]"))
    except ValueError:
        return []


def remember_folders(store: Store, session) -> list[FolderInfo]:
    infos = session.folders()
    rows = [{"path": f.path, "depth": f.depth, "inbox": f.inbox, "trash": f.trash} for f in infos]
    store.set_state(FOLDER_LIST_KEY, json.dumps(rows))
    account = session.account()
    if account:
        store.set_state(ACCOUNT_KEY, account)
    return infos


def chosen_paths(cfg: dict, infos: list[FolderInfo]) -> list[str]:
    """The folders a sync reads: the ones picked (the Inbox when none are), with their subfolders when asked.
    Deleted Items and Junk are never read."""
    picked = cfg["folders"] or [f.path for f in infos if f.inbox]
    paths = []
    for info in infos:
        if info.trash:
            continue
        under = any(info.path == path or (cfg["subfolders"] and info.path.startswith(path + "/")) for path in picked)
        if under:
            paths.append(info.path)
    return paths


def due(store: Store, now: datetime | None = None) -> bool:
    """Whether automatic sync is on and its interval has passed since the last sync."""
    cfg = config(store)
    if not cfg["auto"]:
        return False
    last = store.get_state(LAST_SYNC_KEY)
    if not last:
        return True
    try:
        then = datetime.fromisoformat(last)
    except ValueError:
        return True
    now = now or datetime.now(timezone.utc)
    return now - then >= timedelta(minutes=cfg["interval"])


def sync(store: Store, settings: Settings, session, *, now: datetime | None = None, on_progress: Progress | None = None) -> dict:
    """One sync. Returns what changed: ``new``, ``updated``, ``moved``, ``removed``, ``already``, ``failed``."""
    now = now or datetime.now(timezone.utc)
    cfg = config(store)
    infos = remember_folders(store, session)
    bins = [f.path for f in infos if f.trash]
    # A folder deleted in Outlook is moved into Deleted Items with its mail: everything under a bin is in the bin.
    trash = {f.path for f in infos if f.trash or any(f.path.startswith(b + "/") for b in bins)}
    paths = chosen_paths(cfg, infos)
    known = {f.path for f in infos}
    since = now - timedelta(days=cfg["days"])
    report: dict = {"new": 0, "updated": 0, "moved": 0, "removed": 0, "already": 0, "failed": [], "folders": paths, "new_ids": []}
    for path in cfg["folders"]:
        if path not in known:
            # Renamed or deleted in Outlook since it was picked: said, rather than syncing nothing in silence.
            report["failed"].append({"folder": path, "error": f"The folder {path} isn't in Outlook any more. Pick it again in Settings → Outlook."})
    if on_progress:
        on_progress("outlook", 0, 0, "Reading Outlook's folders")

    seen: list[Seen] = []
    read: set[str] = set()
    for path in paths:
        try:
            seen.extend(session.scan(path, since))
            read.add(path)
        except OutlookError as exc:
            report["failed"].append({"folder": path, "error": str(exc)[:300]})
        except Exception as exc:  # one folder Outlook trips on must not stop the others
            log.warning("Couldn't read the Outlook folder %s", path, exc_info=True)
            report["failed"].append({"folder": path, "error": str(exc)[:300]})

    tracked = store.outlook_items()
    by_message = {row["message_id"]: row for row in tracked.values() if row["message_id"]}
    current = {item.entry_id for item in seen}
    fresh: list[Seen] = []
    for item in seen:
        row = tracked.get(item.entry_id)
        if row is None and item.message_id:
            # Moved where Outlook gives it a new id (another mailbox or data file): the same message, so the copy
            # is kept and follows it, unless the old id is still in Outlook too (a copy of the message).
            old = by_message.get(item.message_id)
            # Received at the same time: a move keeps that, while another message reusing the Message-ID (a scanner
            # gives every scan the same one) doesn't.
            if old is not None and old["entry_id"] not in current and _same_time(old["received_at"], item.received):
                store.forget_outlook_item(old["entry_id"])
                tracked.pop(old["entry_id"], None)
                row = {**old, "entry_id": item.entry_id, "store_id": item.store_id}
                store.save_outlook_item(row)
                tracked[item.entry_id] = row
        if row is None:
            fresh.append(item)
            continue
        if (row["folder"], bool(row["unread"]), bool(row["flagged"])) != (item.folder, item.unread, item.flagged):
            if row["folder"] != item.folder:
                report["moved"] += 1
            else:
                report["updated"] += 1
            if row["owned"] and bool(row["unread"]) != item.unread:
                store.set_read(row["email_id"], not item.unread)
            store.save_outlook_item({**row, "folder": item.folder, "unread": item.unread, "flagged": item.flagged})

    fresh.sort(key=lambda item: item.received)
    for index, item in enumerate(fresh, start=1):
        if on_progress:
            on_progress("outlook", index, len(fresh), f"Copying from {item.folder}")
        try:
            email_id, owned, added = _copy_in(store, settings, session, item, now=now, report=report)
        except Exception as exc:
            log.warning("Couldn't copy an Outlook message from %s", item.folder, exc_info=True)
            report["failed"].append({"folder": item.folder, "error": str(exc)[:300]})
            continue
        store.save_outlook_item(
            {
                "entry_id": item.entry_id,
                "store_id": item.store_id,
                "email_id": email_id,
                "folder": item.folder,
                "message_id": item.message_id,
                "unread": item.unread,
                "flagged": item.flagged,
                "received_at": item.received.astimezone(timezone.utc).isoformat(),
                "owned": owned,
            }
        )
        if added:
            report["new"] += 1
            report["new_ids"].append(email_id)
        else:
            report["already"] += 1

    for row in list(tracked.values()):
        if row["entry_id"] in current:
            continue
        if row["folder"] in known and row["folder"] not in read:
            # Its folder couldn't be read this time, or isn't synced (no longer picked, or the message was moved
            # out of the synced folders before): left as it is. A folder that is gone from Outlook (deleted, so now
            # inside Deleted Items, or renamed) is looked into below.
            continue
        if _before(row["received_at"], since):
            continue  # older than the sync looks back: no longer followed
        try:
            where = session.locate(row["entry_id"], row["store_id"])
        except NotInOutlook:
            where = None
        except Exception:
            continue  # Outlook couldn't say: ask again next sync
        if where is None:
            # Gone under this id. A message moved to another folder can get a new id (Exchange, or another mailbox or
            # data file, such as an archive): found by its Message-ID, it was moved, not deleted.
            try:
                moved = _moved_copy(session.find_message(row["message_id"]), row, trash)
            except Exception:
                continue  # Outlook couldn't say: ask again next sync
            if moved is not None:
                store.forget_outlook_item(row["entry_id"])
                store.save_outlook_item({**row, "entry_id": moved.entry_id, "store_id": moved.store_id, "folder": moved.folder})
                report["moved"] += 1
                continue
        elif where not in trash:
            if where != row["folder"]:
                report["moved"] += 1
                store.save_outlook_item({**row, "folder": where})
            continue
        if _remove(store, settings, row):
            report["removed"] += 1

    stamp = now.astimezone(timezone.utc).isoformat()
    store.set_state(LAST_SYNC_KEY, stamp)
    store.set_state(
        LAST_RESULT_KEY,
        json.dumps({key: report[key] for key in ("new", "updated", "moved", "removed", "already")} | {"failed": len(report["failed"]), "at": stamp}),
    )
    return report


# A message keeps its received time when moved; two copies of one message saved apart can differ by a few seconds.
SAME_TIME = timedelta(minutes=2)


def _same_time(stamp: str, when: datetime) -> bool:
    try:
        then = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return False
    then = then if then.tzinfo else then.replace(tzinfo=timezone.utc)
    return abs(then - (when if when.tzinfo else when.replace(tzinfo=timezone.utc))) <= SAME_TIME


def _moved_copy(found: list[Seen], row: dict, trash: set[str]) -> Seen | None:
    """The message ``row`` follows, found by its Message-ID in a folder that isn't Deleted Items or Junk, and
    received when it was: it was moved and given a new id. None when it was deleted."""
    if not row.get("message_id"):
        return None
    for item in found:
        if item.message_id == row["message_id"] and item.folder not in trash and _same_time(row["received_at"], item.received):
            return item
    return None


def changed(report: dict) -> bool:
    return any(report.get(key) for key in ("new", "updated", "moved", "removed"))


def describe(report: dict) -> str:
    """"3 new, 1 removed in Outlook" — what a sync did, for a message on screen."""
    parts = [
        f"{report['new']} new" if report.get("new") else "",
        f"{report['removed']} deleted in Outlook" if report.get("removed") else "",
        f"{report['moved']} moved" if report.get("moved") else "",
        f"{report['updated']} updated" if report.get("updated") else "",
    ]
    said = ", ".join(part for part in parts if part) or "nothing new"
    if report.get("failed"):
        said += f"; {len(report['failed'])} couldn't be read"
    return said


def _copy_in(store: Store, settings: Settings, session, item: Seen, *, now: datetime, report: dict) -> tuple[str, bool, bool]:
    """Save a copy of the message, read it, and file it. Returns its email id, whether CloseDesk filed it from
    Outlook (``owned``: a deletion in Outlook removes it) and whether it is new here."""
    from controller_inbox.folder_mail import _another_message, _parse_msg, _restore_sample, _stable_id, _write_extracted
    from controller_inbox.pipeline import process_message

    key = hashlib.sha256(f"{item.store_id}:{item.entry_id}".encode()).hexdigest()[:24]
    staging = settings.data_dir / "outlook-copying" / f"{key}.msg"
    session.save_copy(item.entry_id, item.store_id, staging)
    try:
        raw = _parse_msg(staging)
        raw.source = "outlook"
        raw.is_read = not item.unread
        raw.outlook_importance = item.importance
        sent = raw.received_at.astimezone(timezone.utc).isoformat()
        if not raw.internet_message_id:
            # No Message-ID: named by what it says, so a copy of it in another synced folder (or the same message
            # under a new Outlook id) is the same email, and two different messages are two.
            said = "\n".join([raw.subject, raw.sender_email.lower(), sent, raw.body_text[:5000]])
            raw.id = "mail-" + hashlib.sha256(f"outlook:{said}".encode()).hexdigest()[:20]
        existing = store.get_email(raw.id)
        if existing is not None and raw.internet_message_id and _another_message(existing, raw):
            # Another message with the same Message-ID (scanners and some mail tools reuse one), as the drop
            # folder tells them apart.
            raw.id = _stable_id("\n".join([raw.internet_message_id, raw.subject, raw.sender_email, sent]))
            existing = store.get_email(raw.id)
        if existing is not None:
            # Already here: dropped in by hand, or the same message in two synced folders. Kept as it is, so the
            # tasks and corrections on it stay.
            return existing.id, existing.source == "outlook", False
        cleared = 0
        if not store.real_mail_count():
            cleared = store.clear_sample()
            report["sample_cleared"] = cleared
        try:
            _write_extracted(settings, raw)
            record = process_message(raw, store, settings, now=now)
        except Exception:
            if cleared:
                _restore_sample(store, settings, report, now)  # a message that couldn't be read never empties the board
            raise
        dest = copy_path(settings, record.id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging), dest)
        store.set_source_path(record.id, str(dest))
        return record.id, True, True
    finally:
        staging.unlink(missing_ok=True)


def copy_path(settings: Settings, email_id: str) -> Path:
    """Where the .msg copy of a synced message is kept (under the processed folder, so Open in Outlook opens it)."""
    return settings.inbox_processed / "outlook" / f"{email_id}.msg"


def _remove(store: Store, settings: Settings, row: dict) -> bool:
    """Stop following a message deleted in Outlook, and remove CloseDesk's copy. Returns whether the copy went: one
    the user dropped in by hand stays, and so does one still in another synced folder."""
    store.forget_outlook_item(row["entry_id"])
    if not row["owned"]:
        return False  # dropped in by hand: the user's own copy stays
    if any(other["email_id"] == row["email_id"] for other in store.outlook_items().values()):
        return False  # the same message is still in another synced folder
    store.delete_emails([row["email_id"]])
    copy_path(settings, row["email_id"]).unlink(missing_ok=True)
    shutil.rmtree(settings.inbox_extracted / row["email_id"], ignore_errors=True)
    return True


def _before(stamp: str, since: datetime) -> bool:
    try:
        when = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return False
    when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    return when < since


def _int(value: str | None, default: int) -> int:
    try:
        return int(value or default)
    except ValueError:
        return default
