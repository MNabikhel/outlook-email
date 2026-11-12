"""Meetings: read from classic Outlook's calendar, and from the invitations that arrive by mail.

A meeting is kept as one row per occurrence (``Meeting``): a weekly meeting is a row for each week in the window,
told apart by its start, under the series' id (Outlook's GlobalAppointmentID, an invitation's UID). Times are kept
in UTC; an all-day meeting keeps its dates as dates, so a time zone set in CloseDesk never moves it a day.

Where meetings come from:

- ``outlook``: classic Outlook's calendar, read only (outlook_desktop.py and outlook_sync.py), when the user turned
  the calendar on in Settings → Sync inbox. Outlook expands repeating meetings itself.
- ``invite``: an invitation's .ics file on an email, or an Outlook meeting request (.msg) dropped in the drop
  folder or copied from Outlook. Repeating meetings are expanded over the window here (dateutil's rrule), with the
  occurrences that were moved or cancelled taken into account. A cancellation marks its meeting cancelled; a reply
  ("Accepted: ...") is no meeting.
- ``demo``: the sample mailbox's.

Nothing here writes to Outlook or to any calendar: meetings are only read and shown.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WINDOW_BACK = timedelta(days=90)
WINDOW_AHEAD = timedelta(days=180)
MAX_OCCURRENCES = 400  # of one repeating meeting in the window


@dataclass
class Meeting:
    uid: str  # the series' id: Outlook's GlobalAppointmentID or an invitation's UID
    start: str  # ISO: UTC "2026-10-05T14:00:00+00:00", or a date "2026-10-05" for all day
    end: str
    subject: str = ""
    all_day: bool = False
    location: str = ""
    organizer: str = ""
    attendees: list[str] = field(default_factory=list)
    cancelled: bool = False
    recurring: bool = False
    source: str = "invite"  # "outlook", "invite" or "demo"
    email_id: str = ""  # the email an invitation came on
    busy: str = ""  # "busy", "tentative", "free", "away" (Outlook's)

    @property
    def id(self) -> str:
        """One occurrence: the series and its start (a weekly meeting's weeks share the series' uid)."""
        return "mtg-" + hashlib.sha256(f"{self.source}|{self.uid}|{self.start}".encode()).hexdigest()[:20]

    def to_row(self) -> dict:
        return {"id": self.id, **asdict(self)}


# --- iCalendar (.ics) ------------------------------------------------------------------------------------------------


def from_ics(data: bytes | str, *, email_id: str = "", now: datetime | None = None) -> list[Meeting]:
    """The meetings in an iCalendar file, each occurrence in the window around ``now`` once. A METHOD:REPLY (an
    attendee's answer) holds none; METHOD:CANCEL or STATUS:CANCELLED marks the meetings cancelled."""
    text = data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else data
    lines = _unfolded(text)
    method = next((value.upper() for name, _params, value in lines if name == "METHOD"), "")
    if method == "REPLY":
        return []
    zones = _vtimezones(lines)
    events = _blocks(lines, "VEVENT")
    now = now or datetime.now(timezone.utc)
    overrides: dict[tuple[str, str], dict] = {}
    series: list[dict] = []
    for event in events:
        if "RECURRENCE-ID" in event:
            when = _when(event["RECURRENCE-ID"], zones)
            if when is not None:
                overrides[(event.get("UID", ("", {}, ""))[2], _key(when[0]))] = event
        else:
            series.append(event)
    out: list[Meeting] = []
    for event in series:
        out += _occurrences(event, zones, overrides, method, email_id, now)
    # A moved occurrence whose series isn't in this file (an update to one week only) is a meeting of its own.
    seen = {(m.uid, m.start) for m in out}
    for (uid, _start), event in overrides.items():
        meeting = _meeting(event, zones, method, email_id)
        if meeting is not None and (meeting.uid, meeting.start) not in seen:
            out.append(meeting)
    return out


def _occurrences(event: dict, zones: dict, overrides: dict, method: str, email_id: str, now: datetime) -> list[Meeting]:
    first = _meeting(event, zones, method, email_id)
    if first is None:
        return []
    rule = event.get("RRULE")
    if rule is None:
        return [first]
    from dateutil.rrule import rrulestr

    start = _when(event["DTSTART"], zones)
    if start is None:
        return [first]
    begin, all_day = start
    length = _parse(first.end, first.all_day) - _parse(first.start, first.all_day)
    try:
        anchor = begin if not all_day else datetime.combine(begin, time())
        rrule = rrulestr(rule[2], dtstart=anchor.replace(tzinfo=None) if all_day else anchor, ignoretz=all_day)
    except (ValueError, TypeError):
        first.recurring = True
        return [first]
    excluded = {_key(when[0]) for name in ("EXDATE",) for raw in event.get("_all", {}).get(name, []) for when in _whens(raw, zones)}
    low = now - WINDOW_BACK
    high = now + WINDOW_AHEAD
    if all_day:
        low, high = low.replace(tzinfo=None), high.replace(tzinfo=None)
    out = []
    for when in rrule.between(low, high, inc=True):
        stamp = when.date() if all_day else when
        key = _key(stamp)
        if key in excluded:
            continue
        moved = overrides.get((first.uid, key))
        if moved is not None:
            meeting = _meeting(moved, zones, method, email_id)
            if meeting is not None:
                meeting.recurring = True
                out.append(meeting)
            continue
        out.append(
            Meeting(**{**asdict(first), "start": _iso(stamp, all_day), "end": _iso(stamp + length if not all_day else stamp + length, all_day), "recurring": True})
        )
        if len(out) >= MAX_OCCURRENCES:
            break
    return out


def _meeting(event: dict, zones: dict, method: str, email_id: str) -> Meeting | None:
    start = _when(event.get("DTSTART"), zones) if event.get("DTSTART") else None
    if start is None:
        return None
    begin, all_day = start
    end_raw = _when(event.get("DTEND"), zones) if event.get("DTEND") else None
    if end_raw is not None:
        end = end_raw[0]
    elif event.get("DURATION"):
        end = begin + _duration(event["DURATION"][2])
    else:
        end = begin + (timedelta(days=1) if all_day else timedelta(0))
    uid = event.get("UID", ("", {}, ""))[2] or hashlib.sha256(f"{event.get('SUMMARY', ('', {}, ''))[2]}{_iso(begin, all_day)}".encode()).hexdigest()[:24]
    organizer = event.get("ORGANIZER")
    attendees = [_person(raw) for raw in event.get("_all", {}).get("ATTENDEE", [])]
    status = event.get("STATUS", ("", {}, ""))[2].upper()
    return Meeting(
        uid=uid,
        start=_iso(begin, all_day),
        end=_iso(end, all_day),
        subject=_text(event.get("SUMMARY", ("", {}, ""))[2]),
        all_day=all_day,
        location=_text(event.get("LOCATION", ("", {}, ""))[2]),
        organizer=_person(organizer) if organizer else "",
        attendees=[person for person in attendees if person][:60],
        cancelled=method == "CANCEL" or status == "CANCELLED",
        recurring="RRULE" in event,
        source="invite",
        email_id=email_id,
    )


def _unfolded(text: str) -> list[tuple[str, dict, str]]:
    """The file's content lines, folded lines joined, each as (NAME, params, value)."""
    raw = re.sub(r"\r?\n[ \t]", "", text.replace("\r\n", "\n"))
    out = []
    for line in raw.split("\n"):
        if ":" not in line:
            continue
        head, value = _split_value(line)
        name, *params = head.split(";")
        parsed = {}
        for param in params:
            key, _eq, val = param.partition("=")
            parsed[key.upper()] = val.strip('"')
        out.append((name.upper(), parsed, value))
    return out


def _split_value(line: str) -> tuple[str, str]:
    """A content line at its first colon outside quotes ("ATTENDEE;CN=\"Chen: AP\":mailto:...")."""
    quoted = False
    for index, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif char == ":" and not quoted:
            return line[:index], line[index + 1 :]
    return line, ""


def _blocks(lines: list[tuple[str, dict, str]], kind: str) -> list[dict]:
    """Each BEGIN:kind ... END:kind, its properties by name (the first of each; every one under "_all")."""
    blocks: list[dict] = []
    depth = 0
    current: dict | None = None
    for name, params, value in lines:
        if name == "BEGIN" and value.upper() == kind:
            current, depth = {"_all": {}}, 0
            continue
        if current is None:
            continue
        if name == "BEGIN":
            depth += 1  # a VALARM inside the event: its lines aren't the event's
            continue
        if name == "END":
            if value.upper() == kind and depth == 0:
                blocks.append(current)
                current = None
            else:
                depth -= 1
            continue
        if depth == 0:
            current.setdefault(name, (name, params, value))
            current["_all"].setdefault(name, []).append((name, params, value))
    return blocks


def _vtimezones(lines) -> dict[str, ZoneInfo]:
    """The zones an invitation names, by its TZID: an IANA name, a Windows name ("Eastern Standard Time"), or
    Outlook's long form ("(UTC-05:00) Eastern Time (US & Canada)") read by its offset when nothing else fits."""
    zones: dict[str, ZoneInfo] = {}
    for name, params, value in lines:
        tzid = params.get("TZID") or (value if name == "TZID" else "")
        if tzid and tzid not in zones and (zone := zone_named(tzid)) is not None:
            zones[tzid] = zone
    return zones


def zone_named(name: str) -> ZoneInfo | None:
    from tzlocal import windows_tz

    candidates = [name, windows_tz.win_tz.get(name, ""), windows_tz.win_tz.get(re.sub(r"^\(UTC[^)]*\)\s*", "", name), "")]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return None


def _when(prop, zones) -> tuple[datetime | date, bool] | None:
    found = _whens(prop, zones)
    return found[0] if found else None


def _whens(prop, zones) -> list[tuple[datetime | date, bool]]:
    if not prop:
        return []
    _name, params, value = prop
    out = []
    for piece in value.split(","):
        piece = piece.strip()
        if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", piece):
            try:
                out.append((datetime.strptime(piece[:8], "%Y%m%d").date(), True))
            except ValueError:
                continue
            continue
        match = re.fullmatch(r"(\d{8})T(\d{4,6})(Z?)", piece)
        if not match:
            continue
        stamp = datetime.strptime(match.group(1) + match.group(2).ljust(6, "0"), "%Y%m%d%H%M%S")
        if match.group(3):
            out.append((stamp.replace(tzinfo=timezone.utc), False))
            continue
        zone = zones.get(params.get("TZID", "")) or (zone_named(params["TZID"]) if params.get("TZID") else None)
        local = stamp.replace(tzinfo=zone) if zone else stamp.astimezone()  # no zone: this computer's local time
        out.append((local.astimezone(timezone.utc), False))
    return out


def _duration(value: str) -> timedelta:
    match = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value.strip())
    if not match:
        return timedelta(0)
    weeks, days, hours, minutes, seconds = (int(part or 0) for part in match.groups()[1:])
    span = timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)
    return -span if match.group(1) == "-" else span


