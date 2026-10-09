"""The JSON the workspace at /app is built from, and the page that hosts it.

The classic pages render on the server; the workspace fetches these endpoints and renders in the browser,
so moving between folders and emails never reloads the page. Everything here reuses what the classic
pages use (the store's queries, ``build_digest`` for the focus list, the fraud, coding and correction
helpers), so both views always agree.

Every request under /api must come from CloseDesk's own page: it carries the X-CloseDesk header (which
another website cannot add without the browser asking this server first, and it never agrees), and a
browser that says the request came from another site is refused, as the classic forms are. Files of an
email held as possible payment fraud are never shown here: not their text, not their tables, not their pages.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from controller_inbox import agent, chats, cost_codes, documents, fixes, fraud, model_roles, ocr, page_details, page_view, semantic, table_lookup, vision
from controller_inbox.digest import build_digest
from controller_inbox.local_llm import check_model, context_target, needs_more_context
from controller_inbox.models import DOCUMENT_LABELS, FOLDER_LABELS, IMPORTANCE_LABELS, ActionStatus, EmailRecord
from controller_inbox.profile import active_profile, is_finance, set_profile
from controller_inbox.classify import month_end
from controller_inbox.clock import effective_timezone, format_when, offset_label, on_daylight_time, set_timezone, zone_name
from controller_inbox.config import PROFILES
from controller_inbox.web import DOWNLOADABLE, NOTICES, _json_body, _nearest_step, _require_page, _same_origin, templates

log = logging.getLogger(__name__)

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
# When CloseDesk last ran overnight, read the drop folder and synced Outlook, as Setup shows them.
RUN_STATES = {"last_run": "last_overnight_at", "last_folder": "last_folder_ingest", "last_sync": "last_sync_at"}
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
    setup: dict[str, Callable],
) -> None:
    """Add the workspace page (/app) and its JSON API (/api) to the dashboard. ``setup`` holds what the classic
    Setup page's buttons do (web.py), so the workspace's Settings page changes the same things the same way."""
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
            "models": model_roles.models_in_use(settings, status=model),
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
    def mail(
        folder: str = "", q: str = "", importance: str = "", category: str = "", flag: str = "", done: int = 0, limit: int = 200, offset: int = 0
    ):
        # ``total`` is how many match; the page asks again with ``offset`` for the next ones.
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        today = board_date()
        if folder:
            if folder not in FOLDER_LABELS:
                raise HTTPException(status_code=404, detail="Unknown folder")
            filters = dict(folder=folder, done=bool(done), q=q or None)
            emails = store.list_emails(**filters, order="score", limit=limit, offset=offset)
            items = [mail_card(email, settings.tz, today, done=bool(done)) for email in emails]
            total = store.count_emails(**filters)
            return {"items": items, "total": total, "done_count": store.done_count(folder), "title": FOLDER_LABELS[folder]}
        if q:
            cost_codes.refresh_if_changed(store, settings)
        filters = dict(importance=importance or None, category=category or None, flag=flag or None, q=q[:200] or None)
        emails = store.list_emails(**filters, limit=limit, offset=offset)
        items = [mail_card(email, settings.tz, today, done=store.is_done(email.id)) for email in emails]
        return {"items": items, "total": store.count_emails(**filters), "title": "Search results" if q else "All mail"}

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
                    preview=vision.readable_file(att.filename) and vision.can_render() and agent.original_file(settings, email, att) is not None,
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
            "original_downloads": _original_downloads(original_path(email)),
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
                # Its pages can be shown as pictures, with where each piece of text was read (the Page tab).
                "preview": vision.readable_file(att.filename) and original is not None and vision.can_render(),
            },
            "parts": [{"label": part.label, "text": part.text} for part in documents.split_parts(text)] if text.strip() else [],
            "tables": [table_json(table, number) for number, table in enumerate(found[:MAX_TABLES], start=1)],
            "more_tables": max(0, len(found) - MAX_TABLES),
            "vision": {
                "offer": vision.offer(store, settings, email, att) if vision.readable_file(att.filename) else None,
                "readings": vision.readings_json(store.page_readings(att.id, att.sha256)),
            },
        }

    def page_file(email_id: str, n: int) -> tuple[Any, Any, bytes]:
        """The email, the attachment and its bytes as they arrived, for showing its pages; never a locked file's."""
        email = known(email_id)
        if not 1 <= n <= len(email.attachments):
            raise HTTPException(status_code=404, detail="No such file on this email")
        if fraud.attachments_locked(email):
            raise HTTPException(status_code=403, detail=LOCKED_MESSAGE)
        att = email.attachments[n - 1]
        if not vision.readable_file(att.filename):
            raise HTTPException(status_code=404, detail="Only a PDF or a picture has pages to show.")
        if not vision.can_render():
            raise HTTPException(status_code=404, detail="The page renderer isn't installed (double-click CloseDesk once to install it).")
        data = vision.original_bytes(settings, email, att)
        if data is None:
            raise HTTPException(status_code=404, detail="The original file wasn't kept for this email.")
        return email, att, data

    def no_page(p: int) -> HTTPException:
        return HTTPException(status_code=404, detail=f"This file has no page {p}.")

    @api.get("/mail/{email_id}/files/{n}/pages/{p}.png")
    def mail_file_page(email_id: str, n: int, p: int):
        """The page drawn as a picture (a picture file is its page 1)."""
        _email, att, data = page_file(email_id, n)
        try:
            png = page_view.page_png(settings, data, att.filename, p)
        except ValueError:
            raise no_page(p) from None
        except Exception:
            raise HTTPException(status_code=422, detail="This page couldn't be drawn.") from None
        return Response(png, media_type="image/png", headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})

    def page_state(email, att, data: bytes, p: int) -> dict:
        """What the Page tab shows of page ``p``: the boxes read there (with the vision model's reading and the
        user's fixes), the tables and the key details."""
        try:
            pages = page_view.page_count(data, att.filename)
        except Exception:
            raise HTTPException(status_code=422, detail="This file's pages couldn't be read.") from None
        if not 1 <= p <= pages:
            raise no_page(p)
        try:
            found = page_view.regions(settings, data, att.filename, p)
            width, height = page_view.png_size(page_view.page_png(settings, data, att.filename, p))
        except ValueError:
            raise no_page(p) from None
        except Exception:
            raise HTTPException(status_code=422, detail="This page couldn't be read.") from None
        reading = store.page_readings(att.id, att.sha256).get(p)
        boxes = found["regions"]
        model_text = (reading.get("model_text") or "") if reading is not None else None
        if reading is not None:
            try:
                sure = json.loads(reading.get("comparison") or "{}").get("sure")
            except (ValueError, AttributeError):
                sure = None
            boxes = page_view.with_model(boxes, model_text, sure if isinstance(sure, dict) else None)
        else:
            boxes = [{**box, "model": None} for box in boxes]
        own = store.page_fixes(att.id, att.sha256, page=p)
        learnt = store.sender_fixes(email.sender_email, not_attachment=att.id)
        # A PDF's own text is exact: a word fixed on a scan from the same sender isn't put on it.
        boxes = fixes.apply(boxes, own, learnt if found["source"] == "ocr" else [])
        known_fields = [att.extracted_fields, email.extracted]
        extracted = {
            "invoices": [value for fields in known_fields for value in fields.invoice_numbers],
            "pos": [value for fields in known_fields for value in fields.po_numbers],
            "due_dates": [value for fields in known_fields for value in fields.due_dates],
            "vendors": [*(value for fields in known_fields for value in fields.vendor_candidates), email.sender_name or ""],
        }
        try:
            seen = page_view.found_tables(settings, data, att.filename, p) if boxes else None
            on_page = page_details.page_tables(boxes, found["source"], att.extracted_text or "", p, model_text, seen)
            on_page = fixes.tables(boxes, on_page, own, learnt)
            details = page_details.key_details(boxes, found["source"], model_text, extracted)
        except Exception:
            # Tables and details only help to read the page; the page and its boxes still show without them.
            log.exception("Couldn't find the tables and key details on page %s of %s", p, att.filename)
            on_page, details = [], []
        return {
            "page": p,
            "pages": pages,
            "width": width,
            "height": height,
            "source": found["source"],
            "reason": found["reason"],
            "regions": boxes,
            "model_name": (vision.reader_name(reading.get("model") or "") or "The vision model") if reading is not None else "",
            "tables": on_page,
            "fields": details,
            "fixes": len(own),
        }

    @api.get("/mail/{email_id}/files/{n}/pages/{p}/regions")
    def mail_file_regions(email_id: str, n: int, p: int):
        """Where each piece of the page's text was read, how sure the reading is, and what the vision model read
        there (when it has read the page); the tables on the page and the invoice's key details, each tied to its
        boxes; and what the user fixed on it."""
        email, att, data = page_file(email_id, n)
        return page_state(email, att, data, p)

    @api.post("/mail/{email_id}/files/{n}/pages/{p}/fixes")
    async def mail_file_fix(request: Request, email_id: str, n: int, p: int):
        """Keep a fix made on the page: {kind: "text", region: index, now: "what the page says"}, or {kind: "table"
        | "not_table", box: {x, y, w, h}}. It shows at once, and is learnt for the sender's later files."""
        data_in = await _json_body(request)
        email, att, data = page_file(email_id, n)
        kind = str(data_in.get("kind") or "")
        if kind not in fixes.KINDS:
            raise HTTPException(status_code=400, detail="That isn't something that can be fixed on a page.")
        state = page_state(email, att, data, p)
        boxes = state["regions"]
        if kind == "text":
            index = data_in.get("region")
            now = str(data_in.get("now") or "")
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(boxes):
                raise HTTPException(status_code=400, detail="That box isn't on this page any more. Open the page again.")
            if not now.strip():
                raise HTTPException(status_code=400, detail="Type what the page says there.")
            fix = fixes.text_fix(boxes[index], now)
            message = "Fixed. The file's text has it now, and the same word is put right on this sender's later files." if fixes.learnable(fix["was"], fix["now"]) else "Fixed. The file's text has it now."
        else:
            box = data_in.get("box")
            try:
                fix = fixes.table_fix(boxes, box, kind)
            except (TypeError, KeyError, ValueError):
                raise HTTPException(status_code=400, detail="Draw a box round the table.") from None
            if kind == "table" and (fix["w"] < 0.02 or fix["h"] < 0.01):
                raise HTTPException(status_code=400, detail="That box is too small to hold a table. Drag across the whole table.")
            message = (
                "Table added. CloseDesk looks for it in the same place on this sender's later pages."
                if kind == "table"
                else "Outline removed. It won't be shown here again, or where the same lines are on this sender's later pages."
            )
        fix_id = store.add_page_fix({**fix, "attachment_id": att.id, "sha256": att.sha256, "page": p, "sender": email.sender_email})
        return {"ok": True, "id": fix_id, "message": message}

    @api.post("/mail/{email_id}/files/{n}/pages/{p}/fixes/{fix_id}/undo")
    def mail_file_fix_undo(email_id: str, n: int, p: int, fix_id: str):
        """Take back a fix made on this file."""
        _email, att, _data = page_file(email_id, n)
        if not store.remove_page_fix(att.id, fix_id):
            raise HTTPException(status_code=404, detail="That fix was already taken back.")
        return {"ok": True, "message": "Fix taken back."}

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

    # Settings: everything the classic Setup page shows and changes.

    def context_note(model) -> dict[str, Any]:
        """The line under the model saying whether its context window holds whole attachments, as Setup words it."""
        window = model.context_length or settings.chat_context_tokens
        target = context_target(settings, model)
        recommended = agent.RECOMMENDED_CONTEXT
        if needs_more_context(settings):
            advice = (
                f"The next time you ask something, CloseDesk reloads {model.key} in LM Studio with {target:,} tokens "
                "(the minimum set below). If your computer runs short of memory, it goes back to the current size."
            )
        elif window >= recommended:
            advice = "Enough to read whole attachments when you ask about them."
        else:
            advice = "Ask CloseDesk reads long attachments a section at a time with this."
            if model.key and model.context_length and window < target:
                advice += (
                    f" LM Studio couldn't load {model.key} with {target:,} tokens. "
                    "Close other apps or pick a smaller minimum below, then Save to try again."
                )
            elif model.key and model.max_context and model.max_context < recommended:
                advice += f" {model.key} supports at most {model.max_context:,} tokens. For whole workbooks and PDFs, load a model with a longer context."
            elif model.key:
                advice += " For whole workbooks and PDFs, move the minimum below to 16k or more and Save."
            else:
                advice += (
                    f" For whole workbooks and PDFs, reload the model in LM Studio with Context Length {recommended:,} or more "
                    "(My Models → the model's settings → Context Length)."
                )
            if not window:
                advice += " If your server doesn't report it, set CONTROLLER_INBOX_CHAT_CONTEXT_TOKENS."
        return {
            "shown": model.active,
            "tokens": window,
            "label": f"{window:,} tokens" if window else "not reported",
            "tone": "ok" if window >= recommended else "short",
            "advice": advice,
        }

    def settings_state(recheck: bool = False) -> dict[str, Any]:
        model = check_model(settings, use_cache=not recheck)
        choice = model_roles.chat_choices(settings, model)
        search = semantic.coverage(store, settings)
        counts = store.counts()
        snapshot = job.snapshot()
        return {
            "profile": {"current": active_profile(settings, store), "choices": [{"value": key, "label": label} for key, label in PROFILES.items()]},
            "timezone": {
                "choice": settings.timezone,
                "options": setup["timezone_options"](settings.timezone),
                "zone": settings.tz.key,
                "zone_name": zone_name(effective_timezone(settings.timezone)),
                "offset": offset_label(settings.tz),
                "daylight": on_daylight_time(settings.tz),
                "now_local": datetime.now(settings.tz).strftime("%a, %b %d, %Y · %H:%M"),
            },
            "model": {
                "mode": model.mode,
                "active": model.active,
                "reachable": model.reachable,
                "describe": model.describe(),
                "base_url": model.base_url,
                "configured_url": settings.llm_base_url,
                "error": model.error,
                "loaded": list(model.loaded if model.lm_studio else model.models),
                "server": "LM Studio" if model.lm_studio else "the model server",
            },
            "models": model_roles.models_in_use(settings, status=model),
            "chat": None
            if choice is None
            else {
                "pinned": choice["pinned"],
                "env_model": settings.llm_model if choice["pinned"] else "",
                "current": choice["current"],
                "models": [{"key": key, "loaded": key in choice["loaded"]} for key in choice["models"]],
                "min_context_tokens": settings.min_context_tokens,
            },
            "context": {
                "steps": [agent.context_capacity(tokens, settings.chat_max_tokens) for tokens in agent.CONTEXT_STEPS],
                "step": _nearest_step(settings.min_context_tokens),
                "window": context_note(model),
            },
            "vision": {**setup["vision_setup"](model), "minutes_per_run": round(settings.vision_minutes_per_run)},
            "search": {**search, "indexed_label": format_when(search.get("indexed_at") or "", settings.tz)},
            "ocr_engine": ocr.engine_name(),
            "folders": {
                "incoming": str(settings.inbox_incoming),
                "attachments": str(settings.inbox_attachments),
                "processed": str(settings.inbox_processed),
                "extracted": str(settings.inbox_extracted),
                "failed": str(settings.inbox_failed),
            },
            "runs": {name: format_when(store.get_state(key) or "", settings.tz) for name, key in RUN_STATES.items()},
            "graph_configured": settings.graph_configured,
            "corrections": store.correction_count(),
            "corrections_file": str(settings.training_path),
            "cost_codes": {"workbook": str(cost_codes.workbook_path(settings)), "counts": store.coding_counts()},
            "sample": {
                "emails": counts["emails"],
                "is_sample": bool(counts["emails"]) and not store.real_mail_count(),
                # Loading the sample erases the mail there, so it is offered only when that is none of yours.
                "can_load": not (counts["emails"] and store.real_mail_count()),
            },
            "job": {**snapshot, "stage_label": STAGES.get(snapshot["stage"], snapshot["stage"])},
        }

    def saved(notice: str, **extra) -> dict[str, Any]:
        return {"ok": True, "notice": notice, "message": NOTICES.get(notice, "Saved."), **extra}

    def refused(notice: str, status_code: int = 409) -> HTTPException:
        return HTTPException(status_code=status_code, detail=NOTICES[notice])

    @api.get("/settings")
    def settings_get(recheck: int = 0):
        return settings_state(recheck=bool(recheck))

    @api.post("/settings/profile")
    async def settings_profile(request: Request):
        data = await _json_body(request)
        try:
            set_profile(store, str(data.get("profile") or ""))
        except ValueError:
            raise HTTPException(status_code=400, detail="Choose General or Finance & accounting.") from None
        return saved("profile")

    @api.post("/settings/timezone")
    async def settings_timezone(request: Request):
        data = await _json_body(request)
        try:
            set_timezone(store, settings, str(data.get("zone") or "")[:100])
        except ValueError:
            raise HTTPException(status_code=400, detail="That time zone isn't on the list. Choose one from it.") from None
        return saved("timezone", tz=settings.tz.key)

    @api.post("/settings/chat-model")
    async def settings_chat_model(request: Request):
        data = await _json_body(request)
        model = str(data.get("model") or "").strip()
        if not model:
            raise HTTPException(status_code=400, detail="Choose a model.")
        # Loading a model in LM Studio can take minutes: off the event loop, so other requests carry on meanwhile.
        notice = await run_in_threadpool(setup["switch_chat_model"], model)
        if notice == "chat-model-busy":
            raise refused(notice)
        if notice == "chat-model-failed":
            raise HTTPException(status_code=409, detail=f"{NOTICES[notice]} {setup['chat_problem']()}".strip())
        return saved(notice)

    @api.post("/settings/vision")
    async def settings_vision(request: Request):
        data = await _json_body(request)
        model = data.get("model")
        try:
            vision.save_mode(settings, store, str(data.get("mode") or ""), None if model is None else str(model))
        except ValueError:
            raise HTTPException(status_code=400, detail="Choose Automatic, Only when I ask, or Off.") from None
        return saved("vision-saved")

    @api.post("/settings/context")
    async def settings_context(request: Request):
        data = await _json_body(request)
        step = data.get("step")
        if not isinstance(step, int) or isinstance(step, bool):
            raise HTTPException(status_code=400, detail="Unknown context size")
        try:
            notice = setup["save_context_step"](step)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return saved(notice)

    @api.post("/settings/index")
    def settings_index():
        notice = setup["start_indexing"]()
        if notice == "index-off":
            raise refused(notice, 400)
        if notice == "busy":
            raise HTTPException(status_code=409, detail="Already processing. Index again when it finishes.")
        return saved(notice, job=job.snapshot())

    @api.post("/settings/sample")
    def settings_sample():
        notice = setup["load_sample_mailbox"]()
        if notice:
            raise refused(notice)
        return {"ok": True, "message": "The sample mailbox is loaded.", "counts": store.counts()}

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


def _original_downloads(path: Path | None) -> bool:
    from controller_inbox.web import original_downloads  # web imports this module

    return original_downloads(path)
