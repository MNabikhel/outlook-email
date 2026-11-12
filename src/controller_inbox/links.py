"""Which emails belong together: the same conversation, the same invoice or PO, the same file, a file's versions.

Every email is filed under a few keys when it is stored (``keys``, kept in the ``email_keys`` table):

- ``thread``: its own Message-ID, the ones it replies to (In-Reply-To, References), Outlook's conversation (from
  the Thread-Index) and Microsoft 365's conversation id. Two emails sharing any of them are in one conversation;
  the subject alone never is (newsletters and notices repeat theirs).
- ``invoice`` / ``po``: the invoice and PO numbers it or its files name. A number of digits only is filed under
  the sender's domain too, so two vendors' "10482" are not one invoice. A workbook or CSV that lists several
  invoices names none of its own (an aging lists every vendor's).
- ``file``: each attached file's contents (its SHA-256), past inline pictures, tiny files and generic names
  ("image001.png", "ATT00001.htm"): a logo or signature on every email would link them all.
- ``stem``: each file's name without its version marks ("Budget v2 FINAL (1).xlsx" is "budget" + ".xlsx"), so a
  newer version of a file sent before is found.

Related emails are worked out when an email is opened (``related``), from those keys: nothing is stored per pair,
so a fifty-email thread costs fifty rows, not 1,225. A key that many emails share (more than ``COMMON``) says
nothing about any two of them and is left out.

Mail held as possible payment fraud is shown in its conversation with its warning, and its files are not linked:
a reply slipped into a real thread is how payment fraud usually looks, so it must never borrow the thread's trust.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Iterable
from datetime import datetime
from email.utils import getaddresses

from controller_inbox.fraud import attachments_locked
from controller_inbox.models import AttachmentRecord, EmailRecord

KEYS_VERSION = "1"  # changed whenever keys() files emails differently, so stored mail is filed again
COMMON = 25  # a key shared by more emails than this links none of them
MAX_THREAD = 60  # emails shown in one conversation (the newest kept)
MAX_PER_REASON = 12
MIN_FILE_BYTES = 1024

_MESSAGE_ID = re.compile(r"<([^<>\s]{3,250})>")
_GENERIC_STEM = re.compile(
    r"(?i)^(image|img|att|attachment|untitled|document|doc|scan|scanned|file|picture|photo|outlook|logo|"
    r"signature|invoice|statement|noname|unnamed|winmail|smime|part|text|mail|message|copy|download)[\s_-]*\d*$"
)
_PICTURE = re.compile(r"(?i)\.(png|jpe?g|gif|bmp|emz|wmz|ico|svg)$")
# Version marks in a file's name: "v2", "rev 3", "final", "draft", "(1)", "- Copy", dates, "updated", "revised".
_VERSION_MARKS = re.compile(
    r"(?i)(\s*[-_ ]\s*copy(\s*\(\d+\))?|\s*\(\d+\)|[\s_-]*\bv(er(sion)?)?\s*\.?\s*\d+(\.\d+)*\b|[\s_-]*\brev(ision)?\s*\.?\s*\d+\b|"
    r"[\s_-]*\b(final|draft|updated|revised|latest|clean|redline|signed|new|old)\b|"
    r"[\s_-]*\b\d{1,2}[-./]\d{1,2}[-./]\d{2,4}\b|[\s_-]*\b(19|20)\d{2}[-_.]?\d{2}[-_.]?\d{2}\b)"
)


# --- What a message's headers say -------------------------------------------------------------------------------


def message_ids(value: str | None) -> list[str]:
    """The Message-IDs in a References or In-Reply-To header, as ``norm_id`` writes them, oldest first."""
    found = [norm_id(match) for match in _MESSAGE_ID.findall(value or "")]
    if not found and value and "@" in value and " " not in value.strip():
        found = [norm_id(value)]
    return list(dict.fromkeys(found))


def norm_id(value: str | None) -> str:
    return (value or "").strip().strip("<>").strip().lower()


def conversation_of(index: bytes | str | None) -> str:
    """Outlook's conversation from a Thread-Index (base64 in a header, bytes in a .msg): bytes 6 to 22 are the
    conversation's GUID, the same on every message of it. "" when there is none to read."""
    if isinstance(index, str):
        try:
            index = base64.b64decode(index.strip(), validate=False)
        except (binascii.Error, ValueError):
            return ""
    if not isinstance(index, (bytes, bytearray)) or len(index) < 22:
        return ""
    guid = bytes(index[6:22])
    return guid.hex() if any(guid) else ""


