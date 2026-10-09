from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Generator
from email.message import EmailMessage
from email.utils import formataddr, format_datetime
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urlencode

import anyio
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool

from controller_inbox import agent, chats, cost_codes, documents, fraud, model_roles, ocr, semantic, vision
from controller_inbox.actions import local_today
from controller_inbox.assistant import answer_stream, draft_reply
from controller_inbox.classify import month_end
from controller_inbox.cli import DEMO_NOW, export_actions_csv, load_sample, make_digest
from controller_inbox.clock import (
    AUTO,
    apply_saved_timezone,
    computer_timezone,
    effective_timezone,
    format_when,
    offset_label,
    on_daylight_time,
    set_timezone,
    zone_choices,
    zone_name,
    zone_option,
)
from controller_inbox.config import PROFILES, Settings
from controller_inbox.digest import build_digest, write_digest_files
from controller_inbox.local_llm import check_model, context_target, needs_more_context, set_min_context, use_chat_model
from controller_inbox.models import DOCUMENT_LABELS, FOLDER_LABELS, IMPORTANCE_LABELS, DocumentType, Importance
from controller_inbox.profile import active_profile, is_finance, set_profile
from controller_inbox.store import Store

PACKAGE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
# Changes whenever the stylesheet does, so browsers never keep an old copy after an update.
templates.env.globals["static_version"] = hashlib.sha256(
    b"".join((PACKAGE_DIR / "static" / name).read_bytes() for name in ("app.css", "app.js"))
).hexdigest()[:10]
templates.env.filters["shortdt"] = lambda value: format_when(value or "", templates.env.globals.get("display_tz"))
templates.env.globals["coding_choices"] = cost_codes.choices
templates.env.filters["money"] = lambda value: "" if value is None else f"${value:,.2f}"
templates.env.filters["label_doc"] = lambda value: DOCUMENT_LABELS.get(
    value if isinstance(value, DocumentType) else DocumentType(value), value
)
templates.env.filters["label_imp"] = lambda value: IMPORTANCE_LABELS.get(
    value if isinstance(value, Importance) else Importance(value), value
)

MIN_CONTEXT_KEY = "min_context_tokens"

NOTICES = {
    "sample-blocked": "The sample mailbox was not loaded: it would erase your own mail. "
    "Run the sample from a separate data folder instead (see README).",
    "sample-busy": "The sample mailbox was not loaded: mail is being processed right now. Try again when it finishes.",
    "processing": "Processing started. This page updates as it goes.",
    "busy": "Already processing. This page updates as it goes.",
    "busy-vision": "The vision model is reading pages of a scan. Stop it from the bar at the top, or try again when it's done.",
    "stopping": "Stopping after the page being read now.",
    "profile": "Saved. The digest and Today page now use this profile; new mail is sorted with it.",
    "timezone": "Saved. Times and “today” now use this time zone.",
    "context": "Saved. If the model in LM Studio is loaded with less, the next question reloads it with this context.",
    "context-off": "Saved. CloseDesk now uses the model as LM Studio loaded it.",
    "fraud-safe": "Saved as not fraud. Mail it covers was checked again, and the fraud check learns from it.",
    "fraud-reported": "Reported as fraud. Its files stay locked, and the fraud check learns from it.",
    "fraud-freemail": "That is a free email service anyone can sign up for, so it can't be trusted as a whole. "
    "Trust the sender's address instead.",
    "fraud-invalid": "That didn't save. Use the buttons in the email's fraud check.",
    "domain-invalid": "That didn't save: enter a domain such as taz.com.",
    "trust-added": "Domain saved. Its mail was checked again.",
    "trust-reported": "Domain reported. Its mail was checked again.",
    "trust-removed": "Removed. Its mail was checked again.",
    "corrected": "Category corrected. Mail from this sender follows it, and the example is saved for the local model.",
    "findings-cleared": "Notes cleared. Ask CloseDesk reads the files fresh next time.",
    "indexing": "Indexing started. This page updates as it goes.",
    "indexed": "Indexed for search. Ask CloseDesk can now find these files by meaning.",
    "vision-saved": "Saved. Scans are read with the vision model as you chose.",
    "chat-model": "Loaded in LM Studio. It answers your questions now.",
    "chat-model-failed": "The model that answers wasn't changed.",
    "chat-model-busy": "Mail is being processed or a scan read with the model right now. Change the model when it finishes.",
    "index-failed": "The embedding model didn't answer, so nothing was indexed. Load one in LM Studio and try again.",
    "index-off": "No embedding model found. Load one in LM Studio (for example nomic-embed-text) and try again.",
    "coding-confirmed": "Cost code confirmed. The next invoice from this sender is suggested the same code.",
    "coding-revised": "Cost code changed and confirmed. The next invoice from this sender is suggested this code.",
    "coding-checked": "Every AP invoice was checked against the workbook again. Confirmed codes were left as they are.",
}

# Files that download from the email page. Programs, scripts, and macro-enabled Office files stay in Outlook.
DOWNLOADABLE = {
    ".pdf", ".docx", ".doc", ".rtf", ".xlsx", ".xls", ".csv", ".pptx", ".txt", ".png", ".jpg", ".jpeg",
    ".gif", ".tif", ".tiff", ".bmp", ".msg", ".eml", ".ics",
}


LOCAL_CLIENTS = {"127.0.0.1", "::1", "localhost", "testclient"}
# Only mail files are handed to the OS; anything else dropped in the inbox could be a program.
MAIL_FILES = {".msg", ".eml"}
# Most rows a classic mail list shows at once; past that, search narrows it.
MAX_PAGE_ROWS = 2000


def original_downloads(path: Path | None) -> bool:
    """Whether an email's original downloads: a rebuilt .eml (no original), or a mail or downloadable file."""
    return path is None or path.suffix.lower() in MAIL_FILES | DOWNLOADABLE


