"""One pass over the inbox: read the drop folder, let the local model read, write the digest.

Used by the overnight job, ``run``, ``watch``, and the dashboard's "Process
new mail" button. When no model is answering, every message is still filed
from the script draft, so the morning board is usable either way.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from controller_inbox import cost_codes
from controller_inbox.actions import local_today
from controller_inbox.config import Settings
from controller_inbox.digest import build_digest, write_digest_files
from controller_inbox.file_summaries import summarize_files
from controller_inbox.folder_mail import ingest_folder, reread_attachments
from controller_inbox.local_llm import LocalReader, check_model, llm_active
from controller_inbox.profile import is_finance
from controller_inbox.reading import build_packet, overlay_reading
from controller_inbox.semantic import index_mail
from controller_inbox.store import Store
from controller_inbox.vision import Result, read_waiting

log = logging.getLogger(__name__)

Progress = Callable[[str, int, int, str], None]

LOCK_NAME = "closedesk-run.lock"


class RunBusy(RuntimeError):
    """Another run (the scheduled job, the dashboard button, ``watch``) is reading the drop folder right now."""

    def __init__(self) -> None:
        super().__init__(
            "Another CloseDesk run is already processing mail (the scheduled run, watch, or the dashboard). "
            "This one was skipped; try again when it finishes."
        )


@contextmanager
def run_lock(settings: Settings) -> Iterator[bool]:
    """Hold the data folder's run lock while reading the drop folder and the queue. Yields False when another
    process or thread holds it. The operating system lets go if a run crashes, so a lock is never left stuck."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    handle = open(settings.data_dir / LOCK_NAME, "a+b")
    try:
        if not _try_lock(handle):
            yield False
            return
        try:
            yield True
        finally:
            _unlock(handle)
    finally:
        handle.close()


def _try_lock(handle) -> bool:
    try:
        if sys.platform.startswith("win"):
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle) -> None:
    try:
        if sys.platform.startswith("win"):
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def read_queue(
    store: Store,
    settings: Settings,
    *,
    limit: int | None = None,
    now: datetime | None = None,
    reader: LocalReader | None = None,
    on_progress: Progress | None = None,
) -> dict:
    """Let the local model read waiting messages, most important first."""
    now = now or datetime.now(timezone.utc)
    result = {"read_ids": [], "model": "", "note": "", "stats": None}
    if reader is None:
        if not llm_active(settings):
            result["note"] = check_model(settings).describe()
            return result
        with LocalReader(settings) as own_reader:
            return read_queue(store, settings, limit=limit, now=now, reader=own_reader, on_progress=on_progress)
    result["model"] = reader.model
    batch = limit if limit is not None else settings.overnight_batch
    # 0 means read none (SQLite would read everything for a negative LIMIT).
    waiting = store.list_emails(model_status="script_draft", order="queue", limit=max(0, batch))
    corrections = store.list_corrections()
    for index, email in enumerate(waiting, start=1):
        if on_progress:
            on_progress("reading", index, len(waiting), email.subject)
        packet = build_packet(email, corrections)
        parsed = reader.read(packet)
        if reader.stopped:
            break
        if not parsed:
            continue
        # The model takes a while. Meanwhile the user may have corrected this email or given a fraud verdict
        # on it, or another reader may have saved one. The reading is saved onto the email as it is now, and
        # only while it is still the draft the model read; otherwise it waits for the next run.
        current = store.get_email(email.id)
        if current is None or current.model_status != "script_draft" or build_packet(current, corrections) != packet:
            continue
        overlay_reading(current, parsed, now=now, tz=settings.tz)
        store.upsert_email(current)
        result["read_ids"].append(email.id)
    result["stats"] = reader.stats
    if reader.stats.stopped_reason:
        result["note"] = f"Stopped reading: {reader.stats.stopped_reason}."
    return result


