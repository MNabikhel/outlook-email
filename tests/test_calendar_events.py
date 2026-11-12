"""Meetings from invitations (.ics) and Outlook meeting requests: times, zones, repeats, moves, cancellations."""

from __future__ import annotations

from datetime import datetime, timezone

from controller_inbox import calendar_events as cal

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def _ics(*events: str, method: str = "REQUEST", zones: str = "") -> str:
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nMETHOD:" + method + "\r\n" + zones + "".join(events) + "END:VCALENDAR\r\n"


def _event(*lines: str) -> str:
    return "BEGIN:VEVENT\r\n" + "".join(line + "\r\n" for line in lines) + "END:VEVENT\r\n"


def test_a_meeting_in_utc_with_its_people_and_place():
    data = _ics(_event(
        "UID:abc-123@taz.com", "SUMMARY:Q4 planning review", "DTSTART:20261008T180000Z", "DTEND:20261008T190000Z",
        "LOCATION:Board room\\, 4th floor", 'ORGANIZER;CN="Maya Chen":mailto:maya@taz.com',
        "ATTENDEE;CN=Priya Raman;ROLE=REQ-PARTICIPANT:mailto:priya@taz.com", "ATTENDEE:mailto:ap@taz.com",
        "BEGIN:VALARM", "TRIGGER:-PT15M", "DESCRIPTION:Reminder", "END:VALARM",
    ))
    [meeting] = cal.from_ics(data, email_id="e1", now=NOW)
    assert (meeting.subject, meeting.start, meeting.end) == ("Q4 planning review", "2026-10-08T18:00:00+00:00", "2026-10-08T19:00:00+00:00")
    assert meeting.location == "Board room, 4th floor" and meeting.organizer == "Maya Chen <maya@taz.com>"
    assert meeting.attendees == ["Priya Raman <priya@taz.com>", "ap@taz.com"] and meeting.email_id == "e1"
    assert not meeting.all_day and not meeting.cancelled


def test_a_windows_zone_name_and_a_folded_line():
    data = _ics(_event(
        "UID:x1", "SUMMARY:Vendor call with Northwind about the INV-10482 dispute and the credit ", " memo",
        "DTSTART;TZID=Eastern Standard Time:20261008T100000", "DTEND;TZID=Eastern Standard Time:20261008T103000",
    ))
    [meeting] = cal.from_ics(data, now=NOW)
    assert meeting.start == "2026-10-08T14:00:00+00:00", "10am Eastern (daylight time) is 14:00 UTC"
    assert meeting.subject.endswith("credit memo")


def test_an_all_day_meeting_keeps_its_dates():
    [meeting] = cal.from_ics(_ics(_event("UID:x2", "SUMMARY:Close day", "DTSTART;VALUE=DATE:20261030", "DTEND;VALUE=DATE:20261031")), now=NOW)
    assert (meeting.start, meeting.end, meeting.all_day) == ("2026-10-30", "2026-10-31", True)


def test_a_weekly_meeting_is_each_week_with_a_moved_week_and_a_skipped_one():
    data = _ics(
        _event("UID:wk", "SUMMARY:AP standup", "DTSTART:20261005T140000Z", "DTEND:20261005T141500Z",
               "RRULE:FREQ=WEEKLY;COUNT=4", "EXDATE:20261019T140000Z"),
        _event("UID:wk", "RECURRENCE-ID:20261012T140000Z", "SUMMARY:AP standup (moved)", "DTSTART:20261013T150000Z", "DTEND:20261013T151500Z"),
    )
    meetings = cal.from_ics(data, now=NOW)
    assert [(m.start, m.subject) for m in meetings] == [
        ("2026-10-05T14:00:00+00:00", "AP standup"),
        ("2026-10-13T15:00:00+00:00", "AP standup (moved)"),
        ("2026-10-26T14:00:00+00:00", "AP standup"),
    ]
    assert all(m.recurring and m.uid == "wk" for m in meetings)
    assert len({m.id for m in meetings}) == 3, "each week its own row"


def test_a_cancellation_cancels_and_a_reply_is_no_meeting():
    event = _event("UID:c1", "SUMMARY:Budget review", "DTSTART:20261009T150000Z", "DTEND:20261009T160000Z")
    [cancelled] = cal.from_ics(_ics(event, method="CANCEL"), now=NOW)
    assert cancelled.cancelled
    assert cal.from_ics(_ics(event, method="REPLY"), now=NOW) == []
    [status] = cal.from_ics(_ics(_event("UID:c2", "STATUS:CANCELLED", "DTSTART:20261009T150000Z")), now=NOW)
    assert status.cancelled and status.end == status.start


def test_a_repeating_meeting_without_end_stops_at_the_window():
    meetings = cal.from_ics(_ics(_event("UID:d", "SUMMARY:Daily", "DTSTART:20250101T130000Z", "DTEND:20250101T131500Z", "RRULE:FREQ=DAILY")), now=NOW)
    assert 0 < len(meetings) <= cal.MAX_OCCURRENCES
    assert meetings[0].start >= "2026-07-07" and meetings[-1].start <= "2027-04-04"


