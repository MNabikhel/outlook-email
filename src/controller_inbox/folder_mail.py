"""Read Outlook .msg / .eml files, and loose attachments, from a drop folder."""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import re
import shutil
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from controller_inbox.config import Settings
from controller_inbox.extract import explode_archives, html_to_text, sha256_bytes
from controller_inbox.models import RawAttachment, RawMessage
from controller_inbox.pipeline import KEEP_READINGS, attachment_text, process_message
from controller_inbox.store import Store


# extract-msg logs every Outlook property it doesn't know; the file still reads fine.
logging.getLogger("extract_msg").setLevel(logging.ERROR)
log = logging.getLogger(__name__)

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
    report.update({"read": 0, "already_read": 0, "failed": [], "waiting": []})
    records = []
    seen: set[str] = set()
    batches = collect_batches(settings)
    busy = _still_copying([file for path, sidecars in batches for file in (path, *sidecars)])
    sample_checked = False
    for index, (path, sidecars) in enumerate(batches, start=1):
        if on_progress:
            on_progress(index, len(batches), path.name)
        owned = [path, *sidecars] if path.suffix.lower() in MESSAGE_SUFFIXES else [path]
        if any(file in busy for file in owned):
            # Still being copied in: leave it for the next run instead of reading half a file.
            report["waiting"].append(path.name)
            continue
        cleared = 0
        try:
            raw = _read_batch(path, sidecars)
            existing = store.get_email(raw.id)
            if existing is not None and raw.internet_message_id and _another_message(existing, raw):
                # Some scanners and mail tools give every message the same Message-ID. A stored email with
                # another subject, sender or date is a different message, so this one gets an id from those
                # too, which a copy of it saved again later shares.
                sent = raw.received_at.astimezone(timezone.utc).isoformat()
                raw.id = _stable_id("\n".join([raw.internet_message_id, raw.subject, raw.sender_email, sent]))
                existing = store.get_email(raw.id)
            if existing is not None:
                # Another copy of a stored message: only files it doesn't hold yet are read and added.
                raw.attachments = _new_files(settings, raw, existing)
            if not raw.attachments and (raw.id in seen or (existing is not None and existing.model_status in KEEP_READINGS)):
                report["already_read"] += 1
                archived = _archive(settings, owned)
                if existing is not None and not existing.source_path and archived and archived[0]:
                    store.set_source_path(raw.id, str(archived[0]))
                continue
            if raw.id in seen:
                records = [item for item in records if item.id != raw.id]
            seen.add(raw.id)
            if not sample_checked:
                # Only once a real message has parsed, so a bad file cannot empty the board.
                sample_checked = True
                if not store.real_mail_count():
                    cleared = store.clear_sample()
                    report["sample_cleared"] = cleared
            # The files are saved first: an email is only stored once everything that came with it is.
            _write_extracted(settings, raw)
            record = process_message(raw, store, settings, now=now)
            archived = _archive(settings, owned)
            if archived and archived[0]:
                store.set_source_path(record.id, str(archived[0]))
                record.source_path = str(archived[0])
            records.append(record)
            if existing is None:
                report["read"] += 1
            else:
                report["already_read"] += 1
        except PermissionError:
            # Windows: another program (Outlook, Explorer's copy) still holds the file open.
            log.info("%s is in use; it will be read on the next run", path)
            report["waiting"].append(path.name)
            if cleared:
                sample_checked = _restore_sample(store, settings, report, now)
        except Exception as exc:
            report["failed"].append({"file": path.name, "error": str(exc)[:300]})
            _quarantine(settings, owned, exc)
            if cleared:
                sample_checked = _restore_sample(store, settings, report, now)
    if records:
        stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        # Not ``last_sync_at``: that is the Graph cursor, and a folder import must not move it.
        store.set_state("last_folder_ingest", stamp)
    return records


def _restore_sample(store: Store, settings: Settings, report: dict, now: datetime | None) -> bool:
    """The first real message failed after the sample was cleared: put the sample back, so a bad
    file never leaves an empty board. Returns whether the sample check is still done."""
    if store.real_mail_count():
        return True
    from controller_inbox.pipeline import ingest_demo

    try:
        ingest_demo(store, settings, now=now)
    except Exception:
        log.warning("Couldn't restore the sample mailbox", exc_info=True)
    report.pop("sample_cleared", None)
    return False


