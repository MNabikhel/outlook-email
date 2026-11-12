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
  ("Accepted: ...") is no meeting, nor is a proposed new time (COUNTER) or the answer to one. An update to one
  occurrence of a repeating meeting (its RECURRENCE-ID) names the occurrence it replaces (``occurrence``), so it
  replaces that one alone (store._invite_winners). A file is read within limits (``MAX_ICS_BYTES``, ``MAX_EVENTS``,
  ``MAX_ROWS``, ``ICS_SECONDS``): anyone can email one, and one built to take hours must not hold the run.
- ``demo``: the sample mailbox's.

Nothing here writes to Outlook or to any calendar: meetings are only read and shown.
"""

from __future__ import annotations

import hashlib
import re
from time import monotonic
from dataclasses import asdict, dataclass, field
from functools import cache
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WINDOW_BACK = timedelta(days=90)
WINDOW_AHEAD = timedelta(days=180)
MAX_OCCURRENCES = 400  # of one repeating meeting in the window
MAX_ATTENDEES = 60  # listed per meeting
MAX_PERSON = 200  # characters of a name or an address
MAX_STEPS = 50_000  # occurrences of one rule looked at, from its first: a daily meeting from 1890 reaches today
MAX_ICS_BYTES = 1_000_000  # of an invitation read: Outlook's for a year of a busy meeting is a few hundred kilobytes
MAX_EVENTS = 200  # meetings (VEVENTs) read from one file
MAX_ROWS = 2000  # occurrences built from one email's invitations (store.MAX_INVITE_ROWS keeps as many)
ICS_SECONDS = 3.0  # spent expanding one file's repeating meetings; those left are kept as their first occurrence
# Methods whose file holds no meeting of its own: an attendee's answer, a proposed new time and the organizer's
# answer to it, and a request to be sent the meeting again.
NOT_MEETINGS = frozenset({"REPLY", "COUNTER", "DECLINECOUNTER", "REFRESH"})


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
    sequence: int = 0  # an invitation's SEQUENCE: a later update has a higher one
    # An update to one occurrence of a repeating meeting: the start it replaces, written as ``start`` is (a date
    # "2026-10-19" from an Outlook .msg, which says only the day). "" for a series and a meeting that doesn't repeat.
    occurrence: str = ""

    @property
    def id(self) -> str:
        """One occurrence: the series and its start (a weekly meeting's weeks share the series' uid). An invitation's
        rows are its email's own, so another email naming the same uid can't overwrite them."""
        own = f"|{self.email_id}" if self.source == "invite" else ""
        return "mtg-" + hashlib.sha256(f"{self.source}|{self.uid}|{self.start}{own}".encode()).hexdigest()[:20]

    def to_row(self) -> dict:
        return {"id": self.id, **asdict(self)}


# --- iCalendar (.ics) ------------------------------------------------------------------------------------------------


def from_ics(
    data: bytes | str, *, email_id: str = "", now: datetime | None = None, limit: int = MAX_ROWS, deadline: float | None = None
) -> list[Meeting]:
    """The meetings in an iCalendar file, each occurrence in the window around ``now`` once, ``limit`` at most. A
    METHOD:REPLY (an attendee's answer), COUNTER (a proposed new time), DECLINECOUNTER or REFRESH holds none;
    METHOD:CANCEL or STATUS:CANCELLED marks the meetings cancelled. Repeating meetings are expanded until
    ``deadline`` (``time.monotonic()``; ``ICS_SECONDS`` from now when not given), those left after it as their first
    occurrence."""
    if isinstance(data, (bytes, bytearray)):
        text = bytes(data[:MAX_ICS_BYTES]).decode("utf-8", "replace")
    else:
        text = data[:MAX_ICS_BYTES]
    lines = _unfolded(text)
    method = next((value.upper() for name, _params, value in lines if name == "METHOD"), "")
    if method in NOT_MEETINGS or limit <= 0:
        return []
    zones = _vtimezones(lines, text)
    events = _blocks(lines, "VEVENT", limit=MAX_EVENTS)
    now = now or datetime.now(timezone.utc)
    deadline = monotonic() + ICS_SECONDS if deadline is None else deadline
    overrides: dict[tuple[str, str], tuple[dict, str]] = {}
    series: list[dict] = []
    for event in events:
        if "RECURRENCE-ID" in event:
            when = _when(event["RECURRENCE-ID"], zones)
            if when is not None:
                overrides[(event.get("UID", ("", {}, ""))[2], _key(when[0]))] = (event, _iso(*when))
        else:
            series.append(event)
    out: list[Meeting] = []
    for event in series:
        if len(out) >= limit:
            break
        out += _occurrences(event, zones, overrides, method, email_id, now, rows=limit - len(out), deadline=deadline)
    # A moved occurrence whose series isn't in this file (an update to one week only) is a meeting of its own.
    seen = {(m.uid, m.start) for m in out}
    for (uid, _start), (event, replaces) in overrides.items():
        meeting = _meeting(event, zones, method, email_id)
        if meeting is not None and (meeting.uid, meeting.start) not in seen:
            meeting.occurrence = replaces
            meeting.recurring = True  # one occurrence of a repeating meeting
            out.append(meeting)
    return out[:limit]


def _occurrences(
    event: dict, zones: dict, overrides: dict, method: str, email_id: str, now: datetime, *, rows: int = MAX_ROWS,
    deadline: float | None = None,
) -> list[Meeting]:
    """One meeting's occurrences in the window, ``rows`` at most. Once ``deadline`` has passed (the file's time is
    up), a repeating meeting is its first occurrence, marked as repeating; one being walked then keeps what was found."""
    first = _meeting(event, zones, method, email_id)
    if first is None:
        return []
    rule = event.get("RRULE")
    if rule is None:
        return [first]
    # The rule alone: dateutil reads what follows a space on the line as more rules and dates ("... RDATE:..."), as a
    # set of rules none of the limits below would reach.
    rule_text = (rule[2].split() or [""])[0]
    if deadline is not None and monotonic() >= deadline:
        first.recurring = True
        return [first]
    if _too_often(rule_text):
        # No meeting repeats more often than a few times a day: such a rule (which anyone can email, from years back)
        # would take minutes to walk. Its first occurrence is kept, marked as repeating.
        first.recurring = True
        return [first]
    from dateutil.rrule import rrulestr

    start = _when(event["DTSTART"], zones)
    if start is None:
        return [first]
    begin, all_day = start
    length = _parse(first.end, first.all_day) - _parse(first.start, first.all_day)
    try:
        anchor = begin if not all_day else datetime.combine(begin, time())
        rrule = rrulestr(_until_in_utc(rule_text, begin, all_day), dtstart=anchor.replace(tzinfo=None) if all_day else anchor, ignoretz=all_day)
    except (ValueError, TypeError):
        first.recurring = True
        return [first]
    low = now - WINDOW_BACK
    high = now + WINDOW_AHEAD
    if all_day:
        low, high = low.replace(tzinfo=None), high.replace(tzinfo=None)
    excluded = {_key(when[0]) for name in ("EXDATE",) for raw in event.get("_all", {}).get(name, []) for when in _whens(raw, zones)}
    # Walked a step at a time from its first occurrence, up to the window's end, and at most ``MAX_STEPS`` steps
    # however far back it starts: a rule from years back that repeats often, which anyone can email, mustn't hold the
    # run or fill the memory. (dateutil's own "after" walks from the start too, with no limit.)
    out = []
    from itertools import islice

    for when in islice(_walk(rrule, high, deadline), MAX_STEPS):
        if when < low:
            continue
        stamp = when.date() if all_day else when
        key = _key(stamp)
        if key in excluded:
            continue
        moved = overrides.get((first.uid, key))
        if moved is not None:
            meeting = _meeting(moved[0], zones, method, email_id)
            if meeting is not None:
                meeting.recurring = True
                meeting.occurrence = _iso(stamp, all_day)
                out.append(meeting)
            continue
        out.append(Meeting(**{**asdict(first), "start": _iso(stamp, all_day), "end": _iso(stamp + length, all_day), "recurring": True}))
    cap = min(MAX_OCCURRENCES, rows)
    if len(out) > cap:
        # More than a calendar shows: those from today on are kept first.
        upcoming = [m for m in out if m.start >= _iso(now.date() if all_day else now, all_day)]
        out = (upcoming[:cap] or out[-cap:])
    return out


class _PastTheWindow(Exception):
    """dateutil started on a year past the window's: what it finds from there is no occurrence to keep."""


