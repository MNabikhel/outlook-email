"""Meetings from invitations (.ics) and Outlook meeting requests: times, zones, repeats, moves, cancellations."""

from __future__ import annotations

from datetime import date, datetime, timezone

import re

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
    assert re.search(r"You have \d+ meetings on Wednesday September 23 \[1\]:", text), "the answer to the question first"
    monday = "".join(e["text"] for e in assistant.answer_stream(loaded, settings, "Am I free on Monday?", today="2026-09-22") if e["type"] == "delta")
    assert "Mon Sep 28" in monday and "Sep 23" not in monday, "the coming Monday"
    saturday = "".join(e["text"] for e in assistant.answer_stream(loaded, settings, "Am I free on Saturday?", today="2026-09-22") if e["type"] == "delta")
    assert "Nothing on your calendar on Saturday September 26 [1]." in saturday

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
    and neither removes the other's rows; a lookalike's "update" never changes the organizer's meeting: it is a
    meeting of its own, from its sender."""
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
    assert [(m["email_id"], m["cancelled"]) for m in store.meetings_between(*window) if m["uid"] == "Z"] == [("own", False), ("fake", True)]
    assert all(store.meeting(m["id"]) is not None for m in store.meetings_between(*window) if m["uid"] == "Z")
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
    # Someone outside, or a lookalike of the organizer's domain, can't cancel a week of it (the lookalike's word is a
    # meeting of its own, from its sender).
    save("outsider", _one_week("20261102T140000Z", "20261102T140000Z", "20261102T143000Z", sequence=9, method="CANCEL", cancelled=True),
         "x@evil.example", "2026-10-04T09:00:00+00:00")
    save("lookalike", _one_week("20261109T140000Z", "20261109T140000Z", "20261109T143000Z", sequence=9, method="CANCEL", cancelled=True,
                                organizer="mailto:maya@taz-c0rp.com"), "maya@taz-c0rp.com", "2026-10-04T09:00:00+00:00")
    shown = store.meetings_between("2026-10-01", "2026-11-30")
    assert [(m["start"][:16], m["cancelled"], m["email_id"]) for m in shown if m["sender"] != "maya@taz.com"] == [("2026-11-09T14:00", True, "lookalike")]
    assert [(m["start"][:16], m["cancelled"]) for m in shown if m["sender"] == "maya@taz.com"] == [
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
    assert {m["start"][11:16] for m in store.meetings_between("2026-10-01", "2026-11-30") if m["sender"] == "maya@taz.com"} == {"15:00"}


def test_a_proposed_new_time_and_the_answers_to_one_add_no_meeting(store):
    event = _event("UID:B1", "SUMMARY:Budget review", "DTSTART:20261009T180000Z", "DTEND:20261009T190000Z", "ORGANIZER:mailto:maya@taz.com")
    for method in ("COUNTER", "DECLINECOUNTER", "REFRESH", "REPLY"):
        assert cal.from_ics(_ics(event, method=method), now=NOW) == [], method
    assert len(cal.from_ics(_ics(event, method="PUBLISH"), now=NOW)) == 1


def test_an_invitation_from_another_domain_with_the_meetings_uid_is_a_meeting_of_its_own(store):
    """A meeting is its organizer's: another domain's invitation with its UID (a lookalike's, read first, dated
    earlier, or several of them) never moves, renames or cancels it. It shows as a meeting of its own, from its
    sender. Nothing is decided by the date an email says it was sent: its sender sets that."""
    window = ("2026-10-01T00:00:00+00:00", "2026-10-20T00:00:00+00:00")

    def mine(uid):
        return [(m["start"], m["subject"], m["cancelled"], m["email_id"]) for m in store.meetings_between(*window) if m["uid"] == uid]

    store.save_invites("real", cal.from_ics(_invite("U2", "20261006T140000Z"), email_id="real", now=NOW),
                       sender="maya@taz.com", received="2026-10-01T09:00:00+00:00")
    store.save_invites("fake", cal.from_ics(_invite("U2", "20261007T140000Z", subject="Moved: call +1 555 0100", organizer="mailto:maya@taz-c0rp.com"),
                                            email_id="fake", now=NOW), sender="maya@taz-c0rp.com", received="2026-09-30T09:00:00+00:00")
    assert mine("U2") == [("2026-10-06T14:00:00+00:00", "Budget review", False, "real"),
                          ("2026-10-07T14:00:00+00:00", "Moved: call +1 555 0100", False, "fake")]
    assert [m["sender"] for m in store.meetings_between(*window) if m["uid"] == "U2"] == ["maya@taz.com", "maya@taz-c0rp.com"]

    store.save_invites("vendor", cal.from_ics(_invite("U4", "20261006T140000Z", organizer="mailto:priya@northwind.com"), email_id="vendor", now=NOW),
                       sender="priya@northwind.com", received="2026-10-01T09:00:00+00:00")
    for n in (1, 2):
        store.save_invites(f"other{n}", cal.from_ics(_invite("U4", "20261006T140000Z", sequence=1, method="CANCEL", organizer="mailto:priya@other-co.example"),
                                                     email_id=f"other{n}", now=NOW), sender="priya@other-co.example", received=f"2026-10-0{2 + n}T09:00:00+00:00")
    assert [(email_id, cancelled) for _start, _subject, cancelled, email_id in mine("U4")] == [("vendor", False), ("other2", True)]

    # Within the organizer's own domain the latest word counts, read in any order.
    store.save_invites("upd4", cal.from_ics(_invite("U4", "20261009T160000Z", sequence=1, organizer="mailto:priya@northwind.com"), email_id="upd4", now=NOW),
                       sender="ap@northwind.com", received="2026-10-02T09:00:00+00:00")
    assert [(start, email_id) for start, _subject, _cancelled, email_id in mine("U4")] == [
        ("2026-10-06T14:00:00+00:00", "other2"), ("2026-10-09T16:00:00+00:00", "upd4")]


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
    # Outlook adds a meeting request to the calendar as tentative by itself, organized by whoever sent it; the next
    # calendar read brings it in.
    outlook = [Meeting(uid="U1", start="2026-10-06T14:00:00+00:00", end="2026-10-06T15:00:00+00:00", subject="Urgent wire approval call",
                       location="call +1 555 0100", organizer="CEO <ceo@taz-corp.co>", busy="tentative", source="outlook"),
               Meeting(uid="U9", start="2026-10-07T14:00:00+00:00", end="2026-10-07T15:00:00+00:00", subject="Mine", source="outlook")]
    store.replace_meetings("outlook", outlook, start=window[0], end=window[1])
    assert [m["subject"] for m in store.meetings_between(*window)] == ["Mine"]
    assert store.meeting(outlook[0].id) is None and store.meeting(outlook[1].id) is not None


def test_outlooks_copy_of_a_held_invitation_is_matched_by_the_organizers_name(store, settings):
    """Outlook gives the organizer's name only (CloseDesk doesn't ask it for addresses): the held invitation's
    organizer is matched by name, and another person's meeting with the same id stays."""
    import sys

    from controller_inbox.calendar_events import Meeting

    sys.path.insert(0, "tests")
    from test_links import _record

    window = ("2026-10-01T00:00:00+00:00", "2026-10-20T00:00:00+00:00")
    record = _record("held", sender="ceo@taz-corp.co", flags=["fraud_risk"])
    record.sender_name = "Dana Ortiz"
    store.upsert_email(record)
    store.save_invites("held", cal.from_ics(_invite("U1", "20261006T140000Z", subject="Urgent wire approval call",
                                                    organizer="mailto:ceo@taz-corp.co"),
                                            email_id="held", now=NOW), sender="ceo@taz-corp.co")
    copy = Meeting(uid="U1", start="2026-10-06T14:00:00+00:00", end="2026-10-06T15:00:00+00:00", subject="Urgent wire approval call",
                   organizer="Dana Ortiz", busy="tentative", source="outlook")
    store.replace_meetings("outlook", [copy], start=window[0], end=window[1])
    assert store.meetings_between(*window) == []
    other = Meeting(uid="U1", start="2026-10-06T14:00:00+00:00", end="2026-10-06T15:00:00+00:00", subject="Board meeting",
                    organizer="Pat Lee", source="outlook")
    store.replace_meetings("outlook", [other], start=window[0], end=window[1])
    assert [m["subject"] for m in store.meetings_between(*window)] == ["Board meeting"]


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
                                               organizer="Acme Billing", busy="tentative", source="outlook")], start=window[0], end=window[1])
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