def _read_batch(path: Path, sidecars: list[Path]) -> RawMessage:
    """One message (with its sidecar files) or one loose file, with attachment names made unique."""
    if path.suffix.lower() in MESSAGE_SUFFIXES:
        raw = _parse_message(path)
        for extra in sidecars:
            raw.attachments.append(_file_attachment(extra))
            raw.has_attachments = True
    else:
        raw = _standalone(path)
    raw.attachments = unique_attachments(raw.attachments)
    return raw


SETTLE_SECONDS = 2.0
SETTLE_PROBE_SECONDS = 0.25


def _still_copying(paths: list[Path]) -> set[Path]:
    """Files written in the last couple of seconds that are still growing or still locked.

    One short pause covers every fresh file at once; a file that has finished copying is read now.
    """
    fresh: dict[Path, tuple[int, int]] = {}
    now = time.time()
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        if now - stat.st_mtime < SETTLE_SECONDS:
            fresh[path] = (stat.st_size, stat.st_mtime_ns)
    if not fresh:
        return set()
    time.sleep(SETTLE_PROBE_SECONDS)
    busy: set[Path] = set()
    for path, before in fresh.items():
        try:
            stat = path.stat()
            with path.open("rb"):
                pass
        except OSError:
            busy.add(path)
            continue
        if (stat.st_size, stat.st_mtime_ns) != before:
            busy.add(path)
    return busy


def collect_messages(settings: Settings) -> list[tuple[RawMessage, list[Path]]]:
    """Backwards-compatible helper used by tests that inspect parsed messages."""
    parsed = []
    for path, sidecars in collect_batches(settings):
        owned = [path, *sidecars] if path.suffix.lower() in MESSAGE_SUFFIXES else [path]
        parsed.append((_read_batch(path, sidecars), owned))
    return parsed


def collect_batches(settings: Settings) -> list[tuple[Path, list[Path]]]:
    incoming = [p for p in _files(settings.inbox_incoming)]
    attachment_files = [p for p in _files(settings.inbox_attachments)]
    messages = [p for p in incoming if p.suffix.lower() in MESSAGE_SUFFIXES]
    loose = [p for p in incoming if p.suffix.lower() not in MESSAGE_SUFFIXES]
    consumed: set[Path] = set()
    batches: list[tuple[Path, list[Path]]] = []

    roots = {settings.inbox_incoming.resolve(), settings.inbox_attachments.resolve()}
    for path in messages:
        sidecars = _sidecars_for(path, loose + attachment_files, consumed, roots)
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


def _sidecars_for(
    message_path: Path, candidates: list[Path], consumed: set[Path], roots: set[Path] = frozenset()
) -> list[Path]:
    """Files that belong to a message: "<stem>.pdf" beside it, or anything in a folder named "<stem>".
    The drop folders themselves never count as such a folder, so "incoming.eml" doesn't take every loose file."""
    stem = message_path.stem.casefold()
    matched: list[Path] = []
    for path in candidates:
        if path in consumed or path.resolve() == message_path.resolve():
            continue
        parent = path.parent.name.casefold()
        sibling = path.parent.resolve() == message_path.parent.resolve() and path.stem.casefold() == stem
        folder = parent == stem and path.parent.resolve() not in roots
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
    body, attachments = _eml_content(parsed)
    message_id = str(parsed.get("message-id") or "").strip()
    raw = _raw_message(path, data, subject, sender_name, sender_email, received, body, attachments, message_id)
    raw.reply_to = _reply_address(str(parsed.get("reply-to") or ""))
    return raw


def _eml_content(message, prefix: str = "", depth: int = 0) -> tuple[str, list[RawAttachment]]:
    """A MIME message's body text and files. An email attached to it (``message/rfc822``) is kept
    apart, as the .msg reader keeps one: its text as "<name>.txt", its files as "<name> › file"."""
    body_parts: list[str] = []
    attachments: list[RawAttachment] = []
    html_fallback = ""
    for part in _eml_leaves(message):
        filename = part.get_filename()
        ctype = part.get_content_type()
        if ctype == "message/rfc822":
            attachments.extend(_attached_email(part, filename or "", prefix, depth))
            continue
        disposition = (part.get_content_disposition() or "").lower()
        payload = part.get_payload(decode=True) or b""
        if _inline_picture(ctype, filename or "", len(payload), disposition != "attachment" and bool(part.get("content-id"))):
            continue
        if filename or disposition == "attachment":
            attachments.append(
                RawAttachment(
                    id=prefix + (filename or f"part-{len(attachments)+1}"),
                    filename=prefix + (filename or f"attachment-{len(attachments)+1}"),
                    content_type=ctype,
                    size_bytes=len(payload),
                    content=payload,
                )
            )
            continue
        if ctype == "text/plain":
            body_parts.append(_decode_text_part(part, payload))
        elif ctype == "text/html" and not html_fallback:
            html_fallback = html_to_text(_decode_text_part(part, payload))
    body = "\n".join(p for p in body_parts if p).strip() or html_fallback
    return body, attachments