def _walk(rule, high, deadline: float | None = None):
    """The rule's occurrences from its first up to ``high``, until ``deadline`` (``time.monotonic()``). dateutil looks
    for a rule's next occurrence day by day, with no end, until year 9999: a rule no day meets ("every day in February
    that is the 31st"), which anyone can email, would take seconds each. So the rule is stopped once dateutil reaches
    a year past ``high``'s, or the deadline has passed: it reads which weeks of the year a rule names (``_byweekno``)
    as it starts each year, and that read ends the walk. The rules of a set (``rruleset``) are stopped alike."""
    from dateutil.rrule import rrule as dateutil_rule
    from dateutil.rrule import rruleset

    def stopping(one):
        if type(one) is not dateutil_rule:
            return one
        kind = _stops_after_year()
        bounded = kind.__new__(kind)
        bounded.__dict__.update(one.__dict__)
        bounded._years_left = high.year - one._dtstart.year + 3  # the start's year, the window's, and dateutil's first look
        bounded._deadline = deadline
        return bounded

    if isinstance(rule, rruleset):
        rule._rrule = [stopping(one) for one in rule._rrule]
        rule._exrule = [stopping(one) for one in rule._exrule]
    else:
        rule = stopping(rule)
    try:
        for count, when in enumerate(rule):
            if when > high or (deadline is not None and count % 64 == 63 and monotonic() >= deadline):
                return
            yield when
    except _PastTheWindow:
        return