def addresses(*values: str | None) -> list[str]:
    """The email addresses in To / Cc headers, lowercased, each once."""
    found = [address.strip().lower() for _name, address in getaddresses([value for value in values if value])]
    return list(dict.fromkeys(address for address in found if "@" in address))


def threading(*, in_reply_to: str = "", references: str = "", thread_index: bytes | str | None = None, to: Iterable[str] = ()) -> dict:
    """What ``EmailRecord.threading`` holds, with the empty parts left out."""
    out: dict = {}
    if parents := message_ids(in_reply_to):
        out["in_reply_to"] = parents[-1]
    if refs := message_ids(references):
        out["references"] = refs[-20:]
    if conversation := conversation_of(thread_index):
        out["conversation"] = conversation
    if recipients := list(dict.fromkeys(to)):
        out["to"] = recipients[:50]
    return out


# --- Keys ----------------------------------------------------------------------------------------------------------


def keys(email: EmailRecord) -> list[tuple[str, str]]:
    """The keys ``email`` is filed under (see the module's docstring)."""
    found: set[tuple[str, str]] = set()
    info = email.threading or {}
    for mid in (email.internet_message_id, info.get("in_reply_to"), *(info.get("references") or [])):
        if norm := norm_id(mid):
            found.add(("thread", f"mid:{norm}"))
    if info.get("conversation"):
        found.add(("thread", f"conv:{info['conversation']}"))
    if email.conversation_id:
        found.add(("thread", f"graph:{email.conversation_id}"))
    domain = sender_domain(email)
    for kind, numbers in (("invoice", _numbers(email, "invoice_numbers")), ("po", _numbers(email, "po_numbers"))):
        for number in numbers:
            found.add((kind, f"{domain}|{number}" if number.isdigit() else number))
    # A held email's files link to nothing, either way: a "new bank details" form sent from a lookalike domain must
    # not show up as the same file the real vendor sent.
    for att in [] if attachments_locked(email) else email.attachments:
        if not _real_file(att):
            continue
        if att.sha256:
            found.add(("file", att.sha256))
        if stem := file_stem(att.filename):
            found.add(("stem", stem))
    return sorted(found)


def sender_domain(email: EmailRecord) -> str:
    return (email.sender_email or "").rpartition("@")[2].lower()


def number_key(text: str | None) -> str:
    """An invoice or PO number as it is matched: letters and digits only, upper case, leading zeros of its digits
    dropped ("INV-0010482" and "inv 10482" are one number)."""
    plain = re.sub(r"[^0-9A-Za-z]", "", text or "").upper()
    return re.sub(r"(?<![0-9])0+(?=\d)", "", plain)


def _numbers(email: EmailRecord, name: str) -> set[str]:
    found = {number_key(n) for n in getattr(email.extracted, name, []) or []}
    for att in email.attachments:
        listed = getattr(att.extracted_fields, name, []) or []
        # A workbook or CSV that lists several numbers is a list (an aging, a remittance), not one document of them.
        if len(listed) > 1 and re.search(r"(?i)\.(xlsx|xlsm|xls|csv|tsv)$", att.filename or ""):
            continue
        found |= {number_key(n) for n in listed}
    return {n for n in found if len(n) >= 4 and any(ch.isdigit() for ch in n)}


def _real_file(att: AttachmentRecord) -> bool:
    """A file someone sent, not a picture in a signature, a tiny file, or a part of the message itself."""
    name = att.filename or ""
    if (att.size_bytes or 0) < MIN_FILE_BYTES and not re.search(r"(?i)\.(csv|txt)$", name):
        return False
    if " › " in name:  # a file inside a forwarded email or a zip is the outer one's
        name = name.rsplit(" › ", 1)[-1]
    if _PICTURE.search(name) and (att.size_bytes or 0) < 200_000:
        return False
    return not _GENERIC_STEM.match(re.sub(r"\.[A-Za-z0-9]{1,5}$", "", name).strip())