def _eml_leaves(part):
    """The parts that hold content, without descending into an attached email."""
    if part.get_content_type() == "message/rfc822":
        yield part
    elif part.is_multipart():
        for sub in part.get_payload() or []:
            yield from _eml_leaves(sub)
    else:
        yield part


def _attached_email(part, filename: str, prefix: str, depth: int) -> list[RawAttachment]:
    payload = part.get_payload()
    inner = payload[0] if isinstance(payload, list) and payload else None
    if inner is None:
        data = part.as_bytes() if hasattr(part, "as_bytes") else b""
        return [_attachment(prefix + (filename or "forwarded message.eml"), "message/rfc822", data)]
    return forwarded_attachments(inner, filename, prefix, depth)


def forwarded_attachments(inner, filename: str = "", prefix: str = "", depth: int = 0) -> list[RawAttachment]:
    """A forwarded email (a parsed MIME message) as attachments: its text, then the files it carried.

    Used for .eml files and for Graph item attachments, so both read like a forwarded .msg.
    """
    subject = _tidy(str(inner.get("subject") or ""))
    stem = _forward_stem(filename, subject)
    body, files = _eml_content(inner, f"{prefix}{stem} › ", depth + 1)
    text = _forward_text(
        subject,
        _tidy(str(inner.get("from") or "")),
        _tidy(str(inner.get("date") or "")),
        [att.filename.rsplit(" › ", 1)[-1] for att in files],
        body,
    )
    found = [_attachment(f"{prefix}{stem}.txt", "text/plain", text.encode("utf-8"))]
    if depth < MAX_NESTING:
        found.extend(files)
    return found


def _forward_stem(filename: str, subject: str) -> str:
    stem = Path(filename).stem if filename else ""
    stem = stem or subject or "forwarded message"
    return re.sub(r"[\\/]", "_", stem).strip()[:80] or "forwarded message"


def _parse_msg(path: Path) -> RawMessage:
    try:
        import extract_msg
    except ImportError as exc:
        raise RuntimeError("Reading .msg files requires the extract-msg package.") from exc

    message = extract_msg.Message(str(path))
    try:
        subject = message.subject or path.stem
        sender_name, sender_email = _split_address(message.sender or "")
        if "@" not in sender_email:
            sender_email = _msg_smtp_address(message) or ""
            sender_name = sender_name if sender_name and "/o=" not in sender_name.lower() else sender_email
        body = message.body or ""
        html_body = getattr(message, "htmlBody", None)
        if not body and html_body:
            if isinstance(html_body, bytes):
                html_body = html_body.decode("utf-8", errors="replace")
            body = html_to_text(html_body)
        received = _coerce_date(getattr(message, "date", None), path)
        attachments = _msg_attachments(message)
        header = getattr(message, "header", None)
        reply_to = _reply_address(str(header.get("Reply-To") or "")) if header is not None else ""
        message_id = str(getattr(message, "messageId", None) or "").strip()
    finally:
        message.close()
    data = path.read_bytes()
    raw = _raw_message(path, data, subject, sender_name, sender_email, received, body, attachments, message_id)
    raw.reply_to = reply_to
    return raw


MAX_NESTING = 3


def _msg_attachments(message, prefix: str = "", depth: int = 0) -> list[RawAttachment]:
    """Files on a .msg, and the files inside any email attached to it (named "forwarded › file.pdf")."""
    html = getattr(message, "htmlBody", None) or b""
    html = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else str(html)
    found: list[RawAttachment] = []
    for att in getattr(message, "attachments", None) or []:
        filename = att.longFilename or att.shortFilename or getattr(att, "displayName", None) or "attachment"
        payload = att.data if att.data is not None else b""
        if isinstance(payload, str):
            payload = payload.encode("utf-8", errors="replace")
        if isinstance(payload, (bytes, bytearray)):
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            cid = str(getattr(att, "cid", None) or getattr(att, "contentId", None) or "")
            inline = bool(getattr(att, "hidden", False)) or bool(cid and cid.strip("<>") in html)
            if _inline_picture(content_type, filename, len(payload), inline):
                continue
            found.append(_attachment(prefix + filename, content_type, bytes(payload)))
            continue
        # A forwarded email attached as an item: keep its text, then the files it carried.
        stem = Path(filename).stem or "forwarded message"
        inner = getattr(payload, "attachments", None) or []
        text = _embedded_message_text(payload, [a.longFilename or a.shortFilename or "" for a in inner])
        found.append(_attachment(f"{prefix}{stem}.txt", "text/plain", text.encode("utf-8")))
        if depth < MAX_NESTING:
            found.extend(_msg_attachments(payload, f"{prefix}{stem} › ", depth + 1))
    return found