def test_a_garbled_file_gives_no_meetings_rather_than_an_error():
    assert cal.from_ics(b"\xff\xfe not a calendar", now=NOW) == []
    assert cal.from_ics(_ics(_event("UID:z", "SUMMARY:No start")), now=NOW) == []


def test_outlooks_global_id_carries_another_calendars_uid():
    wrapped = bytes.fromhex("040000008200E00074C5B7101A82E008") + b"\x00" * 20 + b"vCal-Uid\x01\x00\x00\x00abc-123@google.com\x00"
    assert cal.invitation_uid(wrapped) == "abc-123@google.com"
    assert cal.invitation_uid(bytes(range(16))) == bytes(range(16)).hex().upper()


def test_the_calendar_page_s_data_and_a_meeting_with_its_mail(loaded, settings):
    from fastapi.testclient import TestClient
    from helpers import PAGE

    from controller_inbox.web import create_app

    client = TestClient(create_app(settings, loaded), headers=PAGE)
    week = client.get("/api/calendar?start=2026-09-21&end=2026-09-27").json()
    subjects = [m["subject"] for m in week["meetings"]]
    assert subjects.count("AP team standup") == 5 and "Northwind invoice INV-10482 review" in subjects
    assert "Q4 planning review" in subjects, "the sample invitation's meeting"
    standup = next(m for m in week["meetings"] if m["subject"] == "AP team standup")
    assert standup["start"].endswith("T09:30"), "shown in the app's time zone (New York)"
    review = next(m for m in week["meetings"] if m["subject"].startswith("Northwind"))
    detail = client.get(f"/api/calendar/{review['id']}").json()
    assert {"demo-inv-10482", "demo-inv-10482-dup"} <= {card["id"] for card in detail["related"]}
    invite = next(m for m in week["meetings"] if m["subject"] == "Q4 planning review")
    assert client.get(f"/api/calendar/{invite['id']}").json()["related"][0]["id"] == "demo-meeting"
    assert client.get("/api/calendar?start=2026-09-01&end=2026-12-31").status_code == 400
    assert client.get("/api/calendar/mtg-nope").status_code == 404
    today = client.get("/api/focus").json()["meetings_today"]
    assert [m["subject"] for m in today] == ["AP team standup", "Northwind invoice INV-10482 review"]


def test_ask_closedesk_reads_the_calendar_for_a_question_about_meetings(loaded, settings, monkeypatch):
    from controller_inbox import assistant

    events = list(assistant.answer_stream(loaded, settings, "What meetings do I have tomorrow?", today="2026-09-22"))
    sources = next(e for e in events if e["type"] == "sources")["sources"]
    assert sources[0]["subject"] == "Your calendar" and sources[0]["calendar"]
    text = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "Wed Sep 23, 9:30am–9:45am · AP team standup" in text and "PBC list walkthrough" in text
    assert "Northwind" not in text, "tomorrow's meetings only"
    monday = "".join(e["text"] for e in assistant.answer_stream(loaded, settings, "Am I free on Monday?", today="2026-09-22") if e["type"] == "delta")
    assert "Mon Sep 28" in monday and "Sep 23" not in monday, "the coming Monday"

    asked = []

    def stream(_settings, messages, **_kwargs):
        asked.append(messages[-1]["content"])
        yield "You have the AP team standup at 9:30 and the PBC walkthrough at 11 [1]."

    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "stream_text", stream)
    monkeypatch.setattr(assistant, "chat_with_tools", lambda *_a, **_k: (_ for _ in ()).throw(assistant.ToolsUnsupported("no tools")))
    list(assistant.answer_stream(loaded, settings, "What's on my calendar tomorrow?", today="2026-09-22"))
    assert any("Times are America/New_York" in prompt and "PBC list walkthrough with the auditors" in prompt for prompt in asked)
    assert loaded.findings("calendar") == []
    assert not assistant.asks_about_meetings("What is the amount due on INV-10482?")


def _invite(uid, start, *, subject="Budget review", method="REQUEST", sequence=0, organizer="mailto:maya@taz.com"):
    return _ics(_event(f"UID:{uid}", f"SUMMARY:{subject}", f"DTSTART:{start}", f"SEQUENCE:{sequence}", f"ORGANIZER:{organizer}"), method=method)


def test_an_update_moves_a_meeting_and_a_cancellation_cancels_it_from_the_same_sender_only(store):
    store.save_invites("e1", cal.from_ics(_invite("X", "20261009T140000Z"), email_id="e1", now=NOW), sender="maya@taz.com", received="2026-10-01T09:00:00+00:00")
    store.save_invites("e2", cal.from_ics(_invite("X", "20261009T180000Z", sequence=1), email_id="e2", now=NOW), sender="maya@taz.com", received="2026-10-02T09:00:00+00:00")
    assert [m["start"] for m in store.meetings_between("2026-10-09", "2026-10-10")] == ["2026-10-09T18:00:00+00:00"], "moved, not shown twice"
    # Someone outside who knows the uid can't cancel or move it.
    store.save_invites("e3", cal.from_ics(_invite("X", "20261009T180000Z", method="CANCEL", sequence=5), email_id="e3", now=NOW), sender="x@evil.example", received="2026-10-03T09:00:00+00:00")
    kept = store.meetings_between("2026-10-09", "2026-10-10")
    assert [(m["start"], m["cancelled"], m["sender"]) for m in kept] == [("2026-10-09T18:00:00+00:00", False, "maya@taz.com")]
    store.save_invites("e4", cal.from_ics(_invite("X", "20261009T180000Z", method="CANCEL", sequence=2), email_id="e4", now=NOW), sender="maya@taz.com", received="2026-10-04T09:00:00+00:00")
    assert [m["cancelled"] for m in store.meetings_between("2026-10-09", "2026-10-10")] == [True]
    # Read again later, the first invitation doesn't bring the old time back.
    store.save_invites("e1", cal.from_ics(_invite("X", "20261009T140000Z"), email_id="e1", now=NOW), sender="maya@taz.com", received="2026-10-01T09:00:00+00:00")
    assert [m["start"] for m in store.meetings_between("2026-10-09", "2026-10-10")] == ["2026-10-09T18:00:00+00:00"]


