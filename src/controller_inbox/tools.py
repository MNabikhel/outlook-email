"""JSON tools the local Bionic agent calls. stdout is JSON and nothing else.

The agent should not open the raw PDFs. prepare_queue already did that.
"""

from __future__ import annotations

import json
from datetime import datetime

from controller_inbox.actions import local_today
from controller_inbox.config import Settings
from controller_inbox.digest import build_digest, write_digest_files
from controller_inbox.models import FOLDERS, FOLDER_LABELS
from controller_inbox.reading import apply_bionic_reading, build_packet
from controller_inbox.store import Store


def queue_status(store: Store, settings: Settings) -> dict:
    counts = store.counts()
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
        "llm_enabled": settings.llm,
        "llm_base_url": settings.llm_base_url,
        "llm_model": settings.llm_model,
    }


def prepare_queue(store: Store, *, limit: int = 20) -> dict:
    waiting = store.list_emails(model_status="script_draft", oldest_first=True, limit=limit)
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
    }


def list_folder(store: Store, folder: str, *, limit: int = 50) -> dict:
    if folder not in FOLDERS:
        return {
            "ok": False,
            "error": "folder must be important, informational, or reference",
        }
    emails = store.list_emails(folder=folder, limit=limit)
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


def build_digest_tool(store: Store, settings: Settings, *, on: str | None = None) -> dict:
    now = datetime.now(settings.tz)
    as_of = datetime.strptime(on, "%Y-%m-%d").date() if on else local_today(settings.tz, now)
    payload = build_digest(store, as_of=as_of, generated_at=now)
    md_path, html_path = write_digest_files(payload, settings.digest_dir, as_of.isoformat())
    return {
        "ok": True,
        "date": as_of.isoformat(),
        "markdown_path": md_path,
        "html_path": html_path,
        "kpis": payload["kpis"],
        "folders": payload.get("folder_counts", {}),
        "markdown": payload["markdown"],
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
    return {
        "ok": False,
        "error": "Unknown tool. Use queue_status, prepare_queue, save_reading, list_folder, or build_digest.",
    }