def _attachment(filename: str, content_type: str, payload: bytes) -> RawAttachment:
    return RawAttachment(id=filename, filename=filename, content_type=content_type, size_bytes=len(payload), content=payload)


def _inline_picture(content_type: str, filename: str, size: int, inline: bool) -> bool:
    """Signature logos and pasted pictures shown inside the body, not files anyone sent."""
    picture = content_type.startswith("image/") or filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp"))
    return picture and inline and size < 400_000


def _msg_smtp_address(message) -> str:
    """Exchange stores internal senders as /O=EXCHANGELABS/…; the SMTP address is kept in other fields."""
    read = getattr(message, "getStringStream", None)
    if read is None:
        return ""
    for stream in ("__substg1.0_5D01", "__substg1.0_5D02", "__substg1.0_0C1F", "__substg1.0_0065", "__substg1.0_39FE"):
        try:
            value = read(stream)
        except Exception:
            continue
        if value and "@" in str(value):
            return _tidy(value)
    header = getattr(message, "header", None)
    if header is not None:
        _name, address = _split_address(str(header.get("From") or ""))
        if "@" in address:
            return address
    return ""


def _reply_address(raw: str) -> str:
    _name, address = _split_address(raw)
    return _tidy(address).lower() if "@" in address else ""


def _embedded_message_text(item, filenames: list[str] | None = None) -> str:
    subject = getattr(item, "subject", "") or ""
    sender = getattr(item, "sender", "") or ""
    date = getattr(item, "date", "") or ""
    header = getattr(item, "header", None)
    if not date and header is not None:
        date = _tidy(str(header.get("Date") or ""))
    body = getattr(item, "body", "") or ""
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    return _forward_text(subject, sender, date, filenames or [], body)


def _forward_text(subject, sender, date, filenames: list[str], body: str) -> str:
    files = [name for name in filenames if name]
    attached = f"\nAttachments: {', '.join(files)}" if files else ""
    return f"Forwarded message\nSubject: {subject}\nFrom: {sender}\nDate: {date}{attached}\n\n{body}".strip()


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
    subject = _tidy(subject) or _tidy(path.stem)
    sender_name, sender_email = _tidy(sender_name), _tidy(sender_email)
    body = (body or "").replace("\x00", "").strip()
    message_id = _tidy(message_id)
    return RawMessage(
        id=_stable_id(message_id) if message_id else _message_id(data),
        internet_message_id=message_id,
        subject=subject,
        sender_name=sender_name or ("" if sender_email else "Unknown sender"),
        sender_email=sender_email,
        received_at=received,
        body_text=body,
        body_preview=(body or subject)[:240],
        has_attachments=bool(attachments),
        source="folder",
        attachments=attachments,
    )


def _tidy(value) -> str:
    """Outlook pads some .msg strings with NUL characters; drop them and fold whitespace."""
    return re.sub(r"\s+", " ", str(value or "").replace("\x00", "")).strip()


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


# A .msg and an .eml of one message can disagree on the sent time by a few seconds.
SAME_MESSAGE_SLACK = timedelta(minutes=2)


def _another_message(stored, raw: RawMessage) -> bool:
    """Whether a stored email that has this message's Message-ID is a different message after all."""
    if _tidy(stored.subject).casefold() != _tidy(raw.subject).casefold():
        return True
    if (stored.sender_email or "").strip().lower() != (raw.sender_email or "").strip().lower():
        return True
    try:
        sent = datetime.fromisoformat(str(stored.received_at))
    except ValueError:
        return False
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=timezone.utc)
    return abs(sent - raw.received_at) > SAME_MESSAGE_SLACK