def test_which_invitation_counts_is_decided_when_the_calendar_is_read(store):
    """An update read before the invitation it replaces still counts; the organizer's word beats a forward of it,
    and neither removes the other's rows; a lookalike's "update" never counts once the organizer has written."""
    window = ("2026-10-09", "2026-10-10")
    vendor = "mailto:priya@northwind.com"
    store.save_invites("upd", cal.from_ics(_invite("Y", "20261009T180000Z", sequence=1, organizer=vendor), email_id="upd", now=NOW),
                       sender="priya@northwind.com", received="2026-10-02T09:00:00+00:00")
    store.save_invites("orig", cal.from_ics(_invite("Y", "20261009T140000Z", organizer=vendor), email_id="orig", now=NOW),
                       sender="priya@northwind.com", received="2026-10-01T09:00:00+00:00")
    assert [m["start"] for m in store.meetings_between(*window)] == ["2026-10-09T18:00:00+00:00"], "read out of order"

    # A colleague's forward read first counts only until the organizer's own invitation arrives.
    store.save_invites("fwd", cal.from_ics(_invite("Z", "20261009T150000Z", organizer=vendor), email_id="fwd", now=NOW),
                       sender="maya@taz.com", received="2026-10-01T08:00:00+00:00")
    assert [m["email_id"] for m in store.meetings_between(*window) if m["uid"] == "Z"] == ["fwd"]
    store.save_invites("own", cal.from_ics(_invite("Z", "20261009T160000Z", organizer=vendor), email_id="own", now=NOW),
                       sender="priya@northwind.com", received="2026-10-01T07:00:00+00:00")
    assert [m["email_id"] for m in store.meetings_between(*window) if m["uid"] == "Z"] == ["own"]
    store.save_invites("fake", cal.from_ics(_invite("Z", "20261009T200000Z", method="CANCEL", sequence=9, organizer="mailto:priya@northwlnd.com"),
                                            email_id="fake", now=NOW), sender="priya@northwlnd.com", received="2026-10-03T09:00:00+00:00")
    assert [(m["email_id"], m["cancelled"]) for m in store.meetings_between(*window) if m["uid"] == "Z"] == [("own", False)]
    assert store.meeting(next(m["id"] for m in store.meetings_between(*window) if m["uid"] == "Z")) is not None
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(DISTINCT email_id) FROM meetings WHERE uid = 'Z'").fetchone()[0] == 3, "nothing removed"


def test_an_answered_invitation_deleted_in_outlook_keeps_its_meeting_and_a_flood_keeps_outlooks_own(store):
    from controller_inbox.calendar_events import Meeting
    from controller_inbox.models import DocumentType, EmailRecord, ExtractedFields, Importance

    record = EmailRecord(id="inv", subject="Budget review", sender_name="Maya", sender_email="maya@taz.com", received_at="2026-10-01T09:00:00+00:00",
                         body_text="", body_preview="", has_attachments=True, outlook_importance="normal", is_read=True,
                         category=DocumentType.MEETING, category_confidence=0.5, importance=Importance.LOW, importance_score=1,
                         importance_reasons=[], flags=[], extracted=ExtractedFields(), source="outlook")
    store.upsert_email(record)
    store.save_invites("inv", cal.from_ics(_invite("K", "20261009T140000Z"), email_id="inv", now=NOW), sender="maya@taz.com", received=record.received_at)
    store.delete_emails(["inv"])
    assert [m["uid"] for m in store.meetings_between("2026-10-09", "2026-10-10")] == ["K"]

    flood = [Meeting(uid=f"f{n}", start="2026-10-12T09:00:00+00:00", end="2026-10-12T10:00:00+00:00", subject="x", source="invite", email_id="flood")
             for n in range(3000)]
    store.save_invites("flood", flood, sender="spam@x.example", received="2026-10-02T09:00:00+00:00")
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM meetings WHERE email_id = 'flood'").fetchone()[0] == store_module().MAX_INVITE_ROWS
    store.replace_meetings("outlook", [Meeting(uid="o1", start="2026-10-12T23:00:00+00:00", end="2026-10-12T23:30:00+00:00", subject="Mine", source="outlook")],
                           start="2026-10-12T00:00:00+00:00", end="2026-10-13T00:00:00+00:00")
    shown = store.meetings_between("2026-10-12", "2026-10-13", limit=50)
    assert len(shown) == 50 and any(m["uid"] == "o1" for m in shown)


