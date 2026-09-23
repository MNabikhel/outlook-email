"""One pass over the inbox: read the drop folder, let the local model read, write the digest.

Used by the overnight job, ``run``, ``watch``, and the dashboard's "Process
new mail" button. When no model is answering, every message is still filed
from the script draft, so the morning board is usable either way.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from controller_inbox.actions import local_today
from controller_inbox.config import Settings
from controller_inbox.digest import build_digest, write_digest_files
from controller_inbox.folder_mail import ingest_folder
from controller_inbox.local_llm import LocalReader, check_model, llm_active
from controller_inbox.profile import is_finance
from controller_inbox.reading import build_packet, overlay_reading
from controller_inbox.store import Store

Progress = Callable[[str, int, int, str], None]


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
        reader = LocalReader(settings)
    result["model"] = reader.model
    batch = limit if limit is not None else settings.overnight_batch
    waiting = store.list_emails(model_status="script_draft", order="queue", limit=max(1, batch))
    corrections = store.list_corrections()
    for index, email in enumerate(waiting, start=1):
        if on_progress:
            on_progress("reading", index, len(waiting), email.subject)
        parsed = reader.read(build_packet(email, corrections))
        if reader.stopped:
            break
        if not parsed:
            continue
        overlay_reading(email, parsed, now=now)
        store.upsert_email(email)
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
    graph_note = ""
    graph_count = 0
    if sync_graph and settings.graph_configured:
        graph_count, graph_note = _sync_graph(store, settings, now)

    reading = read_queue(store, settings, limit=limit, now=now, reader=reader, on_progress=on_progress)
    stats = reading["stats"]

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
