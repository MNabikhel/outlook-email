"""Read Outlook .msg / .eml files, and loose attachments, from a drop folder."""

from __future__ import annotations

import base64
import hashlib
import logging
import mimetypes
import quopri
import re
import shutil
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from controller_inbox.config import Settings
from controller_inbox.documents import decode_text
from controller_inbox.extract import _rtf_text, explode_archives, html_to_text, redact_financial_secrets, sha256_bytes
from controller_inbox.models import RawAttachment, RawMessage
from controller_inbox.pipeline import attachment_text, process_message
from controller_inbox.store import Store


# extract-msg logs every Outlook property it doesn't know; the file still reads fine.
logging.getLogger("extract_msg").setLevel(logging.ERROR)
log = logging.getLogger(__name__)

MESSAGE_SUFFIXES = {".msg", ".eml"}
# Compared in lower case. Windows writes desktop.ini and Thumbs.db into folders; Office's "~$" lock files are
# skipped by their prefix (``_files``).
SKIP_NAMES = {".gitkeep", ".ds_store", "desktop.ini", "thumbs.db"}


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
    dropped again; only files it brings that aren't stored yet are added), ``failed`` rows, and ``waiting``
    (names of files left for a later run: still being copied in, or waiting for their message).
    A file that cannot be read moves to ``inbox/failed`` with a note, so it is
    not retried on every run.
    """
    settings.ensure_data_dir()
    report = report if report is not None else {}
    report.update({"read": 0, "already_read": 0, "failed": [], "waiting": []})
    records = []
    seen: set[str] = set()
    batches = collect_batches(settings, report["waiting"])
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
                # What the message doesn't say itself (a subject or date taken from its file) is left out of it.
                subject = "" if "subject" in raw.from_file else raw.subject
                sent = "" if "sent" in raw.from_file else raw.received_at.astimezone(timezone.utc).isoformat()
                raw.id = _stable_id("\n".join([raw.internet_message_id, subject, raw.sender_email, sent]))
                existing = store.get_email(raw.id)
            if existing is not None:
                # Another copy of a stored message: only files it doesn't hold yet are read and added.
                raw.attachments = _new_files(settings, raw, existing)
            # A copy that brings no new file is already read, however it was filed: reading it again would put
            # back the tasks the user snoozed, and a copy without a subject or date would rename and redate it.
            if not raw.attachments and (raw.id in seen or existing is not None):
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


def collect_batches(settings: Settings, waiting: list[str] | None = None) -> list[tuple[Path, list[Path]]]:
    """Each message with the files that belong to it, then the loose files. Files left for a later run (an
    attachments folder still waiting for its message) are named in ``waiting``."""
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

    # A file in inbox/attachments/<name>/ whose message isn't here (read on an earlier run, or named otherwise)
    # is read as a loose file, so it still reaches the board. The folder is often copied in before its message
    # is saved, so it is only read on its own once nothing in it has changed for a day; until then it waits.
    orphans: dict[Path, list[Path]] = {}
    attachments_root = settings.inbox_attachments
    for path in loose + attachment_files:
        if path in consumed:
            continue
        if path.parent != settings.inbox_incoming and path.parent != attachments_root:
            try:
                folder = attachments_root / path.relative_to(attachments_root).parts[0]
            except ValueError:
                folder = None
            if folder is not None:
                orphans.setdefault(folder, []).append(path)
                continue
        batches.append((path, []))
    now = time.time()
    for folder, files in orphans.items():
        if now - _newest_mtime(files) < ORPHAN_WAIT_SECONDS:
            if waiting is not None:
                waiting.extend(path.name for path in files)
            continue
        batches.extend((path, []) for path in files)
    return batches


# How long files in inbox/attachments/<name>/ wait for their message before they are read without it.
ORPHAN_WAIT_SECONDS = 24 * 60 * 60


def _newest_mtime(paths: list[Path]) -> float:
    newest = 0.0
    for path in paths:
        try:
            stat = path.stat()
            # A copy keeps its old modified time (Explorer, robocopy): when it arrived is the later of the two.
            newest = max(newest, stat.st_mtime, stat.st_ctime)
        except OSError:
            continue
    return newest


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
    subject = _header(parsed, "subject")
    sender_name, sender_email = _split_address(_address_header(parsed, "from"))
    received = _email_date(parsed.get("date"))
    body, attachments = _eml_content(parsed)
    message_id = _header(parsed, "message-id").strip()
    raw = _raw_message(path, data, subject, sender_name, sender_email, received, body, attachments, message_id)
    raw.reply_to = _reply_address(_address_header(parsed, "reply-to"))
    return raw


def _address_header(message, name: str) -> str:
    """A From or Reply-To header as written, encoded words decoded. The parsed header keeps only the name when it
    looks like an address ("ap@taz.com <ap@evil-pay.net>" reads as ap@taz.com), so ``_split_address`` reads this."""
    for key, raw in message.raw_items():
        if key.lower() == name and not any("\udc80" <= char <= "\udcff" for char in raw):
            try:
                return _tidy(str(make_header(decode_header(raw))))
            except Exception:
                break
    return _header(message, name)


def _header(message, name: str) -> str:
    """A header's text. One sent as raw 8-bit bytes instead of an encoded word (=?utf-8?...?=), as some older mail
    systems do, is read as UTF-8 or Windows-1252 rather than losing its accented letters. One the standard parser
    can't read ("Message-ID: <>" from some scanners makes it raise) is taken as written."""
    try:
        value = str(message.get(name) or "")
    except Exception:
        return next((str(raw) for key, raw in message.raw_items() if key.lower() == name), "")
    if "\ufffd" not in value:
        return value
    for key, raw in message.raw_items():
        if key.lower() == name and any("\udc80" <= char <= "\udcff" for char in raw):
            return _tidy(decode_text(raw.encode("ascii", "surrogateescape")))
    return value