def store_module():
    from controller_inbox import store

    return store


def test_an_invitation_on_held_mail_is_no_meeting(loaded, settings):
    from controller_inbox import assistant

    held = loaded.get_email("demo-bec-wire")
    loaded.save_invites(held.id, cal.from_ics(_invite("H", "20260923T150000Z", subject="Confirm new Apex account 9988776612"), email_id=held.id, now=NOW),
                        sender=held.sender_email, received=held.received_at)
    assert not any("Apex" in m["subject"] for m in loaded.meetings_between("2026-09-20", "2026-09-30"))
    source = assistant.calendar_source(loaded, settings, "2026-09-22")
    assert "9988776612" not in source.body_text
    assert "from an invitation emailed by" in source.body_text, "an invitation's meeting is the sender's words"


def test_a_weekly_meeting_keeps_its_local_time_across_the_clock_change_with_its_skipped_and_moved_weeks():
    data = _ics(
        _event("UID:ny", "SUMMARY:Close sync", "DTSTART;TZID=America/New_York:20261019T090000", "DTEND;TZID=America/New_York:20261019T093000",
               "RRULE:FREQ=WEEKLY;COUNT=5", "EXDATE;TZID=America/New_York:20261109T090000"),
        _event("UID:ny", "RECURRENCE-ID;TZID=America/New_York:20261102T090000", "SUMMARY:Close sync (moved)",
               "DTSTART;TZID=America/New_York:20261103T090000", "DTEND;TZID=America/New_York:20261103T093000"),
    )
    got = [(m.start, m.subject) for m in cal.from_ics(data, now=NOW)]
    assert got == [
        ("2026-10-19T13:00:00+00:00", "Close sync"),  # 9:00 daylight time
        ("2026-10-26T13:00:00+00:00", "Close sync"),
        ("2026-11-03T14:00:00+00:00", "Close sync (moved)"),  # 9:00 standard time, the next day
        ("2026-11-16T14:00:00+00:00", "Close sync"),  # Nov 9 skipped
    ]


def test_a_monday_evening_in_los_angeles_stays_on_monday():
    data = _ics(_event("UID:la", "SUMMARY:Pacific review", "DTSTART;TZID=America/Los_Angeles:20261005T200000",
                       "DTEND;TZID=America/Los_Angeles:20261005T210000", "RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=3"))
    from zoneinfo import ZoneInfo

    local = [datetime.fromisoformat(m.start).astimezone(ZoneInfo("America/Los_Angeles")) for m in cal.from_ics(data, now=NOW)]
    assert [(d.strftime("%a"), d.hour) for d in local] == [("Mon", 20)] * 3


def test_outlooks_own_zone_name_is_read_from_the_invitation():
    london = (
        "BEGIN:VTIMEZONE\r\nTZID:(UTC+00:00) Dublin, Edinburgh, Lisbon, London\r\n"
        "BEGIN:STANDARD\r\nDTSTART:16010101T020000\r\nTZOFFSETFROM:+0100\r\nTZOFFSETTO:+0000\r\nRRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=10\r\nEND:STANDARD\r\n"
        "BEGIN:DAYLIGHT\r\nDTSTART:16010101T010000\r\nTZOFFSETFROM:+0000\r\nTZOFFSETTO:+0100\r\nRRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=3\r\nEND:DAYLIGHT\r\n"
        "END:VTIMEZONE\r\n"
    )
    tzid = "(UTC+00:00) Dublin, Edinburgh, Lisbon, London"
    data = _ics(_event("UID:ldn", f'DTSTART;TZID="{tzid}":20261008T150000', f'DTEND;TZID="{tzid}":20261008T160000'), zones=london)
    [meeting] = cal.from_ics(data, now=NOW)
    assert meeting.start == "2026-10-08T14:00:00+00:00", "15:00 British Summer Time"
    [plain] = cal.from_ics(_ics(_event("UID:off", "DTSTART;TZID=(UTC+01:00) Amsterdam, Berlin:20261208T150000")), now=NOW)
    assert plain.start == "2026-12-08T14:00:00+00:00", "no definition in the file: the offset its name gives"


def test_a_repeat_ending_on_a_date_or_a_time_without_zone_keeps_its_occurrences():
    for until in ("20261102", "20261102T090000"):
        data = _ics(_event("UID:u", "DTSTART;TZID=America/New_York:20261005T090000", "DTEND;TZID=America/New_York:20261005T100000",
                           f"RRULE:FREQ=WEEKLY;UNTIL={until}"))
        assert len(cal.from_ics(data, now=NOW)) == 5, until


def test_an_accounting_schedule_is_no_question_about_meetings():
    from controller_inbox.assistant import asks_about_meetings

    for question in ("What is the payment schedule in the MSA?", "Show the depreciation schedule", "Did they invite us to bid?", "What's the amortization schedule?"):
        assert not asks_about_meetings(question), question
    for question in ("What meetings do I have tomorrow?", "What's on my calendar Friday?", "Am I free on Monday?", "When is my 1:1 with Priya?", "What's on my schedule today?"):
        assert asks_about_meetings(question), question