def file_stem(filename: str) -> str:
    """A file's name without its version marks, with its extension: "Budget v2 FINAL (1).xlsx" is "budget.xlsx".
    "" for a generic name, which says nothing about which file it is."""
    name = (filename or "").rsplit(" › ", 1)[-1]
    match = re.match(r"^(.*?)(\.[A-Za-z0-9]{1,5})?$", name)
    base, ext = (match.group(1), (match.group(2) or "").lower()) if match else (name, "")
    base = base.replace("_", " ")  # "Budget_v3" is "Budget v3"
    previous = None
    while previous != base:
        previous = base
        base = _VERSION_MARKS.sub("", base)
    base = re.sub(r"[\s_.-]+", " ", base).strip().lower()
    if len(base) < 4 or _GENERIC_STEM.match(base) or base.isdigit():
        return ""
    return f"{base}{ext}"


# --- Related emails -----------------------------------------------------------------------------------------------

REASONS = {
    "invoice": "Same invoice",
    "po": "Same PO",
    "file": "Same file",
    "stem": "Another version of",
}


AI_LABEL = "CloseDesk thinks these are about the same matter"


def related(store, email: EmailRecord, *, ai: bool = True) -> dict:
    """The conversation ``email`` is in, oldest first, and the other emails it shares an invoice, a PO, a file or a
    file's name with, grouped by why; with ``ai``, then those the local model said are about the same matter
    (link_judge.py), each with its reason in the group's ``why``. Every email listed is one ``store`` still has."""
    mine = store.link_keys(email.id) or keys(email)
    thread_ids = _conversation(store, email.id, [value for kind, value in mine if kind == "thread"])
    groups: list[dict] = []
    shown = set(thread_ids) | {email.id}
    locked_here = attachments_locked(email)
    for kind in ("invoice", "po", "file", "stem"):
        values = [value for k, value in mine if k == kind]
        if not values or (locked_here and kind in {"file", "stem"}):
            continue
        for value, others in store.emails_with_keys(kind, values, exclude=email.id, common=COMMON).items():
            others = [other for other in others if other not in shown]
            if kind == "stem":
                # A version is a different file under the same name: the same contents is "Same file" already.
                same = {v for k, v in mine if k == "file"}
                others = [other for other in others if not same & set(store.link_values(other, "file"))]
            if not others:
                continue
            groups.append({"kind": kind, "value": value, "ids": others[:MAX_PER_REASON]})
            shown |= set(others)
    if ai and not locked_here:
        judged = {other: why for other, why in store.ai_links_of(email.id).items() if other not in shown}
        if judged:
            groups.append({"kind": "ai", "value": "", "ids": list(judged)[:MAX_PER_REASON], "why": judged})
            shown |= set(judged)
    cards = store.get_emails(list(dict.fromkeys([*thread_ids, *(i for g in groups for i in g["ids"])])))
    thread = sorted(
        (email if i == email.id else cards[i] for i in thread_ids if i == email.id or i in cards),
        key=lambda record: record.received_at or "",
    )
    out_groups = []
    for group in groups:
        records = [cards[i] for i in group["ids"] if i in cards and not (group["kind"] == "ai" and attachments_locked(cards[i]))]
        if records:
            label = AI_LABEL if group["kind"] == "ai" else _label(group, email, records)
            out_groups.append({"kind": group["kind"], "label": label, "emails": sorted(records, key=lambda r: r.received_at or "", reverse=True), "why": group.get("why", {})})
    return {"thread": thread[-MAX_THREAD:] if len(thread) > 1 else [], "groups": out_groups, "new_domains": new_domains(thread)}


def _conversation(store, email_id: str, values: list[str]) -> list[str]:
    """The emails sharing a thread key with ``email_id``, and those sharing one with them, a few rounds out."""
    found = {email_id}
    frontier = list(values)
    seen_values: set[str] = set()
    for _round in range(4):
        frontier = [value for value in frontier if value not in seen_values]
        if not frontier or len(found) > MAX_THREAD * 2:
            break
        seen_values |= set(frontier)
        new_ids = {other for ids in store.emails_with_keys("thread", frontier, exclude="", common=MAX_THREAD * 2).values() for other in ids} - found
        if not new_ids:
            break
        found |= new_ids
        frontier = [value for other in new_ids for value in store.link_values(other, "thread")]
    return sorted(found)


