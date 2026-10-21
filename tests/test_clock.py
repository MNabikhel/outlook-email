from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from controller_inbox.actions import received_day
from controller_inbox.clock import aware_utc, format_when, parse_stamp
from controller_inbox.pipeline import sent_date

EASTERN = timezone(timedelta(hours=-4))
NEW_YORK = ZoneInfo("America/New_York")


@pytest.mark.parametrize(
    "when, utc",
    [
        (datetime(2026, 10, 6, 9, 0), datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)),
        (datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc), datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)),
        (datetime(2026, 10, 6, 5, 0, tzinfo=EASTERN), datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)),
    ],
)
def test_aware_utc(when, utc):
    got = aware_utc(when)
    assert got == utc and got.utcoffset() == timedelta(0)
    assert got.isoformat() == utc.isoformat()


@pytest.mark.parametrize(
    "text, parsed",
    [
        ("2026-09-22T16:00:00+00:00", datetime(2026, 9, 22, 16, 0, tzinfo=timezone.utc)),
        ("2026-09-22T16:00:00Z", datetime(2026, 9, 22, 16, 0, tzinfo=timezone.utc)),
        ("2026-09-22T16:00:00", datetime(2026, 9, 22, 16, 0, tzinfo=timezone.utc)),
        (" 2026-09-22T16:00:00 ", datetime(2026, 9, 22, 16, 0, tzinfo=timezone.utc)),
        ("2026-09-22 16:00:00+00:00", datetime(2026, 9, 22, 16, 0, tzinfo=timezone.utc)),
        ("2026-10-06T05:00:00-04:00", datetime(2026, 10, 6, 5, 0, tzinfo=EASTERN)),
        ("2026-09-22", datetime(2026, 9, 22, tzinfo=timezone.utc)),
    ],
)
def test_parse_stamp(text, parsed):
    got = parse_stamp(text)
    assert got == parsed and got.utcoffset() == parsed.utcoffset(), "kept in the zone it was written in"


@pytest.mark.parametrize("text", ["", None, "garbage", "2026-13-01T00:00:00"])
def test_parse_stamp_is_none_for_what_isnt_a_stamp(text):
    assert parse_stamp(text) is None


def test_format_when_reads_a_stamp_in_the_zone():
    assert format_when("2026-10-02T01:30:00Z", NEW_YORK) == "Oct 01 · 21:30"
    assert format_when("2026-10-02T01:30:00", NEW_YORK) == "Oct 01 · 21:30", "no offset is UTC"
    assert format_when("not a time") == "not a time"


@pytest.mark.parametrize(
    "received, zone, day",
    [
        ("2026-10-02T01:30:00+00:00", NEW_YORK, date(2026, 10, 1)),
        ("2026-10-02T01:30:00Z", NEW_YORK, date(2026, 10, 1)),
        ("2026-10-02T01:30:00", NEW_YORK, date(2026, 10, 1)),
        ("2026-10-02T01:30:00+00:00", None, date(2026, 10, 2)),
        (datetime(2026, 10, 2, 1, 30), NEW_YORK, date(2026, 10, 1)),
        (datetime(2026, 10, 1, 23, 0, tzinfo=EASTERN), None, date(2026, 10, 2)),
    ],
)
def test_received_day_and_sent_date_agree(received, zone, day):
    assert received_day(received, zone) == day
    settings = SimpleNamespace(tz=zone or timezone.utc)
    assert sent_date(received, settings, fallback=None) == day


@pytest.mark.parametrize("received", ["", None, "garbage", datetime.min.replace(tzinfo=timezone.utc)])
def test_received_day_falls_back(received):
    fallback = date(2026, 1, 5)
    assert received_day(received, NEW_YORK, fallback=fallback) == fallback
    assert sent_date(received, SimpleNamespace(tz=NEW_YORK), fallback=fallback) == fallback
