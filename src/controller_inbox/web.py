from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from controller_inbox.actions import local_today
from controller_inbox.classify import month_end
from controller_inbox.cli import DEMO_NOW, export_actions_csv, load_sample, make_digest
from controller_inbox.config import PROFILES, Settings
from controller_inbox.digest import build_digest, write_digest_files
from controller_inbox.local_llm import check_model
from controller_inbox.models import DOCUMENT_LABELS, FOLDER_LABELS, IMPORTANCE_LABELS, DocumentType, Importance
from controller_inbox.profile import active_profile, is_finance, set_profile
from controller_inbox.store import Store

PACKAGE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
# Changes whenever the stylesheet does, so browsers never keep an old copy after an update.
templates.env.globals["static_version"] = hashlib.sha256((PACKAGE_DIR / "static" / "app.css").read_bytes()).hexdigest()[:10]
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

NOTICES = {
    "sample-blocked": "The sample mailbox was not loaded: it would erase your own mail. "
    "Run the sample from a separate data folder instead (see README).",
    "processing": "Processing started. This page updates as it goes.",
    "busy": "Already processing. This page updates as it goes.",
    "profile": "Saved. The digest and Today page now use this profile; new mail is sorted with it.",
}


class ProcessJob:
    """The dashboard's 'Process new mail' run, in a background thread so the page stays usable."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.state = "idle"
        self.stage = ""
        self.done = 0
        self.total = 0
        self.note = ""
        self.result: dict | None = None
        self.error = ""
        self.finished_at = ""

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self.state,
                "stage": self.stage,
                "done": self.done,
                "total": self.total,
                "note": self.note,
                "result": self.result,
                "error": self.error,
                "finished_at": self.finished_at,
            }

    def start(self, target) -> bool:
        with self._lock:
            if self.state == "running":
                return False
            self.state, self.stage, self.done, self.total = "running", "starting", 0, 0
            self.note, self.result, self.error = "", None, ""
        threading.Thread(target=self._run, args=(target,), daemon=True).start()
        return True

    def progress(self, stage: str, done: int, total: int, note: str) -> None:
        with self._lock:
            self.stage, self.done, self.total, self.note = stage, done, total, note

    def _run(self, target) -> None:
        try:
            result = target(self.progress)
            with self._lock:
                self.state, self.result = "done", result
        except Exception as exc:  # the page shows the error instead of a dead spinner
            with self._lock:
                self.state, self.error = "error", str(exc)
        finally:
            with self._lock:
                self.finished_at = datetime.now(timezone.utc).isoformat()


def create_app(settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.ensure_data_dir()
    store = store or Store(settings.db_path)
    app = FastAPI(title="CloseDesk", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")
    job = ProcessJob()
    app.state.job = job

    def board_date():
        """Today, or the sample's date when only the sample mailbox is loaded."""
        if store.counts()["emails"] and not store.real_mail_count():
            return local_today(settings.tz, DEMO_NOW)
        return local_today(settings.tz)

    def ctx(request: Request, **extra):
        as_of = board_date()
        counts = store.counts()
        close = month_end(as_of)
        profile = active_profile(settings, store)
        base = {
            "request": request,
            "settings": settings,
            "counts": counts,
            "today": as_of.isoformat(),
            "today_long": f"{as_of.strftime('%A')}, {as_of.strftime('%B')} {as_of.day}, {as_of.year}",
            "days_to_close": (close - as_of).days,
            "close_date": close.isoformat(),
            "graph_configured": settings.graph_configured,
            "model": check_model(settings),
            "job": job.snapshot(),
            "inbox_incoming": str(settings.inbox_incoming),
            "inbox_attachments": str(settings.inbox_attachments),
            "inbox_failed": str(settings.inbox_failed),
            "correction_count": store.correction_count(),
            "last_sync": store.get_state("last_sync_at"),
            "last_run": store.get_state("last_overnight_at"),
            "doc_labels": DOCUMENT_LABELS,
            "filter_importance": "",
            "filter_category": "",
            "query": "",
            "notice": NOTICES.get(request.query_params.get("notice", ""), ""),
            "is_sample": bool(counts["emails"]) and not store.real_mail_count(),
            "profile": profile,
            "profiles": PROFILES,
            "finance": profile == "finance",
        }
        base["llm_enabled"] = base["model"].active
        base.update(extra)
        return base

    def render(request: Request, name: str, **extra):
        return templates.TemplateResponse(request, name, ctx(request, **extra))

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        if not store.list_emails(limit=1):
            return render(request, "empty.html", page="home")
        as_of = board_date()
        now = datetime.now(settings.tz)
        payload = build_digest(
            store,
            as_of=as_of,
            generated_at=now,
            tz=settings.tz,
            lookback_days=settings.digest_lookback_days,
            save=False,
            finance=is_finance(settings, store),
        )
        return render(
            request,
            "morning.html",
            page="home",
            heading="Today",
            payload=payload,
        )

    @app.get("/folder/{name}", response_class=HTMLResponse)
    def folder_page(request: Request, name: str):
        if name not in FOLDER_LABELS:
            raise HTTPException(status_code=404, detail="Unknown folder")
        blurbs = {
            "important": "Needs a decision, a reply, a payment check, or a task. Most important first.",
            "informational": "Worth knowing. Nothing is waiting on you.",
            "reference": "Notifications, receipts, statements, and files to keep. Not a task.",
        }
        return render(
            request,
            "inbox.html",
            page=name,
            emails=store.list_emails(folder=name, order="score", limit=200),
            heading=FOLDER_LABELS[name],
            blurb=blurbs[name],
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
            filter_importance=importance,
            filter_category=category,
            query=q,
            heading="Filtered mail" if any([importance, category, flag, q]) else "All mail",
        )

    @app.get("/inbox/{email_id}", response_class=HTMLResponse)
    def email_detail(request: Request, email_id: str):
        email = store.get_email(email_id)
        if not email:
            return HTMLResponse("Not found", status_code=404)
        return render(request, "detail.html", page="inbox", email=email)

    @app.post("/inbox/{email_id}/correct")
    def correct_category(email_id: str, category: str = Form(...), reason: str = Form(...)):
        from controller_inbox.learn import record_correction

        try:
            record_correction(
                store,
                settings,
                email_id=email_id,
                corrected_category=category,
                reason=reason,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Message not found") from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return RedirectResponse(f"/inbox/{email_id}", status_code=303)

    @app.post("/process")
    def process():
        from controller_inbox.overnight import run_overnight

        started = job.start(lambda progress: run_overnight(store, settings, sync_graph=False, on_progress=progress))
        return RedirectResponse(f"/?notice={'processing' if started else 'busy'}", status_code=303)

    @app.get("/process/status")
    def process_status():
        return JSONResponse(job.snapshot())

    @app.post("/folder/ingest")
    def folder_ingest():
        return process()

    @app.get("/actions", response_class=HTMLResponse)
    def actions_page(request: Request, status: str = "open"):
        rows = store.list_actions(status=status if status != "all" else None)
        as_of = board_date().isoformat()
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
        return RedirectResponse(_local_path(next, "/actions"), status_code=303)

    @app.post("/actions/{action_id}/reopen")
    def reopen_action(action_id: str, next: str = Query("/actions?status=done")):
        store.set_action_status(action_id, "open")
        return RedirectResponse(_local_path(next, "/actions?status=done"), status_code=303)

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
    def digest_page(request: Request, date: str = ""):
        row = store.get_digest(date) if date else (store.get_digest(board_date().isoformat()) or store.latest_digest())
        if date and row is None:
            raise HTTPException(status_code=404, detail=f"No digest saved for {date}")
        payload = None
        if row and row.get("payload"):
            payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
        history = store.list_digests(limit=30)
        dates = [item["period_date"] for item in history]
        current = row["period_date"] if row else ""
        index = dates.index(current) if current in dates else -1
        return render(
            request,
            "digest.html",
            page="digest",
            digest=row,
            payload=payload,
            history=history[:8],
            newer=dates[index - 1] if index > 0 else "",
            older=dates[index + 1] if 0 <= index < len(dates) - 1 else "",
            as_of=board_date().isoformat(),
        )

    @app.get("/digest/{period}.html", response_class=HTMLResponse)
    def digest_print(period: str):
        row = store.get_digest(period)
        if not row:
            raise HTTPException(status_code=404, detail=f"No digest saved for {period}")
        return HTMLResponse(row["html"])

    @app.get("/digests", response_class=HTMLResponse)
    def digests_page(request: Request):
        return render(request, "digests.html", page="digests", history=store.list_digests(limit=120))

    @app.post("/digest/rebuild")
    def digest_rebuild():
        now = datetime.now(settings.tz)
        as_of = board_date()
        payload = make_digest(store, settings, as_of=as_of, now=now)
        write_digest_files(payload, settings.digest_dir, as_of.isoformat())
        return RedirectResponse("/digest", status_code=303)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, recheck: int = 0):
        model = check_model(settings, use_cache=not recheck)
        return render(request, "settings.html", page="settings", model=model, llm_enabled=model.active)

    @app.post("/settings/profile")
    def save_profile(profile: str = Form(...)):
        try:
            set_profile(store, profile)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return RedirectResponse("/settings?notice=profile", status_code=303)

    @app.post("/demo/reload")
    def demo_reload():
        if store.real_mail_count():
            return RedirectResponse("/settings?notice=sample-blocked", status_code=303)
        load_sample(store, settings)
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
        model = check_model(settings)
        return {
            "ok": True,
            "emails": store.counts()["emails"],
            "graph": settings.graph_configured,
            "model": model.model if model.active else "",
        }

    return app


def _local_path(value: str, fallback: str) -> str:
    """Only redirect within the dashboard."""
    if value.startswith("/") and not value.startswith("//"):
        return value
    return fallback
