"""The JSON the workspace at /app is built from, and the page that hosts it.

The classic pages render on the server; the workspace fetches these endpoints and renders in the browser,
so moving between folders and emails never reloads the page. Everything here reuses what the classic
pages use (the store's queries, ``build_digest`` for the focus list, the fraud, coding and correction
helpers), so both views always agree.

Every request under /api must come from CloseDesk's own page: it carries the X-CloseDesk header (which
another website cannot add without the browser asking this server first, and it never agrees), and a
browser that says the request came from another site is refused, as the classic forms are. Files of an
email held as possible payment fraud are never shown here: not their text, not their tables.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from controller_inbox import agent, chats, cost_codes, documents, fraud, semantic, table_lookup, vision
from controller_inbox.digest import build_digest
from controller_inbox.local_llm import check_model
from controller_inbox.models import DOCUMENT_LABELS, FOLDER_LABELS, IMPORTANCE_LABELS, ActionStatus, EmailRecord
from controller_inbox.profile import active_profile, is_finance
from controller_inbox.classify import month_end
from controller_inbox.clock import format_when
from controller_inbox.web import DOWNLOADABLE, _json_body, _require_page, _same_origin, templates

UI_DIR = Path(__file__).parent / "static" / "ui"
# Each script is fetched by a URL that changes with its contents, so an update never runs beside an old copy.
UI_VERSION = hashlib.sha256(
    b"".join(path.name.encode() + path.read_bytes() for path in sorted(UI_DIR.glob("*")) if path.is_file())
).hexdigest()[:10]

MAX_TABLES = 40
MAX_TABLE_ROWS = 1500
CLIP_CHARS = 280
LOCKED_MESSAGE = (
    "This email is flagged as possible payment fraud, so its files don't open. "
    "Verify it by phone, then mark it safe in its fraud check."
)
STAGES = {
    "starting": "Starting",
    "importing": "Reading files",
    "rereading": "Re-reading attachments",
    "reading": "Local model reading",
    "summarizing": "Summarizing attachments",
    "vision": "Reading scans with the vision model",
    "indexing": "Indexing for search",
    "digest": "Writing the digest",
}


def page_only(request: Request) -> None:
    """The workspace's own scripts, and nothing a page on another site can send."""
    headers = dict(request.scope.get("headers") or [])
    if not _same_origin(headers, headers.get(b"host", b"").decode("latin-1")):
        raise HTTPException(status_code=403, detail="Use the CloseDesk page for this.")
    _require_page(request)


def _money(value: float | None) -> str:
    return "" if value is None else f"${value:,.2f}"


def _size(size: int) -> str:
    size = size or 0
    if size >= 1_000_000:
        return f"{size / 1_000_000:.1f} MB"
    if size >= 1_000:
        return f"{round(size / 1_000)} KB"
    return f"{size} B"


def _local(value: str, tz) -> datetime | None:
    try:
        parsed = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(tz)


def short_when(value: str, tz, today: date) -> str:
    """``10:30`` today, ``Sep 22`` this year, ``Sep 22, 2025`` before."""
    local = _local(value, tz)
    if local is None:
        return value or ""
    if local.date() == today:
        return local.strftime("%H:%M")
    if local.year == today.year:
        return f"{local.strftime('%b')} {local.day}"
    return f"{local.strftime('%b')} {local.day}, {local.year}"


def due_label(due: str, today: date) -> str:
    """``Due today``, ``Due tomorrow``, ``Due Fri Sep 25``, ``Overdue · Mon Sep 21``."""
    if not due:
        return ""
    try:
        when = date.fromisoformat(due)
    except ValueError:
        return f"Due {due}"
    days = (when - today).days
    if days == 0:
        return "Due today"
    if days == 1:
        return "Due tomorrow"
    label = f"{when.strftime('%a')} {when.strftime('%b')} {when.day}" + ("" if when.year == today.year else f", {when.year}")
    return f"Overdue · {label}" if days < 0 else f"Due {label}"


def _category(email_category) -> dict[str, str]:
    return {"category": email_category.value, "category_label": DOCUMENT_LABELS.get(email_category, email_category.value)}