def _person(prop) -> str:
    """"Maya Chen <maya@taz.com>" from an ORGANIZER or ATTENDEE line."""
    _name, params, value = prop
    address = re.sub(r"(?i)^mailto:", "", value).strip()
    shown = params.get("CN", "").strip()
    if shown and "@" in address:
        return f"{shown} <{address}>"
    return shown or address


def _text(value: str) -> str:
    return re.sub(r"\\([,;\\])", r"\1", value.replace("\\n", " ").replace("\\N", " ")).strip()[:300]


def _iso(value: datetime | date, all_day: bool) -> str:
    if all_day:
        return (value.date() if isinstance(value, datetime) else value).isoformat()
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(value: str, all_day: bool) -> datetime:
    if all_day:
        return datetime.combine(date.fromisoformat(value[:10]), time())
    return datetime.fromisoformat(value)


def _key(value: datetime | date) -> str:
    """One occurrence's start, written alike however it was given (an override's RECURRENCE-ID, the rule's dates)."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return value.strftime("%Y%m%d")


# --- Outlook meeting requests (.msg) ---------------------------------------------------------------------------------


def from_msg(path, *, email_id: str = "") -> list[Meeting]:
    """The meeting an Outlook meeting request, update or cancellation (.msg) is about; [] for anything else, a
    reply to one included. A repeating meeting's request gives its first occurrence, marked as repeating."""
    import extract_msg
    from extract_msg.msg_classes import MeetingCancellation, MeetingRequest, MeetingResponse

    try:
        message = extract_msg.openMsg(str(path), delayAttachments=True)
    except Exception:
        return []
    try:
        if isinstance(message, MeetingResponse) or not isinstance(message, (MeetingRequest, MeetingCancellation)):
            return []
        start, end = getattr(message, "appointmentStartWhole", None), getattr(message, "appointmentEndWhole", None)
        if not isinstance(start, datetime):
            return []
        end = end if isinstance(end, datetime) else start
        start, end = _aware(start), _aware(end)
        uid = str(getattr(message, "cleanGlobalObjectID", None) or b"").strip() or str(getattr(message, "subject", "") or "")
        if isinstance(getattr(message, "cleanGlobalObjectID", None), (bytes, bytearray)):
            uid = invitation_uid(message.cleanGlobalObjectID)
        all_day = (end - start) >= timedelta(days=1) and start.time() == time() and end.time() == time()
        attendees = [part.strip() for part in str(getattr(message, "allAttendeesString", "") or "").split(";") if part.strip()]
        return [
            Meeting(
                uid=uid,
                start=_iso(start.date() if all_day else start, all_day),
                end=_iso(end.date() if all_day else end, all_day),
                subject=str(getattr(message, "subject", "") or "")[:300],
                all_day=all_day,
                location=str(getattr(message, "location", "") or "")[:300],
                organizer=str(getattr(message, "sender", "") or "")[:200],
                attendees=attendees[:60],
                cancelled=isinstance(message, MeetingCancellation),
                recurring=bool(getattr(message, "isRecurring", False) or getattr(message, "appointmentRecur", None)),
                source="invite",
                email_id=email_id,
            )
        ]
    except Exception:  # a request extract-msg can't fully read is still an email: it just adds no meeting
        return []
    finally:
        message.close()