def test_a_held_invitation_naming_someone_elses_meeting_doesnt_hide_it(store):
    """Outlook's copy of a held invitation is hidden; a real meeting whose UID a held email also names (from another
    organizer) stays on the calendar."""
    import sys

    from controller_inbox.calendar_events import Meeting

    sys.path.insert(0, "tests")
    from test_links import _record

    window = ("2026-10-01T00:00:00+00:00", "2026-10-20T00:00:00+00:00")
    real = Meeting(uid="REAL", start="2026-10-06T14:00:00+00:00", end="2026-10-06T15:00:00+00:00", subject="Board meeting",
                   organizer="Dana CEO <ceo@taz.com>", busy="busy", source="outlook")
    copy = Meeting(uid="BAIT", start="2026-10-07T14:00:00+00:00", end="2026-10-07T15:00:00+00:00", subject="Urgent wire approval call",
                   organizer="CEO <ceo@taz-corp.co>", busy="tentative", source="outlook")
    store.replace_meetings("outlook", [real, copy], start=window[0], end=window[1])
    store.upsert_email(_record("held", sender="x@evil.example", flags=["fraud_risk"]))
    store.save_invites("held", cal.from_ics(_invite("REAL", "20261009T140000Z", subject="whatever"), email_id="held", now=NOW),
                       sender="x@evil.example", held=True)
    store.upsert_email(_record("held2", sender="ceo@taz-corp.co", flags=["fraud_risk"]))
    store.save_invites("held2", cal.from_ics(_invite("BAIT", "20261007T140000Z", organizer="mailto:ceo@taz-corp.co"), email_id="held2", now=NOW),
                       sender="ceo@taz-corp.co", held=True)
    assert [m["subject"] for m in store.meetings_between(*window)] == ["Board meeting"]


