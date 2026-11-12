"""Meetings from invitations at their edges: invitations of a normal size read again, hostile zones, odd files,
lookalike senders, and what a held, declined or answered invitation leaves on the calendar."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from controller_inbox import calendar_events as cal
from controller_inbox.models import RawAttachment, RawMessage

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def _ics(*events: str, method: str = "REQUEST", zones: str = "") -> str:
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nMETHOD:" + method + "\r\n" + zones + "".join(events) + "END:VCALENDAR\r\n"


def _event(*lines: str) -> str:
    return "BEGIN:VEVENT\r\n" + "".join(line + "\r\n" for line in lines) + "END:VEVENT\r\n"


EASTERN = (
    "BEGIN:VTIMEZONE\r\nTZID:Eastern Standard Time\r\nBEGIN:STANDARD\r\nDTSTART:16010101T020000\r\nTZOFFSETFROM:-0400\r\n"
    "TZOFFSETTO:-0500\r\nRRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=1SU;BYMONTH=11\r\nEND:STANDARD\r\nBEGIN:DAYLIGHT\r\n"
    "DTSTART:16010101T020000\r\nTZOFFSETFROM:-0500\r\nTZOFFSETTO:-0400\r\nRRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=2SU;BYMONTH=3\r\n"
    "END:DAYLIGHT\r\nEND:VTIMEZONE\r\n"
)


def _outlook_sized_weekly() -> bytes:
    """A weekly invitation as Outlook writes one: its zone, a long folded agenda, about 3.5 KB in all."""
    agenda = "DESCRIPTION;LANGUAGE=en-US:" + "Agenda: review the AP aging, accruals and the close checklist. " * 40
    folded = "\r\n ".join(agenda[i:i + 73] for i in range(0, len(agenda), 73))
    return _ics(_event(
        "ORGANIZER;CN=Maya Chen:mailto:maya@taz.com", "ATTENDEE;CN=Dana:mailto:dana@taz.com", folded,
        "RRULE:FREQ=WEEKLY;BYDAY=MO;INTERVAL=1", "UID:weekly-close@taz.com", "SUMMARY;LANGUAGE=en-US:Weekly close sync",
        "DTSTART;TZID=Eastern Standard Time:20261005T100000", "DTEND;TZID=Eastern Standard Time:20261005T103000", "SEQUENCE:0",
    ), zones=EASTERN).encode()


def _read_invitation(store, settings, data: bytes, *, email_id="m1", filename="invite.ics", content_type="text/calendar",
                     subject="Weekly close sync", sender="maya@taz.com", received=NOW):
    from controller_inbox.pipeline import process_message

    settings.ensure_data_dir()
    raw = RawMessage(id=email_id, subject=subject, sender_name="Maya Chen", sender_email=sender, received_at=received,
                     body_text="Join us.", body_preview="Join us.", has_attachments=True, source="folder",
                     attachments=[RawAttachment(id="0", filename=filename, content_type=content_type, size_bytes=len(data), content=data)])
    process_message(raw, store, settings, now=received)


def test_an_outlook_sized_invitation_is_read_again_overnight_after_an_update_and_when_a_hold_is_cleared(store, settings):
    data = _outlook_sized_weekly()
    assert len(data) > 3000
    _read_invitation(store, settings, data)
    assert store.get_email("m1").attachments[0].extracted_text.rstrip().endswith("END:VCALENDAR"), "kept whole"
    assert store.meetings_between("2026-10-01", "2026-10-31")

    later = NOW + timedelta(days=31)
    assert cal.refresh_invites(store, settings, now=later) == 1
    assert store.meetings_between(later.date().isoformat(), (later + timedelta(days=30)).date().isoformat()), "overnight"

    with store.connect() as conn:
        conn.execute("DELETE FROM meetings")  # as a version that kept no meetings stored it
    cal.catch_up_invites(store, settings, now=NOW)
    assert store.meetings_between("2026-10-01", "2026-10-31"), "after an update"

    email = store.get_email("m1")
    email.flags = [*email.flags, "fraud_risk"]
    store.upsert_email(email)
    cal.follow_hold(store, settings, store.get_email("m1"), now=NOW)
    assert store.held_markers("m1") and not store.meetings_between("2026-10-01", "2026-10-31")
    email = store.get_email("m1")
    email.flags = [*email.flags, "fraud_cleared"]
    store.upsert_email(email)
    cal.follow_hold(store, settings, store.get_email("m1"), now=NOW)
    assert store.meetings_between("2026-10-01", "2026-10-31"), "a cleared hold"


def test_an_invitation_stored_cut_off_is_read_again_from_its_kept_file(store, settings):
    from controller_inbox.folder_mail import safe_filename

    data = _outlook_sized_weekly()
    _read_invitation(store, settings, data)
    email = store.get_email("m1")
    email.attachments[0].extracted_text = data.decode()[:2000]  # as an older version stored it
    store.upsert_email(email)
    folder = settings.inbox_extracted / "m1"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / safe_filename("invite.ics")).write_bytes(data)
    found = cal._kept_invitations(store.get_email("m1"), settings, NOW)
    assert found and found[0].subject == "Weekly close sync"


def _zone(tzid: str, standard_rule: str, daylight_rule: str = "FREQ=YEARLY;BYDAY=2SU;BYMONTH=3", extra: str = "") -> str:
    return (
        f"BEGIN:VTIMEZONE\r\nTZID:{tzid}\r\n{extra}BEGIN:STANDARD\r\nDTSTART:16010101T020000\r\nTZOFFSETFROM:-0400\r\n"
        f"TZOFFSETTO:-0500\r\nRRULE:{standard_rule}\r\nEND:STANDARD\r\nBEGIN:DAYLIGHT\r\nDTSTART:16010101T020000\r\n"
        f"TZOFFSETFROM:-0500\r\nTZOFFSETTO:-0400\r\nRRULE:{daylight_rule}\r\nEND:DAYLIGHT\r\nEND:VTIMEZONE\r\n"
    )


def test_a_zone_whose_rules_repeat_every_hour_or_never_come_true_is_read_in_a_moment():
    from time import monotonic

    for rule in ("FREQ=SECONDLY", "FREQ=HOURLY", "FREQ=DAILY;BYMONTH=2;BYMONTHDAY=31", "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=31"):
        data = _ics(_event("UID:a", "SUMMARY:x", "DTSTART;TZID=Customized Time Zone:20261026T090000",
                           "DTEND;TZID=Customized Time Zone:20261026T100000"), zones=_zone("Customized Time Zone", rule))
        began = monotonic()
        [meeting] = cal.from_ics(data, now=NOW)
        assert monotonic() - began < 1, rule
        assert meeting.start == "2026-10-26T14:00:00+00:00", "read at the zone's standard offset"
    # Many zones, each built to take seconds, in one file.
    zones = "".join(_zone(f"Z{n}", "FREQ=DAILY;BYMONTH=2;BYMONTHDAY=31", "FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30") for n in range(10))
    events = [_event(f"UID:u{n}", f"DTSTART;TZID=Z{n}:20261026T090000") for n in range(10)]
    began = monotonic()
    assert len(cal.from_ics(_ics(*events, zones=zones), now=NOW)) == 10
    assert monotonic() - began < 2


def test_a_zone_with_outlooks_rules_keeps_its_daylight_saving():
    data = _ics(_event("UID:a", "DTSTART;TZID=Customized Time Zone:20261019T090000", "RRULE:FREQ=WEEKLY;COUNT=4"),
                zones=_zone("Customized Time Zone", "FREQ=YEARLY;INTERVAL=1;BYDAY=1SU;BYMONTH=11"))
    assert [m.start[11:16] for m in cal.from_ics(data, now=NOW)] == ["13:00", "13:00", "14:00", "14:00"]


def test_a_thunderbird_or_sogo_zone_with_its_own_properties_is_still_read(monkeypatch):
    zone = (
        "BEGIN:VTIMEZONE\r\nTZID:/inverse.ca/20091015_1/Custom/Place\r\nX-LIC-LOCATION:Custom/Place\r\nBEGIN:DAYLIGHT\r\n"
        "TZOFFSETFROM:-0500\r\nTZOFFSETTO:-0400\r\nTZNAME;LANGUAGE=en:EDT\r\nDTSTART:19700308T020000\r\n"
        "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU\r\nEND:DAYLIGHT\r\nBEGIN:STANDARD\r\nTZOFFSETFROM:-0400\r\nTZOFFSETTO:-0500\r\n"
        "TZNAME:EST\r\nDTSTART:19701101T020000\r\nRRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU\r\nEND:STANDARD\r\nEND:VTIMEZONE\r\n"
    )
    broken = "BEGIN:VTIMEZONE\r\nTZID:Broken\r\nBEGIN:STANDARD\r\nEND:STANDARD\r\nEND:VTIMEZONE\r\n"
    data = _ics(_event("UID:s", "DTSTART;TZID=/inverse.ca/20091015_1/Custom/Place:20261008T100000"), zones=broken + zone)
    [meeting] = cal.from_ics(data, now=NOW)
    assert meeting.start == "2026-10-08T14:00:00+00:00", "10:00 at UTC-4"
    # A path-like name ending in a real zone is that zone.
    data = _ics(_event("UID:m", "DTSTART;TZID=/mozilla.org/20050126_1/America/New_York:20261208T100000"))
    [meeting] = cal.from_ics(data, now=NOW)
    assert meeting.start == "2026-12-08T15:00:00+00:00"


def test_unknown_zone_names_are_looked_up_once(monkeypatch):
    looked = []
    real = cal.zone_named
    monkeypatch.setattr(cal, "zone_named", lambda name: looked.append(name) or real(name))
    lines = "".join(f"X-NOTE;TZID=Z{n}:1\r\n" for n in range(500)) + "X-NOTE;TZID=Nowhere/Land:1\r\n" * 500
    events = [_event(f"UID:u{n}", "DTSTART;TZID=Nowhere/Land:20261009T140000") for n in range(50)]
    cal.from_ics(_ics(*events, zones=lines), now=NOW)
    assert len(looked) <= cal.MAX_ZONES + 1


def _record(email_id, *, sender="maya@taz.com", flags=(), received="2026-10-05T14:00:00+00:00", subject=None, source="folder"):
    from controller_inbox.models import DocumentType, EmailRecord, ExtractedFields, Importance

    return EmailRecord(
        id=email_id, subject=subject or email_id, sender_name="", sender_email=sender, received_at=received, body_text="",
        body_preview="", has_attachments=False, outlook_importance="normal", is_read=False, category=DocumentType.OTHER,
        category_confidence=0, importance=Importance.LOW, importance_score=0, flags=list(flags), extracted=ExtractedFields(),
        source=source,
    )


def test_outlooks_copy_of_a_held_weekly_invitation_stays_hidden_past_the_weeks_it_named(store, settings):
    from dataclasses import replace

    from controller_inbox.calendar_events import Meeting

    store.upsert_email(_record("held", sender="ceo@taz-corp.co", flags=["fraud_risk"]))
    data = _ics(_event("UID:U1", "SUMMARY:Urgent wire approval call", "DTSTART:20261006T140000Z", "DTEND:20261006T150000Z",
                       "RRULE:FREQ=WEEKLY", "ORGANIZER;CN=CEO:mailto:ceo@taz-corp.co"))
    store.save_invites("held", cal.from_ics(data, email_id="held", now=NOW), sender="ceo@taz-corp.co", held=True, sender_name="CEO")
    day = datetime(2027, 4, 27, 14, tzinfo=timezone.utc)  # Outlook expands the series itself, with no end
    copy = Meeting(uid="U1", start=day.isoformat(timespec="seconds"), end=(day + timedelta(hours=1)).isoformat(timespec="seconds"),
                   subject="Urgent wire approval call", organizer="CEO", busy="tentative", source="outlook", recurring=True)
    store.replace_meetings("outlook", [copy], start="2027-01-01T00:00:00+00:00", end="2027-10-01T00:00:00+00:00")
    assert store.meetings_between("2027-04-26", "2027-04-28") == []
    # Held after it was read: the same.
    store.upsert_email(_record("later", sender="ceo@taz-corp.co"))
    store.save_invites("later", cal.from_ics(data.replace("UID:U1", "UID:U2"), email_id="later", now=NOW), sender="ceo@taz-corp.co")
    store.upsert_email(_record("later", sender="ceo@taz-corp.co", flags=["fraud_risk"]))
    store.hold_invites("later")
    store.replace_meetings("outlook", [copy, replace(copy, uid="U2")], start="2027-01-01T00:00:00+00:00", end="2027-10-01T00:00:00+00:00")
    assert store.meetings_between("2027-04-26", "2027-04-28") == []