def invitation_uid(global_id: bytes) -> str:
    """The series id from Outlook's GlobalObjectID. A meeting sent from another calendar (Google's) carries that
    calendar's UID inside it ("vCal-Uid"), which is the UID its .ics invitation has: both are kept under that UID."""
    data = bytes(global_id)
    marker = data.find(b"vCal-Uid")
    if marker >= 0:
        uid = data[marker + 12 :].split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
        if uid:
            return uid
    return data.hex().upper()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# --- For the calendar page and the chat -----------------------------------------------------------------------------


def shown(meeting: dict, tz) -> dict:
    """A stored meeting as the calendar page shows it: its times in the app's time zone ("2026-10-06T09:00"),
    its day(s), and nothing it doesn't need."""
    if meeting["all_day"]:
        start_local, end_local = meeting["start"][:10], meeting["end"][:10]
    else:
        start_local = datetime.fromisoformat(meeting["start"]).astimezone(tz).strftime("%Y-%m-%dT%H:%M")
        end_local = datetime.fromisoformat(meeting["end"]).astimezone(tz).strftime("%Y-%m-%dT%H:%M")
    return {
        "id": meeting["id"],
        "subject": meeting["subject"] or "(no title)",
        "start": start_local,
        "end": end_local,
        "all_day": meeting["all_day"],
        "location": meeting["location"],
        "organizer": meeting["organizer"],
        "attendees": meeting["attendees"],
        "cancelled": meeting["cancelled"],
        "recurring": meeting["recurring"],
        "source": meeting["source"],
        "email_id": meeting["email_id"],
        "busy": meeting.get("busy") or "",
    }