def run_overnight(
    store: Store,
    settings: Settings,
    *,
    now: datetime | None = None,
    limit: int | None = None,
    sync_graph: bool = True,
    reader: LocalReader | None = None,
    on_progress: Progress | None = None,
    vision_minutes: float | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    """One full pass. Raises ``RunBusy`` instead of racing another run over the same drop folder. ``vision_minutes``:
    how long it may spend reading scanned pages with the vision model (default ``Settings.vision_minutes_per_run``);
    ``should_stop``: the reading of scans ends after the page being read when it says so."""
    with run_lock(settings) as locked:
        if not locked:
            raise RunBusy()
        return _run_overnight(
            store, settings, now=now, limit=limit, sync_graph=sync_graph, reader=reader, on_progress=on_progress,
            vision_minutes=vision_minutes, should_stop=should_stop,
        )


def _run_overnight(
    store: Store,
    settings: Settings,
    *,
    now: datetime | None,
    limit: int | None,
    sync_graph: bool,
    reader: LocalReader | None,
    on_progress: Progress | None,
    vision_minutes: float | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    settings.ensure_data_dir()
    folder_report: dict = {}
    ingested = ingest_folder(
        store,
        settings,
        now=now,
        report=folder_report,
        on_progress=(lambda i, n, name: on_progress("importing", i, n, name)) if on_progress else None,
    )
    reread = reread_attachments(
        store, settings, on_progress=(lambda i, n, name: on_progress("rereading", i, n, name)) if on_progress else None
    )
    graph_note = ""
    graph_count = 0
    if sync_graph and settings.graph_configured:
        graph_count, graph_note = _sync_graph(store, settings, now)

    reading = read_queue(store, settings, limit=limit, now=now, reader=reader, on_progress=on_progress)
    # After reading: the model may have filed more mail as AP invoices, and the workbook may have changed.
    coded = cost_codes.refresh(store, settings)
    stats = reading["stats"]
    # Scans first, so the summaries below are written from the pages as read both ways.
    looked = Result()
    if reading["model"] and not (stats and stats.stopped_reason):
        try:
            looked = read_waiting(
                store, settings, minutes=vision_minutes, should_stop=should_stop,
                on_progress=(lambda i, n, name: on_progress("vision", i, n, name)) if on_progress else None,
            )
        except Exception:  # the rest of the run (search index, digest) still happens
            log.warning("Reading scans with the vision model failed", exc_info=True)
    summarized = 0
    if reading["model"] and not (stats and stats.stopped_reason) and settings.overnight_file_summaries:
        summarized = summarize_files(
            store,
            settings,
            limit=settings.overnight_file_summaries,
            model=reading["model"],
            on_progress=(lambda i, n, name: on_progress("summarizing", i, n, name)) if on_progress else None,
        )
    # Runs whenever an embedding model answers, even with no chat model loaded.
    indexed = max(0, index_mail(store, settings, on_progress=(lambda i, n, _name: on_progress("indexing", i, n, "")) if on_progress else None))

    if on_progress:
        on_progress("digest", 1, 1, "Writing the digest")
    as_of = local_today(settings.tz, now)
    payload = build_digest(
        store,
        as_of=as_of,
        generated_at=now.astimezone(settings.tz),
        tz=settings.tz,
        lookback_days=settings.digest_lookback_days,
        finance=is_finance(settings, store),
    )
    digest_md, _digest_html = write_digest_files(payload, settings.digest_dir, as_of.isoformat())
    counts = store.counts()
    summary = {
        "ok": True,
        "date": as_of.isoformat(),
        "ingested": len(ingested),
        "already_read": folder_report.get("already_read", 0),
        "failed_files": folder_report.get("failed", []),
        "graph_synced": graph_count,
        "graph_note": graph_note,
        "read_by_bionic": len(reading["read_ids"]),
        "files_summarized": summarized,
        "pages_seen": looked.pages,
        "files_reread": reread,
        "invoices_coded": coded,
        "indexed_for_search": indexed,
        "waiting_on_bionic": counts["waiting_on_bionic"],
        "folders": {
            "important": counts["important"],
            "informational": counts["informational"],
            "reference": counts["reference"],
        },
        "fraud_alerts": counts["fraud_alerts"],
        "digest_path": digest_md,
        "headline": payload["headline"],
        "model": reading["model"],
        "model_note": reading["note"],
        "avg_seconds": round(stats.average_seconds, 1) if stats else 0.0,
        "llm_enabled": bool(reading["model"]),
    }
    log_path = settings.overnight_dir / f"{as_of.isoformat()}.md"
    log_path.write_text(_log(summary, store=store), encoding="utf-8")
    summary["log_path"] = str(log_path)
    store.set_state("last_overnight_at", now.astimezone(timezone.utc).isoformat())
    return summary


def _sync_graph(store: Store, settings: Settings, now: datetime) -> tuple[int, str]:
    try:
        from controller_inbox.cli import _graph_mailbox
        from controller_inbox.pipeline import ingest_mailbox

        mailbox = _graph_mailbox(settings)
        last = store.get_state("last_sync_at")
        after = datetime.fromisoformat(last) if last else now - timedelta(hours=settings.lookback_hours)
        records = ingest_mailbox(mailbox, store, settings, received_after=after, now=now)
        return len(records), ""
    except Exception as exc:
        return 0, str(exc)


def _log(summary: dict, *, store: Store) -> str:
    folders = summary["folders"]
    lines = [
        f"# CloseDesk run — {summary['date']}",
        "",
        f"**{summary['headline']}**",
        "",
        f"- Drop folder messages read: {summary['ingested']}",
        f"- Already filed earlier (skipped): {summary['already_read']}",
        f"- Files that could not be read (moved to inbox/failed): {len(summary['failed_files'])}",
        f"- Outlook messages pulled: {summary['graph_synced']}",
        f"- Read by Bionic this run: {summary['read_by_bionic']}"
        + (f" (about {summary['avg_seconds']}s each)" if summary["avg_seconds"] else ""),
        f"- Still waiting on Bionic: {summary['waiting_on_bionic']}",
        f"- Attachments summarized for Ask CloseDesk: {summary.get('files_summarized', 0)}",
        f"- Scanned pages read with the vision model: {summary.get('pages_seen', 0)}",
        f"- Emails and file sections added to search by meaning: {summary.get('indexed_for_search', 0)}",
        f"- Important / informational / reference: {folders['important']} / {folders['informational']} / {folders['reference']}",
        f"- Fraud alerts: {summary['fraud_alerts']}",
        f"- Digest: {summary['digest_path']}",
        "",
    ]
    for row in summary["failed_files"]:
        lines.append(f"Could not read {row['file']}: {row['error']}")
    if summary["graph_note"]:
        lines += [f"Outlook sync note: {summary['graph_note']}", ""]
    if summary["model"]:
        lines.append(f"Model: {summary['model']}")
        if summary["model_note"]:
            lines.append(summary["model_note"])
    else:
        lines += [
            "Bionic was off. Every message is a script draft in a folder, with text, amounts, and dates already pulled.",
            summary["model_note"] or "",
            "Start the LM Studio server with a model loaded, and run this again.",
            "Or open the closedesk-inbox skill in Bionic Studio and let the agent call the tools.",
        ]
    lines += ["", "## Still waiting"]
    waiting = store.list_emails(model_status="script_draft", order="queue", limit=30)
    if waiting:
        for email in waiting:
            lines.append(f"- {email.subject} — {email.summary}")
    else:
        lines.append("- None. The queue is clear.")
    lines += ["", "## Important"]
    for email in store.list_emails(folder="important", order="score", limit=20):
        mark = "Bionic" if email.model_status == "bionic" else email.model_status
        lines.append(f"- [{mark}] {email.subject} — {email.summary}")
    lines.append("")
    return "\n".join(lines)