def open_file(path: Path) -> None:
    """Open a file with the computer's default app (Outlook for .msg/.eml on most work laptops)."""
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _host_name(header: str) -> str:
    header = header.strip().lower()
    if header.startswith("["):
        return header[: header.find("]") + 1] if "]" in header else header
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


class LocalHostOnly:
    """Refuse requests addressed to another hostname, so a web page cannot rebind its own domain to this server."""

    def __init__(self, app, allowed: set[str]) -> None:
        self.app = app
        self.allowed = allowed

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            host = headers.get(b"host", b"").decode("latin-1")
            if "*" not in self.allowed and _host_name(host) not in self.allowed:
                await PlainTextResponse("CloseDesk only answers on this computer's own address.", status_code=400)(scope, receive, send)
                return
            if scope.get("method") == "POST" and not _same_origin(headers, host, self.allowed):
                await PlainTextResponse("Use the CloseDesk page for this.", status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _same_origin(headers: dict[bytes, bytes], host: str, allowed: set[str] | None = None) -> bool:
    """A form another website submits to this server carries that site's Origin; refuse it."""
    origin = headers.get(b"origin", b"").decode("latin-1").strip()
    if origin:
        origin_host = origin.lower().split("://", 1)[-1].rstrip("/")
        if origin_host != host.strip().lower():
            return False
        return allowed is None or "*" in allowed or _host_name(origin_host) in allowed
    return headers.get(b"sec-fetch-site", b"").decode("latin-1").lower() not in {"cross-site", "same-site"}


LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1", "[::1]", "testserver"}
WILDCARD_BINDS = {"", "0.0.0.0", "::", "[::]", "*"}


def is_loopback(bind_host: str) -> bool:
    bind = (bind_host or "").strip().lower().strip("[]")
    return bind == "localhost" or bind == "::1" or bind.startswith("127.")


def _bracketed(name: str) -> set[str]:
    # "http://closedesk.lan:8765/" and "closedesk.lan:8765" both mean the host closedesk.lan
    name = _host_name(name.strip().lower().split("://", 1)[-1].split("/", 1)[0])
    if not name or name == "*":
        return set()
    if ":" in name and not name.startswith("["):
        return {f"[{name}]"}
    return {name}


def _this_computer() -> set[str]:
    """This computer's own names and network addresses, for a dashboard bound beyond loopback."""
    found: set[str] = set()
    try:
        name = socket.gethostname()
        found.update({name, socket.getfqdn()})
        for info in socket.getaddrinfo(name, None):
            found.add(str(info[4][0]).split("%", 1)[0])
    except OSError:
        pass
    for family, probe in ((socket.AF_INET, ("192.0.2.1", 9)), (socket.AF_INET6, ("2001:db8::1", 9))):
        try:  # the address used to reach the network; connecting a UDP socket sends nothing
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.connect(probe)
                found.add(sock.getsockname()[0].split("%", 1)[0])
        except OSError:
            pass
    return found


def allowed_hosts(bind_host: str, extra: str = "") -> set[str]:
    """Names the dashboard answers to. Never everything: a page could otherwise rebind its own domain here."""
    bind = (bind_host or "").strip().lower()
    hosts = set(LOOPBACK_NAMES)
    if bind not in WILDCARD_BINDS:
        hosts |= _bracketed(bind) | {bind}
    if not is_loopback(bind):
        for name in _this_computer():
            hosts |= _bracketed(name)
    for name in re.split(r"[,;\s]+", extra or ""):
        hosts |= _bracketed(name)
    hosts.discard("*")
    hosts.discard("")
    return hosts


def _require_page(request: Request) -> None:
    """The page's own scripts send this header; other sites cannot without the browser blocking them."""
    if request.headers.get("x-closedesk") != "1":
        raise HTTPException(status_code=403, detail="Use the CloseDesk page for this.")


class _AnswerLog:
    """What the chat stream said, kept so the conversation can be shown again as it was."""

    def __init__(self) -> None:
        self.text = ""
        self.data: dict = {"sources": [], "steps": [], "notes": []}

    def take(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "sources":
            self.data["sources"] = event.get("sources") or []
            self.data["mode"] = event.get("mode", "")
            if event.get("warning"):
                self.data["warning"] = event["warning"]
        elif kind in {"step", "note"}:
            self.data[kind + "s"].append(event.get("text", ""))
        elif kind == "delta":
            self.text += event.get("text", "")
            if "stopped answering partway" in event.get("text", ""):
                self.data["failed"] = True
        elif kind == "revise":
            self.text = event.get("text", "")
        elif kind == "error":
            self.data["failed"] = True
            self.text = (self.text + "\n\n" if self.text else "") + event.get("text", "")
        elif kind == "check":
            self.data["checks"] = event.get("items") or []
        elif kind == "context":
            self.data["context"] = event.get("text", "")
        elif kind == "mode":
            self.data["mode"] = event.get("mode", "")
            self.data["note"] = event.get("note", "")
        elif kind == "vision":
            self.data["vision"] = {key: event.get(key) for key in ("email_id", "n", "file", "pages", "estimate", "text", "question")}
        elif kind == "reading" and event.get("label"):  # which reading of the scans the answer used
            self.data["reading"] = {key: event.get(key) for key in ("state", "reader", "label", "text")}


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
        # What a job other than Process new mail is about (a vision read: its email and file), for the pages.
        self.about: dict | None = None
        self._stop = threading.Event()

    @property
    def stopping(self) -> bool:
        """Asked to stop: reading scans with the vision model stops before its next page (in Process new mail too,
        whose other steps run to the end)."""
        return self._stop.is_set()

    def request_stop(self) -> bool:
        with self._lock:
            if self.state != "running":
                return False
        self._stop.set()
        return True

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
                "about": self.about,
                "stopping": self._stop.is_set(),
            }

    def start(self, target, *, about: dict | None = None) -> bool:
        with self._lock:
            if self.state == "running":
                return False
            self.state, self.stage, self.done, self.total = "running", "starting", 0, 0
            self.note, self.result, self.error, self.about = "", None, "", about
            self._stop.clear()
        threading.Thread(target=self._run, args=(target,), daemon=True).start()
        return True

    def progress(self, stage: str, done: int, total: int, note: str) -> None:
        with self._lock:
            self.stage, self.done, self.total, self.note = stage, done, total, note

    def _run(self, target) -> None:
        try:
            result, state, error = target(self.progress), "done", ""
        except Exception as exc:  # the page shows the error instead of a dead spinner
            result, state, error = None, "error", str(exc)
        with self._lock:  # all at once: a page that sees the job done also sees when it finished
            self.state, self.result, self.error = state, result, error
            self.finished_at = datetime.now(timezone.utc).isoformat()


def _nearest_step(tokens: int) -> int:
    return min(range(len(agent.CONTEXT_STEPS)), key=lambda i: abs(agent.CONTEXT_STEPS[i] - tokens))


def create_app(settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.ensure_data_dir()
    store = store or Store(settings.db_path)
    apply_saved_timezone(settings, store)
    vision.apply_saved_mode(settings, store)
    saved_context = store.get_state(MIN_CONTEXT_KEY)
    if saved_context and saved_context.isdigit():
        set_min_context(settings, int(saved_context))
    app = FastAPI(title="CloseDesk", docs_url=None, redoc_url=None)
    app.add_middleware(LocalHostOnly, allowed=allowed_hosts(settings.host, settings.allowed_hosts))
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
        templates.env.globals["display_tz"] = settings.tz
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
            "last_folder": store.get_state("last_folder_ingest"),
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
            "coding_counts": store.coding_counts(),
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

    def more_link(request: Request, shown: int, total: int, limit: int) -> dict:
        """What the page says when a long list stops short, and the link that shows another 200."""
        if total <= shown:
            return {}
        query = dict(request.query_params)
        query["limit"] = str(limit + 200)
        return {"total": total, "more_url": f"{request.url.path}?{urlencode(query)}" if limit < MAX_PAGE_ROWS else ""}

    @app.get("/folder/{name}", response_class=HTMLResponse)
    def folder_page(request: Request, name: str, done: int = 0, limit: int = 200):
        if name not in FOLDER_LABELS:
            raise HTTPException(status_code=404, detail="Unknown folder")
        blurbs = {
            "important": "Needs a decision, a reply, a payment check, or a task. Most important first.",
            "informational": "Worth knowing. Nothing is waiting on you.",
            "reference": "Notifications, receipts, statements, and files to keep. Not a task.",
        }
        limit = max(1, min(limit, MAX_PAGE_ROWS))
        emails = store.list_emails(folder=name, order="score", done=bool(done), limit=limit)
        return render(
            request,
            "inbox.html",
            page=name,
            emails=emails,
            heading=FOLDER_LABELS[name] + (" · done" if done else ""),
            blurb="Mail you marked done. Undo puts it back in the list." if done else blurbs[name],
            folder_name=name,
            showing_done=bool(done),
            done_count=store.done_count(name),
            **more_link(request, len(emails), store.count_emails(folder=name, done=bool(done)), limit),
        )

    @app.post("/inbox/{email_id}/done")
    def mark_done(email_id: str, undo: int = 0, next: str = Query("/")):
        if store.get_email(email_id) is None:
            raise HTTPException(status_code=404, detail="Message not found")
        store.set_done(email_id, not undo)
        return RedirectResponse(_local_path(next, "/"), status_code=303)

    @app.get("/inbox", response_class=HTMLResponse)
    def inbox(
        request: Request,
        importance: str = "",
        category: str = "",
        flag: str = "",
        q: str = "",
        limit: int = 200,
    ):
        if q:
            cost_codes.refresh_if_changed(store, settings)
        limit = max(1, min(limit, MAX_PAGE_ROWS))
        filters = dict(importance=importance or None, category=category or None, flag=flag or None, q=q or None)
        emails = store.list_emails(**filters, limit=limit)
        return render(
            request,
            "inbox.html",
            page="inbox",
            emails=emails,
            filter_importance=importance,
            filter_category=category,
            query=q,
            heading="Filtered mail" if any([importance, category, flag, q]) else "All mail",
            **more_link(request, len(emails), store.count_emails(**filters), limit),
        )

    @app.get("/inbox/{email_id}", response_class=HTMLResponse)
    def email_detail(request: Request, email_id: str):
        email = store.get_email(email_id)
        if not email:
            return HTMLResponse("Not found", status_code=404)
        cost_codes.refresh(store, settings, email_ids=[email.id])
        return render(
            request,
            "detail.html",
            page="inbox",
            email=email,
            coding=store.cost_coding(email.id),
            codebook=cost_codes.load(settings),
            has_original=original_path(email) is not None,
            original_downloads=original_downloads(original_path(email)),
            check=fraud_view(email),
            files=file_cards(email),
            locked=fraud.attachments_locked(email),
            findings=store.findings(email.id, limit=20),
            sender_domain=fraud.domain_of(email.sender_email),
            freemail=fraud.domain_of(email.sender_email) in fraud.FREEMAIL,
        )

    def fraud_view(email) -> dict:
        """The saved fraud check for the page, or a fresh one for mail stored before checks existed."""
        saved = store.fraud_check(email.id)
        if saved is None:
            check = fraud.assess_email(store, fraud.trust_context(store, settings), email)
            saved = {"level": check.level, "score": check.score, "signals": check.signal_dicts(), "checked_at": ""}
        verdict = "safe" if "fraud_cleared" in email.flags else "fraud" if "fraud_confirmed" in email.flags else ""
        return {**saved, "verdict": verdict}

    def file_cards(email) -> list[dict]:
        cards = []
        states = semantic.file_states(store, settings, email)
        for index, att in enumerate(email.attachments, start=1):
            text = att.extracted_text or ""
            suffix = Path(att.filename).suffix.lower()
            cards.append(
                {
                    "n": index,
                    "att": att,
                    "kind": agent.file_kind(att),
                    "sections": len(documents.split_parts(text)) if text.strip() else 0,
                    "chars": len(text),
                    "downloadable": suffix in DOWNLOADABLE and agent.original_file(settings, email, att) is not None,
                    "blocked_type": suffix not in DOWNLOADABLE,
                    "search": states.get(att.id, "no_text"),
                }
            )
        return cards

    def email_file(email_id: str, n: int):
        chat_id = chats.chat_id_of(email_id)
        email = chats.chat_mail(store, chat_id) if chat_id else store.get_email(email_id)
        if not email:
            raise HTTPException(status_code=404, detail="Message not found")
        if not 1 <= n <= len(email.attachments):
            raise HTTPException(status_code=404, detail="No such file on this email")
        return email, email.attachments[n - 1]

    @app.get("/inbox/{email_id}/files/{n}", response_class=HTMLResponse)
    def file_page(request: Request, email_id: str, n: int, q: str = "", at: str = ""):
        email, att = email_file(email_id, n)
        text = att.extracted_text or ""
        parts = documents.split_parts(text) if text.strip() else []
        query = q.strip()[:120]
        matches = {part.label for part in documents.search_parts(text, query, limit=12)} if query else set()
        target, marked = documents.locate(parts, at) or (None, "")
        return render(
            request,
            "file.html",
            page="inbox",
            email=email,
            att=att,
            n=n,
            kind=agent.file_kind(att),
            parts=parts,
            matches=matches,
            q=query,
            target=target,
            marked=marked,
            locked=fraud.attachments_locked(email),
            downloadable=Path(att.filename).suffix.lower() in DOWNLOADABLE and agent.original_file(settings, email, att) is not None,
            viewable=Path(att.filename).suffix.lower() in agent.VIEWABLE and agent.original_file(settings, email, att) is not None,
            search="" if email.source == "chat" else semantic.file_states(store, settings, email).get(att.id, "no_text"),
            readings=vision.side_by_side(store.page_readings(att.id, att.sha256)),
            vision_offer=vision.offer(store, settings, email, att) if vision.readable_file(att.filename) else None,
        )

    @app.post("/inbox/{email_id}/index")
    def index_email(email_id: str):
        email = store.get_email(email_id)
        if not email:
            raise HTTPException(status_code=404, detail="Message not found")
        if not semantic.embedding_model(settings):
            notice = "index-off"
        else:
            notice = "index-failed" if semantic.index_mail(store, settings, emails=[email]) < 0 else "indexed"
        return RedirectResponse(f"/inbox/{email_id}?notice={notice}#files", status_code=303)

    def original_or_refuse(email, att, allowed) -> Path:
        if fraud.attachments_locked(email):
            raise HTTPException(
                status_code=403,
                detail="This email is flagged as possible payment fraud, so its files don't open. "
                "Verify it by phone, then mark it safe on the email page.",
            )
        if Path(att.filename).suffix.lower() not in allowed:
            raise HTTPException(status_code=403, detail="This kind of file only opens from Outlook.")
        path = agent.original_file(settings, email, att)
        if path is None:
            raise HTTPException(status_code=404, detail="The original file wasn't kept for this email.")
        return path

    @app.get("/inbox/{email_id}/files/{n}/view")
    def file_view(email_id: str, n: int):
        """The original PDF or picture shown in the browser; a citation adds #page=N to open at the page."""
        email, att = email_file(email_id, n)
        path = original_or_refuse(email, att, agent.VIEWABLE)
        return FileResponse(
            path,
            media_type=agent.VIEWABLE[Path(att.filename).suffix.lower()],
            content_disposition_type="inline",
            filename=att.filename,
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @app.get("/inbox/{email_id}/files/{n}/vision")
    def file_vision(email_id: str, n: int):
        email, att = email_file(email_id, n)
        return JSONResponse(vision.offer(store, settings, email, att))

    @app.post("/inbox/{email_id}/files/{n}/vision")
    def file_vision_read(request: Request, email_id: str, n: int, again: int = 0):
        """Read the file's pages that need it (``again``: every one of them, a second time) in the background."""
        _require_page(request)
        email, att = email_file(email_id, n)
        reply, status = vision.start(store, settings, job, email, att, n, again=bool(again))
        return JSONResponse(reply, status_code=status)

    @app.get("/inbox/{email_id}/files/{n}/download")
    def file_download(email_id: str, n: int):
        email, att = email_file(email_id, n)
        path = original_or_refuse(email, att, DOWNLOADABLE)
        return FileResponse(
            path,
            filename=att.filename,
            media_type="application/octet-stream",
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @app.post("/inbox/{email_id}/findings/clear")
    def findings_clear(email_id: str):
        store.clear_findings(email_id)
        return RedirectResponse(f"/inbox/{email_id}?notice=findings-cleared#notes", status_code=303)

    @app.post("/inbox/{email_id}/fraud")
    def fraud_verdict(email_id: str, choice: str = Form(...), note: str = Form("")):
        verdict, _, scope = choice.partition(":")
        try:
            fraud.record_fraud_verdict(store, settings, email_id, verdict=verdict, scope=scope or "email", note=note)
        except KeyError:
            raise HTTPException(status_code=404, detail="Message not found") from None
        except ValueError as exc:
            notice = "fraud-freemail" if "free email" in str(exc) else "fraud-invalid"
            return RedirectResponse(f"/inbox/{email_id}?notice={notice}#fraud", status_code=303)
        notice = "fraud-safe" if verdict == "safe" else "fraud-reported"
        return RedirectResponse(f"/inbox/{email_id}?notice={notice}#fraud", status_code=303)

    @app.get("/fraud", response_class=HTMLResponse)
    def fraud_page(request: Request):
        entries = store.trust_entries()
        weights = fraud.learned_weights(store)
        return render(
            request,
            "fraud.html",
            page="fraud",
            configured=settings.trusted_domain_list,
            domains=[row for row in entries if row["kind"] == "domain"],
            senders=[row for row in entries if row["kind"] == "sender"],
            suggestions=fraud.suggested_domains(store, settings),
            weights=[
                {"key": key, "label": fraud.LABELS.get(key, key), "points": fraud.POINTS[key], "weight": weights.get(key, 1.0)}
                for key in sorted(fraud.LEARNABLE, key=lambda item: -fraud.POINTS[item])
            ],
            flagged=store.flagged(limit=100),
            log=store.fraud_log(limit=60),
            high_at=fraud.HIGH_AT,
            caution_at=fraud.CAUTION_AT,
        )

    @app.post("/fraud/domain")
    def fraud_domain(domain: str = Form(...), verdict: str = Form("safe"), note: str = Form("")):
        try:
            fraud.set_domain_trust(store, settings, domain, verdict="fraud" if verdict == "fraud" else "safe", note=note)
        except ValueError as exc:
            notice = "fraud-freemail" if "free email" in str(exc) else "domain-invalid"
            return RedirectResponse(f"/fraud?notice={notice}", status_code=303)
        return RedirectResponse(f"/fraud?notice={'trust-reported' if verdict == 'fraud' else 'trust-added'}", status_code=303)

    @app.post("/fraud/domain/remove")
    def fraud_domain_remove(domain: str = Form(...)):
        try:
            fraud.set_domain_trust(store, settings, domain, remove=True)
        except ValueError:
            return RedirectResponse("/fraud?notice=domain-invalid", status_code=303)
        return RedirectResponse("/fraud?notice=trust-removed", status_code=303)

    @app.post("/fraud/sender/remove")
    def fraud_sender_remove(sender: str = Form(...)):
        fraud.remove_sender_trust(store, settings, sender)
        return RedirectResponse("/fraud?notice=trust-removed", status_code=303)

    @app.get("/fraud/log.csv")
    def fraud_log_csv():
        return Response(
            content=fraud.log_csv(store),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=closedesk-fraud-log.csv"},
        )

    def original_path(email) -> Path | None:
        if not email.source_path:
            return None
        path = Path(email.source_path).resolve()
        root = settings.inbox_processed.resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return None
        return path

    @app.get("/inbox/{email_id}/preview", response_class=HTMLResponse)
    def email_preview(request: Request, email_id: str, next: str = "/"):
        email = store.get_email(email_id)
        if not email:
            return HTMLResponse("<p class='muted'>This email is no longer here.</p>", status_code=404)
        cost_codes.refresh(store, settings, email_ids=[email.id])
        return render(
            request,
            "_preview.html",
            email=email,
            coding=store.cost_coding(email.id),
            codebook=cost_codes.load(settings),
            back=_local_path(next, "/"),
            done=store.is_done(email.id),
            has_original=original_path(email) is not None,
            original_downloads=original_downloads(original_path(email)),
            files=file_cards(email),
            locked=fraud.attachments_locked(email),
        )

    @app.post("/inbox/{email_id}/open")
    def open_original(request: Request, email_id: str):
        _require_page(request)
        if request.client and request.client.host not in LOCAL_CLIENTS:
            raise HTTPException(status_code=403, detail="Files open on the computer running CloseDesk only.")
        email = store.get_email(email_id)
        if not email:
            raise HTTPException(status_code=404, detail="Message not found")
        download = f"/inbox/{email_id}/original"
        path = original_path(email)
        if path is None:
            return JSONResponse(
                {"ok": False, "download": download, "message": "No original file for this email, so here is a copy to open."}
            )
        if path.suffix.lower() not in MAIL_FILES | DOWNLOADABLE:
            # A file dropped loose in the inbox, of a kind that doesn't download: it was never in Outlook.
            return JSONResponse(
                {"ok": False, "message": f"This came in as a {path.suffix or 'plain'} file, which CloseDesk doesn't open "
                 f"or download. Open it from {path.parent} on this computer."}
            )
        if path.suffix.lower() not in MAIL_FILES:
            return JSONResponse(
                {"ok": False, "download": download, "message": f"This came in as a {path.suffix or 'plain'} file, so it downloads instead of opening."}
            )
        try:
            open_file(path)
        except OSError as exc:
            return JSONResponse(
                {"ok": False, "download": download, "message": f"Couldn't open it directly ({exc}). Downloading a copy."}
            )
        return JSONResponse({"ok": True, "message": f"Opening {path.name} in your mail app."})

    @app.get("/inbox/{email_id}/original")
    def download_original(email_id: str):
        email = store.get_email(email_id)
        if not email:
            raise HTTPException(status_code=404, detail="Message not found")
        path = original_path(email)
        if path is not None:
            if fraud.attachments_locked(email) and email.attachments:
                raise HTTPException(
                    status_code=403,
                    detail="This email may be payment fraud, so its original (with its files) doesn't download. Open it in Outlook.",
                )
            # A file dropped loose in the inbox is its own original; the attachment rule applies to it too.
            if not original_downloads(path):
                raise HTTPException(
                    status_code=403,
                    detail=f"This came in as a {path.suffix or 'plain'} file, which CloseDesk doesn't download. "
                    "Open it from the inbox's processed folder on the computer running CloseDesk.",
                )
            return FileResponse(path, filename=path.name, headers={"X-Content-Type-Options": "nosniff"})
        return Response(
            content=_rebuilt_eml(email),
            media_type="message/rfc822",
            headers={"Content-Disposition": f'attachment; filename="{_ascii_name(email.subject)}.eml"'},
        )

    @app.post("/inbox/{email_id}/draft")
    async def draft(request: Request, email_id: str):
        _require_page(request)
        email = store.get_email(email_id)
        if not email:
            raise HTTPException(status_code=404, detail="Message not found")
        data = await _json_body(request)
        instructions = str(data.get("instructions") or "")[:300]
        return JSONResponse(await run_in_threadpool(draft_reply, settings, email, instructions=instructions))

    def answer_lines(data: dict, question: str):
        """Saves the question and gathers what its answer needs, then gives the answer's lines to stream."""
        chat_id = str(data.get("chat_id") or "")
        if not chats.valid_id(chat_id):
            chat_id = chats.new_id()
        store.create_chat(chat_id)
        saved = store.chat_turns(chat_id)
        history = [{"role": turn["role"], "text": turn["text"]} for turn in saved][-6:]
        if not saved:
            store.touch_chat(chat_id, title=chats.title_for(question))
        store.add_chat_turn(chat_id, "user", question)
        uploads = chats.chat_mail(store, chat_id)
        past = chats.past_context(store, question, exclude=chat_id)
        email_id = str(data.get("email_id") or "") or None
        as_of = board_date()
        focus = build_digest(
            store,
            as_of=as_of,
            generated_at=datetime.now(settings.tz),
            tz=settings.tz,
            lookback_days=settings.digest_lookback_days,
            save=False,
            finance=is_finance(settings, store),
        )["focus"]

        def lines():
            finished = False
            answer = _AnswerLog()
            events = None
            try:
                yield json.dumps({"type": "chat", "id": chat_id, "title": (store.chat(chat_id) or {}).get("title", "")}) + "\n"
                events = answer_stream(
                    store,
                    settings,
                    question,
                    history=history,
                    email_id=email_id,
                    focus=focus,
                    today=as_of.isoformat(),
                    uploads=uploads,
                    past=past,
                )
                for event in events:
                    answer.take(event)
                    finished = finished or event.get("type") == "done"
                    yield json.dumps(event) + "\n"
            except GeneratorExit:  # the browser went away mid-answer: keep what was said, marked as cut short
                answer.data["failed"] = True
                if answer.text:
                    answer.text += "\n\n(Stopped: the page closed before the answer finished.)"
                raise
            except Exception as exc:  # the chat box shows a message instead of hanging
                event = {"type": "error", "text": f"Something went wrong answering that ({type(exc).__name__})."}
                answer.take(event)
                yield json.dumps(event) + "\n"
            finally:
                if events is not None:
                    events.close()  # stops the model writing an answer nobody will read
                if not answer.text:
                    answer.data["failed"] = True
                store.add_chat_turn(chat_id, "assistant", answer.text or "(No answer: the question was stopped.)", answer.data)
            if not finished:
                yield json.dumps({"type": "done"}) + "\n"

        return lines()

    @app.post("/chat")
    async def chat(request: Request):
        _require_page(request)
        data = await _json_body(request)
        question = str(data.get("message") or "").strip()
        if not question:
            raise HTTPException(status_code=400, detail="Ask a question first.")
        # The database and today's list take a moment: in a worker thread, so other pages aren't held up.
        lines = await run_in_threadpool(answer_lines, data, question)
        return StreamingResponse(_closing(lines), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"})

    @app.get("/chats")
    def chat_list(q: str = ""):
        return JSONResponse({"chats": store.list_chats(q[:100])})

    @app.post("/chats")
    def chat_new(request: Request):
        _require_page(request)
        chat_id = chats.new_id()
        store.create_chat(chat_id)
        return JSONResponse({"id": chat_id})

    def known_chat(chat_id: str) -> dict:
        found = store.chat(chat_id) if chats.valid_id(chat_id) else None
        if found is None:
            raise HTTPException(status_code=404, detail="That conversation isn't saved here.")
        return found

    @app.get("/chats/{chat_id}")
    def chat_show(chat_id: str):
        found = known_chat(chat_id)
        return JSONResponse({**found, "turns": store.chat_turns(chat_id), "files": chats.file_cards(store, chat_id)})

    @app.post("/chats/{chat_id}/delete")
    def chat_delete(request: Request, chat_id: str):
        _require_page(request)
        known_chat(chat_id)
        chats.delete(store, settings, chat_id)
        return JSONResponse({"ok": True})

    @app.post("/chats/{chat_id}/files")
    async def chat_add_files(request: Request, chat_id: str, files: list[UploadFile] = File(...)):
        _require_page(request)
        known_chat(chat_id)
        problems = []
        for upload in files[: chats.MAX_FILES]:
            data = await upload.read(chats.MAX_UPLOAD_BYTES + 1)
            try:
                await run_in_threadpool(chats.add_file, store, settings, chat_id, upload.filename or "file", upload.content_type or "", data)
            except ValueError as exc:
                problems.append(str(exc))
            except Exception as exc:  # a damaged file is reported, the others still go in
                problems.append(f"{upload.filename}: couldn't be read ({type(exc).__name__}).")
        for upload in files[chats.MAX_FILES :]:
            problems.append(f"{upload.filename or 'file'} wasn't added: up to {chats.MAX_FILES} files can be added at once.")
        return JSONResponse({"files": chats.file_cards(store, chat_id), "problems": problems})

    @app.post("/chats/{chat_id}/files/{n}/delete")
    def chat_remove_file(request: Request, chat_id: str, n: int):
        _require_page(request)
        known_chat(chat_id)
        rows = store.chat_files(chat_id)
        if not 1 <= n <= len(rows):
            raise HTTPException(status_code=404, detail="No such file in this conversation")
        chats.remove_file(store, settings, chat_id, rows[n - 1]["filename"])
        return JSONResponse({"files": chats.file_cards(store, chat_id)})

    @app.get("/chat/window", response_class=HTMLResponse)
    def chat_window(request: Request):
        return render(request, "chat_window.html", page="chat")

    @app.post("/inbox/{email_id}/correct")
    def correct_category(email_id: str, category: str = Form(...), reason: str = Form(...), next: str = Form("")):
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
        back = _local_path(next, f"/inbox/{email_id}")
        return RedirectResponse(back + ("&" if "?" in back else "?") + "notice=corrected", status_code=303)

    @app.post("/inbox/{email_id}/coding/confirm")
    def confirm_coding(email_id: str, next: str = Form("")):
        if store.get_email(email_id) is None:
            raise HTTPException(status_code=404, detail="Message not found")
        try:
            cost_codes.confirm(store, settings, email_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return _back(next, f"/inbox/{email_id}", "coding-confirmed", "coding")

    @app.post("/inbox/{email_id}/coding/revise")
    def revise_coding(email_id: str, codes: list[str] = Form(default=[]), next: str = Form("")):
        if store.get_email(email_id) is None:
            raise HTTPException(status_code=404, detail="Message not found")
        try:
            cost_codes.revise(store, settings, email_id, codes)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return _back(next, f"/inbox/{email_id}", "coding-revised", "coding")

    @app.get("/coding", response_class=HTMLResponse)
    def coding_page(request: Request, status: str = "review", q: str = ""):
        cost_codes.refresh_if_changed(store, settings)
        status = status if status in {"review", "suggested", "unmatched", "confirmed", "all"} else "review"
        return render(
            request,
            "coding.html",
            page="coding",
            rows=store.cost_codings(status=None if status == "all" else status, q=q or None),
            status=status,
            coding_q=q,
            codebook=cost_codes.load(settings),
            workbook=str(cost_codes.workbook_path(settings)),
            coding_folder=str(settings.cost_codes_folder),
        )

    @app.post("/coding/check")
    def coding_check():
        cost_codes.ensure_workbook(settings)
        cost_codes.refresh(store, settings, force=True)
        return RedirectResponse("/coding?notice=coding-checked", status_code=303)

    @app.post("/coding/open")
    def coding_open(request: Request):
        _require_page(request)
        if request.client and request.client.host not in LOCAL_CLIENTS:
            raise HTTPException(status_code=403, detail="Files open on the computer running CloseDesk only.")
        path = cost_codes.ensure_workbook(settings)
        try:
            open_file(path if path.exists() else settings.cost_codes_folder)
        except OSError as exc:
            return JSONResponse({"ok": False, "message": f"Couldn't open it ({exc}). It is at {path}."})
        return JSONResponse({"ok": True, "message": f"Opening {path.name}. Save it when you're done; CloseDesk reads it again by itself."})

    @app.post("/process")
    def process():
        from controller_inbox.overnight import run_overnight

        started = job.start(
            lambda progress: run_overnight(
                store, settings, sync_graph=False, on_progress=progress, vision_minutes=vision.process_minutes(settings),
                should_stop=lambda: job.stopping,
            )
        )
        busy = "busy-vision" if (job.snapshot()["about"] or {}).get("kind") == "vision" else "busy"
        return RedirectResponse(f"/?notice={'processing' if started else busy}", status_code=303)

    @app.get("/process/status")
    def process_status():
        return JSONResponse(job.snapshot())

    @app.post("/process/stop")
    def process_stop():
        return RedirectResponse("/?notice=stopping" if job.request_stop() else "/", status_code=303)

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

    # Why the last change of chat model in Setup didn't happen, shown with its notice.
    chat_switch = {"problem": ""}

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, recheck: int = 0):
        model = check_model(settings, use_cache=not recheck)
        return render(
            request,
            "settings.html",
            page="settings",
            model=model,
            llm_enabled=model.active,
            recommended_context=agent.RECOMMENDED_CONTEXT,
            context_steps=[agent.context_capacity(tokens, settings.chat_max_tokens) for tokens in agent.CONTEXT_STEPS],
            context_step=_nearest_step(settings.min_context_tokens),
            context_target=context_target(settings, model),
            will_reload=needs_more_context(settings),
            ocr_engine=ocr.engine_name(),
            models=model_roles.models_in_use(settings, status=model),
            chat_choice=model_roles.chat_choices(settings, model),
            chat_problem=chat_switch["problem"] if request.query_params.get("notice") == "chat-model-failed" else "",
            vision_setup=vision_setup(model),
            search=semantic.coverage(store, settings),
            timezone_choice=settings.timezone,
            timezone_options=_timezone_options(settings.timezone),
            active_zone_name=zone_name(effective_timezone(settings.timezone)),
            active_offset=offset_label(settings.tz),
            active_daylight=on_daylight_time(settings.tz),
            now_local=datetime.now(settings.tz).strftime("%a, %b %d, %Y · %H:%M"),
        )

    def _timezone_options(choice: str) -> list[dict]:
        now = datetime.now(timezone.utc)

        def option(value: str, zone: str, label: str) -> dict:
            info = ZoneInfo(zone)
            return {
                "value": value,
                "zone": zone,
                "label": label,
                "now": offset_label(info, now),
                "daylight": on_daylight_time(info, now),
            }

        computer = computer_timezone()
        rows = [option(AUTO, computer, f"This computer: {zone_option(computer, now)}")]
        rows.extend(option(name, name, label) for name, label in zone_choices(now, keep=choice))
        return rows

    @app.post("/settings/timezone")
    def save_timezone(zone: str = Form(...)):
        try:
            set_timezone(store, settings, zone)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return RedirectResponse("/settings?notice=timezone#timezone", status_code=303)

    def vision_setup(model) -> dict:
        # When it is off, still say which model would read pages, so the choice can be made before turning it on.
        on = settings if settings.vision_mode != "off" else settings.model_copy(update={"vision_mode": "auto"})
        reader = vision.reading_model(on)
        per_page = vision.seconds_per_page(store, on) if reader else None
        # A model chosen in Setup that LM Studio no longer has stays chosen (listed as gone) until another is.
        missing = settings.vision_model if settings.vision_model and settings.vision_model not in model.vision_models else ""
        return {
            "reachable": model.reachable,
            "by_default": bool(reader) and vision.trusted_reader(reader),
            "sees": bool(reader),
            "reader": reader,
            "reader_label": vision.reader_for(reader).label if reader else "",
            "chosen": settings.vision_model or "auto",
            "missing": missing,
            "choices": [*model.vision_models, *([missing] if missing else [])],
            "renderer": vision.can_render(),
            "mode": settings.vision_mode,
            "pages_read": store.vision_pages_read(),
            "speed": f"On this computer it reads a page in {vision.duration(per_page)}." if per_page else "",
        }

    @app.post("/settings/vision")
    def save_vision(mode: str = Form(...), model: str | None = Form(None)):
        try:
            vision.save_mode(settings, store, mode, model)
        except ValueError:
            return RedirectResponse("/settings#vision", status_code=303)
        return RedirectResponse("/settings?notice=vision-saved#vision", status_code=303)

    @app.post("/settings/chat-model")
    def save_chat_model(model: str = Form(...)):
        if job.snapshot()["state"] == "running":
            return RedirectResponse("/settings?notice=chat-model-busy#chat-model", status_code=303)
        chat_switch["problem"] = use_chat_model(settings, model)
        notice = "chat-model-failed" if chat_switch["problem"] else "chat-model"
        return RedirectResponse(f"/settings?notice={notice}#chat-model", status_code=303)

    @app.post("/settings/index")
    def index_all():
        if not semantic.embedding_model(settings):
            return RedirectResponse("/settings?notice=index-off#search", status_code=303)

        def run(progress):
            progress("indexing", 0, 0, "")
            return {"kind": "index", "indexed": max(0, semantic.index_mail(store, settings, on_progress=lambda i, n, _name: progress("indexing", i, n, "")))}

        started = job.start(run)
        return RedirectResponse(f"/settings?notice={'indexing' if started else 'busy'}#search", status_code=303)

    @app.post("/settings/context")
    def save_context(step: int = Form(...)):
        if not 0 <= step < len(agent.CONTEXT_STEPS):
            raise HTTPException(status_code=400, detail="Unknown context size")
        tokens = agent.CONTEXT_STEPS[step]
        store.set_state(MIN_CONTEXT_KEY, str(tokens))
        set_min_context(settings, tokens)
        return RedirectResponse(f"/settings?notice={'context' if tokens else 'context-off'}#context", status_code=303)

    @app.post("/settings/profile")
    def save_profile(profile: str = Form(...)):
        try:
            set_profile(store, profile)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return RedirectResponse("/settings?notice=profile", status_code=303)

    @app.post("/demo/reload")
    def demo_reload():
        from controller_inbox.overnight import RunBusy

        if store.real_mail_count():
            return RedirectResponse("/settings?notice=sample-blocked", status_code=303)
        if job.snapshot()["state"] == "running":
            return RedirectResponse("/settings?notice=sample-busy", status_code=303)
        try:
            load_sample(store, settings)
        except RunBusy:
            return RedirectResponse("/settings?notice=sample-busy", status_code=303)
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

    from controller_inbox.web_api import register_workspace

    register_workspace(
        app, store=store, settings=settings, job=job, board_date=board_date,
        fraud_view=fraud_view, file_cards=file_cards, original_path=original_path,
    )
    return app


async def _closing(lines: Generator[str, None, None]) -> AsyncIterator[str]:
    """Streams a generator's lines from a worker thread, and closes it however the response ends.

    When the browser goes away mid-answer, the response stops reading without closing a plain generator,
    so its cleanup (saving the answer so far, stopping the model) would never run."""
    try:
        async for line in iterate_in_threadpool(lines):
            yield line
    finally:
        # Shielded: the response is being cancelled, which would otherwise cancel this as well.
        with anyio.CancelScope(shield=True):
            await run_in_threadpool(lines.close)


async def _json_body(request: Request) -> dict:
    try:
        data = await request.json()
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _ascii_name(subject: str) -> str:
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", subject or "email").strip(" ._")[:80] or "email"


def _rebuilt_eml(email) -> bytes:
    """A readable .eml from what CloseDesk stored, for mail that has no original file (sample, Graph sync)."""
    message = EmailMessage()
    one_line = lambda value: re.sub(r"[\r\n]+", " ", value or "").strip()  # noqa: E731
    message["Subject"] = one_line(email.subject)
    sender = one_line(email.sender_email)
    message["From"] = _from_header(one_line(email.sender_name), sender)
    try:
        message["Date"] = format_datetime(datetime.fromisoformat(email.received_at.replace("Z", "+00:00")))
    except (TypeError, ValueError):
        pass
    message.set_content(email.body_text or "")
    return bytes(message)


def _from_header(name: str, sender: str) -> str:
    """``Name <address>``. An internationalized domain is written in its ASCII (IDNA) form, which formataddr
    needs; an address it still can't write, such as one with a non-ASCII local part, goes in on its own."""
    local, at, domain = sender.rpartition("@")
    if at and not domain.isascii():
        try:
            sender = f"{local}@{domain.encode('idna').decode('ascii')}"
        except UnicodeError:
            pass
    if not name:
        return sender
    try:
        return formataddr((name, sender))
    except UnicodeEncodeError:
        return sender


def _back(next_path: str, fallback: str, notice: str, anchor: str = "") -> RedirectResponse:
    back = _local_path(next_path, fallback).split("#", 1)[0]
    back += ("&" if "?" in back else "?") + f"notice={notice}"
    return RedirectResponse(back + (f"#{anchor}" if anchor else ""), status_code=303)


def _local_path(value: str, fallback: str) -> str:
    """Only redirect within the dashboard."""
    if value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return fallback