def test_a_rule_with_more_after_it_on_its_line_is_walked_within_limits():
    import time as clock

    padded = _ics(*[_event(f"UID:p{n}", "DTSTART:20261005T090000Z", "DTEND:20261005T100000Z",
                           "RRULE:FREQ=DAILY;BYMONTH=2;BYMONTHDAY=31 RDATE:20261006T090000Z") for n in range(5)])
    ancient = _ics(_event("UID:a", "DTSTART:00010101T090000Z", "DTEND:00010101T100000Z", "RRULE:FREQ=DAILY;BYMONTH=2;BYMONTHDAY=31"))
    started = clock.monotonic()
    cal.from_ics(padded, now=NOW)
    assert clock.monotonic() - started < 0.5
    started = clock.monotonic()
    cal.from_ics(ancient, now=NOW, deadline=clock.monotonic() + 0.1)
    assert clock.monotonic() - started < 0.3, "the time left is looked at while a rule is walked, not only between meetings"


def test_an_outlook_exception_occurrence_keeps_its_series_id():
    head = "040000008200E00074C5B7101A82E008"
    series = head + "00000000" + "00" * 8 + "00" * 8 + "10000000" + "AB" * 16
    exception = head + "07EA0A13" + "00" * 8 + "00" * 8 + "10000000" + "AB" * 16
    assert cal.series_uid(exception) == cal.series_uid(series) == series.upper()
    assert cal.outlook_occurrence(exception) == "2026-10-19" and cal.outlook_occurrence(series) == ""


def _mail_with_invitation(email_id, subject, data, *, sender="jordan@horizon-goods.com", received=NOW):
    from controller_inbox.models import RawAttachment, RawMessage

    return RawMessage(id=email_id, subject=subject, sender_name="Jordan Lee", sender_email=sender, received_at=received,
                      body_text="See the invitation.", body_preview="", has_attachments=True, source="folder",
                      attachments=[RawAttachment(id="a1", filename="invite.ics", content_type="text/calendar", size_bytes=len(data), content=data.encode())])


def test_meeting_mail_is_filed_by_what_its_invitation_says(store, settings):
    """Outlook's invitations have no "Invitation:" in their subject: the invitation's METHOD says what the email is.
    A request asks for an answer; a cancellation and a proposed new time don't."""
    from controller_inbox import pipeline
    from controller_inbox.models import DocumentType

    organizer = "ORGANIZER;CN=Jordan Lee:mailto:jordan@horizon-goods.com"
    event = _event("UID:wk1", "SUMMARY:Weekly AP payment run review", "DTSTART:20261116T150000Z", "DTEND:20261116T153000Z", organizer)
    cases = {
        "req": ("Weekly AP payment run review", _ics(event, method="REQUEST")),
        "cxl": ("Canceled: Weekly AP payment run review", _ics(event, method="CANCEL")),
        "ctr": ("New Time Proposed: Weekly AP payment run review", _ics(event, method="COUNTER")),
        "rep": ("Accepted: Weekly AP payment run review", _ics(event, method="REPLY")),
    }
    got = {}
    for email_id, (subject, data) in cases.items():
        record = pipeline.process_message(_mail_with_invitation(email_id, subject, data), store, settings, now=NOW)
        got[email_id] = (record.category, record.folder, [a.source for a in record.actions if a.source == "meeting"])
    assert got["req"] == (DocumentType.MEETING, "informational", ["meeting"])
    assert got["cxl"] == (DocumentType.MEETING, "informational", [])
    assert got["ctr"] == got["rep"] == (DocumentType.MEETING, "informational", [])