def new_domains(thread: list[EmailRecord]) -> set[str]:
    """The emails in a conversation whose sender's domain hadn't written in it before: a reply from a lookalike
    domain slipped into a real thread is how payment-change fraud usually starts."""
    seen: set[str] = set()
    marked: set[str] = set()
    for record in thread:
        domain = sender_domain(record)
        if seen and domain and domain not in seen:
            marked.add(record.id)
        # The domains a message went to are in the conversation too: the reply of someone it was sent to is not new.
        seen |= {domain} - {""} | {address.rpartition("@")[2] for address in (record.threading or {}).get("to") or []}
    return marked


def _label(group: dict, email: EmailRecord, records: list[EmailRecord]) -> str:
    kind, value = group["kind"], group["value"]
    if kind in {"invoice", "po"}:
        shown = value.split("|")[-1]
        named = next((n for n in [*email.extracted.invoice_numbers, *email.extracted.po_numbers,
                                  *(x for att in email.attachments for x in [*att.extracted_fields.invoice_numbers, *att.extracted_fields.po_numbers])]
                      if number_key(n) == shown), shown)
        return f"{REASONS[kind]} {named}"
    if kind == "file":
        name = next((att.filename for att in email.attachments if att.sha256 == value), "")
        return f"{REASONS[kind]}: {name.rsplit(' › ', 1)[-1]}" if name else REASONS[kind]
    name = next((att.filename for att in email.attachments if file_stem(att.filename) == value), value)
    return f"{REASONS[kind]} {name.rsplit(' › ', 1)[-1]}"


def when(record: EmailRecord) -> datetime | None:
    try:
        return datetime.fromisoformat((record.received_at or "").replace("Z", "+00:00"))
    except ValueError:
        return None


# --- Mail stored before this version -------------------------------------------------------------------------------


def catch_up(store, settings, *, limit: int = 20_000) -> dict:
    """File mail stored before links were kept: its keys from what is stored, then its conversation headers from
    the .eml or .msg it was read from (kept in the inbox folder), a batch at a time. Runs with the run lock held, so
    a dropped email isn't read twice over. Returns how many emails each step did."""
    filed = headers = 0
    while filed < limit and (done := store.file_link_keys(batch=500)):
        filed += done
    while headers < limit and (done := _headers_from_files(store, settings, batch=200)):
        headers += done
    return {"filed": filed, "headers": headers}


def _headers_from_files(store, settings, *, batch: int) -> int:
    """Read the conversation headers of stored mail that has none from its kept file. Resumes where it stopped."""
    from pathlib import Path

    with store.connect() as conn:
        after = int((conn.execute("SELECT value FROM sync_state WHERE key = 'thread_headers_rowid'").fetchone() or ["0"])[0] or 0)
        rows = conn.execute(
            "SELECT rowid, id, source_path FROM emails WHERE rowid > ? AND COALESCE(threading, '') = '' ORDER BY rowid LIMIT ?",
            (after, batch),
        ).fetchall()
    if not rows:
        return 0
    for _rowid, email_id, source_path in rows:
        path = Path(source_path or "")
        if not source_path or path.suffix.lower() not in {".eml", ".msg"} or not path.is_file():
            continue
        try:
            info = _file_threading(path)
        except Exception:  # a file that can't be read again keeps what was read before
            continue
        email = store.get_email(email_id)
        if email is not None and info:
            email.threading = info
            store.save_threading(email)
    with store.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO sync_state(key, value) VALUES ('thread_headers_rowid', ?)", (str(rows[-1][0]),))
    return len(rows)


def _file_threading(path) -> dict:
    from email import policy
    from email.parser import BytesParser

    from controller_inbox import folder_mail

    if path.suffix.lower() == ".eml":
        with path.open("rb") as handle:
            return folder_mail.eml_threading(BytesParser(policy=policy.default).parse(handle, headersonly=True))
    import extract_msg

    message = extract_msg.Message(str(path), delayAttachments=True)
    try:
        return folder_mail.msg_threading(message)
    finally:
        message.close()


# --- A whole matter, and what changed in its files ------------------------------------------------------------------

MAX_TRACE = 40
MAX_CHANGES = 25


