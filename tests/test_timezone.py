"""Times follow this computer unless Setup pins another zone."""

import re
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from controller_inbox.clock import (
    ZONES,
    _matching_offset,
    apply_saved_timezone,
    computer_timezone,
    effective_timezone,
    format_when,
    is_timezone,
    offset_label,
    on_daylight_time,
    set_timezone,
    zone_choices,
    zone_option,
)
from controller_inbox.config import Settings
from controller_inbox.reading import _as_of
from controller_inbox.web import create_app

STAMP = "2026-09-22T16:00:00+00:00"


def selected_zone(page: str) -> str:
    found = re.search(r'<option value="([^"]+)"[^>]*\sselected\s*>', page)
    return found.group(1) if found else ""


def test_the_computer_zone_is_a_real_zone():
    assert is_timezone(computer_timezone())
    assert effective_timezone("auto") == computer_timezone()
    assert effective_timezone("") == computer_timezone()
    assert effective_timezone("not-a-zone") == computer_timezone()
    assert effective_timezone("America/Chicago") == "America/Chicago"


def test_the_list_uses_the_windows_names_and_every_zone_loads():
    names = dict(ZONES)
    assert names["America/New_York"] == "Eastern Time (US & Canada)"
    assert names["America/Chicago"] == "Central Time (US & Canada)"
    assert names["America/Los_Angeles"] == "Pacific Time (US & Canada)"
    for name, _label in ZONES:
        assert is_timezone(name), name


def test_the_computer_clock_and_the_chosen_zone_keep_the_same_hours():
    now = datetime.now(timezone.utc)
    computer = ZoneInfo(computer_timezone())
    assert now.astimezone(computer).utcoffset() == now.astimezone().utcoffset()
    fallback = _matching_offset()
    assert fallback and now.astimezone(ZoneInfo(fallback)).utcoffset() == now.astimezone().utcoffset()


def test_a_utc_stamp_is_shown_in_the_chosen_zone():
    assert format_when(STAMP, ZoneInfo("America/New_York")) == "Sep 22 · 12:00"
    assert format_when(STAMP, ZoneInfo("America/Chicago")) == "Sep 22 · 11:00"
    assert format_when(STAMP, ZoneInfo("America/Los_Angeles")) == "Sep 22 · 09:00"
    assert format_when("2026-09-22T16:00:00", ZoneInfo("America/Chicago")) == "Sep 22 · 11:00"
    assert format_when("", ZoneInfo("UTC")) == ""


def test_auto_is_the_default_and_a_bad_name_falls_back(tmp_path):
    assert Settings(_env_file=None).timezone == "auto"
    assert Settings(timezone="Nope", _env_file=None).timezone == "auto"
    pinned = Settings(data_dir=tmp_path, timezone="America/Denver", _env_file=None)
    assert pinned.tz == ZoneInfo("America/Denver")


def test_a_saved_choice_wins_and_auto_follows_the_computer(settings, store):
    set_timezone(store, settings, "America/Chicago")
    assert settings.tz == ZoneInfo("America/Chicago")
    fresh = Settings(data_dir=settings.data_dir, inbox_dir=settings.inbox_dir, timezone="America/New_York", _env_file=None)
    apply_saved_timezone(fresh, store)
    assert fresh.timezone == "America/Chicago"

    set_timezone(store, settings, "auto")
    assert settings.timezone == "auto"
    assert settings.tz == ZoneInfo(computer_timezone())
    try:
        set_timezone(store, settings, "Mars/Olympus")
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown zone should be refused")


def test_setup_shows_the_computer_zone_and_a_saved_zone_changes_the_clock(settings, store):
    store.set_state("last_overnight_at", STAMP)
    client = TestClient(create_app(settings, store))
    page = client.get("/settings")
    assert page.status_code == 200
    assert "This computer" in page.text
    assert "America/New_York" in page.text
    assert selected_zone(page.text) == "America/New_York"
    assert "Sep 22 · 12:00" in client.get("/").text

    saved = client.post("/settings/timezone", data={"zone": "America/Los_Angeles"}, follow_redirects=False)
    assert saved.status_code == 303
    assert saved.headers["location"].endswith("#timezone")
    follow = client.get("/settings?notice=timezone")
    assert "Saved. Times" in follow.text
    assert selected_zone(follow.text) == "America/Los_Angeles"
    assert "Sep 22 · 09:00" in client.get("/").text
    assert "Sep 22 · 12:00" not in client.get("/").text

    back = client.post("/settings/timezone", data={"zone": "auto"}, follow_redirects=True)
    assert back.status_code == 200
    assert selected_zone(back.text) == "auto"
    shown = format_when(STAMP, ZoneInfo(computer_timezone()))
    assert shown in client.get("/").text
    assert client.post("/settings/timezone", data={"zone": "Mars/Olympus"}).status_code == 400