@cache
def _stops_after_year():
    from dateutil.rrule import rrule as dateutil_rule

    class StopsAfterYear(dateutil_rule):
        """dateutil's rule, ending its walk after a number of years (``_walk``)."""

        @property
        def _byweekno(self):
            self._years_left -= 1
            if self._years_left < 0 or (self._deadline is not None and monotonic() >= self._deadline):
                raise _PastTheWindow
            return self.__dict__["_byweekno"]

        @_byweekno.setter
        def _byweekno(self, value):
            self.__dict__["_byweekno"] = value

    return StopsAfterYear


def _too_often(rule: str) -> bool:
    """A rule repeating more often than a few times a day: every hour, minute or second, or a daily rule listing
    seconds, several minutes or many hours of each day."""
    if re.search(r"FREQ=(SECONDLY|MINUTELY|HOURLY)", rule, re.I) or re.search(r"BYSECOND=", rule, re.I):
        return True
    minutes = re.search(r"BYMINUTE=([^;]*)", rule, re.I)
    hours = re.search(r"BYHOUR=([^;]*)", rule, re.I)
    return bool((minutes and "," in minutes.group(1)) or (hours and hours.group(1).count(",") >= 4))


def _until_in_utc(rule: str, begin, all_day: bool) -> str:
    """A rule's UNTIL as dateutil needs it beside a start with a zone: in UTC ("...Z"). Invitations also write it as
    a date, or a time with no zone (the start's), which dateutil refuses; an all-day series' UNTIL is a date."""
    match = re.search(r"UNTIL=(\d{8})(T(\d{6}))?(Z?)", rule, re.I)
    if not match or match.group(4) or all_day:
        if match and all_day:
            return rule.replace(match.group(0), f"UNTIL={match.group(1)}")
        return rule
    day = datetime.strptime(match.group(1), "%Y%m%d")
    until = datetime.strptime(match.group(1) + match.group(3), "%Y%m%d%H%M%S") if match.group(3) else day.replace(hour=23, minute=59, second=59)
    until = until.replace(tzinfo=begin.tzinfo).astimezone(timezone.utc)
    return rule.replace(match.group(0), f"UNTIL={until:%Y%m%dT%H%M%SZ}")


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
    attendees = [_person(raw) for raw in event.get("_all", {}).get("ATTENDEE", [])[:MAX_ATTENDEES * 2]]
    status = event.get("STATUS", ("", {}, ""))[2].upper()
    return Meeting(
        uid=uid,
        start=_iso(begin, all_day),
        end=_iso(end, all_day),
        subject=_text(event.get("SUMMARY", ("", {}, ""))[2]),
        all_day=all_day,
        location=_text(event.get("LOCATION", ("", {}, ""))[2]),
        organizer=_person(organizer) if organizer else "",
        attendees=[person for person in attendees if person][:MAX_ATTENDEES],
        cancelled=method == "CANCEL" or status == "CANCELLED",
        recurring="RRULE" in event,
        source="invite",
        email_id=email_id,
        sequence=_int(event.get("SEQUENCE", ("", {}, "0"))[2]),
    )


def _int(value: str) -> int:
    try:
        return int(str(value).strip() or 0)
    except ValueError:
        return 0


def _unfolded(text: str) -> list[tuple[str, dict, str]]:
    """The file's content lines, folded lines joined, each as (NAME, params, value)."""
    raw = re.sub(r"\r?\n[ \t]", "", text.replace("\r\n", "\n"))
    out = []
    for line in raw.split("\n"):
        if ":" not in line:
            continue
        if line.upper().startswith(("DTSTART", "DTEND", "RECURRENCE-ID", "EXDATE", "RDATE")):
            # A date never holds a colon, but an unquoted zone name may ("TZID=(UTC+01:00) Berlin:2026...").
            head, _sep, value = line.rpartition(":")
        else:
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