def test_outlooks_id_for_a_meeting_from_another_calendar_is_its_invitations_uid():
    wrapped = bytes.fromhex("040000008200E00074C5B7101A82E008") + b"\x00" * 20 + b"vCal-Uid\x01\x00\x00\x00abc-123@google.com\x00"
    assert cal.series_uid(wrapped.hex().upper()) == "abc-123@google.com"
    assert cal.series_uid("not-hex") == "not-hex"


def test_an_all_day_meeting_request_keeps_its_days():
    from zoneinfo import ZoneInfo

    days = (datetime(2026, 10, 30).date(), datetime(2026, 10, 31).date())
    new_york = (datetime(2026, 10, 30, 4, 0, tzinfo=timezone.utc), datetime(2026, 10, 31, 4, 0, tzinfo=timezone.utc))
    assert cal._organizer_dates(*new_york, ZoneInfo("America/New_York")) == days
    tokyo = (datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc), datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc))
    assert cal._organizer_dates(*tokyo, ZoneInfo("Asia/Tokyo")) == days
    # Auckland is 13 hours ahead in October: its midnight is 11:00 UTC the day before, which rounding gets wrong.
    auckland = (datetime(2026, 10, 29, 11, 0, tzinfo=timezone.utc), datetime(2026, 10, 30, 11, 0, tzinfo=timezone.utc))
    assert cal._organizer_dates(*auckland, ZoneInfo("Pacific/Auckland")) == days


def test_an_invitation_with_huge_names_or_endless_attendees_is_kept_small():
    huge = "A" * 5000
    lines = [f"ATTENDEE;CN={huge}:mailto:{'b' * 3000}@x.example" for _ in range(3)] + [f"ATTENDEE:mailto:p{n}@x.example" for n in range(5000)]
    [meeting] = cal.from_ics(_ics(_event("UID:a", "SUMMARY:Big", "DTSTART:20261012T150000Z", "DTEND:20261012T160000Z",
                                         f"ORGANIZER;CN={huge}:mailto:o@x.example", *lines)), now=NOW)
    assert len(meeting.attendees) == cal.MAX_ATTENDEES and all(len(person) <= 2 * cal.MAX_PERSON + 3 for person in meeting.attendees)
    assert len(meeting.organizer) <= 2 * cal.MAX_PERSON + 3


def test_an_invitation_built_to_overload_is_read_in_a_moment_and_a_broken_one_adds_nothing(store, settings):
    import time as clock

    from controller_inbox import pipeline
    from controller_inbox.models import RawAttachment, RawMessage

    started = clock.monotonic()
    every_minute = cal.from_ics(_ics(_event("UID:m", "DTSTART:20000101T000000Z", "DTEND:20000101T000100Z", "RRULE:FREQ=MINUTELY")), now=NOW)
    daily_for_decades = cal.from_ics(_ics(_event("UID:d", "DTSTART:19500101T090000Z", "DTEND:19500101T091500Z", "RRULE:FREQ=DAILY")), now=NOW)
    assert clock.monotonic() - started < 3
    assert len(every_minute) == 1 and every_minute[0].recurring
    assert 0 < len(daily_for_decades) <= cal.MAX_OCCURRENCES and daily_for_decades[-1].start >= "2027-03-01", "the window, ahead included"

    broken = _ics(_event("UID:b", "SUMMARY:Bad", "DTSTART:20261012T999999Z"))
    raw = RawMessage(id="e-broken", subject="Invoice INV-77 and a call", sender_name="Vendor", sender_email="ar@vendor.example",
                     received_at=NOW, body_text="Invoice INV-77 attached, and an invitation.", body_preview="", has_attachments=True,
                     source="folder", attachments=[RawAttachment(id="a1", filename="invite.ics", content_type="text/calendar", size_bytes=len(broken), content=broken.encode())])
    assert pipeline.invites(raw, raw.id, NOW) == [], "the email is still read; its invitation adds nothing"


def test_a_daily_rule_listing_every_minute_is_refused_and_a_twice_daily_one_is_kept():
    """A DAILY rule can list every hour, minute and second of each day: dateutil walks it from its start, so one
    from 1970 would take hours. It is kept as its first occurrence; a meeting at 9 and 2 every day is expanded."""
    import time as clock

    started = clock.monotonic()
    hostile = cal.from_ics(_ics(_event("UID:h", "DTSTART:19700101T000000Z", "DTEND:19700101T000100Z",
                                       "RRULE:FREQ=DAILY;BYHOUR=0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23;BYMINUTE=0,15,30,45")), now=NOW)
    seconds = cal.from_ics(_ics(_event("UID:s", "DTSTART:19700101T000000Z", "DTEND:19700101T000100Z", "RRULE:FREQ=WEEKLY;BYSECOND=1,2,3")), now=NOW)
    assert clock.monotonic() - started < 3
    assert len(hostile) == 1 and hostile[0].recurring and len(seconds) == 1
    twice = cal.from_ics(_ics(_event("UID:t", "DTSTART:20260901T090000Z", "DTEND:20260901T091500Z", "RRULE:FREQ=DAILY;BYHOUR=9,14;COUNT=60")), now=NOW)
    assert {m.start[11:16] for m in twice} == {"09:00", "14:00"} and len(twice) > 2


