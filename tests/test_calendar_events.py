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
    assert "2026-09-23 09:30–09:45 · AP team standup" in text and "PBC list walkthrough" in text
    assert "Northwind" not in text, "tomorrow's meetings only"
    monday = "".join(e["text"] for e in assistant.answer_stream(loaded, settings, "Am I free on Monday?", today="2026-09-22") if e["type"] == "delta")
    assert "2026-09-28" in monday and "2026-09-23" not in monday, "the coming Monday"

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