def test_offsets_west_of_greenwich_are_negative():
    winter = datetime(2026, 1, 15, tzinfo=timezone.utc)
    summer = datetime(2026, 7, 15, tzinfo=timezone.utc)
    assert offset_label(ZoneInfo("Atlantic/Cape_Verde"), winter) == "UTC-1"
    assert offset_label(ZoneInfo("America/New_York"), winter) == "UTC-5"
    assert offset_label(ZoneInfo("America/New_York"), summer) == "UTC-4"
    assert offset_label(ZoneInfo("America/Chicago"), summer) == "UTC-5"
    assert offset_label(ZoneInfo("Europe/Paris"), winter) == "UTC+1"
    assert offset_label(ZoneInfo("Asia/Kolkata"), winter) == "UTC+5:30"
    assert offset_label(ZoneInfo("UTC"), winter) == "UTC+0"


def test_the_list_reads_like_windows_with_the_standard_offset_all_year():
    winter = datetime(2026, 1, 15, tzinfo=timezone.utc)
    october = datetime(2026, 10, 2, 22, tzinfo=timezone.utc)
    for moment in (winter, october):
        assert zone_option("America/New_York", moment) == "(UTC-05:00) Eastern Time (US & Canada)"
        assert zone_option("America/Chicago", moment) == "(UTC-06:00) Central Time (US & Canada)"
        assert zone_option("Atlantic/Cape_Verde", moment) == "(UTC-01:00) Cabo Verde Is."
        assert zone_option("Asia/Kolkata", moment) == "(UTC+05:30) Chennai, Kolkata, Mumbai, New Delhi"
    assert zone_option("UTC", october) == "(UTC) Coordinated Universal Time"
    assert offset_label(ZoneInfo("America/New_York"), october) == "UTC-4"
    assert on_daylight_time(ZoneInfo("America/New_York"), october)
    assert not on_daylight_time(ZoneInfo("America/New_York"), winter)

    labels = [label for _name, label in zone_choices(october)]
    assert labels[0] == "(UTC-12:00) International Date Line West"
    assert labels[-1] == "(UTC+14:00) Kiritimati Island"
    assert labels.index("(UTC-05:00) Eastern Time (US & Canada)") < labels.index("(UTC) Coordinated Universal Time")
    assert labels.index("(UTC) Coordinated Universal Time") < labels.index("(UTC+00:00) Dublin, Edinburgh, Lisbon, London")
    kept = dict(zone_choices(october, keep="America/Toronto"))
    assert kept["America/Toronto"] == "(UTC-05:00) Toronto (America)"


def test_setup_lists_zones_like_windows_and_says_when_daylight_time_is_on(settings, store):
    client = TestClient(create_app(settings, store))
    page = client.get("/settings").text
    now_label = offset_label(ZoneInfo("America/New_York"))
    assert now_label.startswith("UTC-")
    assert selected_zone(page) == "America/New_York"
    assert ">(UTC-05:00) Eastern Time (US &amp; Canada)</option>" in page
    assert "This computer: (UTC" in page
    assert "Time there now: <b>" in page
    assert f"Eastern Time (US &amp; Canada) is {now_label} right now" in page
    assert 'data-tz="America/New_York"' in page


def test_an_evening_email_belongs_to_the_local_day():
    evening = SimpleNamespace(received_at="2026-10-02T01:30:00+00:00")
    assert str(_as_of(evening, ZoneInfo("America/New_York"))) == "2026-10-01"
    assert str(_as_of(evening)) == "2026-10-02"


def test_a_stamp_with_z_matches_the_offset_form():
    zulu = datetime(2026, 1, 15, 18, 30, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert format_when(zulu, ZoneInfo("America/New_York")) == "Jan 15 · 13:30"