def trace(store, email: EmailRecord) -> dict:
    """Everything that belongs with ``email``, followed two links out (the invoice's PO, the PO's quote), oldest
    first: each email with why it is here, and what changed between the versions of each file sent along the way."""
    reasons: dict[str, list[str]] = {email.id: []}
    records: dict[str, EmailRecord] = {email.id: email}
    frontier = [email]
    for _hop in range(2):
        nxt = []
        for current in frontier:
            found = related(store, current)
            for record in found["thread"]:
                if record.id not in records:
                    records[record.id] = record
                    reasons[record.id] = ["In the conversation"]
                    nxt.append(record)
            for group in found["groups"]:
                for record in group["emails"]:
                    if record.id not in records:
                        records[record.id] = record
                        reasons[record.id] = []
                        nxt.append(record)
                    if group["label"] not in reasons[record.id] and record.id != email.id:
                        reasons[record.id].append(group["label"])
            if len(records) >= MAX_TRACE:
                break
        frontier = nxt
        if len(records) >= MAX_TRACE:
            break
    steps = sorted(records.values(), key=lambda record: record.received_at or "")[-MAX_TRACE:]
    return {"steps": [(record, reasons.get(record.id, [])) for record in steps], "versions": versions(steps)}


def versions(emails: list[EmailRecord]) -> list[dict]:
    """For each file sent more than once with different contents along a matter, each version against the one
    before: its name, the emails, and what changed (``changes``). A held email's files are left out."""
    by_stem: dict[str, list[tuple[EmailRecord, AttachmentRecord]]] = {}
    for record in sorted(emails, key=lambda r: r.received_at or ""):
        if attachments_locked(record):
            continue
        for att in record.attachments:
            if _real_file(att) and (stem := file_stem(att.filename)):
                seen = by_stem.setdefault(stem, [])
                if not any(other.sha256 == att.sha256 for _r, other in seen):
                    seen.append((record, att))
    out = []
    for stem, sent in by_stem.items():
        for (old_email, old), (new_email, new) in zip(sent, sent[1:]):
            listed, more = changes(old.extracted_text or "", new.extracted_text or "")
            out.append({"stem": stem, "old": (old_email, old), "new": (new_email, new), "changes": listed, "more": more})
    return out


_CELL_LINE = re.compile(r"^([A-Z]{1,3})(\d+)(?: \(([^)]*)\))?: (.*)$")


def changes(old: str, new: str, *, limit: int = MAX_CHANGES) -> tuple[list[str], int]:
    """What changed from ``old`` to ``new`` (two versions of a file as read): a workbook cell by cell ("Budget!C7
    (Q3): 1,200 → 1,450"), other files line by line ("Removed: …", "Added: …"). At most ``limit``, with how many more."""
    if "[sheet " in old and "[sheet " in new:
        before, after = _cells(old), _cells(new)
        found = []
        for key in sorted(before.keys() | after.keys(), key=_cell_order):
            was, now = before.get(key), after.get(key)
            if was == now:
                continue
            sheet, ref, heading = key[0], key[1], (after.get(key) or before.get(key))[1]
            place = f"{sheet}!{ref}" + (f" ({heading})" if heading else "")
            if was is None:
                found.append(f"{place}: added {now[0]}")
            elif now is None:
                found.append(f"{place}: removed (was {was[0]})")
            elif was[0] != now[0]:
                found.append(f"{place}: {was[0]} → {now[0]}")
        return found[:limit], max(0, len(found) - limit)
    import difflib

    lines = lambda text: [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("[")]  # noqa: E731
    found = []
    for line in difflib.unified_diff(lines(old), lines(new), lineterm="", n=0):
        if line.startswith(("---", "+++", "@@")):
            continue
        if line.startswith("-"):
            found.append(f"Removed: {line[1:][:200]}")
        elif line.startswith("+"):
            found.append(f"Added: {line[1:][:200]}")
    return found[:limit], max(0, len(found) - limit)


def _cells(text: str) -> dict[tuple[str, str], tuple[str, str]]:
    """A workbook as read: (sheet, cell) → (value without its formula, heading)."""
    cells: dict[tuple[str, str], tuple[str, str]] = {}
    sheet = ""
    for line in text.splitlines():
        if line.startswith('[sheet "'):
            sheet = line.split('"')[1]
            continue
        for piece in line.split(" | "):
            if match := _CELL_LINE.match(piece.strip()):
                value = re.sub(r"\s*\(=.*\)$", "", match.group(4)).strip()
                cells[(sheet, match.group(1) + match.group(2))] = (value, match.group(3) or "")
    return cells


def _cell_order(key: tuple[str, str]) -> tuple:
    sheet, ref = key
    letters = re.match(r"[A-Z]+", ref).group(0)
    return (sheet, int(ref[len(letters):]), len(letters), letters)