def _blocks(lines: list[tuple[str, dict, str]], kind: str, *, limit: int | None = None) -> list[dict]:
    """Each BEGIN:kind ... END:kind, its properties by name (the first of each; every one under "_all"), the first
    ``limit`` of them."""
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
                if limit is not None and len(blocks) >= limit:
                    break
            else:
                depth -= 1
            continue
        if depth == 0:
            current.setdefault(name, (name, params, value))
            current["_all"].setdefault(name, []).append((name, params, value))
    return blocks


def _vtimezones(lines, text: str = ""):
    """The zones an invitation names, by its TZID: an IANA name, a Windows name ("Eastern Standard Time"), else the
    zone the invitation itself defines (its VTIMEZONE block: Outlook's "(UTC-05:00) Eastern Time (US & Canada)" or
    "Customized Time Zone", with its own daylight saving rules), else the "(UTC-05:00)" offset in its name."""
    zones: dict = {}
    defined = {}
    if "BEGIN:VTIMEZONE" in text:
        try:
            import io

            from dateutil.tz import tzical

            calendar = tzical(io.StringIO(re.sub(r"\r?\n", "\r\n", text)))
            defined = {key: calendar.get(key) for key in calendar.keys()}
        except Exception:  # a block dateutil can't read: the names and offsets below still apply
            defined = {}
    for name, params, value in lines:
        tzid = params.get("TZID") or (value if name == "TZID" else "")
        if tzid and tzid not in zones and (zone := zone_named(tzid) or defined.get(tzid) or _offset_zone(tzid)) is not None:
            zones[tzid] = zone
    return zones


def zone_named(name: str) -> ZoneInfo | None:
    from tzlocal import windows_tz

    candidates = [name, windows_tz.win_tz.get(name, "")]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return None


def _offset_zone(name: str):
    """The fixed offset a zone's name gives ("(UTC+01:00) Amsterdam, Berlin"), when nothing better is known."""
    match = re.search(r"\(UTC([+-])(\d{1,2}):(\d{2})\)", name)
    if not match:
        return timezone.utc if re.match(r"\(UTC\)", name) else None
    span = timedelta(hours=int(match.group(2)), minutes=int(match.group(3)))
    return timezone(span if match.group(1) == "+" else -span)


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
        tzid = params.get("TZID", "")
        zone = zones.get(tzid) or (zone_named(tzid) or _offset_zone(tzid) if tzid else None)
        # Kept in its own zone, so a repeating meeting stays at its local time across daylight saving changes.
        out.append((stamp.replace(tzinfo=zone) if zone else stamp.astimezone(), False))  # no zone: this computer's
    return out


def _duration(value: str) -> timedelta:
    match = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value.strip())
    if not match:
        return timedelta(0)
    weeks, days, hours, minutes, seconds = (int(part or 0) for part in match.groups()[1:])
    span = timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)
    return -span if match.group(1) == "-" else span


def _person(prop) -> str:
    """"Maya Chen <maya@taz.com>" from an ORGANIZER or ATTENDEE line, each part kept to a sensible length."""
    _name, params, value = prop
    address = re.sub(r"(?i)^mailto:", "", value).strip()[:MAX_PERSON]
    shown = " ".join(params.get("CN", "").split())[:MAX_PERSON]
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


def invitation_method(attachments, meetings=()) -> str:
    """What an email's invitation is (its METHOD: "REQUEST", "CANCEL", "REPLY", "COUNTER", ...), from its first
    .ics file (``attachments``: as read, or as stored), else from the meeting of an Outlook meeting request
    (``meetings``): "CANCEL" for a cancellation, "REQUEST" for a request. "" when it has no invitation."""
    for att in attachments:
        name = (getattr(att, "filename", "") or "").lower()
        if not (name.endswith((".ics", ".vcs")) or "calendar" in (getattr(att, "content_type", "") or "").lower()):
            continue
        content = getattr(att, "content", None) or getattr(att, "extracted_text", "") or ""
        head = bytes(content[:65_536]).decode("utf-8", "replace") if isinstance(content, (bytes, bytearray)) else str(content)[:65_536]
        found = re.search(r"(?im)^METHOD:\s*([A-Z-]+)", head)
        if found:
            return found.group(1).upper()
        if "BEGIN:VEVENT" in head.upper():
            return "PUBLISH"  # a calendar file with no METHOD: a meeting, sent for the calendar
    if meetings:
        return "CANCEL" if any(meeting.cancelled for meeting in meetings) else "REQUEST"
    return ""


