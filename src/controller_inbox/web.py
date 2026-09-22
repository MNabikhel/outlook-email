from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from controller_inbox.actions import local_today
from controller_inbox.classify import month_end
from controller_inbox.cli import export_actions_csv
from controller_inbox.config import Settings
from controller_inbox.digest import build_digest, write_digest_files
from controller_inbox.models import DOCUMENT_LABELS, IMPORTANCE_LABELS, DocumentType, Importance
from controller_inbox.pipeline import ingest_demo
from controller_inbox.store import Store


PACKAGE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
templates.env.filters["shortdt"] = lambda value: (
    (datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%b %d · %H:%M"))
    if value
    else ""
)
templates.env.filters["money"] = lambda value: "" if value is None else f"${value:,.2f}"
templates.env.filters["label_doc"] = lambda value: DOCUMENT_LABELS.get(
    value if isinstance(value, DocumentType) else DocumentType(value), value
)
templates.env.filters["label_imp"] = lambda value: IMPORTANCE_LABELS.get(
    value if isinstance(value, Importance) else Importance(value), value
)


def create_app(settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.ensure_data_dir()
    store = store or Store(settings.db_path)
    app = FastAPI(title="CloseDesk", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")

    def ctx(request: Request, **extra):
        as_of = local_today(settings.tz)
        counts = store.counts()
        close = month_end(as_of)
        base = {
            "request": request,
            "settings": settings,
            "counts": counts,
            "today": as_of.isoformat(),
            "today_long": as_of.strftime("%A, %B %-d, %Y") if hasattr(as_of, "strftime") else as_of.isoformat(),
            "days_to_close": (close - as_of).days,
            "close_date": close.isoformat(),
            "graph_configured": settings.graph_configured,
            "last_sync": store.get_state("last_sync_at"),
            "doc_labels": DOCUMENT_LABELS,
            "filter_importance": "",
            "filter_category": "",
            "query": "",
        }
        try:
            base["today_long"] = as_of.strftime("%A, %B %d, %Y").replace(" 0", " ")
        except Exception:
            pass
        base.update(extra)
        return base

    def render(request: Request, name: str, **extra):
        return templates.TemplateResponse(request, name, ctx(request, **extra))

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        emails = store.list_emails(limit=12)
        if not emails:
            return render(request, "empty.html", page="inbox")
        attention = [e for e in store.list_emails(limit=200) if e.importance.value in {"critical", "high"} or "fraud_risk" in e.flags]
        overdue = store.list_actions(status="open", due_on_or_before=local_today(settings.tz).isoformat())
        due_today = store.list_actions(status="open", due_on=local_today(settings.tz).isoformat())
        digest = store.latest_digest()
        return render(
            request,
            "inbox.html",
            page="inbox",
            emails=store.list_emails(limit=80),
            attention=attention[:8],
            overdue=overdue[:8],
            due_today=due_today[:8],
            digest=digest,
            filter_importance="",
            filter_category="",
            query="",
            heading="Inbox",
        )

    @app.get("/inbox", response_class=HTMLResponse)
    def inbox(
        request: Request,
        importance: str = "",
        category: str = "",
        flag: str = "",
        q: str = "",
    ):
        emails = store.list_emails(
            importance=importance or None,
            category=category or None,
            flag=flag or None,
            q=q or None,
        )
        return render(
            request,
            "inbox.html",
            page="inbox",
            emails=emails,
            attention=[],
            overdue=[],
            due_today=[],
            digest=None,
            filter_importance=importance,
            filter_category=category,
            query=q,
            heading="Filtered inbox" if any([importance, category, flag, q]) else "Inbox",
        )

    @app.get("/inbox/{email_id}", response_class=HTMLResponse)
    def email_detail(request: Request, email_id: str):
        email = store.get_email(email_id)
        if not email:
            return HTMLResponse("Not found", status_code=404)
        return render(request, "detail.html", page="inbox", email=email)

    @app.get("/actions", response_class=HTMLResponse)
    def actions_page(request: Request, status: str = "open"):
        rows = store.list_actions(status=status if status != "all" else None)
        as_of = local_today(settings.tz).isoformat()
        overdue = [pair for pair in rows if pair[0].due_date and pair[0].due_date < as_of and pair[0].status.value == "open"]
        return render(
            request,
            "actions.html",
            page="actions",
            rows=rows,
            status=status,
            overdue_count=len(overdue),
        )

    @app.post("/actions/{action_id}/complete")
    def complete_action(action_id: str, next: str = Query("/actions")):
        store.set_action_status(action_id, "done")
        return RedirectResponse(next, status_code=303)

    @app.post("/actions/{action_id}/reopen")
    def reopen_action(action_id: str, next: str = Query("/actions?status=done")):
        store.set_action_status(action_id, "open")
        return RedirectResponse(next, status_code=303)

    @app.get("/attachments", response_class=HTMLResponse)
    def attachments_page(request: Request, type: str = ""):
        rows = store.list_attachments(document_type=type or None)
        type_counts = store.attachment_type_counts()
        return render(
            request,
            "attachments.html",
            page="attachments",
            rows=rows,
            type_counts=type_counts,
            selected_type=type,
        )

    @app.get("/digest", response_class=HTMLResponse)
    def digest_page(request: Request):
        as_of = local_today(settings.tz)
        row = store.get_digest(as_of.isoformat()) or store.latest_digest()
        payload = None
        if row and row.get("payload"):
            payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
        return render(
            request,
            "digest.html",
            page="digest",
            digest=row,
            payload=payload,
            as_of=as_of.isoformat(),
        )

    @app.post("/digest/rebuild")
    def digest_rebuild():
        now = datetime.now(settings.tz)
        as_of = local_today(settings.tz, now)
        payload = build_digest(store, as_of=as_of, generated_at=now)
        write_digest_files(payload, settings.digest_dir, as_of.isoformat())
        return RedirectResponse("/digest", status_code=303)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        return render(request, "settings.html", page="settings")

    @app.post("/demo/reload")
    def demo_reload():
        store.reset()
        now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
        ingest_demo(store, settings, now=now)
        as_of = local_today(settings.tz, now)
        payload = build_digest(store, as_of=as_of, generated_at=now)
        write_digest_files(payload, settings.digest_dir, as_of.isoformat())
        return RedirectResponse("/", status_code=303)

    @app.get("/export/actions.csv")
    def export_csv():
        text = export_actions_csv(store)
        return Response(
            content=text,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=closedesk-actions.csv"},
        )

    @app.get("/health")
    def health():
        return {"ok": True, "emails": store.counts()["emails"], "graph": settings.graph_configured}

    return app