def test_a_meetings_mail_is_found_among_a_year_of_mail_with_the_same_words(store):
    import sys

    sys.path.insert(0, "tests")
    from test_links import _record

    for n in range(60):
        old = _record(f"old{n}", sender="close@taz.com", received=f"2026-0{1 + n % 6}-{1 + n % 27:02d}T09:00:00+00:00")
        old.subject = "Month-end close checklist"
        store.upsert_email(old)
    near = _record("near", sender="close@taz.com", received="2026-10-27T09:00:00+00:00")
    near.subject = "Month-end close checklist for October"
    store.upsert_email(near)
    meeting = {"subject": "Month-end close checklist review", "start": "2026-10-30T14:00:00+00:00", "organizer": "", "attendees": [], "email_id": ""}
    assert [e.id for e in cal.related_mail(store, meeting)] == ["near"]


# One week of a weekly meeting, a proposed new time, who owns a meeting, Outlook's own copy ----------------------

WEEKLY = _ics(_event("UID:wk", "SUMMARY:AP standup", "DTSTART:20261005T140000Z", "DTEND:20261005T143000Z", "RRULE:FREQ=WEEKLY;COUNT=10",
                     "SEQUENCE:0", "ORGANIZER:mailto:maya@taz.com"))


def _one_week(recurrence_id, start, end, *, sequence, method="REQUEST", cancelled=False, organizer="mailto:maya@taz.com"):
    lines = ["UID:wk", f"RECURRENCE-ID:{recurrence_id}", "SUMMARY:AP standup", f"DTSTART:{start}", f"DTEND:{end}", f"SEQUENCE:{sequence}",
             f"ORGANIZER:{organizer}"] + (["STATUS:CANCELLED"] if cancelled else [])
    return _ics(_event(*lines), method=method)


def test_cancelling_or_moving_one_week_of_a_weekly_meeting_keeps_the_other_weeks(store):
    def save(email_id, data, sender, received):
        store.save_invites(email_id, cal.from_ics(data, email_id=email_id, now=NOW), sender=sender, received=received)

    save("series", WEEKLY, "maya@taz.com", "2026-10-01T09:00:00+00:00")
    cancel = _one_week("20261019T140000Z", "20261019T140000Z", "20261019T143000Z", sequence=1, method="CANCEL", cancelled=True)
    [cancelled] = cal.from_ics(cancel, now=NOW)
    assert cancelled.occurrence == "2026-10-19T14:00:00+00:00", "the week it replaces, written as a start is"
    assert {m.occurrence for m in cal.from_ics(WEEKLY, now=NOW)} == {""}
    save("cancel", cancel, "maya@taz.com", "2026-10-02T09:00:00+00:00")
    save("move", _one_week("20261026T140000Z", "20261027T150000Z", "20261027T153000Z", sequence=2), "maya@taz.com", "2026-10-03T09:00:00+00:00")
    # Someone outside, or a lookalike of the organizer's domain, can't cancel a week of it.
    save("outsider", _one_week("20261102T140000Z", "20261102T140000Z", "20261102T143000Z", sequence=9, method="CANCEL", cancelled=True),
         "x@evil.example", "2026-10-04T09:00:00+00:00")
    save("lookalike", _one_week("20261109T140000Z", "20261109T140000Z", "20261109T143000Z", sequence=9, method="CANCEL", cancelled=True,
                                organizer="mailto:maya@taz-c0rp.com"), "maya@taz-c0rp.com", "2026-10-04T09:00:00+00:00")
    shown = store.meetings_between("2026-10-01", "2026-11-30")
    assert [(m["start"][:16], m["cancelled"]) for m in shown] == [
        ("2026-10-05T14:00", False), ("2026-10-12T14:00", False), ("2026-10-19T14:00", True), ("2026-10-27T15:00", False),
        ("2026-11-02T14:00", False), ("2026-11-09T14:00", False), ("2026-11-16T14:00", False), ("2026-11-23T14:00", False),
    ]
    assert all(store.meeting(m["id"]) is not None for m in shown)
    with store.connect() as conn:
        replaced = conn.execute("SELECT id FROM meetings WHERE email_id = 'series' AND start = '2026-10-19T14:00:00+00:00'").fetchone()[0]
    assert store.meeting(replaced) is None, "the week as the series had it is replaced"

    # The whole series sent again later (a new time for every week, as Outlook sends it, with its weeks moved or
    # cancelled since inside it): the earlier word on one week no longer stands beside it.
    save("again", _ics(_event("UID:wk", "SUMMARY:AP standup", "DTSTART:20261005T150000Z", "DTEND:20261005T153000Z", "RRULE:FREQ=WEEKLY;COUNT=10",
                              "SEQUENCE:3", "ORGANIZER:mailto:maya@taz.com")), "maya@taz.com", "2026-10-04T10:00:00+00:00")
    assert {m["start"][11:16] for m in store.meetings_between("2026-10-01", "2026-11-30")} == {"15:00"}


def test_a_proposed_new_time_and_the_answers_to_one_add_no_meeting(store):
    event = _event("UID:B1", "SUMMARY:Budget review", "DTSTART:20261009T180000Z", "DTEND:20261009T190000Z", "ORGANIZER:mailto:maya@taz.com")
    for method in ("COUNTER", "DECLINECOUNTER", "REFRESH", "REPLY"):
        assert cal.from_ics(_ics(event, method=method), now=NOW) == [], method
    assert len(cal.from_ics(_ics(event, method="PUBLISH"), now=NOW)) == 1