# --- Outlook meeting requests (.msg) ---------------------------------------------------------------------------------


def from_msg(path, *, email_id: str = "", zone=None) -> list[Meeting]:
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
        clean = _id_bytes(_property(message, "cleanGlobalObjectID"))
        uid = invitation_uid(clean) if clean else str(getattr(message, "subject", "") or "")
        # Outlook's own all-day flag: the times are UTC, so a New York all-day meeting doesn't start at midnight here.
        all_day = bool(getattr(message, "appointmentSubType", False))
        if all_day:
            local = _organizer_dates(start, end, zone)
            start, end = local
        attendees = [part.strip()[:MAX_PERSON] for part in str(getattr(message, "allAttendeesString", "") or "")[:50_000].split(";") if part.strip()]
        return [
            Meeting(
                uid=uid,
                start=_iso(start, all_day),
                end=_iso(end, all_day),
                subject=str(getattr(message, "subject", "") or "")[:300],
                all_day=all_day,
                location=str(getattr(message, "location", "") or "")[:300],
                organizer=str(getattr(message, "sender", "") or "")[:200],
                attendees=attendees[:MAX_ATTENDEES],
                cancelled=isinstance(message, MeetingCancellation),
                recurring=bool(getattr(message, "isRecurring", False) or getattr(message, "appointmentRecur", None)),
                source="invite",
                email_id=email_id,
                occurrence=_exception_day(_id_bytes(_property(message, "globalObjectID"))),
            )
        ]
    except Exception:  # a request extract-msg can't fully read is still an email: it just adds no meeting
        return []
    finally:
        message.close()


def _property(message, name: str):
    """One of the message's properties, or None when extract-msg can't read it."""
    try:
        return getattr(message, name, None)
    except Exception:
        return None


def _id_bytes(value) -> bytes:
    """A GlobalObjectID as its bytes: extract-msg gives an object for it (its ``toBytes``), or the bytes."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    to_bytes = getattr(value, "toBytes", None)
    try:
        return bytes(to_bytes()) if callable(to_bytes) else b""
    except Exception:
        return b""


def _exception_day(global_id: bytes) -> str:
    """The day of the occurrence an Outlook request for one occurrence of a repeating meeting replaces
    ("2026-10-19"): Outlook writes it into the meeting's GlobalObjectID (bytes 16 to 19: year, month, day), which is
    all zeros for the series. Only the day: the request doesn't say the time it replaces."""
    if len(global_id) < 20 or not any(global_id[16:20]):
        return ""
    try:
        return date(int.from_bytes(global_id[16:18], "big"), global_id[18], global_id[19]).isoformat()
    except ValueError:
        return ""


def _organizer_dates(start: datetime, end: datetime, zone=None) -> tuple[date, date]:
    """An all-day meeting's days from its UTC times, read in ``zone`` (this computer's when not given): Outlook
    stores an all-day meeting as midnight where it was set up, which is most likely the zone of the people it is
    for. (Rounding to the nearest UTC midnight puts a New Zealand all-day meeting on the day before.)"""

    def day(value: datetime) -> date:
        local = value.astimezone(zone) if zone is not None else value.astimezone()
        # A midnight a few minutes off (a zone rule changed since) still names its day.
        return (local + timedelta(hours=2)).date() if local.hour >= 22 else local.date()

    first, last = day(start), day(end)
    return first, max(last, first + timedelta(days=1))


_GLOBAL_ID_HEAD = bytes.fromhex("040000008200E00074C5B7101A82E008")  # how every Outlook GlobalObjectID starts


def series_uid(global_id: str) -> str:
    """Outlook's GlobalAppointmentID (hex) as the series id invitations use: a meeting from another calendar keeps
    that calendar's UID, so the meeting and its invitation are one. An occurrence moved or changed on its own
    carries its original day in the id (bytes 16 to 19); without it, it is its series' id (as cleanGlobalObjectID is)."""
    try:
        data = bytes.fromhex(global_id)
    except ValueError:
        return global_id
    if data.startswith(_GLOBAL_ID_HEAD) and len(data) >= 20:
        data = data[:16] + b"\x00\x00\x00\x00" + data[20:]
    return invitation_uid(data)