def mail_card(email: EmailRecord, tz, today: date, *, done: bool | None = None) -> dict[str, Any]:
    """One row of a mail list: enough to read it at a glance, nothing from its files."""
    return {
        "id": email.id,
        "subject": email.subject or "(no subject)",
        "sender": email.sender_name or email.sender_email,
        "sender_email": email.sender_email,
        "received_at": email.received_at,
        "when": short_when(email.received_at, tz, today),
        "when_full": format_when(email.received_at, tz),
        "importance": email.importance.value,
        "importance_label": IMPORTANCE_LABELS.get(email.importance, email.importance.value),
        "score": email.importance_score,
        **_category(email.category),
        "folder": email.folder,
        "flags": email.flags,
        "summary": email.summary,
        "why": next((reason for reason in email.importance_reasons if reason), ""),
        "amount": email.extracted.primary_amount,
        "amount_label": _money(email.extracted.primary_amount),
        "invoice": email.extracted.primary_invoice or "",
        "due": email.extracted.primary_due or "",
        "files": len(email.attachments),
        "open_tasks": sum(1 for action in email.actions if action.status == ActionStatus.OPEN),
        "locked": fraud.attachments_locked(email),
        "caution": "payment_caution" in email.flags,
        "model_status": email.model_status,
        "done": done,
    }


def table_json(table: table_lookup.Table, number: int) -> dict[str, Any]:
    """A table as the reader found it, with how its printed totals check out against its rows."""
    verdict = table_lookup.verify(table)
    labels = list(table.labels)
    rows = []
    for row in table.rows[:MAX_TABLE_ROWS]:
        refs = {label: ref for (label, _value), ref in zip(row.cells, row.refs)}
        # The total cells a check message is about ("Totals (Net pay): printed 6,982.60, ..."), to mark on the grid.
        flagged = [
            next((message for message in verdict.mismatched if row.total and message.startswith(f"{row.name} ({label}): printed {row.value(label)},")), "")
            for label in labels
        ]
        rows.append(
            {
                "cells": [row.value(label) for label in labels],
                "refs": [refs.get(label, "") for label in labels],
                "flags": flagged,
                "total": row.total,
                "name": row.name,
                "group": row.group,
                "page": row.page,
            }
        )
    return {
        "n": number,
        "where": table.where,
        "labels": labels,
        "kinds": [table.kinds.get(label, "text") for label in labels],
        "rows": rows,
        "truncated": max(0, len(table.rows) - MAX_TABLE_ROWS),
        "check": {"matched": verdict.matched, "mismatched": verdict.mismatched, "checked": verdict.checked},
    }


