"""Unattended night run.

Read the drop folder, ask the local model to file anything still in draft,
write the morning digest, and leave a short log. When the model is off, the
folders are still filled from the script draft so the morning board is usable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from controller_inbox.actions import local_today
from controller_inbox.config import Settings
from controller_inbox.digest import build_digest, write_digest_files
from controller_inbox.folder_mail import ingest_folder
from controller_inbox.local_llm import read_packet, resolve_model
from controller_inbox.reading import build_packet, overlay_reading
from controller_inbox.store import Store


def run_overnight(
    store: Store,
    settings: Settings,
    *,
    now: datetime | None = None,
    limit: int | None = None,
    sync_graph: bool = True,
) -> dict:
    now = now or datetime.now(timezone.utc)
    settings.ensure_data_dir()
    ingested = ingest_folder(store, settings, now=now)
    graph_note = ""
    graph_count = 0
    if sync_graph and settings.graph_configured:
        graph_count, graph_note = _sync_graph(store, settings, now)

    batch = limit if limit is not None else settings.overnight_batch
    read_ids: list[str] = []
    model_name = ""
    if settings.llm:
        model_name = resolve_model(settings)
        corrections = store.list_corrections()
        waiting = store.list_emails(model_status="script_draft", oldest_first=True, limit=max(1, batch))
        for email in waiting:
            parsed = read_packet(settings, build_packet(email, corrections))
            if not parsed:
                continue
            overlay_reading(email, parsed, now=now)
            store.upsert_email(email)
            read_ids.append(email.id)

    as_of = local_today(settings.tz, now)
    payload = build_digest(store, as_of=as_of, generated_at=now)
    digest_md, _digest_html = write_digest_files(payload, settings.digest_dir, as_of.isoformat())
    counts = store.counts()
    log_path = settings.overnight_dir / f"{as_of.isoformat()}.md"
    log_path.write_text(
        _log(
            as_of=as_of.isoformat(),
            ingested=len(ingested),
            graph_count=graph_count,
            graph_note=graph_note,
            read_ids=read_ids,
            model_name=model_name,
            llm=settings.llm,
            counts=counts,
            digest_path=digest_md,
            store=store,
        ),
        encoding="utf-8",
    )
    store.set_state("last_overnight_at", now.astimezone(timezone.utc).isoformat())
    return {
        "ok": True,
        "date": as_of.isoformat(),
        "ingested": len(ingested),
        "graph_synced": graph_count,
        "read_by_bionic": len(read_ids),
        "waiting_on_bionic": counts["waiting_on_bionic"],
        "folders": {
            "important": counts["important"],
            "informational": counts["informational"],
            "reference": counts["reference"],
        },
        "fraud_alerts": counts["fraud_alerts"],
        "log_path": str(log_path),
        "digest_path": digest_md,
        "model": model_name,
        "llm_enabled": settings.llm,
    }


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


def _log(
    *,
    as_of: str,
    ingested: int,
    graph_count: int,
    graph_note: str,
    read_ids: list[str],
    model_name: str,
    llm: bool,
    counts: dict,
    digest_path: str,
    store: Store,
) -> str:
    lines = [
        f"# CloseDesk overnight — {as_of}",
        "",
        f"- Drop folder messages read: {ingested}",
        f"- Outlook messages pulled: {graph_count}",
        f"- Read by Bionic this run: {len(read_ids)}",
        f"- Still waiting on Bionic: {counts['waiting_on_bionic']}",
        f"- Important / informational / reference: {counts['important']} / {counts['informational']} / {counts['reference']}",
        f"- Fraud alerts: {counts['fraud_alerts']}",
        f"- Digest: {digest_path}",
        "",
    ]
    if graph_note:
        lines.append(f"Outlook sync note: {graph_note}")
        lines.append("")
    if llm:
        lines.append(f"Model: {model_name or 'not resolved'}")
    else:
        lines += [
            "Bionic was off. Every message is a script draft in a folder, with text, amounts, and dates already pulled.",
            "Start LM Studio, set CONTROLLER_INBOX_LLM=true, and run this again.",
            "Or open the closedesk-inbox skill in Bionic Studio and let the agent call the tools.",
        ]
    lines += ["", "## Still waiting"]
    waiting = store.list_emails(model_status="script_draft", oldest_first=True, limit=30)
    if waiting:
        for email in waiting:
            lines.append(f"- {email.subject} — {email.summary}")
    else:
        lines.append("- None. The queue is clear.")
    lines += ["", "## Important"]
    for email in store.list_emails(folder="important", limit=20):
        mark = "Bionic" if email.model_status == "bionic" else email.model_status
        lines.append(f"- [{mark}] {email.subject} — {email.summary}")
    lines.append("")
    return "\n".join(lines)
