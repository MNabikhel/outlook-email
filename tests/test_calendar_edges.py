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


def _invite(uid, start, *, subject="Budget review", method="REQUEST", sequence=0, organizer="mailto:maya@taz.com", end=""):
    lines = [f"UID:{uid}", f"SUMMARY:{subject}", f"DTSTART:{start}", f"SEQUENCE:{sequence}", f"ORGANIZER:{organizer}"]
    return _ics(_event(*lines, *([f"DTEND:{end}"] if end else [])), method=method)


def _save(store, email_id, data, sender, received="2026-10-01T09:00:00+00:00", **record):
    store.upsert_email(_record(email_id, sender=sender, received=received, **record))
    store.save_invites(email_id, cal.from_ics(data, email_id=email_id, now=NOW), sender=sender, received=received)


def test_a_lookalikes_invitation_says_to_check_who_sent_it_on_its_own_day(store):
    _save(store, "real", _invite("U2", "20261006T140000Z"), "maya@taz.com")
    _save(store, "fake", _invite("U2", "20261007T140000Z", subject="Updated: Budget review", sequence=1, organizer="mailto:maya@taz-c0rp.com"),
          "maya@taz-c0rp.com")
    day = cal.with_cautions(store, store.meetings_between("2026-10-07T00:00:00+00:00", "2026-10-08T00:00:00+00:00"))
    assert [(m["email_id"], bool(m["caution"])) for m in day] == [("fake", True)]
    day = cal.with_cautions(store, store.meetings_between("2026-10-06T00:00:00+00:00", "2026-10-07T00:00:00+00:00"))
    assert [(m["email_id"], bool(m["caution"])) for m in day] == [("real", True)]


def test_another_free_mail_user_cant_move_a_free_mail_organizers_meeting(store):
    _save(store, "real", _invite("G1", "20261009T140000Z", organizer="mailto:maya.chen.cpa@gmail.com"), "maya.chen.cpa@gmail.com")
    _save(store, "other", _invite("G1", "20261009T200000Z", sequence=3, subject="Urgent: wire approval call, dial +1 555 0100",
                                  organizer="mailto:maya.chen.cpa@gmail.com"), "billing.dept.payments@gmail.com", received="2026-10-02T09:00:00+00:00")
    shown = cal.with_cautions(store, store.meetings_between("2026-10-09", "2026-10-10"))
    assert [(m["email_id"], m["subject"]) for m in shown] == [("real", "Budget review")]
    # The organizer's own update still moves it.
    _save(store, "update", _invite("G1", "20261009T160000Z", sequence=1, organizer="mailto:maya.chen.cpa@gmail.com"), "maya.chen.cpa@gmail.com",
          received="2026-10-03T09:00:00+00:00")
    assert [m["email_id"] for m in store.meetings_between("2026-10-09", "2026-10-10")] == ["update"]


def test_a_forward_and_the_organizers_own_word_on_one_week_say_nothing_doubtful(store):
    vendor = "mailto:pat@vendor.com"
    series = _ics(_event("UID:wk", "SUMMARY:AP standup", "DTSTART:20261005T140000Z", "DTEND:20261005T143000Z", "RRULE:FREQ=WEEKLY;COUNT=4",
                         f"ORGANIZER:{vendor}"))
    week = _ics(_event("UID:wk", "RECURRENCE-ID:20261019T140000Z", "SUMMARY:AP standup", "DTSTART:20261019T140000Z",
                       "DTEND:20261019T143000Z", "SEQUENCE:1", f"ORGANIZER:{vendor}", "STATUS:CANCELLED"), method="CANCEL")
    _save(store, "fwd", series, "maya@taz.com")
    _save(store, "cancel", week, "pat@vendor.com", received="2026-10-02T09:00:00+00:00")
    shown = cal.with_cautions(store, store.meetings_between("2026-10-01", "2026-11-03"))
    assert len(shown) == 4 and not any(m["caution"] for m in shown)
    # A second forwarding domain naming the same organizer is doubtful.
    _save(store, "fwd2", series.replace("AP standup", "AP standup, new dial-in"), "maya@taz-c0rp.com", received="2026-10-03T09:00:00+00:00")
    shown = cal.with_cautions(store, store.meetings_between("2026-10-01", "2026-11-03"))
    assert shown and all(m["caution"] for m in shown)


def _weekly(sequence=0, method="REQUEST", count=6):
    return _ics(_event("UID:wk", "SUMMARY:AP standup", "DTSTART:20261005T140000Z", "DTEND:20261005T143000Z", f"RRULE:FREQ=WEEKLY;COUNT={count}",
                       f"SEQUENCE:{sequence}", "ORGANIZER:mailto:maya@taz.com"), method=method)