def register_workspace(
    app: FastAPI,
    *,
    store,
    settings,
    job,
    board_date: Callable[[], date],
    fraud_view: Callable[[EmailRecord], dict],
    file_cards: Callable[[EmailRecord], list[dict]],
    original_path: Callable[[EmailRecord], Path | None],
) -> None:
    """Add the workspace page (/app) and its JSON API (/api) to the dashboard."""
    api = APIRouter(prefix="/api", dependencies=[Depends(page_only)])

    def known(email_id: str) -> EmailRecord:
        email = store.get_email(email_id)
        if email is None:
            raise HTTPException(status_code=404, detail="That email isn't here any more.")
        return email

    def digest_now() -> dict:
        return build_digest(
            store,
            as_of=board_date(),
            generated_at=datetime.now(settings.tz),
            tz=settings.tz,
            lookback_days=settings.digest_lookback_days,
            save=False,
            finance=is_finance(settings, store),
        )

    def meta() -> dict[str, Any]:
        as_of = board_date()
        counts = store.counts()
        model = check_model(settings)
        snapshot = job.snapshot()
        close = month_end(as_of)
        coding = store.coding_counts()
        last_run = store.get_state("last_overnight_at") or ""
        return {
            "today": as_of.isoformat(),
            "today_long": f"{as_of.strftime('%A')}, {as_of.strftime('%B')} {as_of.day}, {as_of.year}",
            "days_to_close": (close - as_of).days,
            "close_date": close.isoformat(),
            "finance": active_profile(settings, store) == "finance",
            "counts": counts,
            "coding": coding,
            "model": {
                "active": model.active,
                "name": model.model if model.active else "",
                "label": model.model if model.active else ("off" if model.mode == "off" else "not running"),
                "describe": model.describe(),
            },
            "job": {**snapshot, "stage_label": STAGES.get(snapshot["stage"], snapshot["stage"])},
            "is_sample": bool(counts["emails"]) and not store.real_mail_count(),
            "last_run": format_when(last_run, settings.tz) if last_run else "",
            "tz": settings.tz.key,
            "folders": [{"key": key, "label": label} for key, label in FOLDER_LABELS.items()],
            "categories": [{"value": key.value, "label": label} for key, label in DOCUMENT_LABELS.items()],
            # Changes whenever a count does, so the page knows when its lists are out of date.
            "version": hashlib.sha256(
                json.dumps([counts, coding, last_run, snapshot["finished_at"]], sort_keys=True).encode()
            ).hexdigest()[:12],
        }

    @api.get("/state")
    def state():
        return meta()

    @api.get("/focus")
    def focus():
        payload = digest_now()
        today = board_date()
        rows = []
        for row in payload["focus"]:
            # "Needs a look" rows have no task to tick off; marking the email done is how they leave the list.
            if not row.get("action_id") and store.is_done(row["email_id"]):
                continue
            email = store.get_email(row["email_id"])
            rows.append(
                {
                    **row,
                    "amount_label": _money(row.get("amount")),
                    "importance": email.importance.value if email else "",
                    "files": len(email.attachments) if email else 0,
                    "locked": fraud.attachments_locked(email) if email else False,
                    "when": short_when(email.received_at, settings.tz, today) if email else "",
                }
            )
        return {
            "headline": payload["headline"],
            "date_long": payload["date_long"],
            "since_label": payload["window"]["since_label"],
            "kpis": payload["kpis"],
            "focus": rows,
            "alerts": payload["critical_alerts"],
            "overdue": payload["overdue_actions"],
            "due_today": payload["due_today"],
            "due_this_week": payload["due_this_week"],
            "new_mail": {name: len(items) for name, items in payload["new_mail"].items()},
        }

    @api.get("/mail")
    def mail(folder: str = "", q: str = "", importance: str = "", category: str = "", flag: str = "", done: int = 0, limit: int = 200):
        limit = max(1, min(limit, 500))
        today = board_date()
        if folder:
            if folder not in FOLDER_LABELS:
                raise HTTPException(status_code=404, detail="Unknown folder")
            emails = store.list_emails(folder=folder, order="score", done=bool(done), q=q or None, limit=limit)
            items = [mail_card(email, settings.tz, today, done=bool(done)) for email in emails]
            return {"items": items, "done_count": store.done_count(folder), "title": FOLDER_LABELS[folder]}
        if q:
            cost_codes.refresh_if_changed(store, settings)
        emails = store.list_emails(
            importance=importance or None, category=category or None, flag=flag or None, q=q[:200] or None, limit=limit
        )
        items = [mail_card(email, settings.tz, today, done=store.is_done(email.id)) for email in emails]
        return {"items": items, "title": "Search results" if q else "All mail"}

    @api.get("/mail/{email_id}")
    def mail_detail(email_id: str):
        email = known(email_id)
        cost_codes.refresh(store, settings, email_ids=[email.id])
        today = board_date()
        locked = fraud.attachments_locked(email)
        files = []
        for card in file_cards(email):
            att = card["att"]
            suffix = Path(att.filename).suffix.lower()
            item = {
                "n": card["n"],
                "name": att.filename,
                "kind": card["kind"],
                "size": _size(att.size_bytes),
                "type_label": DOCUMENT_LABELS.get(att.document_type, att.document_type.value),
                "confidence": round((att.document_confidence or 0) * 100),
                "invoice": att.extracted_fields.primary_invoice or "",
                "amount_label": _money(att.extracted_fields.primary_amount),
                "has_text": bool(card["chars"]),
                "sections": card["sections"],
                "search": card["search"],
                "blocked_type": card["blocked_type"],
            }
            if not locked:
                text = att.extracted_text or ""
                item.update(
                    clip=text[:CLIP_CHARS],
                    tables=len(table_lookup.tables_in(text)) if text.strip() else 0,
                    download=bool(card["downloadable"]),
                    view=suffix in agent.VIEWABLE and agent.original_file(settings, email, att) is not None,
                )
            files.append(item)
        coding = store.cost_coding(email.id)
        if coding is not None:
            book = cost_codes.load(settings)
            coding = {
                **coding,
                "decided_label": format_when(coding.get("decided_at") or "", settings.tz),
                "choices": cost_codes.choices(book, coding),
                "codebook_size": len(book.codes),
            }
        check = fraud_view(email)
        sender_domain = fraud.domain_of(email.sender_email)
        signal_keys = {signal.get("key") for signal in check.get("signals") or []}
        fields = email.extracted
        return {
            **mail_card(email, settings.tz, today, done=store.is_done(email.id)),
            "sender_name": email.sender_name,
            "reply_to": email.reply_to,
            "body": email.body_text or "",
            "reasons": [reason for reason in email.importance_reasons if reason],
            "fields": {
                "invoices": fields.invoice_numbers,
                "pos": fields.po_numbers,
                "amounts": [_money(amount) for amount in fields.amounts],
                "due_dates": fields.due_dates,
                "vendors": fields.vendor_candidates,
                "accounts": [f"••{last4}" for last4 in fields.account_last4],
                "bank_details": fields.mentions_routing_or_account,
            },
            "tasks": [
                {
                    "id": action.id,
                    "title": action.title,
                    "detail": action.detail,
                    "due": action.due_date or "",
                    "due_label": due_label(action.due_date or "", today),
                    "overdue": bool(action.due_date and action.due_date < today.isoformat() and action.status == ActionStatus.OPEN),
                    "priority": action.priority.value,
                    "priority_label": IMPORTANCE_LABELS.get(action.priority, action.priority.value),
                    "status": action.status.value,
                }
                for action in email.actions
            ],
            "attachments": files,
            "has_original": original_path(email) is not None,
            "fraud": {
                **check,
                "sender_domain": sender_domain,
                "can_trust_sender": bool(email.sender_email) and "trusted_sender" not in signal_keys,
                "can_trust_domain": bool(sender_domain)
                and sender_domain not in fraud.FREEMAIL
                and "trusted_domain" not in signal_keys,
            },
            "coding": coding,
            "findings": [
                {**row, "when": format_when(row.get("at") or "", settings.tz)} for row in store.findings(email.id, limit=20)
            ],
        }

    @api.get("/mail/{email_id}/files/{n}")
    def mail_file(email_id: str, n: int):
        email = known(email_id)
        if not 1 <= n <= len(email.attachments):
            raise HTTPException(status_code=404, detail="No such file on this email")
        if fraud.attachments_locked(email):
            raise HTTPException(status_code=403, detail=LOCKED_MESSAGE)
        att = email.attachments[n - 1]
        text = att.extracted_text or ""
        found = table_lookup.tables_in(text) if text.strip() else []
        suffix = Path(att.filename).suffix.lower()
        original = agent.original_file(settings, email, att)
        return {
            "email": {"id": email.id, "subject": email.subject, "sender": email.sender_name or email.sender_email},
            "file": {
                "n": n,
                "name": att.filename,
                "kind": agent.file_kind(att),
                "size": _size(att.size_bytes),
                "type_label": DOCUMENT_LABELS.get(att.document_type, att.document_type.value),
                "search": semantic.file_states(store, settings, email).get(att.id, "no_text"),
                "view": suffix in agent.VIEWABLE and original is not None,
                "download": suffix in DOWNLOADABLE and original is not None,
            },
            "parts": [{"label": part.label, "text": part.text} for part in documents.split_parts(text)] if text.strip() else [],
            "tables": [table_json(table, number) for number, table in enumerate(found[:MAX_TABLES], start=1)],
            "more_tables": max(0, len(found) - MAX_TABLES),
            "vision": {
                "offer": vision.offer(store, settings, email, att) if vision.readable_file(att.filename) else None,
                "readings": vision.readings_json(store.page_readings(att.id, att.sha256)),
            },
        }

    @api.post("/mail/{email_id}/files/{n}/vision")
    def mail_file_vision(email_id: str, n: int, again: int = 0):
        """Read the file's pages with the vision model too, in the background (the process job). A file added to a
        conversation can be read too: the chat offers it like any other."""
        chat_id = chats.chat_id_of(email_id)
        email = chats.chat_mail(store, chat_id) if chat_id else known(email_id)
        if email is None or not 1 <= n <= len(email.attachments):
            raise HTTPException(status_code=404, detail="No such file on this email")
        reply, status = vision.start(store, settings, job, email, email.attachments[n - 1], n, again=bool(again))
        return JSONResponse(reply, status_code=status)

    @api.get("/tasks")
    def tasks(status: str = "open"):
        status = status if status in {"open", "done", "all"} else "open"
        today = board_date()
        items = []
        for action, email in store.list_actions(status=None if status == "all" else status):
            items.append(
                {
                    "id": action.id,
                    "email_id": email.id,
                    "title": action.title,
                    "detail": action.detail,
                    "due": action.due_date or "",
                    "due_label": due_label(action.due_date or "", today),
                    "overdue": bool(action.due_date and action.due_date < today.isoformat() and action.status == ActionStatus.OPEN),
                    "priority": action.priority.value,
                    "priority_label": IMPORTANCE_LABELS.get(action.priority, action.priority.value),
                    "status": action.status.value,
                    "subject": email.subject,
                    "sender": email.sender_name or email.sender_email,
                    **_category(email.category),
                }
            )
        return {"items": items, "status": status}

    @api.get("/fraud")
    def fraud_overview():
        today = board_date()
        flagged = [
            {
                "id": row["email_id"],
                "subject": row["subject"] or "(no subject)",
                "sender": row["sender_name"] or row["sender_email"],
                "sender_email": row["sender_email"],
                "when": short_when(row["received_at"], settings.tz, today),
                "level": row["level"],
                "score": row["score"],
                "signals": [signal.get("label", "") for signal in row["signals"] if (signal.get("points") or 0) > 0],
                "cleared": "fraud_cleared" in row["flags"],
            }
            for row in store.flagged(limit=100)
        ]
        log = [
            {
                "event": row.get("event", ""),
                "email_id": row.get("email_id") or "",
                "subject": row.get("subject") or "",
                "sender_email": row.get("sender_email") or "",
                "when": format_when(row.get("at") or "", settings.tz),
                "note": row.get("note") or "",
                "level": row.get("level") or "",
                "score": row.get("score"),
            }
            for row in store.fraud_log(limit=25)
        ]
        trust = store.trust_entries()
        return {
            "flagged": flagged,
            "log": log,
            "high_at": fraud.HIGH_AT,
            "caution_at": fraud.CAUTION_AT,
            "trusted_domains": len(settings.trusted_domain_list) + sum(1 for row in trust if row["kind"] == "domain" and row["verdict"] == "safe"),
            "trusted_senders": sum(1 for row in trust if row["kind"] == "sender" and row["verdict"] == "safe"),
            "reported": sum(1 for row in trust if row["verdict"] != "safe"),
        }

    @api.get("/coding")
    def coding(status: str = "review", q: str = ""):
        cost_codes.refresh_if_changed(store, settings)
        status = status if status in {"review", "suggested", "unmatched", "confirmed", "all"} else "review"
        today = board_date()
        rows = store.cost_codings(status=None if status == "all" else status, q=q[:200] or None)
        book = cost_codes.load(settings)
        return {
            "status": status,
            "counts": store.coding_counts(),
            "codebook_size": len(book.codes),
            "warnings": book.warnings,
            "items": [
                {
                    "id": row["email_id"],
                    "subject": row["subject"] or "(no subject)",
                    "sender": row["sender_name"] or row["sender_email"],
                    "when": short_when(row["received_at"], settings.tz, today),
                    "invoice": row["extracted"].primary_invoice or "",
                    "amount_label": _money(row["extracted"].primary_amount),
                    "codes": [{"code": item.get("code", ""), "description": item.get("description", "")} for item in row["codes"]],
                    "status": row["status"],
                }
                for row in rows
            ],
        }

    @api.get("/digest")
    def digest(date: str = ""):
        as_of = board_date().isoformat()
        row = store.get_digest(date) if date else (store.get_digest(as_of) or store.latest_digest())
        if date and row is None:
            raise HTTPException(status_code=404, detail=f"No digest saved for {date}")
        history = [item["period_date"] for item in store.list_digests(limit=30)]
        payload = None
        if row and row.get("payload"):
            payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
        current = row["period_date"] if row else ""
        index = history.index(current) if current in history else -1
        return {
            "date": current,
            "payload": payload,
            "markdown": (row or {}).get("markdown", ""),
            "history": history[:12],
            "newer": history[index - 1] if index > 0 else "",
            "older": history[index + 1] if 0 <= index < len(history) - 1 else "",
            "printable": f"/digest/{current}.html" if current else "",
        }

    # Changes. The classic forms post to their own routes; these do the same work for the workspace.

    @api.post("/mail/{email_id}/done")
    async def mail_done(request: Request, email_id: str):
        known(email_id)
        data = await _json_body(request)
        done = bool(data.get("done", True))
        store.set_done(email_id, done)
        return {"ok": True, "done": done, "counts": store.counts()}

    @api.post("/tasks/{action_id}")
    async def task_status(request: Request, action_id: str):
        data = await _json_body(request)
        status = str(data.get("status") or "")
        if status not in {"open", "done"}:
            raise HTTPException(status_code=400, detail="A task is either open or done.")
        if not store.set_action_status(action_id, status):
            raise HTTPException(status_code=404, detail="That task isn't here any more.")
        return {"ok": True, "status": status, "counts": store.counts()}

    @api.post("/mail/{email_id}/fraud")
    async def mail_fraud(request: Request, email_id: str):
        data = await _json_body(request)
        verdict, _, scope = str(data.get("choice") or "").partition(":")
        try:
            fraud.record_fraud_verdict(
                store, settings, email_id, verdict=verdict, scope=scope or "email", note=str(data.get("note") or "")[:300]
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="That email isn't here any more.") from None
        except ValueError as exc:
            if "free email" in str(exc):
                detail = "That is a free email service anyone can sign up for, so it can't be trusted as a whole. Trust the sender's address instead."
            else:
                detail = "That didn't save. Use the buttons in the email's fraud check."
            raise HTTPException(status_code=400, detail=detail) from None
        message = (
            "Saved as not fraud. Mail it covers was checked again, and the fraud check learns from it."
            if verdict == "safe"
            else "Reported as fraud. Its files stay locked, and the fraud check learns from it."
        )
        return {"ok": True, "message": message}

    @api.post("/mail/{email_id}/category")
    async def mail_category(request: Request, email_id: str):
        from controller_inbox.learn import record_correction

        data = await _json_body(request)
        try:
            record_correction(
                store,
                settings,
                email_id=email_id,
                corrected_category=str(data.get("category") or ""),
                reason=str(data.get("reason") or "")[:500],
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="That email isn't here any more.") from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"ok": True, "message": "Category corrected. Mail from this sender follows it, and the example is saved for the local model."}

    @api.post("/mail/{email_id}/coding")
    async def mail_coding(request: Request, email_id: str):
        known(email_id)
        data = await _json_body(request)
        codes = data.get("codes")
        try:
            if isinstance(codes, list):
                cost_codes.revise(store, settings, email_id, [str(code) for code in codes][:20])
                message = "Cost code changed and confirmed. The next invoice from this sender is suggested this code."
            else:
                cost_codes.confirm(store, settings, email_id)
                message = "Cost code confirmed. The next invoice from this sender is suggested the same code."
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"ok": True, "message": message}

    @api.post("/mail/{email_id}/findings/clear")
    def mail_findings_clear(email_id: str):
        known(email_id)
        store.clear_findings(email_id)
        return {"ok": True, "message": "Notes cleared. Ask CloseDesk reads the files fresh next time."}

    @api.post("/process")
    def process():
        from controller_inbox.overnight import run_overnight

        started = job.start(
            lambda progress: run_overnight(
                store, settings, sync_graph=False, on_progress=progress, vision_minutes=vision.process_minutes(settings),
                should_stop=lambda: job.stopping,
            )
        )
        return {"ok": True, "started": started, "job": job.snapshot()}

    @api.post("/process/stop")
    def process_stop():
        """A vision read stops before its next page."""
        return {"ok": True, "stopping": job.request_stop()}

    app.include_router(api)

    def ui_scripts() -> dict[str, str]:
        return {f"/static/ui/{path.name}": f"/static/ui/{path.name}?v={UI_VERSION}" for path in sorted(UI_DIR.glob("*.js"))}

    def workspace(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "app.html",
            {"ui_version": UI_VERSION, "import_map": {"imports": ui_scripts()}, "tz": settings.tz.key},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/app", response_class=HTMLResponse)
    def workspace_home(request: Request):
        return workspace(request)

    @app.get("/app/{rest:path}", response_class=HTMLResponse)
    def workspace_page(request: Request, rest: str):
        return workspace(request)