def _eml_content(message, prefix: str = "", depth: int = 0) -> tuple[str, list[RawAttachment]]:
    """A MIME message's body text and files. An email attached to it (``message/rfc822``) is kept
    apart, as the .msg reader keeps one: its text as "<name>.txt", its files as "<name> › file"."""
    body_parts: list[str] = []
    attachments: list[RawAttachment] = []
    html_fallback = ""
    calendar = b""
    ics_attached = False
    # A picture is shown inside the body only when the HTML refers to its Content-ID, as the .msg reader checks.
    html = "\n".join(
        _decode_text_part(part, part.get_payload(decode=True) or b"")
        for part in _eml_leaves(message)
        if part.get_content_type() == "text/html" and not part.get_filename()
    )
    for part in _eml_leaves(message):
        filename = part.get_filename()
        ctype = part.get_content_type()
        if ctype == "message/rfc822":
            attachments.extend(_attached_email(part, filename or "", prefix, depth))
            continue
        if ctype in _SIGNATURE_TYPES:
            # An S/MIME signature (smime.p7s), not a file anyone sent.
            continue
        disposition = (part.get_content_disposition() or "").lower()
        payload = part.get_payload(decode=True) or b""
        cid = str(part.get("content-id") or "").strip().strip("<>")
        if _inline_picture(ctype, filename or "", len(payload), disposition != "attachment" and bool(cid and cid in html)):
            continue
        if filename or disposition == "attachment":
            mail = mail_file_attachments(filename or "", ctype, payload, prefix, depth)
            if mail is not None:
                attachments.extend(mail)
                continue
            attachments.append(
                RawAttachment(
                    id=prefix + (filename or f"part-{len(attachments)+1}"),
                    filename=prefix + (filename or f"attachment-{len(attachments)+1}"),
                    content_type=ctype,
                    size_bytes=len(payload),
                    content=payload,
                )
            )
            ics_attached = ics_attached or (filename or "").lower().endswith(".ics")
            continue
        if ctype == "text/plain":
            body_parts.append(_decode_text_part(part, payload))
        elif ctype == "text/html" and not html_fallback:
            html_fallback = html_to_text(_decode_text_part(part, payload))
        elif ctype == "text/calendar" and not calendar:
            calendar = payload
    if calendar and not ics_attached:
        # An invitation can carry its calendar inline, with no file name; it is kept as the invite file it is.
        attachments.append(_attachment(f"{prefix}invite.ics", "text/calendar", calendar))
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
    encoding = str(part.get("content-transfer-encoding") or "").strip().lower()
    if inner is not None and encoding in ("base64", "quoted-printable"):
        # Some mail programs encode an attached email, which the standard doesn't allow; the parser then reads the
        # encoded text as the email, and its subject, text and files would be lost. It is decoded and read again.
        try:
            encoded = inner.as_bytes()
            decoded = base64.b64decode(encoded) if encoding == "base64" else quopri.decodestring(encoded)
            inner = BytesParser(policy=policy.default).parsebytes(decoded)
        except Exception:
            log.warning("Couldn't decode an attached email", exc_info=True)
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
    """A forwarded email's name for its files: its file name without a .eml/.msg extension, else its subject.
    Names are often the subject itself ("Statement 09/30", "Q3 accruals v2.1 final"), so only a real mail
    extension is cut, and slashes are replaced rather than read as folders."""
    stem = re.sub(r"\.(eml|msg)$", "", _tidy(filename), flags=re.IGNORECASE)
    stem = stem or _tidy(subject) or "forwarded message"
    return re.sub(r"[\\/]", "_", stem).strip()[:80] or "forwarded message"