def outlook_occurrence(global_id: str) -> str:
    """The day an occurrence changed on its own replaces ("2026-10-19"), from Outlook's GlobalAppointmentID; "" for
    a series or a meeting that doesn't repeat."""
    try:
        data = bytes.fromhex(global_id)
    except ValueError:
        return ""
    return _exception_day(data) if data.startswith(_GLOBAL_ID_HEAD) else ""


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


# --- Repeating invitations, expanded again overnight ----------------------------------------------------------------

EXPAND_AGAIN_AFTER = timedelta(days=30)  # an invitation's repeats are worked out again once this old
MAX_EXPAND_AGAIN = 50  # emails whose invitations are read again in one overnight run


def refresh_invites(store, settings, *, now: datetime | None = None, limit: int = MAX_EXPAND_AGAIN) -> int:
    """Work out again the repeats of the invitations whose were worked out more than ``EXPAND_AGAIN_AFTER`` before
    ``now``, ``limit`` emails at most, the longest ago first. A repeating meeting is kept as its occurrences in the
    window around when its invitation was read (``WINDOW_BACK``, ``WINDOW_AHEAD``): a weekly meeting with no end
    would otherwise leave the calendar six months on. The invitation is read again from what was kept of the email:
    the .ics text stored with it, else the file kept under inbox/extracted, or the Outlook meeting request (.msg) the
    email was read from. Held mail is never read again. Returns how many emails' meetings were saved again."""
    from controller_inbox.fraud import attachments_locked

    now = now or datetime.now(timezone.utc)
    stamp = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    done = 0
    for email_id in store.invites_to_expand((now - EXPAND_AGAIN_AFTER).astimezone(timezone.utc).isoformat(timespec="seconds"), limit=limit):
        email = store.get_email(email_id)
        found = None
        if email is not None and not attachments_locked(email):
            try:
                found = _kept_invitations(email, settings, now)
            except Exception:  # read again next time; the meetings stay as they were
                found = None
        if found is None:
            # Nothing kept to read it from (or held mail): its meetings stay as they are, looked at again in a while.
            store.mark_expanded(email_id, stamp)
            continue
        with store.transaction() as conn:
            current = store.get_email(email_id, conn=conn)
            if current is None or attachments_locked(current):  # held meanwhile
                continue
            store.save_invites(email_id, found, sender=current.sender_email, received=current.received_at, expanded=stamp, conn=conn)
        done += 1
    return done


def follow_hold(store, settings, email, *, now: datetime | None = None) -> None:
    """An email's meetings after a fraud verdict or check changed it: held, its invitation's meetings become only
    which meetings it named (``store.hold_invites``); cleared, an invitation kept only as that is read again from
    what was kept of the email, and its meetings are saved."""
    from controller_inbox.fraud import attachments_locked

    if email is None:
        return
    if attachments_locked(email):
        store.hold_invites(email.id)
        return
    if not store.held_markers(email.id):
        return
    now = now or datetime.now(timezone.utc)
    try:
        found = _kept_invitations(email, settings, now)
    except Exception:  # read again at the next verdict; the markers stay
        return
    if found is None:
        return
    with store.transaction() as conn:
        current = store.get_email(email.id, conn=conn)
        if current is None or attachments_locked(current):  # held again meanwhile
            return
        store.save_invites(email.id, found, sender=current.sender_email, received=current.received_at,
                           expanded=now.astimezone(timezone.utc).isoformat(timespec="seconds"), conn=conn)


CATCH_UP_KEY = "invites_catch_up"  # how far reading the invitations of mail stored before meetings were kept got


def catch_up_invites(store, settings, *, now: datetime | None = None, batch: int = 200, limit: int = 20_000) -> int:
    """Once, after an update: the invitations of mail stored by a version that kept no meetings, read from what was
    kept of the email (as ``refresh_invites`` reads them), ``batch`` emails at a time, resuming where it stopped. Mail
    with an invitation (an .ics file, or a meeting request it was read from) and no meeting saved is read; held mail
    keeps only which meeting it named. Returns how many emails were looked at."""
    from controller_inbox.fraud import attachments_locked

    if store.get_state(CATCH_UP_KEY) == "done":
        return 0
    now = now or datetime.now(timezone.utc)
    stamp = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    looked = 0
    while looked < limit:
        after = int(store.get_state(CATCH_UP_KEY) or 0)
        rows = store.emails_without_meetings(after, batch=batch)
        if not rows:
            store.set_state(CATCH_UP_KEY, "done")
            break
        for _rowid, email_id in rows:
            email = store.get_email(email_id)
            try:
                found = _kept_invitations(email, settings, now) if email is not None else None
            except Exception:  # an invitation that can't be read again adds nothing
                found = None
            if found:
                with store.transaction() as conn:
                    current = store.get_email(email_id, conn=conn)
                    if current is not None and not store.has_meetings(email_id, conn=conn):  # read meanwhile: as it was read
                        store.save_invites(email_id, found, sender=current.sender_email, received=current.received_at, expanded=stamp,
                                           held=attachments_locked(current), sender_name=current.sender_name, conn=conn)
                _refile_invitation(store, settings, email_id, now)
        looked += len(rows)
        store.set_state(CATCH_UP_KEY, str(rows[-1][0]))
    return looked


