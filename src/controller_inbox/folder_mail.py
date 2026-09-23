"""Read Outlook .msg / .eml files, and loose attachments, from a drop folder."""

from __future__ import annotations

import hashlib
import mimetypes
import re
import shutil
from collections.abc import Callable
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path

from controller_inbox.config import Settings
from controller_inbox.extract import html_to_text, sha256_bytes
from controller_inbox.models import RawAttachment, RawMessage
from controller_inbox.pipeline import KEEP_READINGS, process_message
from controller_inbox.store import Store


MESSAGE_SUFFIXES = {".msg", ".eml"}
SKIP_NAMES = {".gitkeep", ".ds_store"}


def ingest_folder(
    store: Store,
    settings: Settings,
    *,
    now: datetime | None = None,
    report: dict | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> list:
    """Read everything in the drop folder. Returns the records that were read.

    ``report`` (when given) is filled with ``read``, ``already_read`` (same mail
    dropped again after the model or the user filed it), and ``failed`` rows.
    A file that cannot be read moves to ``inbox/failed`` with a note, so it is
    not retried on every run.
    """
    settings.ensure_data_dir()
    report = report if report is not None else {}
    report.update({"read": 0, "already_read": 0, "failed": []})
    records = []
    batches = collect_batches(settings)
    for index, (path, sidecars) in enumerate(batches, start=1):
        if on_progress:
            on_progress(index, len(batches), path.name)
        owned = [path, *sidecars] if path.suffix.lower() in MESSAGE_SUFFIXES else [path]
        try:
            if path.suffix.lower() in MESSAGE_SUFFIXES:
                raw = _parse_message(path)
                for extra in sidecars:
                    raw.attachments.append(_file_attachment(extra))
                    raw.has_attachments = True
            else:
                raw = _standalone(path)
            existing = store.get_email(raw.id)
            if existing is not None and existing.model_status in KEEP_READINGS:
                report["already_read"] += 1
                _archive(settings, owned)
                continue
            record = process_message(raw, store, settings, now=now)
            _write_extracted(settings, raw)
            _archive(settings, owned)
            records.append(record)
            report["read"] += 1
        except Exception as exc:
            report["failed"].append({"file": path.name, "error": str(exc)[:300]})
            _quarantine(settings, owned, exc)
    if records:
        stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        store.set_state("last_folder_ingest", stamp)
        store.set_state("last_sync_at", stamp)
    return records


def collect_messages(settings: Settings) -> list[tuple[RawMessage, list[Path]]]:
    """Backwards-compatible helper used by tests that inspect parsed messages."""
    parsed = []
    for path, sidecars in collect_batches(settings):
        if path.suffix.lower() in MESSAGE_SUFFIXES:
            raw = _parse_message(path)
            for extra in sidecars:
                raw.attachments.append(_file_attachment(extra))
                raw.has_attachments = True
            parsed.append((raw, [path, *sidecars]))
        else:
            parsed.append((_standalone(path), [path]))
    return parsed


def collect_batches(settings: Settings) -> list[tuple[Path, list[Path]]]:
    incoming = [p for p in _files(settings.inbox_incoming)]
    attachment_files = [p for p in _files(settings.inbox_attachments)]
    messages = [p for p in incoming if p.suffix.lower() in MESSAGE_SUFFIXES]
    loose = [p for p in incoming if p.suffix.lower() not in MESSAGE_SUFFIXES]
    consumed: set[Path] = set()
    batches: list[tuple[Path, list[Path]]] = []

    for path in messages:
        sidecars = _sidecars_for(path, loose + attachment_files, consumed)
        consumed.update(sidecars)
        batches.append((path, sidecars))

    for path in loose + attachment_files:
        if path in consumed:
            continue
        if path.parent != settings.inbox_incoming and path.parent != settings.inbox_attachments:
            if path.parent.parent == settings.inbox_attachments:
                continue
        batches.append((path, []))
    return batches


def _sidecars_for(message_path: Path, candidates: list[Path], consumed: set[Path]) -> list[Path]:
    stem = message_path.stem.casefold()
    matched: list[Path] = []
    for path in candidates:
        if path in consumed or path.resolve() == message_path.resolve():
            continue
        parent = path.parent.name.casefold()
        sibling = path.parent.resolve() == message_path.parent.resolve() and path.stem.casefold() == stem
        folder = parent == stem
        if sibling or folder:
            matched.append(path)
    return matched


def _parse_message(path: Path) -> RawMessage:
    if path.suffix.lower() == ".eml":
        return _parse_eml(path)
    return _parse_msg(path)


def _parse_eml(path: Path) -> RawMessage:
    data = path.read_bytes()
    parsed = BytesParser(policy=policy.default).parsebytes(data)
    subject = str(parsed.get("subject") or path.stem)
    sender_name, sender_email = _split_address(str(parsed.get("from") or ""))
    received = _email_date(parsed.get("date"), path)
    body_parts: list[str] = []
    attachments: list[RawAttachment] = []
    html_fallback = ""
    for part in parsed.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        disposition = (part.get_content_disposition() or "").lower()
        payload = part.get_payload(decode=True) or b""
        if filename or disposition == "attachment":
            attachments.append(
                RawAttachment(
                    id=filename or f"part-{len(attachments)+1}",
                    filename=filename or f"attachment-{len(attachments)+1}",
                    content_type=part.get_content_type(),
                    size_bytes=len(payload),
                    content=payload,
                )
            )
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain":
            body_parts.append(_decode_text_part(part, payload))
        elif ctype == "text/html" and not html_fallback:
            html_fallback = html_to_text(_decode_text_part(part, payload))
    body = "\n".join(p for p in body_parts if p).strip() or html_fallback
    message_id = str(parsed.get("message-id") or "").strip()
    return _raw_message(path, data, subject, sender_name, sender_email, received, body, attachments, message_id)


def _parse_msg(path: Path) -> RawMessage:
    try:
        import extract_msg
    except ImportError as exc:
        raise RuntimeError("Reading .msg files requires the extract-msg package.") from exc

    message = extract_msg.Message(str(path))
    try:
        subject = message.subject or path.stem
        sender_name, sender_email = _split_address(message.sender or "")
        if not sender_email:
            sender_email = getattr(message, "senderEmail", None) or getattr(message, "sender_email", None) or ""
        body = message.body or ""
        html_body = getattr(message, "htmlBody", None)
        if not body and html_body:
            if isinstance(html_body, bytes):
                html_body = html_body.decode("utf-8", errors="replace")
            body = html_to_text(html_body)
        received = _coerce_date(getattr(message, "date", None), path)
        attachments: list[RawAttachment] = []
        for att in message.attachments:
            filename = att.longFilename or att.shortFilename or "attachment"
            payload = att.data or b""
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            if isinstance(payload, str):
                payload = payload.encode("utf-8", errors="replace")
            elif not isinstance(payload, (bytes, bytearray)):
                # A forwarded email attached as an item: keep its text, not the object.
                payload = _embedded_message_text(payload).encode("utf-8")
                filename = (Path(filename).stem or "forwarded message") + ".txt"
                content_type = "text/plain"
            attachments.append(
                RawAttachment(
                    id=filename,
                    filename=filename,
                    content_type=content_type,
                    size_bytes=len(payload),
                    content=bytes(payload),
                )
            )
        message_id = str(getattr(message, "messageId", None) or "").strip()
    finally:
        message.close()
    data = path.read_bytes()
    return _raw_message(path, data, subject, sender_name, sender_email, received, body, attachments, message_id)


def _embedded_message_text(item) -> str:
    subject = getattr(item, "subject", "") or ""
    sender = getattr(item, "sender", "") or ""
    date = getattr(item, "date", "") or ""
    body = getattr(item, "body", "") or ""
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    return f"Forwarded message\nSubject: {subject}\nFrom: {sender}\nDate: {date}\n\n{body}".strip()


def _standalone(path: Path) -> RawMessage:
    data = path.read_bytes()
    received = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    attachment = _file_attachment(path)
    return RawMessage(
        id=_message_id(data),
        subject=path.stem,
        sender_name="Dropped file",
        sender_email="",
        received_at=received,
        body_text=f"File dropped in the inbox folder: {path.name}",
        body_preview=path.name,
        has_attachments=True,
        source="folder",
        attachments=[attachment],
    )


def _raw_message(
    path, data, subject, sender_name, sender_email, received, body, attachments, message_id: str = ""
) -> RawMessage:
    return RawMessage(
        id=_stable_id(message_id) if message_id else _message_id(data),
        internet_message_id=message_id,
        subject=subject,
        sender_name=sender_name,
        sender_email=sender_email,
        received_at=received,
        body_text=body,
        body_preview=(body or subject)[:240],
        has_attachments=bool(attachments),
        source="folder",
        attachments=attachments,
    )


def _file_attachment(path: Path) -> RawAttachment:
    data = path.read_bytes()
    return RawAttachment(
        id=path.name,
        filename=path.name,
        content_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        size_bytes=len(data),
        content=data,
    )


def _message_id(data: bytes) -> str:
    return "file-" + hashlib.sha256(data).hexdigest()[:20]


def _stable_id(message_id: str) -> str:
    """Same Outlook message, same id — whether it was saved as .msg or .eml, today or next week."""
    normalized = message_id.strip().strip("<>").strip().lower()
    return "mail-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def _files(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    found = []
    for path in folder.rglob("*"):
        if path.is_file() and path.name.lower() not in SKIP_NAMES and not path.name.startswith("."):
            found.append(path)
    return sorted(found)


def _split_address(raw: str) -> tuple[str, str]:
    raw = (raw or "").strip()
    match = re.search(r"<([^>]+)>", raw)
    if match:
        email = match.group(1).strip()
        name = raw[: match.start()].strip().strip('"')
        return name or email, email
    if "@" in raw:
        return raw, raw
    return raw, ""


def _email_date(value, path: Path) -> datetime:
    if value:
        try:
            parsed = parsedate_to_datetime(str(value))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except (TypeError, ValueError, IndexError):
            pass
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _coerce_date(value, path: Path) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    return _email_date(value, path)


def _decode_text_part(part, payload: bytes) -> str:
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _write_extracted(settings: Settings, raw: RawMessage) -> None:
    if not raw.attachments:
        return
    dest = settings.inbox_extracted / raw.id
    dest.mkdir(parents=True, exist_ok=True)
    for att in raw.attachments:
        (dest / _safe_filename(att.filename)).write_bytes(att.content or b"")


def _safe_filename(name: str) -> str:
    """Windows refuses : * ? " < > | in file names; forwarded-mail subjects often have them."""
    base = re.split(r"[\\/]", name or "")[-1]
    cleaned = re.sub(r'[<>:"|?*\x00-\x1f]', "_", base).strip(" .")
    return cleaned[:150] or "attachment"


def _quarantine(settings: Settings, paths: list[Path], exc: Exception) -> None:
    dest_root = settings.inbox_failed
    dest_root.mkdir(parents=True, exist_ok=True)
    _move_all(dest_root, paths)
    if paths:
        note = dest_root / f"{paths[0].name}.why.txt"
        note.write_text(
            "CloseDesk could not read this file.\n"
            f"Reason: {exc}\n\n"
            "If it is an Outlook message, save it again as .msg or .eml and drop it back in inbox/incoming.\n",
            encoding="utf-8",
        )


def _archive(settings: Settings, paths: list[Path]) -> None:
    day = datetime.now().strftime("%Y-%m-%d")
    dest_root = settings.inbox_processed / day
    dest_root.mkdir(parents=True, exist_ok=True)
    _move_all(dest_root, paths)


def _move_all(dest_root: Path, paths: list[Path]) -> None:
    for path in paths:
        if not path.exists():
            continue
        target = dest_root / path.name
        if target.exists():
            target = dest_root / f"{path.stem}-{sha256_bytes(path.read_bytes())[:8]}{path.suffix}"
        shutil.move(str(path), str(target))