def _parse_msg(path: Path) -> RawMessage:
    try:
        import extract_msg
    except ImportError as exc:
        raise RuntimeError("Reading .msg files requires the extract-msg package.") from exc

    message = extract_msg.Message(str(path))
    try:
        subject = message.subject or ""
        sender_name, sender_email = _split_address(message.sender or "")
        if "@" not in sender_email:
            sender_email = _msg_smtp_address(message) or ""
            sender_name = sender_name if sender_name and "/o=" not in sender_name.lower() else sender_email
        body = _msg_body(message)
        received = _coerce_date(getattr(message, "date", None))
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


def _msg_body(message) -> str:
    """A .msg's text: its plain body, else its HTML body, else its RTF body (mail written in Outlook's Rich Text
    format may keep only that). A body extract-msg fails to convert is left out, not the whole email."""
    body = _msg_property(message, "body")
    if body:
        return body if isinstance(body, str) else decode_text(body)
    html = _msg_html(message)
    if html:
        return html_to_text(html)
    rtf = _msg_property(message, "rtfBody")
    return _rtf_text(rtf) if isinstance(rtf, (bytes, bytearray)) and rtf else ""


def _msg_property(message, name: str):
    """One of extract-msg's properties, read when asked; None when reading it fails (its RTF converter
    raises on some bodies)."""
    try:
        return getattr(message, name, None)
    except Exception:
        log.warning("Couldn't read the %s of a .msg file", name, exc_info=True)
        return None


def _msg_html(message) -> str:
    """A .msg's HTML body. Outlook saves it in the code page its <meta> tag names, often Windows-1252."""
    html = _msg_property(message, "htmlBody") or b""
    return html if isinstance(html, str) else _decode_html(bytes(html), None)


def _decode_html(data: bytes, charset: str | None) -> str:
    """HTML in the character set its email part declares, else the one its <meta> tag names, else UTF-8 or
    Windows-1252, so "€" and "é" aren't lost."""
    if not charset:
        named = re.search(rb"""charset\s*=\s*["']?([\w.:-]+)""", data[:4096], re.IGNORECASE)
        charset = named.group(1).decode("ascii") if named else None
    return _decode_with(data, charset)


# Character set names Outlook and other mail programs use that Python doesn't know.
_CHARSET_ALIASES = {
    "windows-874": "cp874",
    "iso-8859-8-i": "iso-8859-8",
    "iso-8859-8-e": "iso-8859-8",
    "iso-8859-6-i": "iso-8859-6",
    "iso-8859-6-e": "iso-8859-6",
    "x-sjis": "shift_jis",
    "x-gbk": "gbk",
}


def _decode_with(data: bytes, charset: str | None) -> str:
    """Text in its declared character set. Undeclared, or declared ASCII while it holds other bytes (as many
    mail programs send it), it is read as UTF-8, else Windows-1252."""
    name = (charset or "").strip().lower()
    name = _CHARSET_ALIASES.get(name, name)
    if name in ("", "us-ascii", "ascii", "ansi_x3.4-1968"):
        return decode_text(data)
    try:
        return data.decode(name, errors="replace")
    except LookupError:
        return decode_text(data)


def _msg_attachments(message, prefix: str = "", depth: int = 0) -> list[RawAttachment]:
    """Files on a .msg, and the files inside any email attached to it (named "forwarded › file.pdf")."""
    html = _msg_html(message)
    found: list[RawAttachment] = []
    for att in getattr(message, "attachments", None) or []:
        filename = att.longFilename or att.shortFilename or getattr(att, "displayName", None) or "attachment"
        payload = att.data if att.data is not None else b""
        if isinstance(payload, str):
            payload = payload.encode("utf-8", errors="replace")
        if isinstance(payload, (bytes, bytearray)):
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            mail = mail_file_attachments(filename, content_type, bytes(payload), prefix, depth)
            if mail is not None:
                found.extend(mail)
                continue
            cid = str(getattr(att, "cid", None) or getattr(att, "contentId", None) or "")
            inline = bool(getattr(att, "hidden", False)) or bool(cid and cid.strip("<>") in html)
            if _inline_picture(content_type, filename, len(payload), inline):
                continue
            found.append(_attachment(prefix + filename, content_type, bytes(payload)))
            continue
        # A forwarded email attached as an item: keep its text, then the files it carried.
        stem = _forward_stem(filename, "")
        inner = getattr(payload, "attachments", None) or []
        text = _embedded_message_text(payload, [a.longFilename or a.shortFilename or "" for a in inner])
        found.append(_attachment(f"{prefix}{stem}.txt", "text/plain", text.encode("utf-8")))
        if depth < MAX_NESTING:
            found.extend(_msg_attachments(payload, f"{prefix}{stem} › ", depth + 1))
    return found


_OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_SIGNATURE_TYPES = {"application/pkcs7-signature", "application/x-pkcs7-signature"}