def test_invitations_already_in_the_mailbox_reach_the_calendar_after_an_update(store, settings):
    """Mail read by a version that kept no meetings: once, in batches, its invitations are read from what was kept
    (the .ics text stored with it, else its kept file), the cancellation counted. Held mail adds no meeting."""
    import sys

    from controller_inbox import cli
    from controller_inbox.extract import sha256_bytes
    from controller_inbox.models import AttachmentRecord, DocumentType, ExtractedFields

    sys.path.insert(0, "tests")
    from test_links import _record

    settings.ensure_data_dir()
    event = ["UID:old1", "SUMMARY:Budget review", "DTSTART:20261009T140000Z", "DTEND:20261009T150000Z", "ORGANIZER:mailto:maya@taz.com"]
    stored = [
        ("invite", _ics(_event(*event, "SEQUENCE:0")), "text", ()),
        ("cancel", _ics(_event(*event, "SEQUENCE:1"), method="CANCEL"), "file", ()),
        ("held", _ics(_event("UID:old2", "SUMMARY:Wire call", "DTSTART:20261010T140000Z", "ORGANIZER:mailto:x@evil.example")), "text", ("fraud_risk",)),
    ]
    for email_id, data, kept_as, flags in stored:
        record = _record(email_id, sender="x@evil.example" if flags else "maya@taz.com", flags=flags,
                         received=f"2026-10-0{1 + len(email_id) % 5}T09:00:00+00:00")
        record.attachments = [AttachmentRecord(id=f"{email_id}:0", email_id=email_id, filename="invite.ics", content_type="text/calendar",
                                               size_bytes=len(data), sha256=sha256_bytes(data.encode()),
                                               extracted_text=data if kept_as == "text" else "",
                                               document_type=DocumentType.OTHER, document_confidence=0, extracted_fields=ExtractedFields())]
        store.upsert_email(record)
        if kept_as == "file":
            (settings.inbox_extracted / email_id).mkdir(parents=True)
            (settings.inbox_extracted / email_id / "invite.ics").write_text(data, encoding="utf-8", newline="")
    assert store.meetings_between("2026-10-01", "2026-10-31") == []
    assert cal.catch_up_invites(store, settings, now=NOW, batch=1) == 3, "a batch at a time, to the end"
    assert [(m["uid"], m["cancelled"]) for m in store.meetings_between("2026-10-01", "2026-10-31")] == [("old1", True)]
    assert cal.catch_up_invites(store, settings, now=NOW) == 0, "once"
    store.set_state(cal.CATCH_UP_KEY, "")
    with store.connect() as conn:
        conn.execute("DELETE FROM meetings")
    cli._link_stored_mail(settings, store)  # what CloseDesk starts in the background after an update
    assert [m["uid"] for m in store.meetings_between("2026-10-01", "2026-10-31")] == ["old1"]


def test_a_cancelled_meeting_shows_its_title_without_canceled_and_a_moved_week_still_repeats():
    from zoneinfo import ZoneInfo

    [cancelled] = cal.from_ics(_one_week("20261019T140000Z", "20261019T140000Z", "20261019T143000Z", sequence=1, method="CANCEL", cancelled=True)
                               .replace("SUMMARY:AP standup", "SUMMARY:Canceled: AP standup"), now=NOW)
    shown = cal.shown(cancelled.to_row(), ZoneInfo("America/New_York"))
    assert (shown["subject"], shown["cancelled"]) == ("AP standup", True), "the Cancelled tag says it"
    [moved] = cal.from_ics(_one_week("20261026T140000Z", "20261027T150000Z", "20261027T153000Z", sequence=2), now=NOW)
    assert moved.recurring and cal.shown(moved.to_row(), ZoneInfo("UTC"))["recurring"], "one week of a repeating meeting"
    kept = cal.shown({**moved.to_row(), "subject": "Canceled: plans", "cancelled": False}, ZoneInfo("UTC"))
    assert kept["subject"] == "Canceled: plans", "a meeting that isn't cancelled keeps its title as it is"


