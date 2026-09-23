"""JSON tools the local Bionic agent calls. stdout is JSON and nothing else.

The agent should not open the raw PDFs. prepare_queue already did that.
"""

from __future__ import annotations

import json
from datetime import datetime

from controller_inbox.actions import local_today
from controller_inbox.config import Settings
from controller_inbox.digest import build_digest, write_digest_files
from controller_inbox.local_llm import check_model
from controller_inbox.models import FOLDERS, FOLDER_LABELS
from controller_inbox.profile import is_finance
from controller_inbox.reading import apply_bionic_reading, build_packet
from controller_inbox.store import Store

TOOL_NAMES = [
    "queue_status",
    "prepare_queue",
    "save_reading",
    "list_folder",
    "build_digest",
    "focus",
    "digest_history",
]


def queue_status(store: Store, settings: Settings) -> dict:
    counts = store.counts()
    model = check_model(settings)
    return {
        "ok": True,
        "emails": counts["emails"],
        "waiting_on_bionic": counts["waiting_on_bionic"],
        "read_by_bionic": counts["read_by_bionic"],
        "folders": {
            "important": counts["important"],
            "informational": counts["informational"],
            "reference": counts["reference"],
        },
        "open_actions": counts["open_actions"],
        "fraud_alerts": counts["fraud_alerts"],
        "llm_enabled": model.active,
        "llm_mode": settings.llm_mode,
        "llm_base_url": settings.llm_base_url,
        "llm_model": model.model or settings.llm_model,
        "llm_message": model.describe(),
    }


def prepare_queue(store: Store, *, limit: int = 20) -> dict:
    waiting = store.list_emails(model_status="script_draft", order="queue", limit=limit)
    corrections = store.list_corrections()
    packets = [build_packet(email, corrections) for email in waiting]
    return {"ok": True, "count": len(packets), "packets": packets}


def save_reading(store: Store, payload: dict) -> dict:
    email_id = str(payload.get("email_id") or "").strip()
    if not email_id:
        return {"ok": False, "error": "email_id is required"}
    try:
        email = apply_bionic_reading(store, email_id, payload)
    except KeyError:
        return {"ok": False, "error": f"No message with id {email_id}"}
    return {
        "ok": True,
        "email_id": email.id,
        "category": email.category.value,
        "folder": email.folder,
        "folder_label": FOLDER_LABELS.get(email.folder, email.folder),
        "importance": email.importance.value,
        "summary": email.summary,
        "model_status": email.model_status,
        "flags": email.flags,
        "actions": [item.title for item in email.actions],
        "guard_notes": [item[len("Guard: "):] for item in email.importance_reasons if item.startswith("Guard: ")],
    }


def list_folder(store: Store, folder: str, *, limit: int = 50) -> dict:
    if folder not in FOLDERS:
        return {
            "ok": False,
            "error": "folder must be important, informational, or reference",
        }
    emails = store.list_emails(folder=folder, order="score", limit=limit)
    return {
        "ok": True,
        "folder": folder,
        "count": len(emails),
        "emails": [
            {
                "email_id": email.id,
                "subject": email.subject,
                "sender": email.sender_name or email.sender_email,
                "summary": email.summary,
                "category": email.category.value,
                "importance": email.importance.value,
                "model_status": email.model_status,
                "flags": email.flags,
            }
            for email in emails
        ],
    }


def _digest(store: Store, settings: Settings, on: str | None) -> dict:
    now = datetime.now(settings.tz)
    as_of = datetime.strptime(on, "%Y-%m-%d").date() if on else local_today(settings.tz, now)
    payload = build_digest(
        store,
        as_of=as_of,
        generated_at=now,
        tz=settings.tz,
        lookback_days=settings.digest_lookback_days,
        finance=is_finance(settings, store),
    )
    md_path, html_path = write_digest_files(payload, settings.digest_dir, as_of.isoformat())
    return payload | {"markdown_path": md_path, "html_path": html_path}


def build_digest_tool(store: Store, settings: Settings, *, on: str | None = None) -> dict:
    payload = _digest(store, settings, on)
    return {
        "ok": True,
        "date": payload["date"],
        "headline": payload["headline"],
        "markdown_path": payload["markdown_path"],
        "html_path": payload["html_path"],
        "kpis": payload["kpis"],
        "folders": payload.get("folder_counts", {}),
        "markdown": payload["markdown"],
    }


def focus_tool(store: Store, settings: Settings, *, on: str | None = None) -> dict:
    """What the manager should do first today, ranked. Answer 'what do I need to do?' from this."""
    payload = _digest(store, settings, on)
    return {
        "ok": True,
        "date": payload["date"],
        "headline": payload["headline"],
        "do_not_process": [
            {"email_id": row["id"], "subject": row["subject"], "sender": row["sender"], "summary": row["summary"]}
            for row in payload["critical_alerts"]
        ],
        "focus": [
            {
                key: row.get(key)
                for key in ("rank", "label", "title", "summary", "sender", "due", "email_id", "action_id", "more_tasks")
            }
            for row in payload["focus"]
        ],
        "coming_up": [
            {"title": row["title"], "due": row["due_date"], "email_id": row["email_id"]}
            for row in payload["due_this_week"]
        ],
    }


def digest_history(store: Store, *, limit: int = 30) -> dict:
    rows = store.list_digests(limit=limit)
    return {
        "ok": True,
        "count": len(rows),
        "digests": [
            {"date": row["period_date"], "headline": row["headline"], "kpis": row["kpis"]} for row in rows
        ],
    }


def dispatch(store: Store, settings: Settings, name: str, args) -> dict:
    if name == "queue_status":
        return queue_status(store, settings)
    if name == "prepare_queue":
        return prepare_queue(store, limit=args.limit)
    if name == "save_reading":
        raw = args.json
        if not raw:
            return {"ok": False, "error": "Pass the reading JSON with --json or on stdin."}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"Reading JSON is not valid: {exc}"}
        if not isinstance(payload, dict):
            return {"ok": False, "error": "Reading JSON must be an object."}
        return save_reading(store, payload)
    if name == "list_folder":
        return list_folder(store, args.folder, limit=args.limit)
    if name == "build_digest":
        return build_digest_tool(store, settings, on=args.date)
    if name == "focus":
        return focus_tool(store, settings, on=args.date)
    if name == "digest_history":
        return digest_history(store, limit=args.limit)
    return {"ok": False, "error": "Unknown tool. Use one of: " + ", ".join(TOOL_NAMES) + "."}