def utc_range(first: date, last: date, tz) -> tuple[str, str]:
    """The UTC instants from the start of ``first`` to the end of ``last`` in ``tz``, as stored meetings compare."""
    start = datetime.combine(first, time(), tzinfo=tz).astimezone(timezone.utc)
    end = datetime.combine(last + timedelta(days=1), time(), tzinfo=tz).astimezone(timezone.utc)
    return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")


_TITLE_NOISE = frozenset("""meeting call sync review weekly daily monthly catch up catchup check in checkin with and the for
of on re fw fwd invitation invite updated accepted tentative declined canceled cancelled zoom teams""".split())


def related_mail(store, meeting: dict, *, limit: int = 8) -> list:
    """The mail about a meeting: the invitation it came on, then mail within two weeks of it that names its title's
    distinctive words or comes from its organizer or attendees, newest first. Held mail is listed, marked, as
    anywhere else."""
    found = []
    if meeting.get("email_id") and (invite := store.get_email(meeting["email_id"])) is not None:
        found.append(invite)
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9&'-]{2,}|\d{3,}", meeting.get("subject") or "") if w.lower() not in _TITLE_NOISE]
    people = [re.sub(r".*<|>.*", "", person).strip().lower() for person in [meeting.get("organizer") or "", *meeting.get("attendees", [])]]
    addresses = {person for person in people if "@" in person}
    when = datetime.fromisoformat(meeting["start"]) if "T" in meeting["start"] else datetime.combine(date.fromisoformat(meeting["start"][:10]), time(), tzinfo=timezone.utc)
    when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    candidates = store.search_ranked(words, limit=limit * 3) if words else []
    for email in candidates:
        try:
            received = datetime.fromisoformat((email.received_at or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        received = received if received.tzinfo else received.replace(tzinfo=timezone.utc)
        if abs(received - when) > timedelta(days=14):
            continue
        named = sum(1 for word in words if word.lower() in (email.subject or "").lower())
        from_people = (email.sender_email or "").lower() in addresses
        if (named >= min(2, len(words)) or (named and from_people)) and email.id not in {e.id for e in found}:
            found.append(email)
        if len(found) >= limit:
            break
    return found


def listing(meetings: list[dict]) -> str:
    """Meetings as lines for the chat's model: when, what, where, who."""
    lines = []
    for meeting in meetings:
        when = meeting["start"].replace("T", " ") + ("" if meeting["all_day"] else f"–{meeting['end'][11:16]}")
        if meeting["all_day"]:
            when = f"{meeting['start']} (all day)"
        parts = [when, meeting["subject"]]
        if meeting["location"]:
            parts.append(f"at {meeting['location']}")
        if meeting["organizer"]:
            parts.append(f"organized by {meeting['organizer']}")
        if meeting["attendees"]:
            parts.append("with " + ", ".join(meeting["attendees"][:8]))
        if meeting["cancelled"]:
            parts.append("CANCELLED")
        lines.append(" · ".join(parts))
    return "\n".join(lines)
