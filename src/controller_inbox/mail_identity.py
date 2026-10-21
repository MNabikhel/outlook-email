"""Whether a message is already stored, and the id it is filed under.

The drop folder (folder_mail), the classic Outlook sync (outlook_sync) and the Microsoft 365 sync (pipeline) all
decide this here, so one message that comes in two ways is one email, and two messages that share a Message-ID
(a scanner gives every scan the same one) are two.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

from controller_inbox.extract import redact_financial_secrets
from controller_inbox.models import RawMessage
from controller_inbox.store import Store

# A .msg and an .eml of one message can disagree on the sent time by a few seconds, and a message keeps its
# received time when it is moved in Outlook: two times this close are the same message's.
SAME_MESSAGE_SLACK = timedelta(minutes=2)


def tidy(value) -> str:
    """Outlook pads some .msg strings with NUL characters; drop them and fold whitespace."""
    return re.sub(r"\s+", " ", str(value or "").replace("\x00", "")).strip()


def file_id(data: bytes) -> str:
    """The id of a message (or loose file) without a Message-ID of its own: from the bytes of its file."""
    return "file-" + hashlib.sha256(data).hexdigest()[:20]


def stable_id(message_id: str) -> str:
    """Same Outlook message, same id — whether it was saved as .msg or .eml, today or next week."""
    normalized = message_id.strip().strip("<>").strip().lower()
    return "mail-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def content_id(raw: RawMessage) -> str:
    """The id of a message without a Message-ID, from what it says: its subject, sender, sent time and text. The
    Outlook sync names such a message this way, so the same message saved into the drop folder meets it."""
    sent = raw.received_at.astimezone(timezone.utc).isoformat()
    said = "\n".join([raw.subject, raw.sender_email.lower(), sent, raw.body_text[:5000]])
    return "mail-" + hashlib.sha256(f"outlook:{said}".encode()).hexdigest()[:20]


def alternative_id(raw: RawMessage) -> str:
    """The id of a message whose Message-ID another message already has: from its Message-ID, subject, sender and
    sent time. What the message doesn't say itself (a subject or date taken from its file) is left out of it."""
    subject = "" if "subject" in raw.from_file else raw.subject
    sent = "" if "sent" in raw.from_file else raw.received_at.astimezone(timezone.utc).isoformat()
    return stable_id("\n".join([raw.internet_message_id, subject, raw.sender_email, sent]))


def same_time(first: datetime, second: datetime) -> bool:
    """Whether two stamps of one message are close enough to be the same (``SAME_MESSAGE_SLACK``). A time without
    a zone is UTC."""
    first = first if first.tzinfo else first.replace(tzinfo=timezone.utc)
    second = second if second.tzinfo else second.replace(tzinfo=timezone.utc)
    return abs(first - second) <= SAME_MESSAGE_SLACK


def another_message(stored, raw: RawMessage) -> bool:
    """Whether a stored email that has this message's Message-ID is a different message after all.

    Only what the message itself says counts: a message without a subject is named after its file and one
    without a date is dated by its file, and those differ between two copies of it saved at different times."""
    # The stored subject has its account numbers masked, as this one's will be.
    subject = redact_financial_secrets(raw.subject)
    if "subject" not in raw.from_file and tidy(stored.subject).casefold() != tidy(subject).casefold():
        return True
    if (stored.sender_email or "").strip().lower() != (raw.sender_email or "").strip().lower():
        return True
    if "sent" in raw.from_file:
        return False
    try:
        sent = datetime.fromisoformat(str(stored.received_at))
    except ValueError:
        return False
    return not same_time(sent, raw.received_at)


def find_existing(store: Store, raw: RawMessage):
    """The stored email this message already is, however it was filed: under its own id, under the id a reused
    Message-ID gives it (``alternative_id``), or from another source (``_stored_elsewhere``). ``raw`` takes the id
    it is filed under. None when it is new. The drop folder and the Outlook sync both file mail through this."""
    existing = store.get_email(raw.id)
    if existing is not None and raw.internet_message_id and another_message(existing, raw):
        # Some scanners and mail tools give every message the same Message-ID. A stored email with another subject,
        # sender or date is a different message, so this one gets an id from those too, which a copy of it saved
        # again later shares.
        raw.id = alternative_id(raw)
        existing = store.get_email(raw.id)
    if existing is None:
        existing = _stored_elsewhere(store, raw)
    return existing


def stored_from(store: Store, raw: RawMessage, sources: tuple[str, ...]):
    """The stored email from one of ``sources`` (other than the message's own) that has this message's Message-ID
    and is this message, not another reusing the Message-ID. None when there is none."""
    for source in sources:
        if source == raw.source:
            continue
        found = store.email_with_message_id(raw.internet_message_id, source)
        stored = store.get_email(found) if found else None
        if stored is not None and not another_message(stored, raw):
            return stored
    return None


def _stored_elsewhere(store: Store, raw: RawMessage):
    """The stored email this dropped message already is, when it came in another way: from Microsoft 365 (same
    Message-ID, under Microsoft's id) or synced from classic Outlook without a Message-ID (named by what it says).
    ``raw`` takes that email's id, so the copy adds only its new files. None when it is new."""
    if raw.internet_message_id:
        stored = stored_from(store, raw, ("graph",))
        if stored is None:
            return None
        # Still the Microsoft 365 email: the next sync reads it under that id, and writes its categories back.
        raw.source = "graph"
    elif raw.sender_email and not raw.from_file:
        # Only for a message that names its own subject and sent time: one taken from its file differs between
        # two copies. A new one is filed under this id too, so the Outlook sync meets it when it comes later; one
        # filed earlier under its file's id was found by that id already.
        stored = store.get_email(content_id(raw))
        if stored is None:
            raw.id = content_id(raw)
            return None
    else:
        return None
    raw.id = stored.id
    return stored
