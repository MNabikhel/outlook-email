"""What the classic pages (web.py) and the workspace's API (web_api.py) share: the notices that say what a
button did, which files download, the checks that a request came from CloseDesk's own page, and finding an
email's attachment."""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, Request

from controller_inbox import agent, chats, fraud

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
# Only mail files are handed to the OS; anything else dropped in the inbox could be a program.
MAIL_FILES = {".msg", ".eml"}


def original_downloads(path: Path | None) -> bool:
    """Whether an email's original downloads: a rebuilt .eml (no original), or a mail or downloadable file."""
    return path is None or path.suffix.lower() in MAIL_FILES | DOWNLOADABLE


def host_name(header: str) -> str:
    """The host of a Host header or origin, without its port ("[::1]" keeps its brackets)."""
    header = header.strip().lower()
    if header.startswith("["):
        return header[: header.find("]") + 1] if "]" in header else header
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


def same_origin(headers: dict[bytes, bytes], host: str, allowed: set[str] | None = None) -> bool:
    """A form another website submits to this server carries that site's Origin; refuse it."""
    origin = headers.get(b"origin", b"").decode("latin-1").strip()
    if origin:
        origin_host = origin.lower().split("://", 1)[-1].rstrip("/")
        if origin_host != host.strip().lower():
            return False
        return allowed is None or "*" in allowed or host_name(origin_host) in allowed
    return headers.get(b"sec-fetch-site", b"").decode("latin-1").lower() not in {"cross-site", "same-site"}


def require_page(request: Request) -> None:
    """The page's own scripts send this header; other sites cannot without the browser blocking them."""
    if request.headers.get("x-closedesk") != "1":
        raise HTTPException(status_code=403, detail="Use the CloseDesk page for this.")


async def json_body(request: Request) -> dict:
    """The request's JSON object, or {} when it sent none (or something else)."""
    try:
        data = await request.json()
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def nearest_step(tokens: int) -> int:
    """The context-size step (``agent.CONTEXT_STEPS``) closest to ``tokens``, as Setup's slider shows it."""
    return min(range(len(agent.CONTEXT_STEPS)), key=lambda i: abs(agent.CONTEXT_STEPS[i] - tokens))


def attachment_or_404(store, email_id: str, n: int, *, allow_locked: bool = False, allow_chat: bool = True, missing: str = "Message not found"):
    """(email, attachment) for file ``n`` (from 1) of an email, or the HTTP error saying why there is none: no such
    email (``missing`` says so), no such file, or (unless ``allow_locked``) the email is held as possible payment
    fraud. ``allow_chat``: ``email_id`` may name a conversation, whose added files are its attachments."""
    chat_id = chats.chat_id_of(email_id) if allow_chat else ""
    email = chats.chat_mail(store, chat_id) if chat_id else store.get_email(email_id)
    if email is None:
        raise HTTPException(status_code=404, detail=missing)
    if not 1 <= n <= len(email.attachments):
        raise HTTPException(status_code=404, detail="No such file on this email")
    if not allow_locked and fraud.attachments_locked(email):
        raise HTTPException(status_code=403, detail=fraud.LOCKED_FILES)
    return email, email.attachments[n - 1]