def test_the_organizers_own_invitations_decide_a_meeting_whichever_is_read_first(store):
    """A lookalike's invitation with the meeting's UID, read first (the drop folder reads by file name), doesn't
    take the meeting over: the domain whose own invitations name it as organizer does."""
    window = ("2026-10-01T00:00:00+00:00", "2026-10-20T00:00:00+00:00")
    store.save_invites("fake", cal.from_ics(_invite("U2", "20261007T140000Z", organizer="mailto:maya@taz-c0rp.com"), email_id="fake", now=NOW),
                       sender="maya@taz-c0rp.com", received="2026-10-02T09:00:00+00:00")
    store.save_invites("real", cal.from_ics(_invite("U2", "20261006T140000Z", sequence=1), email_id="real", now=NOW),
                       sender="maya@taz.com", received="2026-10-01T09:00:00+00:00")
    assert [(m["start"], m["email_id"]) for m in store.meetings_between(*window)] == [("2026-10-06T14:00:00+00:00", "real")]

    # Read first and dated earliest, one lookalike email is outnumbered by the organizer's invitation and update.
    store.save_invites("fake2", cal.from_ics(_invite("U4", "20261008T140000Z", organizer="mailto:maya@taz-c0rp.com"), email_id="fake2", now=NOW),
                       sender="maya@taz-c0rp.com", received="2026-09-30T09:00:00+00:00")
    store.save_invites("orig4", cal.from_ics(_invite("U4", "20261009T140000Z"), email_id="orig4", now=NOW),
                       sender="maya@taz.com", received="2026-10-01T09:00:00+00:00")
    store.save_invites("upd4", cal.from_ics(_invite("U4", "20261009T160000Z", sequence=1), email_id="upd4", now=NOW),
                       sender="maya@taz.com", received="2026-10-02T09:00:00+00:00")
    assert [m["email_id"] for m in store.meetings_between(*window) if m["uid"] == "U4"] == ["upd4"]


def test_an_invitation_for_a_meeting_on_the_outlook_calendar_adds_no_meeting_of_its_own(store):
    from controller_inbox.calendar_events import Meeting

    window = ("2026-10-01T00:00:00+00:00", "2026-10-20T00:00:00+00:00")
    store.replace_meetings("outlook", [Meeting(uid="U3", start="2026-10-06T14:00:00+00:00", end="2026-10-06T15:00:00+00:00", subject="Q4 review",
                                               organizer="maya@taz.com", source="outlook")], start=window[0], end=window[1])
    store.save_invites("fake3", cal.from_ics(_invite("U3", "20261008T140000Z", sequence=9), email_id="fake3", now=NOW),
                       sender="maya@taz-c0rp.com", received="2026-10-02T09:00:00+00:00")
    store.save_invites("own3", cal.from_ics(_invite("U3", "20261006T140000Z"), email_id="own3", now=NOW),
                       sender="maya@taz.com", received="2026-10-01T09:00:00+00:00")
    assert [(m["start"], m["source"]) for m in store.meetings_between(*window)] == [("2026-10-06T14:00:00+00:00", "outlook")]
    with store.connect() as conn:
        invited = [row[0] for row in conn.execute("SELECT id FROM meetings WHERE source = 'invite'")]
    assert invited and all(store.meeting(row) is None for row in invited)
    # Without Outlook's calendar (Disconnect, or the calendar turned off), the invitation is the meeting again.
    assert [m["email_id"] for m in store.meetings_between(*window, outlook=False)] == ["own3"]


def test_outlooks_copy_of_a_meeting_whose_invitation_is_on_held_mail_is_not_shown(store, settings):
    import sys

    from controller_inbox.calendar_events import Meeting

    sys.path.insert(0, "tests")
    from test_links import _record

    window = ("2026-10-01T00:00:00+00:00", "2026-10-20T00:00:00+00:00")
    store.upsert_email(_record("held", sender="ceo@taz-corp.co", flags=["fraud_risk"]))
    store.save_invites("held", cal.from_ics(_invite("U1", "20261006T140000Z", subject="Urgent wire approval call"), email_id="held", now=NOW),
                       sender="ceo@taz-corp.co")
    # Outlook adds a meeting request to the calendar as tentative by itself; the next calendar read brings it in.
    outlook = [Meeting(uid="U1", start="2026-10-06T14:00:00+00:00", end="2026-10-06T15:00:00+00:00", subject="Urgent wire approval call",
                       location="call +1 555 0100", busy="tentative", source="outlook"),
               Meeting(uid="U9", start="2026-10-07T14:00:00+00:00", end="2026-10-07T15:00:00+00:00", subject="Mine", source="outlook")]
    store.replace_meetings("outlook", outlook, start=window[0], end=window[1])
    assert [m["subject"] for m in store.meetings_between(*window)] == ["Mine"]
    assert store.meeting(outlook[0].id) is None and store.meeting(outlook[1].id) is not None