def _refile_invitation(store, settings, email_id: str, now: datetime) -> None:
    """An invitation an older version filed by its subject alone (as "Other", or with an "Accept or decline" task on a
    cancellation): filed again by what its invitation is (its METHOD), as a new one is, keeping what the user did with
    its tasks. Held mail, and mail the user corrected, stays as it is."""
    from controller_inbox.fraud import assess_email, attachments_locked, trust_context
    from controller_inbox.models import ActionStatus, DocumentType
    from controller_inbox.pipeline import rescore_stored

    email = store.get_email(email_id)
    if email is None or attachments_locked(email) or email.model_status == "corrected":
        return
    method = invitation_method(email.attachments)
    if not method:
        return
    asks = any(a.source == "meeting" and a.status == ActionStatus.OPEN for a in email.actions)
    wants = method in ("REQUEST", "PUBLISH", "ADD")
    if email.category == DocumentType.MEETING and asks == wants:
        return  # filed as it would be now
    try:
        rescore_stored(store, settings, email, assess_email(store, trust_context(store, settings), email), now=now)
    except Exception:  # filed as it was; the meeting is on the calendar either way
        return


def _kept_invitations(email, settings, now: datetime) -> list[Meeting] | None:
    """The meetings an email's invitations name, read again from what was kept of it; None when none could be."""
    from controller_inbox import links
    from controller_inbox.agent import is_invitation
    from controller_inbox.folder_mail import _kept_file, safe_filename

    found: list[Meeting] = []
    read = False
    deadline = monotonic() + ICS_SECONDS
    for att in email.attachments:
        if not is_invitation(att) or len(found) >= MAX_ROWS:
            continue
        text = att.extracted_text or ""
        if text.lstrip().upper().startswith("BEGIN:VCALENDAR") and "[CloseDesk stopped reading here" not in text:
            data: bytes | str | None = text
        else:
            data = _kept_file(settings.inbox_extracted / email.id / safe_filename(att.filename), att.sha256) if att.sha256 else None
        if data is None:
            continue
        read = True
        found += from_ics(data, email_id=email.id, now=now, limit=MAX_ROWS - len(found), deadline=deadline)
    kept = links.kept_file(settings, email.source_path)
    if kept is not None and kept.suffix.lower() == ".msg":
        request = from_msg(kept, email_id=email.id)
        read = read or bool(request)
        found = request + found
    return found if read else None


# --- For the calendar page and the chat -----------------------------------------------------------------------------

CAUTION = "Check who sent this before joining or paying"
# Flags on an invitation's email (not held: held mail's meetings aren't shown) that make its meeting doubtful.
_CAUTION_FLAGS = {"payment_caution", "bank_change_caution", "model_said_bank_change", "fraud_confirmed"}


def with_cautions(store, meetings: list[dict]) -> list[dict]:
    """``meetings`` (as the store gives them), each invitation's with ``caution`` set when it is to be doubted: another
    sender sent an invitation with the same meeting's id (a lookalike's "update" is a meeting of its own, and looks
    like the real one), its email carries a payment caution, or its sender's domain is new to the conversation it
    came in. "" for the others. Changed in place, and returned."""
    from controller_inbox import links
    from controller_inbox.store import _meeting_of

    invites = [m for m in meetings if m.get("source") == "invite" and m.get("email_id")]
    senders: dict[str, set[str]] = {}
    for meeting in invites:
        senders.setdefault(meeting["uid"], set()).add(_meeting_of(meeting.get("sender"), meeting.get("organizer")))
    emails = store.get_emails(list(dict.fromkeys(m["email_id"] for m in invites))) if invites else {}
    newcomer: dict[str, bool] = {}
    for meeting in meetings:
        meeting["caution"] = ""
    for meeting in invites:
        email = emails.get(meeting["email_id"])
        why = ""
        if len(senders[meeting["uid"]]) > 1:
            why = "another sender sent an invitation with this meeting's id"
        elif email is not None and _CAUTION_FLAGS & set(email.flags):
            why = "its email has a payment caution"
        elif email is not None and email.threading:
            if email.id not in newcomer:
                try:
                    newcomer[email.id] = email.id in links.related(store, email, ai=False)["new_domains"]
                except Exception:  # the conversation can't be read just now: no warning rather than no calendar
                    newcomer[email.id] = False
            if newcomer[email.id]:
                why = "its sender is new to the conversation it came in"
        if why:
            meeting["caution"] = f"{CAUTION}: {why}."
    return meetings