def test_a_database_from_the_first_calendar_build_gets_every_meeting_column(tmp_path):
    import sqlite3

    from controller_inbox.store import Store

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE meetings (id TEXT PRIMARY KEY, uid TEXT NOT NULL, source TEXT NOT NULL, start TEXT NOT NULL, "end" TEXT NOT NULL,
               all_day INTEGER DEFAULT 0, subject TEXT DEFAULT '', location TEXT DEFAULT '', organizer TEXT DEFAULT '', attendees TEXT DEFAULT '[]',
               cancelled INTEGER DEFAULT 0, recurring INTEGER DEFAULT 0, busy TEXT DEFAULT '', email_id TEXT DEFAULT '')"""
        )
        conn.execute("""INSERT INTO meetings (id, uid, source, start, "end", subject, email_id) VALUES
                        ('m1', 'U1', 'invite', '2026-10-09T14:00:00+00:00', '2026-10-09T15:00:00+00:00', 'Budget review', 'e1')""")
    conn.close()
    store = Store(path)
    with store.connect() as conn:
        have = {row[1] for row in conn.execute("PRAGMA table_info(meetings)")}
    assert {"sequence", "sender", "received", "first_seen", "occurrence", "expanded"} <= have
    assert [m["subject"] for m in store.meetings_between("2026-10-09", "2026-10-10")] == ["Budget review"]


def test_the_calendar_reads_only_the_meetings_near_its_window_and_still_shows_a_long_one_begun_before(store):
    from controller_inbox.calendar_events import Meeting
    from controller_inbox.store import LONGEST_MEETING

    window = ("2026-10-12T00:00:00+00:00", "2026-10-19T00:00:00+00:00")
    long_one = Meeting(uid="L", start="2026-09-20", end="2026-10-14", subject="Audit fieldwork", all_day=True, source="demo")
    timed = Meeting(uid="T", start="2026-10-10T08:00:00+00:00", end="2026-10-13T17:00:00+00:00", subject="Offsite", source="demo")
    old = [Meeting(uid=f"o{n}", start=f"2025-0{1 + n % 9}-0{1 + n % 9}T09:00:00+00:00", end=f"2025-0{1 + n % 9}-0{1 + n % 9}T10:00:00+00:00",
                   subject="Old", source="demo") for n in range(50)]
    store.replace_meetings("demo", [long_one, timed, *old], start="2025-01-01", end="2027-01-01")
    assert sorted(m["subject"] for m in store.meetings_between(*window)) == ["Audit fieldwork", "Offsite"]
    assert LONGEST_MEETING.days >= 31
    from controller_inbox.store import MEETINGS_LONG, MEETINGS_NEAR

    with store.connect() as conn:
        near = " ".join(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + MEETINGS_NEAR, ("2026-09-11", window[1], window[0], window[0], window[1])))
        long = " ".join(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + MEETINGS_LONG, (window[0], "2026-09-11")))
    assert "SCAN m" not in near and "SCAN m" not in long, (near, long)


def _held_scam(store, settings, uid="U6", subject="Urgent wire approval call"):
    from controller_inbox import pipeline

    data = _invite(uid, "20261006T140000Z", subject=subject, organizer="mailto:ceo@taz-corp.co")
    raw = _mail_with_invitation("scam", "Updated remittance details", data, sender="ceo@taz-corp.co")
    raw.sender_name = "Dana Ortiz"
    raw.body_text = "Our bank details have changed. Please use the following account for all payments from today: account 5566778899 routing 021000021."
    record = pipeline.process_message(raw, store, settings, now=NOW)
    assert pipeline.attachments_locked(record)
    return record


def _copy(uid="U6", start="2026-10-06T14:00:00+00:00", organizer="Dana Ortiz", busy="tentative", subject="Urgent wire approval call +1 555 0100"):
    from controller_inbox.calendar_events import Meeting

    return Meeting(uid=uid, start=start, end=start.replace("T14", "T15"), subject=subject, organizer=organizer, busy=busy, source="outlook")


def _record_held(email_id):
    import sys

    sys.path.insert(0, "tests")
    from test_links import _record

    return _record(email_id, sender="ceo@taz-corp.co", flags=["fraud_risk"])


WINDOW = ("2026-10-01T00:00:00+00:00", "2026-10-20T00:00:00+00:00")


def test_outlooks_copy_of_a_held_invitation_stays_hidden_after_the_email_is_deleted(store, settings):
    """Outlook keeps its tentative copy of a scam invitation after the email itself is deleted (in Outlook, or the
    drop folder's copy): what the held invitation named is kept, without its email, so the copy stays hidden."""
    from controller_inbox import assistant, outlook_sync

    _held_scam(store, settings)
    # Held after it was read: its invitation's rows are turned into the same markers when it is deleted.
    store.upsert_email(_record_held("later"))
    store.save_invites("later", cal.from_ics(_invite("U7", "20261008T140000Z", subject="Call me now", organizer="mailto:ceo@taz-corp.co"),
                                             email_id="later", now=NOW), sender="ceo@taz-corp.co")
    store.replace_meetings("outlook", [_copy(), _copy("U7", "2026-10-08T14:00:00+00:00", organizer="ceo@taz-corp.co", subject="Call me now")],
                           start=WINDOW[0], end=WINDOW[1])
    assert store.meetings_between(*WINDOW) == []
    store.delete_emails(["scam", "later"])
    assert store.meetings_between(*WINDOW) == []
    outlook_sync.connect(store, calendar=True, now=NOW)
    source = assistant.calendar_source(store, settings, "2026-10-06")
    assert source is None or "555 0100" not in source.body_text
    with store.connect() as conn:
        kept = conn.execute("SELECT source, subject, location FROM meetings WHERE uid IN ('U6', 'U7') AND source != 'outlook'").fetchall()
    assert kept and {(row[0], row[1], row[2]) for row in kept} == {("held", "", "")}, "nothing of what it said"


def test_a_held_invitation_hides_only_outlooks_tentative_copy_at_its_own_time(store, settings):
    """A held display-name spoof ("Dana Ortiz <dana.ortiz.office@gmail.com>") naming the UID of Dana's real board
    meeting doesn't hide it: Outlook's copy of a held invitation is the tentative one at the invitation's time."""
    import sys

    sys.path.insert(0, "tests")
    from test_links import _record

    real = _copy("REAL", "2026-10-06T14:00:00+00:00", busy="busy", subject="Board meeting")
    store.replace_meetings("outlook", [real], start=WINDOW[0], end=WINDOW[1])
    spoof = _record("held", sender="dana.ortiz.office@gmail.com", flags=["fraud_risk"])
    spoof.sender_name = "Dana Ortiz"
    store.upsert_email(spoof)

    def held_at(start):
        store.save_invites("held", cal.from_ics(_invite("REAL", start, subject="whatever", organizer="mailto:dana.ortiz.office@gmail.com"),
                                                email_id="held", now=NOW), sender="dana.ortiz.office@gmail.com", sender_name="Dana Ortiz", held=True)

    held_at("20261009T140000Z")
    assert [m["subject"] for m in store.meetings_between(*WINDOW)] == ["Board meeting"], "another time, and accepted: Dana's own"
    held_at("20261006T140000Z")
    assert [m["subject"] for m in store.meetings_between(*WINDOW)] == ["Board meeting"], "the same time, but not tentative"
    store.replace_meetings("outlook", [_copy("REAL", "2026-10-06T14:00:00+00:00", subject="whatever")], start=WINDOW[0], end=WINDOW[1])
    assert store.meetings_between(*WINDOW) == [], "tentative, at its time, organized by the name it gave: Outlook's copy of it"


def test_a_held_invitation_cleared_becomes_a_meeting_and_one_held_later_stops_being_one(store, settings):
    from controller_inbox import fraud

    _held_scam(store, settings, uid="U8", subject="Quarterly vendor review")
    assert store.meetings_between(*WINDOW) == []
    fraud.record_fraud_verdict(store, settings, "scam", verdict="safe", now=NOW)
    assert [m["subject"] for m in store.meetings_between(*WINDOW)] == ["Quarterly vendor review"], "cleared: its invitation is read again"
    fraud.record_fraud_verdict(store, settings, "scam", verdict="fraud", now=NOW)
    assert fraud.attachments_locked(store.get_email("scam"))
    with store.connect() as conn:
        rows = conn.execute("SELECT source, subject FROM meetings WHERE email_id = 'scam'").fetchall()
    assert rows and {(row[0], row[1]) for row in rows} == {("held", "")}, "held again: only which meeting it named"
    assert store.meetings_between(*WINDOW) == []


def test_a_forged_organizer_from_a_third_domain_doesnt_move_a_forwarded_meeting(store):
    """A meeting known only from a colleague's forward: someone else claiming the same organizer (x@evil.example
    sending UID X with ORGANIZER pat@vendor.com) is a meeting of their own, never moving or cancelling the forward's."""
    def mine():
        return [(m["start"], m["subject"], m["cancelled"]) for m in store.meetings_between(*WINDOW) if m["sender"] == "maya@taz.com"]

    store.save_invites("e1", cal.from_ics(_invite("X", "20261009T140000Z", organizer="mailto:pat@vendor.com"), email_id="e1", now=NOW),
                       sender="maya@taz.com", received="2026-10-01T09:00:00+00:00")
    store.save_invites("e2", cal.from_ics(_invite("X", "20261010T180000Z", subject="Moved: call +1 555 0100", sequence=5, organizer="mailto:pat@vendor.com"),
                                          email_id="e2", now=NOW), sender="x@evil.example", received="2026-10-02T09:00:00+00:00")
    store.save_invites("e3", cal.from_ics(_invite("X", "20261009T140000Z", method="CANCEL", sequence=9, organizer="mailto:pat@vendor.com"),
                                          email_id="e3", now=NOW), sender="x@evil.example", received="2026-10-03T09:00:00+00:00")
    assert mine() == [("2026-10-09T14:00:00+00:00", "Budget review", False)], "the forward's meeting as it was"
    assert {m["sender"] for m in store.meetings_between(*WINDOW)} == {"maya@taz.com", "x@evil.example"}, "the other is a meeting of its own"
    # A colleague's update of their own forward still counts; once the organizer's own invitation is here, it does.
    store.save_invites("e4", cal.from_ics(_invite("X", "20261009T150000Z", sequence=1, organizer="mailto:pat@vendor.com"), email_id="e4", now=NOW),
                       sender="ann@taz.com", received="2026-10-04T09:00:00+00:00")
    assert [m["start"] for m in store.meetings_between(*WINDOW) if m["sender"].endswith("@taz.com")] == ["2026-10-09T15:00:00+00:00"]
    store.save_invites("own", cal.from_ics(_invite("X", "20261009T160000Z", sequence=2, organizer="mailto:pat@vendor.com"), email_id="own", now=NOW),
                       sender="pat@vendor.com", received="2026-10-05T09:00:00+00:00")
    assert [(m["start"], m["sender"]) for m in store.meetings_between(*WINDOW)] == [("2026-10-09T16:00:00+00:00", "pat@vendor.com")]


def test_a_question_naming_a_date_is_answered_for_that_date(loaded, settings):
    """"Am I free on Monday Oct 5?" is about October 5 (a Monday three weeks on), not the coming Monday; "December 1",
    "10/1" and "the 30th" are dates too, read before any weekday word. "What is on Monday?" asks about the calendar."""
    from controller_inbox import assistant

    def said(question):
        return "".join(e["text"] for e in assistant.answer_stream(loaded, settings, question, today="2026-09-22") if e["type"] == "delta")

    assert "on Monday October 5" in said("Am I free on Monday Oct 5?")
    assert "on Thursday October 1" in said("Am I free October 1st?")
    assert "on Thursday October 1" in said("what's on 10/1?")
    assert "on Wednesday September 30" in said("Am I free on the 30th?")
    assert "on Thursday December 3" in said("Am I free December 3?"), "past the calendar's usual three weeks: read that far"
    assert assistant.asks_about_meetings("What is on Monday?") and assistant.asks_about_meetings("what is on tomorrow?")
    assert not assistant.asks_about_meetings("What is on the invoice?")
    assert assistant._named_day("Is anything due on 9/2?", date(2026, 9, 22)) == date(2027, 9, 2), "a day gone by is next year's"


def test_an_invitations_time_in_a_plain_answer_is_in_the_apps_time_zone(store, settings):
    import sys

    from controller_inbox import assistant
    from controller_inbox.models import AttachmentRecord, DocumentType, ExtractedFields

    sys.path.insert(0, "tests")
    from test_links import _record

    data = _invite("TZ1", "20261008T140000Z", subject="Northwind pricing review")
    record = _record("tz-mail", sender="maya@taz.com")
    record.subject = "Northwind pricing review"
    record.attachments = [AttachmentRecord(id="tz-mail:0", email_id="tz-mail", filename="invite.ics", content_type="text/calendar", size_bytes=len(data),
                                           sha256="", extracted_text=data, document_type=DocumentType.OTHER, document_confidence=0,
                                           extracted_fields=ExtractedFields())]
    store.upsert_email(record)
    text = "".join(e["text"] for e in assistant.answer_stream(store, settings, "Northwind pricing review", today="2026-10-01") if e["type"] == "delta")
    assert "10:00 AM" in text and "(UTC)" not in text, text


def test_one_week_cancelled_by_the_organizer_keeps_a_dropped_or_forwarded_series(store):
    """A series known from a file the user dropped (no sender) or a colleague's forward keeps its weeks when the
    organizer's own word on one week arrives: only that week changes. The organizer's own series replaces a forward."""
    vendor = "mailto:pat@vendor.com"
    series = WEEKLY.replace("ORGANIZER:mailto:maya@taz.com", f"ORGANIZER:{vendor}")
    cancel = _one_week("20261019T140000Z", "20261019T140000Z", "20261019T143000Z", sequence=1, method="CANCEL", cancelled=True, organizer=vendor)

    def weeks():
        return [(m["start"][:10], m["cancelled"]) for m in store.meetings_between("2026-10-01", "2026-11-03")]

    expected = [("2026-10-05", False), ("2026-10-12", False), ("2026-10-19", True), ("2026-10-26", False), ("2026-11-02", False)]
    store.save_invites("dropped", cal.from_ics(series, email_id="dropped", now=NOW), sender="", received="2026-10-01T09:00:00+00:00")
    store.save_invites("cancel", cal.from_ics(cancel, email_id="cancel", now=NOW), sender="pat@vendor.com", received="2026-10-02T09:00:00+00:00")
    assert weeks() == expected, "a dropped file is the user's own copy of the organizer's invitation"

    store.save_invites("dropped", [], sender="")
    store.save_invites("fwd", cal.from_ics(series, email_id="fwd", now=NOW), sender="maya@taz.com", received="2026-10-01T09:00:00+00:00")
    assert weeks() == expected, "a forward's series, with the organizer's own word on one week"
    assert {m["email_id"] for m in store.meetings_between("2026-10-01", "2026-11-03") if not m["cancelled"]} == {"fwd"}

    moved = series.replace("DTSTART:20261005T140000Z", "DTSTART:20261005T160000Z").replace("SEQUENCE:0", "SEQUENCE:2")
    store.save_invites("own", cal.from_ics(moved, email_id="own", now=NOW), sender="pat@vendor.com", received="2026-10-03T09:00:00+00:00")
    assert {m["email_id"] for m in store.meetings_between("2026-10-01", "2026-11-03")} == {"own"}, "the organizer's own series replaces it"


def test_an_invitation_dropped_as_a_file_is_titled_by_its_meeting(store, settings):
    from controller_inbox.folder_mail import ingest_folder

    settings.ensure_data_dir()
    (settings.inbox_incoming / "a_series.ics").write_text(WEEKLY, encoding="utf-8", newline="")
    [record] = ingest_folder(store, settings, now=NOW)
    assert record.subject == "AP standup"
    assert [a.title for a in record.actions if a.source == "meeting"] == ["Accept or decline: AP standup"]


def test_a_meeting_from_a_doubtful_invitation_says_to_check_who_sent_it(store, settings):
    """A lookalike's same-UID "update" is a meeting of its own: it (and the meeting it imitates) says to check who
    sent it, on the calendar and in the chat; so does one whose email carries a payment caution. Others don't."""
    import sys

    from fastapi.testclient import TestClient

    from controller_inbox import assistant, web
    from helpers import PAGE

    sys.path.insert(0, "tests")
    from test_links import _record

    for email_id, sender, flags in (("real", "maya@taz.com", ()), ("fake", "maya@taz-c0rp.com", ()), ("careful", "ap@vendor.example", ("payment_caution",)),
                                    ("plain", "pat@vendor.com", ())):
        store.upsert_email(_record(email_id, sender=sender, flags=flags))
    store.save_invites("real", cal.from_ics(_invite("U2", "20261006T140000Z"), email_id="real", now=NOW), sender="maya@taz.com")
    store.save_invites("fake", cal.from_ics(_invite("U2", "20261007T140000Z", subject="Updated: Budget review", organizer="mailto:maya@taz-c0rp.com"),
                                            email_id="fake", now=NOW), sender="maya@taz-c0rp.com")
    store.save_invites("careful", cal.from_ics(_invite("U3", "20261008T140000Z", subject="Payment run", organizer="mailto:ap@vendor.example"),
                                               email_id="careful", now=NOW), sender="ap@vendor.example")
    store.save_invites("plain", cal.from_ics(_invite("U4", "20261009T140000Z", subject="Vendor sync", organizer="mailto:pat@vendor.com"),
                                             email_id="plain", now=NOW), sender="pat@vendor.com")
    client = TestClient(web.create_app(settings, store))
    meetings = client.get("/api/calendar", params={"start": "2026-10-05", "end": "2026-10-11"}, headers=PAGE).json()["meetings"]
    cautions = {m["email_id"]: m["caution"] for m in meetings}
    assert cautions["plain"] == "" and all(cautions[e].startswith("Check who sent this before joining or paying") for e in ("real", "fake", "careful"))
    fake = next(m for m in meetings if m["email_id"] == "fake")
    assert client.get(f"/api/calendar/{fake['id']}", headers=PAGE).json()["meeting"]["caution"]
    source = assistant.calendar_source(store, settings, "2026-10-05")
    line = next(line for line in source.body_text.splitlines() if "Updated: Budget review" in line)
    assert "check who sent this before joining or paying" in line.lower()
    assert "check who sent" not in next(line for line in source.body_text.splitlines() if "Vendor sync" in line).lower()


def test_after_an_update_stored_invitations_are_filed_as_meetings_keeping_what_was_done(store, settings):
    """Mail an older version filed as "Other" (an invitation with no "Invitation:" in its subject), or with an
    "Accept or decline" task on a cancellation, is filed by its invitation once the catch-up reads it. A task the
    user finished stays finished."""
    import sys

    from controller_inbox.extract import sha256_bytes
    from controller_inbox.models import ActionItem, ActionStatus, AttachmentRecord, DocumentType, ExtractedFields, Importance

    sys.path.insert(0, "tests")
    from test_links import _record

    event = ["UID:up1", "SUMMARY:Weekly AP payment run review", "DTSTART:20261009T140000Z", "DTEND:20261009T150000Z", "ORGANIZER:mailto:jordan@horizon-goods.com"]
    stored = {
        "up-invite": ("Weekly AP payment run review", _ics(_event(*event, "SEQUENCE:0")), []),
        "up-cancel": ("Canceled: Weekly AP payment run review", _ics(_event(*event, "SEQUENCE:1"), method="CANCEL"),
                      [("Accept or decline: Canceled: Weekly AP payment run review", ActionStatus.OPEN)]),
        "up-done": ("Budget sync", _ics(_event("UID:up2", "SUMMARY:Budget sync", "DTSTART:20261012T140000Z", "ORGANIZER:mailto:jordan@horizon-goods.com")),
                    [("Accept or decline: Budget sync", ActionStatus.DONE)]),
    }
    for email_id, (subject, data, tasks) in stored.items():
        record = _record(email_id, sender="jordan@horizon-goods.com")
        record.subject, record.category, record.model_status = subject, DocumentType.OTHER, "needs_model"
        record.attachments = [AttachmentRecord(id=f"{email_id}:0", email_id=email_id, filename="invite.ics", content_type="text/calendar",
                                               size_bytes=len(data), sha256=sha256_bytes(data.encode()), extracted_text=data,
                                               document_type=DocumentType.OTHER, document_confidence=0, extracted_fields=ExtractedFields())]
        record.actions = [ActionItem(id=f"{email_id}-t{n}", email_id=email_id, title=title, detail="", due_date=None, priority=Importance.MEDIUM,
                                     status=status, source="meeting") for n, (title, status) in enumerate(tasks)]
        store.upsert_email(record)
    cal.catch_up_invites(store, settings, now=NOW)
    invite, cancel, done = (store.get_email(email_id) for email_id in stored)
    assert invite.category == cancel.category == done.category == DocumentType.MEETING
    assert [(a.title, a.status) for a in invite.actions if a.source == "meeting"] == [("Accept or decline: Weekly AP payment run review", ActionStatus.OPEN)]
    assert [a for a in cancel.actions if a.status == ActionStatus.OPEN] == [], "a cancellation asks for no answer"
    assert [(a.title, a.status) for a in done.actions if a.source == "meeting"] == [("Accept or decline: Budget sync", ActionStatus.DONE)]