def test_outlooks_copy_of_an_invitation_held_as_it_arrived_is_not_shown(store, settings):
    """Mail held as it is read keeps no meeting from its invitation, only which meeting it named, so Outlook's own
    copy of that meeting isn't shown either; once the email is cleared it is."""
    from controller_inbox import pipeline
    from controller_inbox.calendar_events import Meeting
    from controller_inbox.models import RawAttachment, RawMessage

    window = ("2026-10-01T00:00:00+00:00", "2026-10-20T00:00:00+00:00")
    data = _invite("U5", "20261006T140000Z", subject="Bank change call", organizer="mailto:billing@acme-payments.net").encode()
    raw = RawMessage(id="scam", subject="Updated remittance details", sender_name="Acme Billing", sender_email="billing@acme-payments.net",
                     received_at=NOW, body_text="Our bank details have changed. Please use the following account for all payments from today: "
                     "account 5566778899 routing 021000021.", body_preview="", has_attachments=True, source="folder",
                     attachments=[RawAttachment(id="a1", filename="invite.ics", content_type="text/calendar", size_bytes=len(data), content=data)])
    record = pipeline.process_message(raw, store, settings, now=NOW)
    assert pipeline.attachments_locked(record), "the sample scam is held"
    store.replace_meetings("outlook", [Meeting(uid="U5", start="2026-10-06T14:00:00+00:00", end="2026-10-06T15:00:00+00:00", subject="Bank change call",
                                               source="outlook")], start=window[0], end=window[1])
    assert store.meetings_between(*window) == []
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM meetings WHERE email_id = 'scam' AND subject != ''").fetchone()[0] == 0, "nothing of what it says"
    email = store.get_email("scam")
    email.flags = [*email.flags, "fraud_cleared"]
    store.upsert_email(email)
    assert [m["source"] for m in store.meetings_between(*window)] == ["outlook"]


def test_a_hostile_invitation_is_read_in_a_moment(store):
    import time as clock

    many = _ics(*[_event(f"UID:n{n}", "SUMMARY:x", f"DTSTART:202610{1 + n % 28:02d}T090000Z", "DTEND:20261001T100000Z") for n in range(400)])
    never = _ics(*[_event(f"UID:i{n}", "SUMMARY:Never", "DTSTART:20261005T090000Z", "DTEND:20261005T100000Z",
                          "RRULE:FREQ=DAILY;BYMONTH=2;BYMONTHDAY=31") for n in range(400)])
    ancient = _ics(*[_event(f"UID:a{n}", "SUMMARY:Ancient", "DTSTART:00010101T090000Z", "DTEND:00010101T100000Z",
                            "RRULE:FREQ=DAILY;BYMONTH=2;BYMONTHDAY=31") for n in range(60)])
    daily = _ics(*[_event(f"UID:d{n}", "SUMMARY:Daily", "DTSTART:20260601T090000Z", "DTEND:20260601T091500Z", "RRULE:FREQ=DAILY") for n in range(20)])
    started = clock.monotonic()
    assert len(cal.from_ics(many, now=NOW)) == cal.MAX_EVENTS
    assert len(cal.from_ics(never, now=NOW)) == 0
    assert clock.monotonic() - started < 3
    started = clock.monotonic()
    old = cal.from_ics(ancient, now=NOW)
    assert clock.monotonic() - started < cal.ICS_SECONDS + 2
    assert old and all(m.recurring for m in old), "what time didn't allow is kept as its first occurrence"
    assert len(cal.from_ics(daily, now=NOW)) == cal.MAX_ROWS
    assert len(cal.from_ics(("X-PAD:" + "x" * 70 + "\r\n") * 20_000 + WEEKLY, now=NOW)) == 0, "only the first megabyte is read"


def test_an_outlook_request_for_one_occurrence_names_the_occurrence(monkeypatch):
    import struct

    import extract_msg
    from extract_msg.msg_classes import MeetingRequest
    from extract_msg.structures.misc_id import GlobalObjectID

    def goid(year=0, month=0, day=0):
        head = bytes.fromhex("040000008200E00074C5B7101A82E008") + struct.pack(">H", year) + bytes([month, day])
        return GlobalObjectID(head + b"\x00" * 16 + struct.pack("<I", 4) + b"wk42")

    class Request(MeetingRequest):
        def __init__(self, exception):
            pass

        appointmentStartWhole = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
        appointmentEndWhole = datetime(2026, 10, 20, 15, 30, tzinfo=timezone.utc)
        appointmentSubType = False
        subject, location, sender, allAttendeesString, isRecurring, appointmentRecur = "AP standup", "", "Maya", "", False, None
        cleanGlobalObjectID = goid()

        def close(self):
            pass

    exception = Request(True)
    exception.globalObjectID = goid(2026, 10, 19)
    series = Request(False)
    series.globalObjectID = goid()
    found = {}
    for name, message in (("exception", exception), ("series", series)):
        monkeypatch.setattr(extract_msg, "openMsg", lambda *_a, message=message, **_k: message)
        [found[name]] = cal.from_msg("x.msg")
    assert found["exception"].occurrence == "2026-10-19" and found["series"].occurrence == ""
    assert found["exception"].uid == found["series"].uid == goid().toBytes().hex().upper(), "the same meeting, whichever .msg it came on"