def shown(meeting: dict, tz) -> dict:
    """A stored meeting as the calendar page shows it: its times in the app's time zone ("2026-10-06T09:00"),
    its day(s), and nothing it doesn't need."""
    if meeting["all_day"]:
        start_local, end_local = meeting["start"][:10], meeting["end"][:10]
    else:
        start_local = datetime.fromisoformat(meeting["start"]).astimezone(tz).strftime("%Y-%m-%dT%H:%M")
        end_local = datetime.fromisoformat(meeting["end"]).astimezone(tz).strftime("%Y-%m-%dT%H:%M")
    subject = meeting["subject"] or "(no title)"
    if meeting["cancelled"]:
        # Outlook names a cancellation "Canceled: Weekly review": the Cancelled tag beside it says so already.
        subject = re.sub(r"(?i)^\s*cancell?ed\s*:\s*", "", subject) or subject
    return {
        "id": meeting["id"],
        "subject": subject,
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
        "sender": meeting.get("sender") or "",  # who emailed the invitation: its organizer is what the file says
        "caution": meeting.get("caution") or "",  # with_cautions
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
    # Searched within the two weeks either side in the query itself: the best matches of all time may all be old.
    low, high = (when - timedelta(days=14)).isoformat(), (when + timedelta(days=14)).isoformat()
    candidates = store.search_ranked(words, limit=limit * 3, received_from=low, received_before=high) if words else []
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


# How the chat's lines mark an item Outlook shows as free ("Payday reminder"): not a meeting that keeps the user busy.
SHOWN_FREE = "shown as free"


def _listed_when(meeting: dict) -> str:
    """When a meeting is, as the chat's lines write it: "2026-09-23 09:30–09:45", "2026-09-30 (all day)", and with its
    last day when that is later ("2026-09-29 to 2026-10-02 (all day)", "2026-09-29 09:00 to 2026-10-01 17:00")."""
    start, end = meeting["start"], meeting["end"] or ""
    if meeting["all_day"]:
        try:
            last = date.fromisoformat(end[:10]) - timedelta(days=1)  # an all-day meeting ends the morning after its last day
        except ValueError:
            last = None
        if last is not None and last.isoformat() > start[:10]:
            return f"{start[:10]} to {last.isoformat()} (all day)"
        return f"{start[:10]} (all day)"
    when = start.replace("T", " ")
    try:
        first_day, last_day = date.fromisoformat(start[:10]), date.fromisoformat(end[:10])
    except ValueError:
        return f"{when}–{end[11:16]}"
    # One that ends at midnight ends with its own day.
    if last_day > first_day and not (end[11:16] == "00:00" and last_day == first_day + timedelta(days=1)):
        return f"{when} to {end.replace('T', ' ')}"
    return f"{when}–{end[11:16]}"


def listing(meetings: list[dict]) -> str:
    """Meetings as lines for the chat's model: when, what, whether Outlook shows it as free or tentative, where, who."""
    lines = []
    for meeting in meetings:
        parts = [_listed_when(meeting), meeting["subject"]]
        if meeting.get("busy") == "free":
            parts.append(SHOWN_FREE)
        elif meeting.get("busy") == "tentative":
            parts.append("tentative")
        if meeting["location"]:
            parts.append(f"at {meeting['location']}")
        if meeting["organizer"]:
            parts.append(f"organized by {meeting['organizer']}")
        if meeting["attendees"]:
            parts.append("with " + ", ".join(meeting["attendees"][:8]))
        if meeting["cancelled"]:
            parts.append("CANCELLED")
        if meeting.get("caution"):
            parts.append(meeting["caution"].replace(CAUTION, CAUTION.upper(), 1))
        if meeting["source"] == "invite":
            # Not the user's own calendar: what an emailed invitation says, from whoever sent it.
            parts.append(f"from an invitation emailed by {meeting.get('sender') or 'someone'}")
        lines.append(" · ".join(parts))
    return "\n".join(lines)