def mail_file_attachments(
    filename: str, content_type: str, data: bytes, prefix: str = "", depth: int = 0
) -> list[RawAttachment] | None:
    """An email attached as a file, read as a forwarded email is: an Outlook .msg dragged into another mail
    program, an .eml attached to a .msg, either one in a zip. Its text comes as "<name>.txt" and its files as
    "<name> › file". A message Outlook signed (S/MIME) keeps its files in one "smime.p7m"; they are read from it.
    None when the file isn't an email CloseDesk can read, so it is kept as it came."""
    lower = (filename or "").lower()
    ctype = (content_type or "").lower()
    if not data or depth >= MAX_NESTING:
        return None
    try:
        if (lower.endswith(".msg") or ctype == "application/vnd.ms-outlook") and data.startswith(_OLE_SIGNATURE):
            import extract_msg

            message = extract_msg.Message(data)
            try:
                stem = _forward_stem(filename, _tidy(_msg_property(message, "subject")))
                inner = getattr(message, "attachments", None) or []
                text = _embedded_message_text(message, [a.longFilename or a.shortFilename or "" for a in inner])
                found = [_attachment(f"{prefix}{stem}.txt", "text/plain", text.encode("utf-8"))]
                found.extend(_msg_attachments(message, f"{prefix}{stem} › ", depth + 1))
                return found
            finally:
                message.close()
        if lower.endswith(".eml") or ctype == "message/rfc822":
            parsed = BytesParser(policy=policy.default).parsebytes(data)
            if any(_header(parsed, name) for name in ("from", "subject", "date", "message-id")):
                return forwarded_attachments(parsed, filename, prefix, depth)
        if lower == "smime.p7m" or ctype == "multipart/signed":
            parsed = BytesParser(policy=policy.default).parsebytes(data)
            if parsed.get_content_type() == "multipart/signed":
                _body, files = _eml_content(parsed, prefix, depth)
                return [att for att in files if att.content_type not in _SIGNATURE_TYPES]
    except Exception:
        log.warning("Couldn't read the email %s attached as a file", filename, exc_info=True)
    return None


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
    return _forward_text(subject, sender, date, filenames or [], _msg_body(item))


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
    """A message without a subject is named after its file, and one without a sent time (``received`` None) is
    dated by its file; ``from_file`` says which, as two saved copies of one message differ in those."""
    from_file = tuple(part for part, missing in (("subject", not _tidy(subject)), ("sent", received is None)) if missing)
    subject = _tidy(subject) or _tidy(path.stem)
    if received is None:
        received = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
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
        from_file=from_file,
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
    """Whether a stored email that has this message's Message-ID is a different message after all.

    Only what the message itself says counts: a message without a subject is named after its file and one
    without a date is dated by its file, and those differ between two copies of it saved at different times."""
    # The stored subject has its account numbers masked, as this one's will be.
    subject = redact_financial_secrets(raw.subject)
    if "subject" not in raw.from_file and _tidy(stored.subject).casefold() != _tidy(subject).casefold():
        return True
    if (stored.sender_email or "").strip().lower() != (raw.sender_email or "").strip().lower():
        return True
    if "sent" in raw.from_file:
        return False
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
        if path.is_file() and path.name.lower() not in SKIP_NAMES and not path.name.startswith((".", "~$")):
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
            # Several addresses ("a@evil.com, b@taz.com"): the first, so a trusted one listed after it isn't taken.
            first = re.search(r"[^\s,;<>\"]+@[^\s,;<>\"]+", raw)
            return (raw, first.group(0)) if first else (raw, "")
        email = brackets[-1].group(1).strip()
        name = raw[: brackets[-1].start()].strip().strip('"')
    return name.strip() or email, email


def _email_date(value) -> datetime | None:
    """A Date header's time, or None when there is none that can be read."""
    if value:
        try:
            parsed = parsedate_to_datetime(str(value))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            # A date such as 31 Dec 9999 west of UTC has no UTC time; it is no date, not a reason to set the email aside.
            parsed.astimezone(timezone.utc)
            return parsed
        except (TypeError, ValueError, IndexError, OverflowError):
            pass
    return None


def _coerce_date(value) -> datetime | None:
    if isinstance(value, datetime):
        value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        try:
            value.astimezone(timezone.utc)
        except OverflowError:
            return None
        return value
    return _email_date(value)


def _decode_text_part(part, payload: bytes) -> str:
    if part.get_content_type() == "text/html":
        return _decode_html(payload, part.get_content_charset())
    return _decode_with(payload, part.get_content_charset())


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
        landed = _move_all(dest_root, paths)
        if paths:
            # Named after where the file landed: a second file of the same name gets a new name, and its own note.
            note = dest_root / f"{(landed[0] or paths[0]).name}.why.txt"
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