def _files(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    found = []
    for path in folder.rglob("*"):
        if path.is_file() and path.name.lower() not in SKIP_NAMES and not path.name.startswith("."):
            found.append(path)
    return sorted(found)


def _split_address(raw: str) -> tuple[str, str]:
    """(name, address) from a From or Reply-To header. The address is the one after any quoted name, as mail
    programs read it: a name can itself look like an address ("Acme Billing <billing@acme.com>" <x@acme-pay.net>),
    and taking that one would pass a lookalike sender off as a trusted one."""
    raw = (raw or "").strip()
    name, email = parseaddr(raw)
    if "@" not in email:
        # A header the strict parser gives up on ("Chen, Maya <maya@x.com>"): the last <...> is the address.
        brackets = list(re.finditer(r"<([^<>]+)>", raw))
        if not brackets:
            return (raw, raw) if "@" in raw else (raw, "")
        email = brackets[-1].group(1).strip()
        name = raw[: brackets[-1].start()].strip().strip('"')
    return name.strip() or email, email


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


def _new_files(settings: Settings, raw: RawMessage, existing) -> list[RawAttachment]:
    """The files in this copy of a stored message that aren't stored with it yet (the same bytes under any
    name are), renamed when a kept file under inbox/extracted already has the name, so none replaces it."""
    hashes = {att.sha256 for att in existing.attachments if att.sha256}
    names = {att.filename for att in existing.attachments}
    fresh = []
    for att in raw.attachments:
        parts = explode_archives([att])
        if all(sha256_bytes(part.content) in hashes if part.content else part.filename in names for part in parts):
            continue
        att.id = att.filename
        fresh.append(att)
    taken = {safe_filename(name).casefold() for name in names}
    folder = settings.inbox_extracted / raw.id
    if folder.is_dir():
        taken |= {path.name.casefold() for path in folder.iterdir()}
    return unique_attachments(fresh, taken=taken)


def _write_extracted(settings: Settings, raw: RawMessage) -> None:
    if not raw.attachments:
        return
    dest = settings.inbox_extracted / raw.id
    dest.mkdir(parents=True, exist_ok=True)
    for att in raw.attachments:
        (dest / safe_filename(att.filename)).write_bytes(att.content or b"")


READER_KEY = "attachment_reader"
_REREAD = {".pdf", ".docx", ".pptx", ".xlsx", ".xlsm", ".xls", ".csv", ".tsv"}
_MAX_REREAD_BYTES = 40_000_000


def reread_attachments(store: Store, settings: Settings, *, on_progress: Callable[[int, int, str], None] | None = None) -> int:
    """Once per new version of the attachment reader (``documents.READER_VERSION``), read the kept
    originals again so files already in the inbox get the better text too. Scanned PDFs keep their
    OCR text, and classifications stay as they are. Returns how many files changed."""
    from controller_inbox.documents import READER_VERSION

    if store.get_state(READER_KEY) == READER_VERSION:
        return 0
    todo = []
    for attachment_id, email_id, filename, sha, text in store.attachment_files():
        if Path(filename).suffix.lower() not in _REREAD or "read with OCR" in text[:400] or text.startswith("[This PDF looks scanned"):
            continue
        if sha and (settings.inbox_extracted / email_id).is_dir():
            todo.append((attachment_id, settings.inbox_extracted / email_id, filename, sha, text))
    changed = 0
    zipped: tuple[Path | None, dict[str, bytes]] = (None, {})
    for index, (attachment_id, folder, filename, sha, old) in enumerate(todo, start=1):
        if on_progress:
            on_progress(index, len(todo), filename)
        try:
            # Only the attachment's own bytes: a file that came in a zip is read from the zip, never from
            # another attachment that has its name ("invoice.pdf" beside "older.zip" holding an "invoice.pdf").
            data = _kept_file(folder / safe_filename(filename), sha)
            if data is None:
                if zipped[0] != folder:
                    zipped = (folder, _zipped_files(folder))
                data = zipped[1].get(sha)
            if data is None:
                continue
            new = attachment_text(filename, "", data)
        except Exception:
            log.warning("Couldn't read %s again", folder / filename, exc_info=True)
            continue
        if new.strip() and new != old:
            store.set_attachment_text(attachment_id, new)
            changed += 1
    store.set_state(READER_KEY, READER_VERSION)
    return changed


def _kept_file(path: Path, sha: str) -> bytes | None:
    """The file kept under inbox/extracted, when it holds these bytes."""
    if not path.is_file() or path.stat().st_size > _MAX_REREAD_BYTES:
        return None
    data = path.read_bytes()
    return data if sha256_bytes(data) == sha else None


def _zipped_files(folder: Path) -> dict[str, bytes]:
    """The files inside the zips kept for one email, by SHA-256, as the email's attachments were stored."""
    found: dict[str, bytes] = {}
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() != ".zip" or not path.is_file() or path.stat().st_size > _MAX_REREAD_BYTES:
            continue
        for part in explode_archives([_attachment(path.name, "application/zip", path.read_bytes())]):
            if part.content:
                found.setdefault(sha256_bytes(part.content), part.content)
    return found


MAX_NAME_CHARS = 150
# Most file systems take 255 bytes in a file name; a Japanese or Cyrillic name reaches that well before 150 characters.
MAX_NAME_BYTES = 255


def safe_filename(name: str) -> str:
    """Windows refuses : * ? " < > | in file names; forwarded-mail subjects often have them.

    Names are cut to 150 characters, and a name still over 255 bytes in UTF-8 is cut to fit, keeping its extension.
    """
    base = re.split(r"[\\/]", name or "")[-1]
    cleaned = re.sub(r'[<>:"|?*\x00-\x1f]', "_", base).strip(" .")
    short = cleaned[:MAX_NAME_CHARS]
    if len(short.encode("utf-8")) > MAX_NAME_BYTES:
        stem, suffix = _split_suffix(cleaned)
        short = _shorten(stem, MAX_NAME_CHARS - len(suffix), MAX_NAME_BYTES - len(suffix.encode("utf-8"))).rstrip(" .") + suffix
    return short or "attachment"


def _split_suffix(name: str) -> tuple[str, str]:
    """("invoice", ".pdf") from "invoice.pdf"; a name without a short extension has none."""
    dot = name.rfind(".")
    if 0 < dot and len(name) - dot <= 10 and " " not in name[dot:]:
        return name[:dot], name[dot:]
    return name, ""


def _shorten(text: str, chars: int, size: int) -> str:
    """``text`` cut to ``chars`` characters and ``size`` bytes of UTF-8, never inside a character."""
    return text[: max(0, chars)].encode("utf-8")[: max(0, size)].decode("utf-8", errors="ignore")


def unique_attachments(attachments: list[RawAttachment], *, taken: set[str] = frozenset()) -> list[RawAttachment]:
    """Give a repeated file name a number ("invoice.pdf", "invoice (2).pdf"), in order.

    Two attachments with one name would share an attachment id and a file under inbox/extracted.
    Names are compared as saved on disk (``safe_filename``, any case), and a name that is not
    repeated keeps its name and id, so files already stored still match. ``taken`` are names
    (as saved on disk, casefolded) that are already used.
    """
    taken = set(taken)
    for att in attachments:
        key = safe_filename(att.filename).casefold()
        if key in taken:
            base, suffix = _split_suffix(att.filename)
            number = 2
            while True:
                tail = f" ({number}){suffix}"
                # Cut to fit as ``safe_filename`` would, so the number is never cut off.
                candidate = _shorten(base, MAX_NAME_CHARS - len(tail), MAX_NAME_BYTES - len(tail.encode("utf-8"))) + tail
                if safe_filename(candidate).casefold() not in taken:
                    break
                number += 1
            if att.id == att.filename:
                att.id = candidate
            att.filename = candidate
            key = safe_filename(candidate).casefold()
        taken.add(key)
    return attachments


def _quarantine(settings: Settings, paths: list[Path], exc: Exception) -> None:
    """Move an unreadable file to inbox/failed with a note. A file that can't be moved (locked) stays put."""
    dest_root = settings.inbox_failed
    try:
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
    except OSError:
        log.warning("Couldn't move %s to the failed folder", paths[0] if paths else dest_root, exc_info=True)


def _archive(settings: Settings, paths: list[Path]) -> list[Path | None]:
    day = datetime.now(settings.tz).strftime("%Y-%m-%d")
    dest_root = settings.inbox_processed / day
    dest_root.mkdir(parents=True, exist_ok=True)
    return _move_all(dest_root, paths)


def _move_all(dest_root: Path, paths: list[Path]) -> list[Path | None]:
    """Move files into ``dest_root``; returns where each one landed, in order (None when it could not move)."""
    moved: list[Path | None] = []
    for path in paths:
        if not path.exists():
            moved.append(None)
            continue
        target = dest_root / path.name
        try:
            if target.exists():
                target = dest_root / f"{path.stem}-{sha256_bytes(path.read_bytes())[:8]}{path.suffix}"
            shutil.move(str(path), str(target))
        except OSError:
            log.warning("Couldn't move %s to %s", path, dest_root, exc_info=True)
            moved.append(None)
            continue
        moved.append(target.resolve())
    return moved