def test_a_whole_series_cancelled_after_one_week_moved_cancels_that_week_too(store):
    _save(store, "series", _weekly(), "maya@taz.com")
    _save(store, "move", _ics(_event("UID:wk", "RECURRENCE-ID:20261019T140000Z", "SUMMARY:AP standup", "DTSTART:20261020T150000Z",
                                     "DTEND:20261020T153000Z", "SEQUENCE:1", "ORGANIZER:mailto:maya@taz.com")),
          "maya@taz.com", received="2026-10-02T09:00:00+00:00")
    assert any(m["start"].startswith("2026-10-20T15") for m in store.meetings_between("2026-10-01", "2026-11-30"))
    _save(store, "cancel", _weekly(1, method="CANCEL"), "maya@taz.com", received="2026-10-03T09:00:00+00:00")
    shown = store.meetings_between("2026-10-01", "2026-11-30")
    assert len(shown) == 6 and all(m["cancelled"] for m in shown)


class _EventClient:
    """Microsoft 365 as it gives a meeting request: an eventMessage with no file attached, its calendar only in its
    MIME (``$value``)."""

    def __init__(self, mime: bytes):
        self.mime, self.asked = mime, []

    def _user_root(self) -> str:
        return "/me"

    def get_json(self, path, params=None):
        self.asked.append(path)
        if path.endswith("/attachments"):
            return {"value": []}
        return {"value": [{
            "@odata.type": "#microsoft.graph.eventMessageRequest", "id": "EV1", "subject": "Q3 close review",
            "receivedDateTime": "2026-10-05T14:00:00Z", "from": {"emailAddress": {"name": "Maya Chen", "address": "maya@taz.com"}},
            "body": {"contentType": "text", "content": "Let's review the close."}, "hasAttachments": False,
            "importance": "normal", "isRead": False,
        }]}

    def get_bytes(self, path):
        self.asked.append(path)
        assert path == "/me/messages/EV1/$value"
        return self.mime


def test_a_meeting_request_read_from_microsoft_365_reaches_the_calendar_and_asks_for_an_answer(store, settings):
    from email.message import EmailMessage

    from controller_inbox.graph import GraphMailbox
    from controller_inbox.models import DocumentType
    from controller_inbox.pipeline import ingest_mailbox

    message = EmailMessage()
    message["From"], message["Subject"] = "Maya Chen <maya@taz.com>", "Q3 close review"
    message.set_content("Let's review the close.")
    message.add_alternative(_invite("Q3", "20261008T150000Z", subject="Q3 close review", end="20261008T160000Z"),
                            subtype="calendar", params={"method": "REQUEST"})
    settings.ensure_data_dir()
    [record] = ingest_mailbox(GraphMailbox(_EventClient(bytes(message))), store, settings, now=NOW)
    assert [m["subject"] for m in store.meetings_between("2026-10-08", "2026-10-09")] == ["Q3 close review"]
    email = store.get_email(record.id)
    assert email.category == DocumentType.MEETING
    assert any(action.source == "meeting" for action in email.actions)


def test_a_meeting_declined_or_deleted_in_outlook_doesnt_come_back_from_its_invitation(store):
    from controller_inbox import outlook_sync
    from controller_inbox.calendar_events import Meeting

    window = ("2026-10-01T00:00:00+00:00", "2026-10-20T00:00:00+00:00")
    _save(store, "req", _invite("D1", "20261008T140000Z", subject="Vendor pitch", end="20261008T150000Z",
                                organizer="mailto:sam@vendor.example"), "sam@vendor.example")
    _save(store, "kept", _invite("D2", "20261009T140000Z", subject="Forwarded file", end="20261009T150000Z"), "maya@taz.com")
    outlook_sync.connect(store, calendar=True, now=NOW)
    on = outlook_sync.calendar_on(store)
    mine = Meeting(uid="O1", start="2026-10-07T14:00:00+00:00", end="2026-10-07T15:00:00+00:00", subject="Mine", source="outlook")
    copy = Meeting(uid="D1", start="2026-10-08T14:00:00+00:00", end="2026-10-08T15:00:00+00:00", subject="Vendor pitch", source="outlook",
                   busy="tentative")
    store.replace_meetings("outlook", [mine, copy], start=window[0], end=window[1])
    assert [(m["subject"], m["source"]) for m in store.meetings_between(*window, outlook=on)] == [
        ("Mine", "outlook"), ("Vendor pitch", "outlook"), ("Forwarded file", "invite")]
    # Declined in Outlook: Outlook takes it off its calendar and deletes the request.
    store.replace_meetings("outlook", [mine], start=window[0], end=window[1])
    store.delete_emails(["req"])
    assert [m["subject"] for m in store.meetings_between(*window, outlook=on)] == ["Mine", "Forwarded file"], (
        "an invitation Outlook never had stays")
    # Accepted again (or the calendar turned off): it shows.
    store.replace_meetings("outlook", [mine, copy], start=window[0], end=window[1])
    assert "Vendor pitch" in [m["subject"] for m in store.meetings_between(*window, outlook=on)]
    store.replace_meetings("outlook", [mine], start=window[0], end=window[1])
    outlook_sync.set_calendar(store, False)
    assert [m["subject"] for m in store.meetings_between(*window, outlook=outlook_sync.calendar_on(store))] == ["Vendor pitch", "Forwarded file"]
