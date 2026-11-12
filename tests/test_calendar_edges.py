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
